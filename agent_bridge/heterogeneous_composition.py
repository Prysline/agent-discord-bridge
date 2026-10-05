"""Composition-only selection of persistent adapters for shared agents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from antigravity_adapter.binding_bootstrap import bootstrap_existing_bindings as bootstrap_antigravity
from antigravity_adapter.persistent import AntigravityPersistentAdapter
from antigravity_adapter.transport import AntigravityTransport
from agent_bridge.binding_bootstrap import build_binding_control
from codex_adapter.binding_bootstrap import bootstrap_existing_bindings as bootstrap_codex
from codex_adapter.persistent import AppServerClient, CodexPersistentAdapter

from .conversation_policy import ConversationPolicy, Participant
from .orchestrator import BindingSnapshot, DeliveryPort, SharedOrchestrator
from .root_shared import RootSharedHumanTurn


def compose_existing_heterogeneous_root(
    *,
    binding_path: str | Path,
    primary_agent_id: str,
    adapter_types: Mapping[str, str],
    codex_client: AppServerClient,
    antigravity_transport: AntigravityTransport,
    delivery: DeliveryPort,
    participants: list[Participant],
    display_names: Mapping[str, str],
    global_max_dispatches: int,
    timeout_ms: int = 120_000,
) -> RootSharedHumanTurn:
    raw = json.loads(Path(binding_path).read_text(encoding="utf-8"))
    selected_adapters = dict(adapter_types)
    participant_agent_ids = {item.agent_id for item in participants}
    if primary_agent_id in participant_agent_ids and primary_agent_id not in selected_adapters:
        raise ValueError("every discussion participant must select an adapter")
    selected_adapters.setdefault(primary_agent_id, "codex")
    codex_agents = {agent_id for agent_id, kind in selected_adapters.items() if kind == "codex"}
    codex_raw = dict(raw)
    codex_raw["logicalBindings"] = [
        entry for entry in raw.get("logicalBindings", []) if entry.get("agentId") in codex_agents
    ]
    codex = bootstrap_codex(codex_raw) if codex_agents else None
    antigravity_agents = {
        agent_id for agent_id, kind in selected_adapters.items() if kind == "antigravity"
    }
    antigravity = bootstrap_antigravity(raw) if antigravity_agents else None
    control = build_binding_control(raw).snapshot()
    configured = {primary_agent_id, *participant_agent_ids}
    if set(selected_adapters) != configured:
        raise ValueError("every configured shared agent must select exactly one adapter")
    adapters = {}
    codex_adapter = CodexPersistentAdapter(codex_client, codex.binding_resolver) if codex else None
    antigravity_adapter = (
        AntigravityPersistentAdapter(antigravity_transport, antigravity.binding_resolver)
        if antigravity else None
    )
    for agent_id in configured:
        adapters[agent_id] = (
            codex_adapter if selected_adapters[agent_id] == "codex" else antigravity_adapter
        )
    bindings = {
        key: BindingSnapshot(ref.binding_id, ref.generation)
        for key, ref in control.active_by_room_agent.items()
        if key[1] in configured
    }
    if {key[1] for key in bindings} != configured:
        raise ValueError("configured shared agent has no active binding")
    antigravity_pairs = {
        (entry.get("bindingId"), entry.get("generation"))
        for entry in raw.get("antigravityBindings", [])
        if isinstance(entry, dict)
    }
    if any(
        (binding.binding_id, binding.generation) not in antigravity_pairs
        for (room_id, agent_id), binding in bindings.items()
        if agent_id in antigravity_agents
    ):
        raise ValueError("active Antigravity binding is missing a native mapping")
    return RootSharedHumanTurn(
        SharedOrchestrator(
            policy=ConversationPolicy(participants, global_max_dispatches=global_max_dispatches),
            adapters=adapters,
            bindings=bindings,
            delivery=delivery,
            timeout_ms=timeout_ms,
            display_names=display_names,
        ),
        primary_agent_id,
        participants,
    )
