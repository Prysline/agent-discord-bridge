"""Minimal async stdio wrapper for the Codex app-server JSON-RPC API."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class AppServerError(RuntimeError):
    """Base app-server client error."""


class AppServerTransportError(AppServerError):
    """The transport failed; outcome may be ambiguous when a request was sent."""

    def __init__(self, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


class AppServerProtocolError(AppServerError):
    """The server returned a malformed or contradictory response."""


class AppServerMethodUnsupported(AppServerError):
    """A required app-server method is unavailable."""


class AppServerRpcError(AppServerError):
    def __init__(self, code: int | None, message: str):
        super().__init__(message)
        self.code = code


MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
DisconnectHandler = Callable[[Exception], Awaitable[None]]


class JsonRpcTransport(Protocol):
    async def start(self, on_message: MessageHandler, on_disconnect: DisconnectHandler) -> None: ...

    async def send(self, payload: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...

    def is_open(self) -> bool: ...


class StdioTransport:
    """Own one `codex app-server --stdio` child process."""

    def __init__(self, command: list[str], cwd: Path):
        if not command:
            raise ValueError("command must not be empty")
        self.command = list(command)
        self.cwd = cwd
        self._process: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._on_message: MessageHandler | None = None
        self._on_disconnect: DisconnectHandler | None = None
        self._write_lock = asyncio.Lock()
        self._closing = False
        self._reader_finished = True

    async def start(self, on_message: MessageHandler, on_disconnect: DisconnectHandler) -> None:
        if self.is_open():
            return
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._closing = False
        self._reader_finished = False
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self.command,
                "app-server",
                "--stdio",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.cwd),
            )
        except (OSError, ValueError) as exc:
            raise AppServerTransportError("unable to start Codex app-server") from exc
        self._stdout_task = asyncio.create_task(self._stdout_loop(self._process))
        self._stderr_task = asyncio.create_task(self._stderr_loop(self._process))

    def is_open(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
            and not self._reader_finished
        )

    async def send(self, payload: dict[str, Any]) -> None:
        process = self._process
        if not process or not process.stdin or process.returncode is not None:
            raise AppServerTransportError("app-server is not connected")
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                process.stdin.write(encoded)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                raise AppServerTransportError(
                    "app-server write failed",
                    ambiguous=True,
                ) from exc

    async def _stdout_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        failure: Exception | None = None
        try:
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    failure = AppServerProtocolError("app-server emitted malformed JSON")
                    break
                if not isinstance(message, dict):
                    failure = AppServerProtocolError("app-server emitted a non-object JSON message")
                    break
                if self._on_message:
                    await self._on_message(message)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            failure = (
                exc
                if isinstance(exc, AppServerError)
                else AppServerProtocolError("app-server message handling failed")
            )
        finally:
            self._reader_finished = True
            if not self._closing and self._on_disconnect:
                await self._on_disconnect(
                    failure or AppServerTransportError("app-server stdio closed", ambiguous=True)
                )

    async def _stderr_loop(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        try:
            while await process.stderr.readline():
                pass
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        self._closing = True
        self._reader_finished = True
        process = self._process
        self._process = None
        tasks = [task for task in (self._stdout_task, self._stderr_task) if task]
        self._stdout_task = None
        self._stderr_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if not process:
            return
        if process.stdin:
            process.stdin.close()
            with contextlib.suppress(Exception):
                await process.stdin.wait_closed()
        if process.returncode is None:
            process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 2)
        if process.returncode is None:
            process.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), 2)


@dataclass(frozen=True)
class TurnReference:
    turn_id: str
    status: str


class CodexAppServerClient:
    """Typed subset of app-server methods required by the persistent adapter."""

    def __init__(self, transport_factory: Callable[[], JsonRpcTransport]):
        self._transport_factory = transport_factory
        self._transport: JsonRpcTransport | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._turn_waiters: dict[str, asyncio.Future] = {}
        self._completed_turns: dict[str, dict[str, Any]] = {}
        self._delivered_turns: set[str] = set()
        self.notification_methods: list[str] = []
        self._connect_lock = asyncio.Lock()
        self._initialized = False

    async def connect(self) -> None:
        async with self._connect_lock:
            await self._connect_locked()

    async def reconnect(self) -> None:
        async with self._connect_lock:
            await self._close_locked()
            await self._connect_locked()

    async def _connect_locked(self) -> None:
        if self._transport and self._transport.is_open() and self._initialized:
            return
        if self._transport:
            await self._close_locked()
        transport = self._transport_factory()
        self._transport = transport
        try:
            await transport.start(self._handle_message, self._handle_disconnect)
            await self.initialize()
            self._initialized = True
        except BaseException:
            self._transport = None
            self._initialized = False
            await transport.close()
            raise

    async def initialize(self) -> dict[str, Any]:
        result = await self._request(
            "initialize",
            {
                "clientInfo": {"name": "codex-persistent-adapter", "version": "0.1.0"},
                "capabilities": {"experimentalApi": True},
            },
            ensure_connected=False,
        )
        await self._send_notification("initialized", {})
        return result

    async def resume_thread(self, thread_id: str) -> dict[str, Any]:
        result = await self._request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        thread = _require_object(result.get("thread"), "thread/resume.thread")
        if thread.get("id") != thread_id:
            raise AppServerProtocolError("thread/resume returned a different thread")
        return result

    async def start_turn(
        self,
        thread_id: str,
        text: str,
        client_user_message_id: str,
    ) -> TurnReference:
        result = await self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "clientUserMessageId": client_user_message_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        turn = _require_object(result.get("turn"), "turn/start.turn")
        turn_id = turn.get("id")
        status = turn.get("status")
        if (
            not isinstance(turn_id, str)
            or not turn_id
            or status not in {"inProgress", "completed", "failed", "interrupted", "cancelled"}
        ):
            raise AppServerProtocolError("turn/start returned a malformed turn")
        return TurnReference(turn_id, status)

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        result = await self._request("thread/read", {"threadId": thread_id, "includeTurns": True})
        thread = _require_object(result.get("thread"), "thread/read.thread")
        if thread.get("id") != thread_id or not isinstance(thread.get("turns", []), list):
            raise AppServerProtocolError("thread/read returned a malformed thread")
        return result

    async def list_turns(self, thread_id: str) -> list[dict[str, Any]]:
        turns: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            result = await self._request(
                "thread/turns/list",
                {
                    "threadId": thread_id,
                    "cursor": cursor,
                    "limit": 100,
                    "sortDirection": "asc",
                    "itemsView": "full",
                },
            )
            data = result.get("data")
            if not isinstance(data, list) or any(not isinstance(turn, dict) for turn in data):
                raise AppServerProtocolError("thread/turns/list returned malformed data")
            for turn in data:
                if (
                    not isinstance(turn.get("id"), str)
                    or turn.get("status") not in {"inProgress", "completed", "failed", "interrupted", "cancelled"}
                    or not isinstance(turn.get("items"), list)
                ):
                    raise AppServerProtocolError("thread/turns/list returned malformed turn data")
            turns.extend(data)
            cursor = result.get("nextCursor")
            if cursor is None:
                return turns
            if not isinstance(cursor, str) or not cursor:
                raise AppServerProtocolError("thread/turns/list returned an invalid cursor")
            if cursor in seen_cursors:
                raise AppServerProtocolError("thread/turns/list repeated a cursor")
            seen_cursors.add(cursor)

    async def interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    async def wait_for_turn(self, turn_id: str, timeout_ms: int) -> dict[str, Any]:
        completed = self._completed_turns.pop(turn_id, None)
        if completed is not None:
            self._delivered_turns.add(turn_id)
            return completed
        loop = asyncio.get_running_loop()
        waiter = loop.create_future()
        self._turn_waiters[turn_id] = waiter
        try:
            return await asyncio.wait_for(waiter, timeout_ms / 1000)
        finally:
            if self._turn_waiters.get(turn_id) is waiter:
                self._turn_waiters.pop(turn_id, None)

    async def close(self) -> None:
        async with self._connect_lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        self._fail_pending(AppServerTransportError("app-server client closed"))
        self._completed_turns.clear()
        self._delivered_turns.clear()
        self._initialized = False
        transport = self._transport
        self._transport = None
        if transport:
            await transport.close()

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        ensure_connected: bool = True,
    ) -> dict[str, Any]:
        if ensure_connected:
            await self.connect()
        transport = self._transport
        if not transport:
            raise AppServerTransportError("app-server transport is unavailable")
        request_id = self._next_id
        self._next_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await transport.send({"id": request_id, "method": method, "params": params})
            result = await future
        except asyncio.CancelledError:
            if self._pending.pop(request_id, None) is future and not future.done():
                future.cancel()
            raise
        except Exception:
            self._pending.pop(request_id, None)
            raise
        if not isinstance(result, dict):
            raise AppServerProtocolError(f"{method} returned a non-object result")
        return result

    async def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        if not self._transport:
            raise AppServerTransportError("app-server transport is unavailable")
        await self._transport.send({"method": method, "params": params})

    async def _handle_message(self, message: dict[str, Any]) -> None:
        if "id" in message and "method" not in message:
            request_id = message.get("id")
            future = self._pending.pop(request_id, None)
            if not future or future.done():
                raise AppServerProtocolError("response referenced an unknown request")
            if "result" in message and "error" in message:
                future.set_exception(AppServerProtocolError("response contained result and error"))
                return
            if "result" not in message and "error" not in message:
                future.set_exception(AppServerProtocolError("response contained neither result nor error"))
                return
            if "error" in message:
                error = message.get("error")
                if not isinstance(error, dict):
                    future.set_exception(AppServerProtocolError("malformed JSON-RPC error"))
                    return
                code = error.get("code")
                text = str(error.get("message") or "JSON-RPC error")
                if code == -32601:
                    future.set_exception(AppServerMethodUnsupported(text))
                else:
                    future.set_exception(AppServerRpcError(code, text))
            else:
                future.set_result(message.get("result"))
            return

        method = message.get("method")
        if not isinstance(method, str):
            raise AppServerProtocolError("malformed app-server message")
        if "id" in message:
            if self._transport:
                await self._transport.send(
                    {
                        "id": message["id"],
                        "error": {"code": -32601, "message": "client callbacks unsupported"},
                    }
                )
            return
        self.notification_methods.append(method)
        if method == "turn/completed":
            params = _require_object(message.get("params"), "turn/completed.params")
            turn = _require_object(params.get("turn"), "turn/completed.turn")
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and turn_id:
                if turn_id in self._delivered_turns or turn_id in self._completed_turns:
                    return
                waiter = self._turn_waiters.pop(turn_id, None)
                if waiter and not waiter.done():
                    self._delivered_turns.add(turn_id)
                    waiter.set_result(dict(turn))
                else:
                    self._completed_turns[turn_id] = dict(turn)

    async def _handle_disconnect(self, error: Exception) -> None:
        if isinstance(error, AppServerError):
            exc = error
        else:
            exc = AppServerTransportError("app-server disconnected", ambiguous=True)
        self._fail_pending(exc)

    def _fail_pending(self, error: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        for future in list(self._turn_waiters.values()):
            if not future.done():
                future.set_exception(error)
        self._turn_waiters.clear()


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AppServerProtocolError(f"{field} must be an object")
    return value
