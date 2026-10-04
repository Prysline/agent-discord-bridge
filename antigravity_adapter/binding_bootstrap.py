"""Compose logical bindings with adapter-local Antigravity conversations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_bridge.binding_bootstrap import build_binding_control
from agent_bridge.binding_control import BindingControlState

from .binding import InMemoryBindingResolver, ResolvedBinding


@dataclass(frozen=True)
class AntigravityBindingBootstrap:
    binding_control: BindingControlState
    binding_resolver: InMemoryBindingResolver


def bootstrap_existing_bindings(raw: Mapping[str, Any]) -> AntigravityBindingBootstrap:
    control = build_binding_control(raw)
    entries = raw.get("antigravityBindings")
    if not isinstance(entries, list):
        raise ValueError("antigravityBindings must be an array")
    known = {
        (lineage.binding_id, record.generation)
        for lineage in control.snapshot().bindings_by_id.values()
        for record in lineage.generations
    }
    values: list[ResolvedBinding] = []
    seen: set[tuple[str, int]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(f"antigravityBindings[{index}] must be an object")
        binding_id = _identifier(entry.get("bindingId"), f"antigravityBindings[{index}].bindingId")
        generation = _generation(entry.get("generation"), f"antigravityBindings[{index}].generation")
        conversation_id = _identifier(entry.get("conversationId"), f"antigravityBindings[{index}].conversationId")
        pair = (binding_id, generation)
        if pair not in known:
            raise ValueError("native mapping references an unknown logical binding generation")
        if pair in seen:
            raise ValueError("duplicate exact native binding mapping")
        seen.add(pair)
        values.append(ResolvedBinding(binding_id, generation, conversation_id))
    return AntigravityBindingBootstrap(control, InMemoryBindingResolver(values))


def load_existing_binding_bootstrap(path: str | Path) -> AntigravityBindingBootstrap:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("binding bootstrap is not valid JSON") from exc
    return bootstrap_existing_bindings(raw)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _generation(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value
