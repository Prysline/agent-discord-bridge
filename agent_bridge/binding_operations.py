"""Persist-first local control-plane operations for room associations."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Mapping
from typing import Protocol
from uuid import uuid4

from .binding_bootstrap import build_binding_control
from .binding_control import BindingRef
from .binding_store import document_from_snapshot, save_binding_document
from .root_shared import RootHumanTurnResult, RootSharedHumanTurn


class BindingOperations:
    def __init__(self, path: Path, root: RootSharedHumanTurn) -> None:
        self.path = path
        self.root = root

    async def attach_detached(
        self, room_id: str, agent_id: str, binding_id: str, generation: int
    ) -> RootHumanTurnResult:
        if not any(
            item.room_id == room_id and item.agent_id == agent_id
            for item in self.root.pending_onboarding()
        ):
            raise ValueError("room+agent has no pending onboarding")
        raw, control = self._load()
        _require_unique_native_mapping(raw, binding_id, generation)
        control.attach((room_id, agent_id), BindingRef(binding_id, generation))
        save_binding_document(self.path, document_from_snapshot(raw, control.snapshot()))
        return await self.root.complete_onboarding(
            room_id, agent_id, self._snapshot(binding_id, generation)
        )

    def attach_configured(
        self, room_id: str, agent_id: str, binding_id: str, generation: int
    ) -> None:
        """Attach without synthesizing or dispatching a human turn."""
        raw, control = self._load()
        if self.root.core.binding(room_id, agent_id) is not None:
            raise ValueError("room+agent already has an active runtime binding")
        if (room_id, agent_id) in control.snapshot().active_by_room_agent:
            raise ValueError("room+agent already has an active persisted binding")
        _require_unique_native_mapping(raw, binding_id, generation)
        control.attach((room_id, agent_id), BindingRef(binding_id, generation))
        save_binding_document(self.path, document_from_snapshot(raw, control.snapshot()))
        self.root.core.publish_binding(
            room_id, agent_id, self._snapshot(binding_id, generation)
        )

    def move(self, source_room_id: str, target_room_id: str, agent_id: str) -> None:
        raw, control = self._load()
        if self.root.core.binding(source_room_id, agent_id) is None:
            raise ValueError("source room+agent has no active runtime binding")
        if self.root.core.binding(target_room_id, agent_id) is not None:
            raise ValueError("target room+agent already has an active runtime binding")
        self.root.core.require_binding_idle(source_room_id, agent_id)
        binding = self.root.core.binding(source_room_id, agent_id)
        _require_unique_native_mapping(raw, binding.binding_id, binding.generation)
        control.move((source_room_id, agent_id), (target_room_id, agent_id))
        save_binding_document(self.path, document_from_snapshot(raw, control.snapshot()))
        self.root.core.move_binding(source_room_id, target_room_id, agent_id)

    def move_room(self, source_room_id: str, target_room_id: str) -> None:
        raw, control = self._load()
        snapshot = control.snapshot()
        entries = sorted(
            (
                (agent_id, ref)
                for (room_id, agent_id), ref in snapshot.active_by_room_agent.items()
                if room_id == source_room_id
            ),
            key=lambda item: item[0],
        )
        if not entries:
            raise ValueError("source room has no active bindings")
        for agent_id, ref in entries:
            runtime = self.root.core.binding(source_room_id, agent_id)
            if runtime is None or runtime.binding_id != ref.binding_id or runtime.generation != ref.generation:
                raise ValueError("persisted and runtime binding state differ")
            if self.root.core.binding(target_room_id, agent_id) is not None:
                raise ValueError("target room+agent already has an active runtime binding")
            if (target_room_id, agent_id) in snapshot.active_by_room_agent:
                raise ValueError("target room+agent already has an active persisted binding")
            self.root.core.require_binding_idle(source_room_id, agent_id)
            _require_unique_native_mapping(raw, ref.binding_id, ref.generation)
        for agent_id, _ in entries:
            control.move((source_room_id, agent_id), (target_room_id, agent_id))
        save_binding_document(self.path, document_from_snapshot(raw, control.snapshot()))
        self.root.core.move_room_bindings(
            source_room_id, target_room_id, tuple(agent_id for agent_id, _ in entries)
        )

    def unbind(self, room_id: str, agent_id: str) -> None:
        raw, control = self._load()
        if self.root.core.binding(room_id, agent_id) is None:
            raise ValueError("room+agent has no active runtime binding")
        self.root.core.require_binding_idle(room_id, agent_id)
        control.detach((room_id, agent_id))
        save_binding_document(self.path, document_from_snapshot(raw, control.snapshot()))
        self.root.core.detach_binding(room_id, agent_id)

    def _load(self):
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return raw, build_binding_control(raw)

    @staticmethod
    def _snapshot(binding_id: str, generation: int):
        from .orchestrator import BindingSnapshot

        return BindingSnapshot(binding_id, generation)


class NativeBindingControl(Protocol):
    adapter: str
    mapping_field: str
    can_create: bool
    async def validate_existing(self, native_reference: str) -> None: ...
    async def create(self) -> str: ...
    def find_mapping(self, raw: dict[str, object], native_reference: str) -> tuple[str, int] | None: ...
    def mapping_entry(self, binding_id: str, generation: int, native_reference: str) -> dict[str, object]: ...
    def validate_publication(self, binding_id: str, generation: int, native_reference: str) -> None: ...
    def publish(self, binding_id: str, generation: int, native_reference: str) -> None: ...


class NativeCreatedUnmanaged(ValueError):
    pass


class BindingAdminControl:
    """Serialized in-process boundary for the loopback admin UI."""

    def __init__(
        self,
        operations: BindingOperations,
        *,
        adapter_types: Mapping[str, str],
        display_names: Mapping[str, str],
        native_controls: Mapping[str, NativeBindingControl] | None = None,
        enabled_agents: set[str] | None = None,
        binding_id_factory=None,
    ) -> None:
        self.operations = operations
        self.adapter_types = dict(adapter_types)
        self.display_names = dict(display_names)
        self.native_controls = dict(native_controls or {})
        self.enabled_agents = set(
            self.adapter_types if enabled_agents is None else enabled_agents
        )
        self.binding_id_factory = binding_id_factory or (lambda: uuid4().hex)
        self._lock = asyncio.Lock()

    def state(self) -> dict[str, object]:
        raw, control = self.operations._load()
        snapshot = control.snapshot()
        native_pairs = {
            "codex": _native_pairs(raw, "codexBindings"),
            "antigravity": _native_pairs(raw, "antigravityBindings"),
        }
        active_by_binding = {
            ref.binding_id: (room_id, agent_id, ref.generation)
            for (room_id, agent_id), ref in snapshot.active_by_room_agent.items()
        }
        bindings = []
        for lineage in snapshot.bindings_by_id.values():
            active = active_by_binding.get(lineage.binding_id)
            adapter = self.adapter_types.get(lineage.agent_id, "unknown")
            for record in lineage.generations:
                bindings.append({
                    "bindingId": lineage.binding_id,
                    "generation": record.generation,
                    "agentId": lineage.agent_id,
                    "agentDisplayName": self.display_names.get(lineage.agent_id, lineage.agent_id),
                    "adapter": adapter,
                    "roomId": active[0] if active and active[2] == record.generation else None,
                    "active": bool(active and active[2] == record.generation),
                    "nativeMappingAvailable": (lineage.binding_id, record.generation) in native_pairs.get(adapter, set()),
                })
        pending = [
            {
                "roomId": item.room_id,
                "agentId": item.agent_id,
                "agentDisplayName": self.display_names.get(item.agent_id, item.agent_id),
                "adapter": self.adapter_types.get(item.agent_id, "unknown"),
                "createdAt": item.created_at,
                "status": "pending",
                "triggerPreview": item.trigger_preview,
                "canCreate": item.agent_id in self.native_controls and bool(
                    getattr(self.native_controls.get(item.agent_id), "can_create", True)
                ),
                "canBindExisting": item.agent_id in self.native_controls,
            }
            for item in self.operations.root.pending_onboarding()
        ]
        return {"pendingOnboarding": pending, "bindings": bindings}

    async def attach_pending(
        self, room_id: str, agent_id: str, binding_id: str, generation: int
    ) -> RootHumanTurnResult:
        async with self._lock:
            await self._validate_managed_binding(agent_id, binding_id, generation)
            return await self.operations.attach_detached(
                room_id, agent_id, binding_id, generation
            )

    async def cancel_pending(self, room_id: str, agent_id: str) -> bool:
        async with self._lock:
            return self.operations.root.cancel_onboarding(room_id, agent_id)

    async def bind_existing(
        self, room_id: str, agent_id: str, native_reference: str
    ) -> RootHumanTurnResult:
        async with self._lock:
            self._require_pending(room_id, agent_id)
            native = self._native(agent_id)
            await native.validate_existing(native_reference)
            raw, control = self.operations._load()
            existing = native.find_mapping(raw, native_reference)
            if existing is not None:
                return await self.operations.attach_detached(
                    room_id, agent_id, existing[0], existing[1]
                )
            return await self._register_new(
                raw, control, room_id, agent_id, native_reference, native
            )

    async def create_pending(self, room_id: str, agent_id: str) -> RootHumanTurnResult:
        async with self._lock:
            self._require_pending(room_id, agent_id)
            native = self._native(agent_id)
            native_reference = await native.create()
            raw, control = self.operations._load()
            try:
                return await self._register_new(
                    raw, control, room_id, agent_id, native_reference, native
                )
            except OSError as exc:
                raise NativeCreatedUnmanaged(
                    "聊天窗可能已建立，但 Bridge 尚未完成納管；請在本地 APP 檢查後使用綁定既有聊天窗。"
                ) from exc

    async def bind_existing_direct(
        self, room_id: str, agent_id: str, native_reference: str
    ) -> None:
        async with self._lock:
            self._require_direct_target(room_id, agent_id)
            native = self._native(agent_id)
            await native.validate_existing(native_reference)
            raw, control = self.operations._load()
            existing = native.find_mapping(raw, native_reference)
            if existing is not None:
                await self._validate_managed_binding(agent_id, existing[0], existing[1])
                self.operations.attach_configured(
                    room_id, agent_id, existing[0], existing[1]
                )
                return
            self._register_direct(
                raw, control, room_id, agent_id, native_reference, native
            )

    async def create_direct(self, room_id: str, agent_id: str) -> None:
        async with self._lock:
            self._require_direct_target(room_id, agent_id)
            native = self._native(agent_id)
            if not bool(getattr(native, "can_create", True)):
                raise ValueError("這個 Adapter 不支援建立新的聊天窗")
            native_reference = await native.create()
            raw, control = self.operations._load()
            try:
                self._register_direct(
                    raw, control, room_id, agent_id, native_reference, native
                )
            except OSError as exc:
                raise NativeCreatedUnmanaged(
                    "聊天窗可能已建立，但 Bridge 尚未完成納管；請在本地 APP 檢查後使用綁定既有聊天窗。"
                ) from exc

    async def move(self, source_room_id: str, target_room_id: str, agent_id: str) -> None:
        async with self._lock:
            binding = self.operations.root.core.binding(source_room_id, agent_id)
            if binding is None:
                raise ValueError("source room+agent has no active runtime binding")
            await self._validate_managed_binding(
                agent_id, binding.binding_id, binding.generation
            )
            self.operations.move(source_room_id, target_room_id, agent_id)

    async def move_room(self, source_room_id: str, target_room_id: str) -> None:
        async with self._lock:
            raw, control = self.operations._load()
            entries = [
                (agent_id, ref)
                for (room_id, agent_id), ref in control.snapshot().active_by_room_agent.items()
                if room_id == source_room_id
            ]
            if not entries:
                raise ValueError("source room has no active bindings")
            for agent_id, ref in entries:
                await self._validate_managed_binding(
                    agent_id, ref.binding_id, ref.generation
                )
            self.operations.move_room(source_room_id, target_room_id)

    async def unbind(self, room_id: str, agent_id: str) -> None:
        async with self._lock:
            self.operations.unbind(room_id, agent_id)

    def _native(self, agent_id: str) -> NativeBindingControl:
        try:
            return self.native_controls[agent_id]
        except KeyError as exc:
            raise ValueError("這個 Agent 尚未提供安全的聊天窗 control plane") from exc

    def _require_pending(self, room_id: str, agent_id: str) -> None:
        if not any(
            item.room_id == room_id and item.agent_id == agent_id
            for item in self.operations.root.pending_onboarding()
        ):
            raise ValueError("room+agent has no pending onboarding")

    def _require_direct_target(self, room_id: str, agent_id: str) -> None:
        if agent_id not in self.enabled_agents:
            raise ValueError("agentId: Agent 未啟用")
        if any(
            item.room_id == room_id and item.agent_id == agent_id
            for item in self.operations.root.pending_onboarding()
        ):
            raise ValueError("room+agent 尚有 pending onboarding；請先完成或取消")
        raw, control = self.operations._load()
        if self.operations.root.core.binding(room_id, agent_id) is not None:
            raise ValueError("room+agent already has an active runtime binding")
        if (room_id, agent_id) in control.snapshot().active_by_room_agent:
            raise ValueError("room+agent already has an active persisted binding")

    async def _validate_managed_binding(
        self, agent_id: str, binding_id: str, generation: int
    ) -> None:
        raw, control = self.operations._load()
        lineage = control.snapshot().bindings_by_id.get(binding_id)
        if lineage is None or lineage.agent_id != agent_id:
            raise ValueError("binding does not belong to this agent")
        if generation not in {record.generation for record in lineage.generations}:
            raise ValueError("binding generation is unavailable")
        adapter = self.adapter_types.get(agent_id)
        native_reference = _native_reference(raw, adapter, binding_id, generation)
        native = self.native_controls.get(agent_id)
        if native is not None:
            if native.adapter != adapter:
                raise ValueError("native control adapter does not match agent adapter")
            await native.validate_existing(native_reference)
        elif adapter == "codex":
            raise ValueError("Codex native validation is unavailable")

    async def _register_new(self, raw, control, room_id, agent_id, native_reference, native):
        self._require_pending(room_id, agent_id)
        binding_id = self.binding_id_factory()
        generation = 1
        from .binding_control import BindingGenerationRecord, BindingLineage
        control.register_lineage(BindingLineage(binding_id, agent_id, (BindingGenerationRecord(generation),)))
        control.attach((room_id, agent_id), BindingRef(binding_id, generation))
        native.validate_publication(binding_id, generation, native_reference)
        raw = dict(raw)
        entries = list(raw.get(native.mapping_field, []))
        entries.append(native.mapping_entry(binding_id, generation, native_reference))
        raw[native.mapping_field] = entries
        save_binding_document(self.operations.path, document_from_snapshot(raw, control.snapshot()))
        native.publish(binding_id, generation, native_reference)
        return await self.operations.root.complete_onboarding(
            room_id, agent_id, self.operations._snapshot(binding_id, generation)
        )

    def _register_direct(self, raw, control, room_id, agent_id, native_reference, native) -> None:
        binding_id = self.binding_id_factory()
        generation = 1
        from .binding_control import BindingGenerationRecord, BindingLineage
        control.register_lineage(BindingLineage(binding_id, agent_id, (BindingGenerationRecord(generation),)))
        control.attach((room_id, agent_id), BindingRef(binding_id, generation))
        native.validate_publication(binding_id, generation, native_reference)
        raw = dict(raw)
        entries = list(raw.get(native.mapping_field, []))
        entries.append(native.mapping_entry(binding_id, generation, native_reference))
        raw[native.mapping_field] = entries
        save_binding_document(self.operations.path, document_from_snapshot(raw, control.snapshot()))
        native.publish(binding_id, generation, native_reference)
        self.operations.root.core.publish_binding(
            room_id, agent_id, self.operations._snapshot(binding_id, generation)
        )


def _native_pairs(raw: Mapping[str, object], field: str) -> set[tuple[object, object]]:
    entries = raw.get(field, [])
    if not isinstance(entries, list):
        return set()
    return {
        (entry.get("bindingId"), entry.get("generation"))
        for entry in entries
        if isinstance(entry, Mapping)
    }


def _native_reference(
    raw: Mapping[str, object], adapter: str | None, binding_id: str, generation: int
) -> str:
    fields = {
        "codex": ("codexBindings", "threadId"),
        "antigravity": ("antigravityBindings", "conversationId"),
    }
    try:
        field, reference_field = fields[adapter]
    except KeyError as exc:
        raise ValueError("agent adapter does not support managed bindings") from exc
    entries = raw.get(field, [])
    if not isinstance(entries, list):
        raise ValueError(f"{field} must be an array")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, Mapping)
        and entry.get("bindingId") == binding_id
        and entry.get("generation") == generation
    ]
    if len(matches) != 1:
        raise ValueError("binding must have exactly one native mapping")
    native_reference = matches[0].get(reference_field)
    if not isinstance(native_reference, str) or not native_reference.strip():
        raise ValueError("native binding reference is malformed")
    owners = [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and entry.get(reference_field) == native_reference
    ]
    if len(owners) != 1:
        raise ValueError("native reference has duplicate mappings")
    return native_reference


def _require_unique_native_mapping(
    raw: Mapping[str, object], binding_id: str, generation: int
) -> None:
    matches = 0
    for field in ("codexBindings", "antigravityBindings"):
        entries = raw.get(field, [])
        if not isinstance(entries, list):
            raise ValueError(f"{field} must be an array")
        matches += sum(
            1
            for entry in entries
            if isinstance(entry, Mapping)
            and entry.get("bindingId") == binding_id
            and entry.get("generation") == generation
        )
    if matches != 1:
        raise ValueError("binding must have exactly one native mapping")
