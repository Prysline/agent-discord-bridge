"""Explicit Codex persistent-thread control plane; never used by AgentRequest."""

from __future__ import annotations

from pathlib import Path

from .app_server import (
    AppServerMethodUnsupported,
    AppServerProtocolError,
    AppServerRpcError,
    AppServerTransportError,
    CodexAppServerClient,
)
from .binding import InMemoryBindingResolver, ResolvedBinding


class CodexCreateRejected(ValueError):
    pass


class CodexCreateAmbiguous(ValueError):
    pass


class CodexControlPlane:
    def __init__(
        self,
        client: CodexAppServerClient,
        *,
        cwd: Path,
        model: str,
        base_instructions: str,
        resolver: InMemoryBindingResolver,
    ) -> None:
        self.client = client
        self.cwd = cwd
        self.model = model
        self.base_instructions = base_instructions
        self.resolver = resolver
        self.adapter = "codex"
        self.mapping_field = "codexBindings"

    async def validate_existing(self, thread_id: str) -> None:
        if not thread_id.strip():
            raise ValueError("Codex thread reference is required")
        try:
            await self.client.resume_thread(thread_id)
        except Exception as exc:
            raise ValueError("Codex thread could not be validated") from exc

    async def create(self) -> str:
        try:
            reference = await self.client.start_thread(
                cwd=self.cwd,
                model=self.model,
                base_instructions=self.base_instructions,
            )
        except (AppServerRpcError, AppServerMethodUnsupported) as exc:
            raise CodexCreateRejected("Codex rejected thread creation") from exc
        except AppServerTransportError as exc:
            if exc.ambiguous:
                raise CodexCreateAmbiguous(
                    "Codex thread creation outcome is unknown; do not retry automatically"
                ) from exc
            raise CodexCreateRejected("Codex thread creation did not start") from exc
        except AppServerProtocolError as exc:
            raise CodexCreateAmbiguous(
                "Codex may have created a thread but returned an invalid response"
            ) from exc
        return reference.thread_id

    def find_mapping(self, raw: dict[str, object], native_reference: str) -> tuple[str, int] | None:
        entries = raw.get(self.mapping_field, [])
        if not isinstance(entries, list):
            raise ValueError("codexBindings must be an array")
        matches = [
            (item.get("bindingId"), item.get("generation"))
            for item in entries
            if isinstance(item, dict) and item.get("threadId") == native_reference
        ]
        if len(matches) > 1:
            raise ValueError("native thread has duplicate mappings")
        if not matches:
            return None
        binding_id, generation = matches[0]
        if not isinstance(binding_id, str) or isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError("native thread mapping is malformed")
        return binding_id, generation

    def mapping_entry(self, binding_id: str, generation: int, native_reference: str) -> dict[str, object]:
        return {"bindingId": binding_id, "generation": generation, "threadId": native_reference}

    def validate_publication(self, binding_id: str, generation: int, native_reference: str) -> None:
        self.resolver.validate_publish(ResolvedBinding(binding_id, generation, native_reference))

    def publish(self, binding_id: str, generation: int, native_reference: str) -> None:
        self.resolver.publish(ResolvedBinding(binding_id, generation, native_reference))
