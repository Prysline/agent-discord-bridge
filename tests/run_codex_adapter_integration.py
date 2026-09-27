"""Opt-in, sanitized integration regression for the persistent Codex adapter.

This creates one dedicated non-sensitive Codex thread. It never prints the thread ID,
request IDs, prompts, or model output. Run directly; unittest discovery does not run it.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_adapter.app_server import (
    AppServerRpcError,
    AppServerTransportError,
    CodexAppServerClient,
    StdioTransport,
)
from codex_adapter.binding import ResolvedBinding
from codex_adapter.persistent import (
    CodexPersistentAdapter,
    find_request_turns,
)


class StaticResolver:
    def __init__(self, thread_id: str):
        self.thread_id = thread_id

    async def resolve(self, binding_id: str, generation: int) -> ResolvedBinding:
        return ResolvedBinding(binding_id, generation, self.thread_id)


class ClientProxy:
    def __init__(self, client: CodexAppServerClient):
        self.client = client
        self.list_turns_calls = 0

    async def connect(self):
        return await self.client.connect()

    async def reconnect(self):
        return await self.client.reconnect()

    async def resume_thread(self, thread_id):
        return await self.client.resume_thread(thread_id)

    async def start_turn(self, thread_id, text, client_user_message_id):
        return await self.client.start_turn(thread_id, text, client_user_message_id)

    async def wait_for_turn(self, turn_id, timeout_ms):
        return await self.client.wait_for_turn(turn_id, timeout_ms)

    async def list_turns(self, thread_id):
        self.list_turns_calls += 1
        return await self.client.list_turns(thread_id)

    async def interrupt(self, thread_id, turn_id):
        return await self.client.interrupt(thread_id, turn_id)


class DropCompletionOnceClient(ClientProxy):
    def __init__(self, client: CodexAppServerClient):
        super().__init__(client)
        self.dropped = False

    async def wait_for_turn(self, turn_id, timeout_ms):
        completed = await self.client.wait_for_turn(turn_id, timeout_ms)
        if not self.dropped:
            self.dropped = True
            raise AppServerTransportError(
                "integration probe dropped a local completion acknowledgement",
                ambiguous=True,
            )
        return completed


class HoldCompletedClient(ClientProxy):
    def __init__(self, client: CodexAppServerClient):
        super().__init__(client)
        self.completed = asyncio.Event()
        self.release = asyncio.Event()

    async def wait_for_turn(self, turn_id, timeout_ms):
        completed = await self.client.wait_for_turn(turn_id, timeout_ms)
        self.completed.set()
        await self.release.wait()
        return completed


def make_request(request_id: str, marker: str, *, long_answer: bool = False) -> dict[str, Any]:
    if long_answer:
        text = (
            f"Integration marker {marker}. Produce a numbered list with 500 short items. "
            "Do not use tools and do not mention any local files."
        )
    else:
        text = (
            f"Integration marker {marker}. Reply with exactly ACK_{marker}. "
            "Do not use tools."
        )
    return {
        "requestId": request_id,
        "agentId": "phase15-probe-agent",
        "mode": "human-turn",
        "binding": {"bindingId": "phase15-probe-binding", "generation": 1},
        "context": {
            "kind": "event-delta",
            "events": [
                {
                    "eventId": f"event-{marker}",
                    "seq": 1,
                    "kind": "message",
                    "author": {
                        "type": "human",
                        "id": "phase15-probe-human",
                        "displayName": "Probe Human",
                    },
                    "text": text,
                    "mentions": ["phase15-probe-agent"],
                }
            ],
            "cursor": {"fromSeqExclusive": 0, "throughSeqInclusive": 1},
        },
        "constraints": {"timeoutMs": 30_000},
    }


def new_client(codex_path: str) -> CodexAppServerClient:
    return CodexAppServerClient(
        lambda: StdioTransport([codex_path], ROOT)
    )


def summarize_result(result: dict[str, Any], marker: str) -> dict[str, Any]:
    text = result.get("text")
    return {
        "status": result.get("status"),
        "contextCommit": result.get("contextCommit"),
        "errorCode": result.get("error", {}).get("code"),
        "stage": result.get("diagnostics", {}).get("stage"),
        "textLength": len(text) if isinstance(text, str) else 0,
        "containsMarker": marker in text if isinstance(text, str) else False,
    }


def summarize_items(turn: dict[str, Any]) -> list[dict[str, Any]]:
    summary = []
    for item in turn.get("items", []):
        if not isinstance(item, dict):
            summary.append({"type": "non-object"})
            continue
        text = item.get("text")
        summary.append(
            {
                "type": item.get("type"),
                "phase": item.get("phase"),
                "textLength": len(text) if isinstance(text, str) else 0,
                "hasClientId": isinstance(item.get("clientId"), str),
            }
        )
    return summary


async def wait_for_active_turn(adapter: CodexPersistentAdapter) -> None:
    for _ in range(2_000):
        active = adapter._active
        if active is not None and active.turn_id is not None:
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("adapter did not expose an active turn")


async def wait_for_terminal_turn(
    client: CodexAppServerClient,
    thread_id: str,
    request_id: str,
) -> str | None:
    for _ in range(300):
        matches = find_request_turns(await client.list_turns(thread_id), request_id)
        if len(matches) == 1:
            status = matches[0].get("status")
            if status in {"completed", "failed", "interrupted", "cancelled"}:
                return status if isinstance(status, str) else None
        await asyncio.sleep(0.1)
    return None


async def run() -> dict[str, Any]:
    codex_path = shutil.which("codex")
    if not codex_path:
        raise RuntimeError("codex executable not found")

    results: dict[str, Any] = {}
    client = new_client(codex_path)
    thread_id = ""
    stage = "initialize"
    try:
        await client.connect()
        results["initialize"] = True
        stage = "thread_start"
        setup = await client._request(
            "thread/start",
            {
                "cwd": str(ROOT),
                "sandbox": "read-only",
                "approvalPolicy": "never",
                "model": "gpt-5.5",
                "ephemeral": False,
                "baseInstructions": (
                    "This is a non-sensitive adapter integration probe. "
                    "Follow the supplied text response request and never use tools."
                ),
            },
        )
        thread = setup.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str):
            raise RuntimeError("thread/start returned no thread")
        thread_id = thread["id"]
        results["dedicated_thread_created"] = True

        stage = "empty_thread_resume"
        try:
            await client.resume_thread(thread_id)
            results["empty_thread_resume"] = True
        except AppServerRpcError:
            results["empty_thread_resume"] = False
            bootstrap = await client.start_turn(
                thread_id,
                "Reply exactly ACK_BOOTSTRAP. Do not use tools.",
                f"phase15-bootstrap-{uuid.uuid4()}",
            )
            await client.wait_for_turn(bootstrap.turn_id, 30_000)

        stage = "resume_and_read"
        await client.resume_thread(thread_id)
        read = await client.read_thread(thread_id)
        results["resume_and_read"] = read.get("thread", {}).get("id") == thread_id

        resolver = StaticResolver(thread_id)
        stage = "normal_turn"
        first_marker = "P15A"
        first_request_id = f"phase15-{uuid.uuid4()}"
        counted = ClientProxy(client)
        first_adapter = CodexPersistentAdapter(counted, resolver)
        first = await first_adapter.execute(make_request(first_request_id, first_marker))
        results["first_result_shape"] = summarize_result(first, first_marker)
        results["first_turn_committed"] = (
            first.get("status") == "continue"
            and first.get("contextCommit") == "committed"
            and f"ACK_{first_marker}" in first.get("text", "")
        )
        results["notification_observed"] = "turn/completed" in client.notification_methods
        results["notification_fast_path"] = counted.list_turns_calls == 0

        turns = await client.list_turns(thread_id)
        matches = find_request_turns(turns, first_request_id)
        results["positive_correlation"] = len(matches) == 1
        if len(matches) == 1:
            results["first_turn_status"] = matches[0].get("status")
            results["first_turn_item_shapes"] = summarize_items(matches[0])
            user_items = [
                item
                for item in matches[0].get("items", [])
                if isinstance(item, dict) and item.get("type") == "userMessage"
            ]
            results["read_back_client_id_mapping"] = (
                len(user_items) == 1
                and user_items[0].get("clientId") == first_request_id
                and "clientUserMessageId" not in user_items[0]
            )
        else:
            results["read_back_client_id_mapping"] = False

        await client.close()
        stage = "process_restart"
        client = new_client(codex_path)
        await client.connect()
        await client.resume_thread(thread_id)
        after_restart = find_request_turns(await client.list_turns(thread_id), first_request_id)
        results["process_restart_resume"] = len(after_restart) == 1

        second_marker = "P15B"
        stage = "ack_drop_recovery"
        second_request_id = f"phase15-{uuid.uuid4()}"
        dropped = DropCompletionOnceClient(client)
        second_adapter = CodexPersistentAdapter(dropped, resolver)
        second = await second_adapter.execute(make_request(second_request_id, second_marker))
        results["second_result_shape"] = summarize_result(second, second_marker)
        results["restart_second_final"] = (
            second.get("status") == "continue"
            and f"ACK_{second_marker}" in second.get("text", "")
        )
        results["final_recovery_after_ack_drop"] = (
            dropped.dropped
            and dropped.list_turns_calls > 0
            and second.get("contextCommit") == "committed"
        )

        held_client = HoldCompletedClient(client)
        stage = "max_in_flight_and_late_interrupt"
        held_adapter = CodexPersistentAdapter(held_client, resolver)
        held_request_id = f"phase15-{uuid.uuid4()}"
        held_task = asyncio.create_task(
            held_adapter.execute(make_request(held_request_id, "P15C"))
        )
        await asyncio.wait_for(held_client.completed.wait(), 120)
        busy = await held_adapter.execute(
            make_request(f"phase15-{uuid.uuid4()}", "P15D")
        )
        results["max_in_flight"] = (
            busy.get("status") == "error"
            and busy.get("error", {}).get("code") == "adapter_unavailable"
        )
        late = await held_adapter.cancel(held_request_id)
        results["late_interrupt_internal_only"] = (
            late.state == "late_completion"
            and late.context_commit == "committed"
            and late.state not in {"continue", "complete", "abstain", "error"}
        )
        held_client.release.set()
        held_result = await held_task
        results["held_result_shape"] = summarize_result(held_result, "P15C")
        results["late_completed_body_suppressed"] = (
            held_result.get("status") == "error"
            and held_result.get("diagnostics", {}).get("stage")
            == "cancellation_reconciliation"
            and "text" not in held_result
        )

        interrupt_request_id = f"phase15-{uuid.uuid4()}"
        stage = "interrupt"
        interrupt_adapter = CodexPersistentAdapter(client, resolver)
        interrupt_task = asyncio.create_task(
            interrupt_adapter.execute(
                make_request(interrupt_request_id, "P15E", long_answer=True)
            )
        )
        await wait_for_active_turn(interrupt_adapter)
        interrupt_outcome = await interrupt_adapter.cancel(interrupt_request_id)
        terminal_status = await wait_for_terminal_turn(
            client,
            thread_id,
            interrupt_request_id,
        )
        interrupt_result = await interrupt_task
        results["interrupt_reconciled"] = (
            interrupt_outcome.context_commit == "committed"
            and terminal_status in {"interrupted", "cancelled", "completed"}
            and interrupt_result.get("contextCommit") == "committed"
        )
        results["interrupt_terminal_status"] = terminal_status
        results["interrupt_outcome"] = interrupt_outcome.state

        results["integration_stage"] = "complete"
        return results
    except BaseException as exc:
        results["integration_stage"] = stage
        results["integration_error_type"] = type(exc).__name__
        return results
    finally:
        await client.close()


if __name__ == "__main__":
    report = asyncio.run(run())
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if "integration_error_type" in report:
        raise SystemExit(1)
