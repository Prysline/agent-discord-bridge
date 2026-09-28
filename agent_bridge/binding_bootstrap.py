"""Parse logical existing-binding bootstrap data into shared state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .binding_control import (
    BindingControlState,
    BindingGenerationRecord,
    BindingLineage,
    BindingRef,
)


def build_binding_control(raw: Mapping[str, Any]) -> BindingControlState:
    """Build validated logical state without retaining adapter-native IDs."""
    entries = raw.get("logicalBindings")
    if not isinstance(entries, list) or not entries:
        raise ValueError("logicalBindings must be a non-empty array")

    lineages: list[BindingLineage] = []
    active: list[tuple[tuple[str, str], BindingRef]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(f"logicalBindings[{index}] must be an object")
        room_id = _identifier(entry.get("roomId"), f"logicalBindings[{index}].roomId")
        agent_id = _identifier(entry.get("agentId"), f"logicalBindings[{index}].agentId")
        binding_id = _identifier(
            entry.get("bindingId"), f"logicalBindings[{index}].bindingId"
        )
        generations = entry.get("generations")
        if not isinstance(generations, list) or not generations:
            raise ValueError(f"logicalBindings[{index}].generations must be a non-empty array")
        generation_values = tuple(
            _generation(value, f"logicalBindings[{index}].generations")
            for value in generations
        )
        active_generation = _generation(
            entry.get("activeGeneration"),
            f"logicalBindings[{index}].activeGeneration",
        )
        lineages.append(
            BindingLineage(
                binding_id,
                room_id,
                agent_id,
                tuple(BindingGenerationRecord(value) for value in generation_values),
            )
        )
        active.append(((room_id, agent_id), BindingRef(binding_id, active_generation)))
    return BindingControlState(lineages=lineages, active_by_room_agent=active)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _generation(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must contain integers")
    return value
