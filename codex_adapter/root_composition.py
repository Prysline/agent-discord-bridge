"""Compose the opt-in root human-turn path from existing bindings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_bridge import ConversationPolicy, Participant
from agent_bridge.orchestrator import BindingSnapshot, DeliveryPort, SharedOrchestrator
from agent_bridge.root_shared import RootSharedHumanTurn

from .binding_bootstrap import load_existing_binding_bootstrap
from .persistent import AppServerClient, CodexPersistentAdapter


@dataclass(frozen=True)
class RootDiscussionSettings:
    participants: tuple[Participant, ...]
    display_names: Mapping[str, str]
    global_max_dispatches: int


def parse_root_discussion_settings(config: Mapping[str, Any]) -> RootDiscussionSettings:
    raw = config.get("sharedDiscussion")
    if raw is None:
        return RootDiscussionSettings((), {}, 1)
    if not isinstance(raw, Mapping):
        raise ValueError("sharedDiscussion must be an object")
    maximum = raw.get("globalMaxDispatches")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ValueError("sharedDiscussion.globalMaxDispatches must be positive")
    entries = raw.get("participants")
    if not isinstance(entries, list):
        raise ValueError("sharedDiscussion.participants must be an array")
    participants: list[Participant] = []
    display_names: dict[str, str] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError(f"sharedDiscussion.participants[{index}] must be an object")
        agent_id = _non_empty(entry.get("agentId"), f"participants[{index}].agentId")
        mention_id = _non_empty(
            entry.get("mentionId"), f"participants[{index}].mentionId"
        )
        if not mention_id.isdigit():
            raise ValueError(f"participants[{index}].mentionId must be numeric")
        display_name = _non_empty(
            entry.get("displayName", agent_id), f"participants[{index}].displayName"
        )
        participants.append(
            Participant(
                agent_id,
                mention_id,
                _positive_int(entry.get("budgetChars"), f"participants[{index}].budgetChars"),
                _positive_int(entry.get("maxCalls"), f"participants[{index}].maxCalls"),
                _boolean(entry.get("enabled", True), f"participants[{index}].enabled"),
                _boolean(entry.get("available", True), f"participants[{index}].available"),
            )
        )
        display_names[agent_id] = display_name
    return RootDiscussionSettings(tuple(participants), display_names, maximum)


def _non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def compose_existing_codex_root(
    *,
    binding_path: str | Path,
    agent_id: str,
    client: AppServerClient,
    delivery: DeliveryPort,
    display_name: str,
    participants: list[Participant] | None = None,
    participant_display_names: dict[str, str] | None = None,
    global_max_dispatches: int = 1,
    timeout_ms: int = 120_000,
) -> RootSharedHumanTurn:
    bootstrap = load_existing_binding_bootstrap(binding_path)
    active = bootstrap.binding_control.snapshot().active_by_room_agent
    participants = participants or []
    configured_agent_ids = {agent_id, *(item.agent_id for item in participants)}
    bindings = {
        key: BindingSnapshot(ref.binding_id, ref.generation)
        for key, ref in active.items()
        if key[1] in configured_agent_ids
    }
    if not any(key[1] == agent_id for key in bindings):
        raise ValueError("configured shared agent has no active binding")
    adapter = CodexPersistentAdapter(client, bootstrap.binding_resolver)
    display_names = dict(participant_display_names or {})
    display_names.setdefault(agent_id, display_name)
    core = SharedOrchestrator(
        policy=ConversationPolicy(
            participants, global_max_dispatches=global_max_dispatches
        ),
        adapters={configured_agent_id: adapter for configured_agent_id in configured_agent_ids},
        bindings=bindings,
        delivery=delivery,
        timeout_ms=timeout_ms,
        display_names=display_names,
    )
    return RootSharedHumanTurn(core, agent_id)
