"""Authenticated loopback client for the Antigravity Sidecar."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib import error, request


class SidecarError(RuntimeError):
    pass


class SidecarUnavailable(SidecarError):
    pass


class SidecarRejected(SidecarError):
    pass


class SidecarAmbiguous(SidecarError):
    """The request may have reached the current Sidecar instance."""

    def __init__(self, message: str, request_id: str | None = None) -> None:
        super().__init__(message)
        self.request_id = request_id


@dataclass(frozen=True)
class Rendezvous:
    host: str
    port: int
    instance_id: str
    token: str


class AntigravityTransport(Protocol):
    async def health(self) -> None: ...
    async def send(self, conversation_id: str, message: str, request_id: str) -> str: ...
    async def result(self, request_id: str) -> Mapping[str, Any] | None: ...


class SidecarHttpClient:
    """Reads rotating credentials per call; never logs or returns them."""

    def __init__(self, rendezvous_path: str | Path) -> None:
        self.rendezvous_path = Path(rendezvous_path)

    async def health(self) -> None:
        value = await asyncio.to_thread(self._call, "GET", "/health", None)
        if value.get("status") != "ready":
            raise SidecarUnavailable("Antigravity Sidecar is not ready")

    async def send(self, conversation_id: str, message: str, request_id: str) -> str:
        local_request_id = uuid.uuid4().hex
        try:
            value = await asyncio.to_thread(
                self._call,
                "POST",
                "/send",
                {"conversationId": conversation_id, "message": message, "requestId": local_request_id},
            )
        except SidecarAmbiguous as exc:
            raise SidecarAmbiguous(str(exc), local_request_id) from exc
        returned_request_id = value.get("requestId")
        if value.get("accepted") is not True or returned_request_id != local_request_id:
            raise SidecarRejected("Sidecar did not accept the correlated request")
        return local_request_id

    async def result(self, request_id: str) -> Mapping[str, Any] | None:
        try:
            return await asyncio.to_thread(self._call, "GET", f"/result/{request_id}", None)
        except SidecarRejected as exc:
            if str(exc) == "request_not_found":
                return None
            raise

    def _load(self) -> Rendezvous:
        try:
            raw = json.loads(self.rendezvous_path.read_text(encoding="utf-8"))
            value = Rendezvous(raw["host"], raw["port"], raw["instanceId"], raw["token"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise SidecarUnavailable("Antigravity Sidecar rendezvous is unavailable") from exc
        if value.host != "127.0.0.1" or not isinstance(value.port, int) or not 1 <= value.port <= 65535:
            raise SidecarUnavailable("Antigravity Sidecar rendezvous is invalid")
        if not value.instance_id or not value.token:
            raise SidecarUnavailable("Antigravity Sidecar rendezvous is invalid")
        return value

    def _call(self, method: str, path: str, body: Mapping[str, Any] | None) -> dict[str, Any]:
        endpoint = self._load()
        payload = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = request.Request(
            f"http://127.0.0.1:{endpoint.port}{path}",
            data=payload,
            method=method,
            headers={
                "Authorization": f"Bearer {endpoint.token}",
                "X-Sidecar-Instance": endpoint.instance_id,
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            with request.urlopen(req, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as exc:
            try:
                code = json.loads(exc.read().decode("utf-8")).get("error", "sidecar_rejected")
            except Exception:
                code = "sidecar_rejected"
            raise SidecarRejected(str(code)) from exc
        except (TimeoutError, error.URLError, OSError) as exc:
            if method == "POST" and path == "/send":
                raise SidecarAmbiguous("Sidecar send outcome is ambiguous") from exc
            raise SidecarUnavailable("Antigravity Sidecar is unavailable") from exc
        except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise SidecarError("Antigravity Sidecar returned an invalid response") from exc
