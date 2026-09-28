"""Memory-only logical binding state for the shared core.

This module owns logical identity and active routing only.  Adapter-native
session references and binding lifecycle operations intentionally live
elsewhere.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Iterable


RoomAgentKey = tuple[str, str]


@dataclass(frozen=True)
class BindingRef:
    binding_id: str
    generation: int


@dataclass(frozen=True)
class BindingGenerationRecord:
    generation: int


@dataclass(frozen=True)
class BindingLineage:
    binding_id: str
    room_id: str
    agent_id: str
    generations: tuple[BindingGenerationRecord, ...]

    def __post_init__(self) -> None:
        generations = tuple(self.generations)
        numbers = tuple(item.generation for item in generations)
        if any(
            current >= following
            for current, following in zip(numbers, numbers[1:])
        ):
            raise ValueError("lineage generations must be unique and increasing")
        object.__setattr__(self, "generations", generations)


@dataclass(frozen=True)
class BindingControlSnapshot:
    active_by_room_agent: dict[RoomAgentKey, BindingRef]
    bindings_by_id: dict[str, BindingLineage]


class BindingControlState:
    """Validated memory-only state with detached inspection."""

    def __init__(
        self,
        *,
        lineages: Iterable[BindingLineage] = (),
        active_by_room_agent: Iterable[tuple[RoomAgentKey, BindingRef]] = (),
    ) -> None:
        self._bindings_by_id: dict[str, BindingLineage] = {}
        for lineage in lineages:
            if lineage.binding_id in self._bindings_by_id:
                raise ValueError("bindingId must identify exactly one lineage")
            self._bindings_by_id[lineage.binding_id] = lineage

        self._active_by_room_agent: dict[RoomAgentKey, BindingRef] = {}
        for key, ref in active_by_room_agent:
            if key in self._active_by_room_agent:
                raise ValueError("room+agent may have only one active binding")
            self._validate_active_ref(key, ref)
            self._active_by_room_agent[key] = ref

    def snapshot(self) -> BindingControlSnapshot:
        """Return detached state that callers may inspect or modify safely."""
        return BindingControlSnapshot(
            active_by_room_agent=deepcopy(self._active_by_room_agent),
            bindings_by_id=deepcopy(self._bindings_by_id),
        )

    def _validate_active_ref(self, key: RoomAgentKey, ref: BindingRef) -> None:
        lineage = self._bindings_by_id.get(ref.binding_id)
        if lineage is None:
            raise ValueError("active binding must reference an existing lineage")
        if key != (lineage.room_id, lineage.agent_id):
            raise ValueError("active binding owner must match room+agent")
        if ref.generation not in {
            record.generation for record in lineage.generations
        }:
            raise ValueError("active binding must reference an existing generation")
