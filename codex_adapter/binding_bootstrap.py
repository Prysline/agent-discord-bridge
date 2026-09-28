"""Compose shared logical bindings with adapter-local existing Codex sessions."""

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
class CodexBindingBootstrap:
    binding_control: BindingControlState
    binding_resolver: InMemoryBindingResolver


def bootstrap_existing_bindings(raw: Mapping[str, Any]) -> CodexBindingBootstrap:
    if not isinstance(raw, Mapping):
        raise ValueError("binding bootstrap must be an object")
    control = build_binding_control(raw)
    entries = raw.get("codexBindings")
    if not isinstance(entries, list):
        raise ValueError("codexBindings must be an array")

    snapshot = control.snapshot()
    known_pairs = {
        (lineage.binding_id, record.generation)
        for lineage in snapshot.bindings_by_id.values()
        for record in lineage.generations
    }
    required_pairs = {
        (ref.binding_id, ref.generation)
        for ref in snapshot.active_by_room_agent.values()
    }
    resolved: list[ResolvedBinding] = []
    configured_pairs: set[tuple[str, int]] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(f"codexBindings[{index}] must be an object")
        binding_id = _identifier(entry.get("bindingId"), f"codexBindings[{index}].bindingId")
        generation = _generation(entry.get("generation"), f"codexBindings[{index}].generation")
        thread_id = _identifier(entry.get("threadId"), f"codexBindings[{index}].threadId")
        pair = (binding_id, generation)
        if pair not in known_pairs:
            raise ValueError("native mapping references an unknown logical binding generation")
        if pair in configured_pairs:
            raise ValueError("duplicate exact native binding mapping")
        configured_pairs.add(pair)
        resolved.append(ResolvedBinding(binding_id, generation, thread_id))

    if not required_pairs.issubset(configured_pairs):
        raise ValueError("active logical binding is missing a Codex native mapping")
    return CodexBindingBootstrap(control, InMemoryBindingResolver(resolved))


def load_existing_binding_bootstrap(path: str | Path) -> CodexBindingBootstrap:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("binding bootstrap is not valid JSON") from exc
    return bootstrap_existing_bindings(raw)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _generation(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value
