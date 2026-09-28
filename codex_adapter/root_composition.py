"""Compose the opt-in root human-turn path from existing bindings."""

from __future__ import annotations

from pathlib import Path

from agent_bridge import ConversationPolicy
from agent_bridge.orchestrator import BindingSnapshot, DeliveryPort, SharedOrchestrator
from agent_bridge.root_shared import RootSharedHumanTurn

from .binding_bootstrap import load_existing_binding_bootstrap
from .persistent import AppServerClient, CodexPersistentAdapter


def compose_existing_codex_root(
    *,
    binding_path: str | Path,
    agent_id: str,
    client: AppServerClient,
    delivery: DeliveryPort,
    display_name: str,
    timeout_ms: int = 120_000,
) -> RootSharedHumanTurn:
    bootstrap = load_existing_binding_bootstrap(binding_path)
    active = bootstrap.binding_control.snapshot().active_by_room_agent
    bindings = {
        key: BindingSnapshot(ref.binding_id, ref.generation)
        for key, ref in active.items()
        if key[1] == agent_id
    }
    if not bindings:
        raise ValueError("configured shared agent has no active binding")
    adapter = CodexPersistentAdapter(client, bootstrap.binding_resolver)
    core = SharedOrchestrator(
        policy=ConversationPolicy([], global_max_dispatches=1),
        adapters={agent_id: adapter},
        bindings=bindings,
        delivery=delivery,
        timeout_ms=timeout_ms,
        display_names={agent_id: display_name},
    )
    return RootSharedHumanTurn(core, agent_id)
