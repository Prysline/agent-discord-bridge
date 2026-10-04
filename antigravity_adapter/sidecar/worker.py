"""Antigravity-managed loopback Sidecar.

This runtime intentionally keeps request state in memory.  It publishes rotating
credentials through an atomic, gitignored rendezvous file.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


MAX_BODY = 1024 * 1024
MAX_DELTA = 16 * 1024 * 1024
WRAPPER = re.compile(r'^\s*"(?P<target>[^"]+\.exe)"(?P<fixed>.*?)\s+%\*\s*$', re.I)
CONVERSATION_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,127}")


class SidecarFailure(RuntimeError):
    pass


def discover_agentapi() -> tuple[str, list[str], str]:
    entry = shutil.which("agentapi")
    if not entry:
        raise SidecarFailure("adapter_unavailable: agentapi was not found on PATH")
    suffix = Path(entry).suffix.lower()
    if suffix == ".exe":
        return entry, [], "direct-executable"
    if suffix not in {".bat", ".cmd"}:
        raise SidecarFailure("adapter_unavailable: unsupported agentapi entry type")
    lines = [
        line.strip()
        for line in Path(entry).read_text(encoding="utf-8", errors="strict").splitlines()
        if line.strip() and not re.match(r"(?i)^(@?echo\s+off|rem\s|::)", line.strip())
    ]
    if len(lines) != 1:
        raise SidecarFailure("adapter_unavailable: unsupported agentapi wrapper")
    match = WRAPPER.match(lines[0])
    if not match:
        raise SidecarFailure("adapter_unavailable: unsupported agentapi wrapper")
    target = Path(match.group("target"))
    fixed = match.group("fixed").strip()
    if not target.is_absolute() or not target.is_file() or re.search(r'["&|<>^()%!]', fixed):
        raise SidecarFailure("adapter_unavailable: unsafe agentapi wrapper")
    args = fixed.split() if fixed else []
    if any(not re.fullmatch(r"[A-Za-z0-9_.:/=+\-]+", value) for value in args):
        raise SidecarFailure("adapter_unavailable: unsafe agentapi wrapper")
    return str(target), args, "batch-forwarder"


@dataclass
class Pending:
    request_id: str
    conversation_id: str
    marker: str
    transcript_path: Path
    file_identity: tuple[int, int] | None
    offset: int
    deadline: float
    status: str = "pending"
    text: str | None = None
    error: dict[str, str] | None = None


class State:
    def __init__(self, timeout_seconds: int, transcript_root: str | Path) -> None:
        try:
            root = Path(transcript_root).resolve(strict=True)
        except OSError as exc:
            raise SidecarFailure("transcript_root_invalid") from exc
        if not root.is_dir():
            raise SidecarFailure("transcript_root_invalid")
        self.transcript_root = root
        self.target, self.prefix, self.command_type = discover_agentapi()
        self.timeout_seconds = timeout_seconds
        self.lock = threading.Lock()
        self.requests: dict[str, Pending] = {}
        self.pending_id: str | None = None

    def expire(self) -> None:
        with self.lock:
            if not self.pending_id:
                return
            value = self.requests[self.pending_id]
            self._recover_locked(value)
            if value.status == "pending" and time.monotonic() >= value.deadline:
                self._error_locked(value, "timeout", "Result recovery timeout")

    def result(self, request_id: str) -> Pending | None:
        with self.lock:
            value = self.requests.get(request_id)
            if value is None:
                return None
            if value.status == "pending":
                self._recover_locked(value)
                if value.status == "pending" and time.monotonic() >= value.deadline:
                    self._error_locked(value, "timeout", "Result recovery timeout")
            return value

    def send(self, conversation_id: str, message: str, requested_id: str | None) -> Pending:
        self.expire()
        transcript_path = self._expected_transcript(conversation_id)
        file_identity, offset = self._baseline(transcript_path)
        with self.lock:
            if self.pending_id:
                raise SidecarFailure("busy")
            request_id = requested_id if requested_id and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", requested_id) else uuid.uuid4().hex
            if request_id in self.requests:
                raise SidecarFailure("duplicate_request")
            marker = f"SIDECAR_REQUEST_ID_{request_id}"
            value = Pending(
                request_id, conversation_id, marker, transcript_path, file_identity,
                offset, time.monotonic() + self.timeout_seconds,
            )
            self.requests[request_id] = value
            self.pending_id = request_id
        completed = subprocess.run(
            [self.target, *self.prefix, "send-message", conversation_id, f"[{marker}]\n{message}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            with self.lock:
                self.requests.pop(request_id, None)
                if self.pending_id == request_id:
                    self.pending_id = None
            raise SidecarFailure("agentapi_failed")
        return value

    def stop(self, payload: dict[str, Any]) -> str:
        with self.lock:
            if not self.pending_id:
                return "unknown_stop"
            value = self.requests[self.pending_id]
            if payload.get("conversationId") != value.conversation_id:
                return "unknown_stop"
            if payload.get("fullyIdle") is not True:
                return "not_fully_idle"
            if payload.get("error"):
                self._error_locked(value, "execution_error", "Antigravity reported an execution error")
                return "error"
            if not self._hook_path_matches(value, payload.get("transcriptPath")):
                self._error_locked(value, "transcript_unavailable", "Stop transcript unavailable")
                return "error"
            self._recover_locked(value)
            if value.status == "completed":
                return "completed"
            if value.status == "error":
                return "error"
            if time.monotonic() >= value.deadline:
                self._error_locked(value, "timeout", "Result recovery timeout")
                return "error"
            return "pending"

    def _expected_transcript(self, conversation_id: str) -> Path:
        if not isinstance(conversation_id, str) or not CONVERSATION_REFERENCE.fullmatch(conversation_id):
            raise SidecarFailure("invalid_conversation_reference")
        candidate = self.transcript_root / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.transcript_root):
            raise SidecarFailure("invalid_conversation_reference")
        return candidate

    def _baseline(self, path: Path) -> tuple[tuple[int, int] | None, int]:
        if not path.exists():
            return None, 0
        try:
            with path.open("rb") as stream:
                identity = self._opened_identity(path, stream)
                return identity, os.fstat(stream.fileno()).st_size
        except OSError as exc:
            raise SidecarFailure("transcript_unavailable") from exc

    def _opened_identity(self, path: Path, stream: Any) -> tuple[int, int]:
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(self.transcript_root):
            raise SidecarFailure("transcript_unavailable")
        opened = os.fstat(stream.fileno())
        current = path.stat()
        opened_identity = (opened.st_dev, opened.st_ino)
        if opened_identity != (current.st_dev, current.st_ino):
            raise SidecarFailure("transcript_identity_changed")
        return opened_identity

    def _hook_path_matches(self, value: Pending, supplied: Any) -> bool:
        if not isinstance(supplied, str):
            return False
        try:
            resolved = Path(supplied).resolve(strict=True)
            expected = value.transcript_path.resolve(strict=True)
        except OSError:
            return False
        return resolved.is_relative_to(self.transcript_root) and resolved == expected

    def _recover_locked(self, value: Pending) -> None:
        if value.status != "pending":
            return
        path = value.transcript_path
        if not path.exists():
            if value.file_identity is not None:
                self._error_locked(value, "transcript_identity_changed", "Transcript identity changed")
            return
        try:
            with path.open("rb") as stream:
                identity = self._opened_identity(path, stream)
                size = os.fstat(stream.fileno()).st_size
                if value.file_identity is None:
                    value.file_identity = identity
                elif identity != value.file_identity or size < value.offset:
                    self._error_locked(value, "transcript_identity_changed", "Transcript identity changed")
                    return
                stream.seek(value.offset)
                delta = stream.read(MAX_DELTA + 1)
        except (OSError, SidecarFailure):
            self._error_locked(value, "transcript_unavailable", "Transcript unavailable")
            return
        if len(delta) > MAX_DELTA:
            self._error_locked(value, "correlation_failed", "Transcript delta was ambiguous")
            return
        if delta and not delta.endswith(b"\n"):
            return
        try:
            text = delta.decode("utf-8")
            records = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError
                records.append(item)
        except (UnicodeError, json.JSONDecodeError, ValueError):
            self._error_locked(value, "correlation_failed", "Transcript delta was ambiguous")
            return
        marker_token = f"[{value.marker}]"
        marker_occurrences = sum(
            item.get("content", "").count(marker_token)
            for item in records if isinstance(item.get("content"), str)
        )
        if marker_occurrences == 0:
            return
        if marker_occurrences != 1:
            self._error_locked(value, "correlation_failed", "Transcript delta was ambiguous")
            return
        marker_index = next(
            index for index, item in enumerate(records)
            if isinstance(item.get("content"), str) and marker_token in item["content"]
        )
        candidates = [
            item for item in records[marker_index + 1 :]
            if item.get("source") == "MODEL"
            and item.get("type") == "PLANNER_RESPONSE"
            and item.get("status") == "DONE"
            and not item.get("tool_calls")
            and isinstance(item.get("content"), str)
            and item["content"].strip()
        ]
        if not candidates:
            return
        if len(candidates) != 1:
            self._error_locked(value, "correlation_failed", "Transcript delta was ambiguous")
            return
        self._complete_locked(value, candidates[0]["content"])

    def _complete_locked(self, value: Pending, text: str) -> None:
        if value.status != "pending" or self.pending_id != value.request_id:
            return
        value.status = "completed"
        value.text = text
        self.pending_id = None

    def _error_locked(self, value: Pending, code: str, message: str) -> None:
        if value.status != "pending" or self.pending_id != value.request_id:
            return
        value.status = "error"
        value.error = {"code": code, "message": message}
        self.pending_id = None


class Server(ThreadingHTTPServer):
    state: State
    token: str
    instance_id: str


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, *_args: Any) -> None:
        return

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if not hmac.compare_digest(supplied, expected):
            self._json(401, {"error": "unauthorized"})
            return False
        if not hmac.compare_digest(self.headers.get("X-Sidecar-Instance", ""), self.server.instance_id):
            self._json(409, {"error": "instance_mismatch"})
            return False
        return True

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 1 or length > MAX_BODY:
            raise ValueError
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value

    def _json(self, status: int, value: dict[str, Any]) -> None:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if not self._authorized(): return
        if self.path == "/health":
            self.server.state.expire()
            self._json(200, {"status":"ready", "agentapi":True, "commandType":self.server.state.command_type, "pending":bool(self.server.state.pending_id)})
            return
        match = re.fullmatch(r"/result/([A-Za-z0-9_-]{1,128})", self.path)
        value = self.server.state.result(match.group(1)) if match else None
        if value is None:
            self._json(404, {"error":"request_not_found"}); return
        body: dict[str, Any] = {"requestId": value.request_id, "status": value.status}
        if value.status == "completed": body["text"] = value.text
        if value.status == "error": body["error"] = value.error
        self._json(200, body)

    def do_POST(self) -> None:
        if not self._authorized(): return
        try:
            body = self._body()
            if self.path == "/send":
                conversation_id, message = body.get("conversationId"), body.get("message")
                if not isinstance(conversation_id, str) or not conversation_id.strip() or not isinstance(message, str) or not message.strip():
                    raise ValueError
                value = self.server.state.send(conversation_id, message, body.get("requestId"))
                self._json(202, {"accepted":True, "requestId":value.request_id, "status":"pending"}); return
            if self.path == "/stop":
                self._json(200, {"observed":True, "outcome":self.server.state.stop(body)}); return
            self._json(404, {"error":"not_found"})
        except ValueError:
            self._json(400, {"error":"invalid_payload"})
        except SidecarFailure as exc:
            code = str(exc)
            self._json(409 if code in {"busy", "duplicate_request"} else 502, {"accepted":False, "error":code})


def publish(path: Path, port: int, token: str, instance_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"host":"127.0.0.1", "port":port, "instanceId":instance_id, "token":token}, separators=(",", ":"))
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream: stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--rendezvous", type=Path, required=True)
    parser.add_argument("--transcript-root", type=Path, required=True)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.timeout_seconds < 1: raise SystemExit("invalid Sidecar settings")
    server = Server(("127.0.0.1", args.port), Handler)
    server.state = State(args.timeout_seconds, args.transcript_root)
    server.token = secrets.token_hex(32)
    server.instance_id = uuid.uuid4().hex
    publish(args.rendezvous, args.port, server.token, server.instance_id)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        try:
            current = json.loads(args.rendezvous.read_text(encoding="utf-8"))
            if current.get("instanceId") == server.instance_id: args.rendezvous.unlink()
        except (OSError, ValueError, json.JSONDecodeError):
            pass


if __name__ == "__main__": main()
