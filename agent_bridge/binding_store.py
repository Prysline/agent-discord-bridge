"""Validated atomic persistence for local binding control-plane state."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .binding_bootstrap import build_binding_control
from .binding_control import BindingControlSnapshot


def normalize_binding_document(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return the current schema without changing logical or native identity."""
    control = build_binding_control(raw).snapshot()
    return document_from_snapshot(raw, control)


def document_from_snapshot(
    raw: Mapping[str, Any], control: BindingControlSnapshot
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "bindingLineages": [
            {
                "bindingId": lineage.binding_id,
                "agentId": lineage.agent_id,
                "generations": [item.generation for item in lineage.generations],
            }
            for lineage in control.bindings_by_id.values()
        ],
        "activeBindings": [
            {
                "roomId": room_id,
                "agentId": agent_id,
                "bindingId": ref.binding_id,
                "activeGeneration": ref.generation,
            }
            for (room_id, agent_id), ref in control.active_by_room_agent.items()
        ],
        "codexBindings": _mapping_array(raw, "codexBindings"),
        "antigravityBindings": _mapping_array(raw, "antigravityBindings"),
    }
    build_binding_control(value)
    return value


def save_binding_document(path: Path, raw: Mapping[str, Any]) -> None:
    value = normalize_binding_document(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _mapping_array(raw: Mapping[str, Any], field: str) -> list[dict[str, Any]]:
    entries = raw.get(field, [])
    if not isinstance(entries, list) or any(not isinstance(item, Mapping) for item in entries):
        raise ValueError(f"{field} must be an array of objects")
    return [dict(item) for item in entries]
