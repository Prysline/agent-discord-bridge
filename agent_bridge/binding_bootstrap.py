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
    entries = raw.get("bindingLineages")
    associations = raw.get("activeBindings")
    legacy = entries is None and associations is None
    if legacy:
        entries = raw.get("logicalBindings")
        associations = entries
    if not isinstance(entries, list) or (legacy and not entries):
        raise ValueError("bindingLineages must be an array; legacy logicalBindings must be non-empty")
    if not isinstance(associations, list):
        raise ValueError("activeBindings must be an array")

    lineages: list[BindingLineage] = []
    active: list[tuple[tuple[str, str], BindingRef]] = []
    for index, entry in enumerate(entries):
        prefix = "logicalBindings" if legacy else "bindingLineages"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{prefix}[{index}] must be an object")
        agent_id = _identifier(entry.get("agentId"), f"{prefix}[{index}].agentId")
        binding_id = _identifier(
            entry.get("bindingId"), f"{prefix}[{index}].bindingId"
        )
        generations = entry.get("generations")
        if not isinstance(generations, list) or not generations:
            raise ValueError(f"{prefix}[{index}].generations must be a non-empty array")
        generation_values = tuple(
            _generation(value, f"{prefix}[{index}].generations")
            for value in generations
        )
        lineages.append(
            BindingLineage(
                binding_id,
                agent_id,
                tuple(BindingGenerationRecord(value) for value in generation_values),
            )
        )
    for index, entry in enumerate(associations):
        if not isinstance(entry, Mapping):
            raise ValueError(f"activeBindings[{index}] must be an object")
        prefix = "logicalBindings" if legacy else "activeBindings"
        room_id = _identifier(entry.get("roomId"), f"{prefix}[{index}].roomId")
        agent_id = _identifier(entry.get("agentId"), f"{prefix}[{index}].agentId")
        binding_id = _identifier(entry.get("bindingId"), f"{prefix}[{index}].bindingId")
        active_generation = _generation(entry.get("activeGeneration"), f"{prefix}[{index}].activeGeneration")
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
