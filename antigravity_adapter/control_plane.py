"""Read-only existing-conversation control plane for Antigravity bindings."""

from __future__ import annotations

from .binding import InMemoryBindingResolver, ResolvedBinding
from .transport import AntigravityTransport, SidecarRejected, SidecarUnavailable


class AntigravityControlPlane:
    adapter = "antigravity"
    mapping_field = "antigravityBindings"
    can_create = False

    def __init__(
        self, transport: AntigravityTransport, resolver: InMemoryBindingResolver
    ) -> None:
        self.transport = transport
        self.resolver = resolver

    async def validate_existing(self, conversation_id: str) -> None:
        if not conversation_id.strip():
            raise ValueError("Antigravity conversation reference is required")
        try:
            await self.transport.validate_conversation(conversation_id)
        except SidecarUnavailable as exc:
            raise ValueError(
                "Antigravity Sidecar 目前未連線；請先確認 Antigravity 與 Sidecar 已啟動"
            ) from exc
        except SidecarRejected as exc:
            raise ValueError(
                "找不到此 Antigravity Conversation；請確認 ID 正確，且 transcript 位於目前 Sidecar 設定的 transcript root"
            ) from exc
        except Exception as exc:
            raise ValueError("Antigravity Conversation 驗證失敗") from exc

    async def create(self) -> str:
        raise ValueError("Antigravity conversation creation is not supported")

    def find_mapping(
        self, raw: dict[str, object], native_reference: str
    ) -> tuple[str, int] | None:
        entries = raw.get(self.mapping_field, [])
        if not isinstance(entries, list):
            raise ValueError("antigravityBindings must be an array")
        matches = [
            (item.get("bindingId"), item.get("generation"))
            for item in entries
            if isinstance(item, dict)
            and item.get("conversationId") == native_reference
        ]
        if len(matches) > 1:
            raise ValueError("native conversation has duplicate mappings")
        if not matches:
            return None
        binding_id, generation = matches[0]
        if (
            not isinstance(binding_id, str)
            or isinstance(generation, bool)
            or not isinstance(generation, int)
        ):
            raise ValueError("native conversation mapping is malformed")
        return binding_id, generation

    def mapping_entry(
        self, binding_id: str, generation: int, native_reference: str
    ) -> dict[str, object]:
        return {
            "bindingId": binding_id,
            "generation": generation,
            "conversationId": native_reference,
        }

    def validate_publication(
        self, binding_id: str, generation: int, native_reference: str
    ) -> None:
        self.resolver.validate_publish(
            ResolvedBinding(binding_id, generation, native_reference)
        )

    def publish(
        self, binding_id: str, generation: int, native_reference: str
    ) -> None:
        self.resolver.publish(ResolvedBinding(binding_id, generation, native_reference))
