"""Local-only Agent and Discord sender configuration."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ManagedAgent:
    agent_id: str
    display_name: str
    adapter: str
    mention_id: str
    sender_id: str
    enabled: bool
    available: bool
    budget_chars: int
    max_calls: int


@dataclass(frozen=True)
class DiscordSender:
    sender_id: str
    label: str
    bot_user_id: str
    token: str
    enabled: bool


@dataclass(frozen=True)
class AgentManagement:
    agents: tuple[ManagedAgent, ...]
    senders: tuple[DiscordSender, ...]

    def redacted(
        self,
        bindings: Mapping[tuple[str, str], Any] | None = None,
        native_pairs: Mapping[str, set[tuple[str, int]]] | None = None,
    ) -> dict[str, Any]:
        bindings = bindings or {}
        native_pairs = native_pairs or {}
        adapters = {item.agent_id: item.adapter for item in self.agents}
        by_agent: dict[str, list[Any]] = {}
        for (room_id, agent_id), ref in bindings.items():
            by_agent.setdefault(agent_id, []).append({
                "roomId": room_id,
                "bindingId": ref.binding_id,
                "generation": ref.generation,
                "nativeMappingAvailable": (
                    (ref.binding_id, ref.generation)
                    in native_pairs.get(adapters.get(agent_id, ""), set())
                ),
            })
        usage = sender_usage(self)
        return {
            "agents": [
                {
                    "agentId": item.agent_id,
                    "displayName": item.display_name,
                    "adapter": item.adapter,
                    "mentionId": item.mention_id,
                    "senderId": item.sender_id,
                    "enabled": item.enabled,
                    "available": item.available,
                    "budgetChars": item.budget_chars,
                    "maxCalls": item.max_calls,
                    "bindings": by_agent.get(item.agent_id, []),
                }
                for item in self.agents
            ],
            "senders": [
                {
                    "senderId": item.sender_id,
                    "label": item.label,
                    "botUserId": item.bot_user_id,
                    "enabled": item.enabled,
                    "tokenConfigured": bool(item.token),
                    "status": "disabled" if not item.enabled else ("configured" if item.token else "missing-token"),
                    "usedByAgents": usage.get(item.sender_id, []),
                }
                for item in self.senders
            ],
        }


def parse_agent_management(raw: Mapping[str, Any]) -> AgentManagement:
    errors: list[str] = []
    raw_agents = raw.get("agents")
    raw_senders = raw.get("senders")
    if not isinstance(raw_agents, list):
        errors.append("agents: 必須是陣列")
        raw_agents = []
    if not isinstance(raw_senders, list):
        errors.append("senders: 必須是陣列")
        raw_senders = []
    senders: list[DiscordSender] = []
    sender_ids: set[str] = set()
    for index, value in enumerate(raw_senders):
        field = f"senders[{index}]"
        if not isinstance(value, Mapping):
            errors.append(f"{field}: 必須是物件")
            continue
        sender_id = _text(value.get("senderId"), f"{field}.senderId", errors)
        label = _text(value.get("label"), f"{field}.label", errors)
        bot_user_id = _snowflake(value.get("botUserId"), f"{field}.botUserId", errors)
        token = value.get("token", "")
        enabled = _bool(value.get("enabled", True), f"{field}.enabled", errors)
        if not isinstance(token, str):
            errors.append(f"{field}.token: 必須是字串")
            token = ""
        if sender_id in sender_ids:
            errors.append(f"{field}.senderId: 重複的 Sender ID")
        sender_ids.add(sender_id)
        if enabled and not token.strip():
            errors.append(f"{field}.token: 啟用的 Sender 必須設定 Token")
        senders.append(DiscordSender(sender_id, label, bot_user_id, token.strip(), enabled))
    agents: list[ManagedAgent] = []
    agent_ids: set[str] = set()
    for index, value in enumerate(raw_agents):
        field = f"agents[{index}]"
        if not isinstance(value, Mapping):
            errors.append(f"{field}: 必須是物件")
            continue
        agent_id = _text(value.get("agentId"), f"{field}.agentId", errors)
        display_name = _text(value.get("displayName"), f"{field}.displayName", errors)
        adapter = value.get("adapter")
        if adapter not in {"codex", "antigravity"}:
            errors.append(f"{field}.adapter: 必須是 codex 或 antigravity")
            adapter = ""
        mention_id = _snowflake(value.get("mentionId"), f"{field}.mentionId", errors)
        sender_id = _text(value.get("senderId"), f"{field}.senderId", errors)
        enabled = _bool(value.get("enabled", True), f"{field}.enabled", errors)
        available = _bool(value.get("available", True), f"{field}.available", errors)
        budget = _positive(value.get("budgetChars"), f"{field}.budgetChars", errors)
        calls = _positive(value.get("maxCalls"), f"{field}.maxCalls", errors)
        if agent_id in agent_ids:
            errors.append(f"{field}.agentId: 重複的 Agent ID")
        agent_ids.add(agent_id)
        if sender_id not in sender_ids:
            errors.append(f"{field}.senderId: 找不到對應 Sender")
        agents.append(ManagedAgent(agent_id, display_name, str(adapter), mention_id, sender_id, enabled, available, budget, calls))
    if errors:
        raise ValueError("\n".join(errors))
    return AgentManagement(tuple(agents), tuple(senders))


def load_agent_management(path: Path, legacy_config: Mapping[str, Any], token: str, bot_user_id: int) -> AgentManagement:
    if path.exists():
        return parse_agent_management(json.loads(path.read_text(encoding="utf-8")))
    participants = legacy_config.get("sharedDiscussion", {}).get("participants", [])
    agents = tuple(
        ManagedAgent(
            str(item["agentId"]), str(item.get("displayName", item["agentId"])),
            str(item["adapter"]), str(item["mentionId"]), "legacy-default",
            bool(item.get("enabled", True)), bool(item.get("available", True)),
            int(item["budgetChars"]), int(item["maxCalls"]),
        )
        for item in participants
    )
    sender = DiscordSender("legacy-default", "Legacy Root Bot", str(bot_user_id), token or "", True)
    return AgentManagement(agents, (sender,))


def merge_secret_placeholders(candidate: Mapping[str, Any], current: AgentManagement) -> dict[str, Any]:
    result = json.loads(json.dumps(candidate))
    old = {item.sender_id: item.token for item in current.senders}
    for sender in result.get("senders", []):
        if isinstance(sender, dict) and not sender.get("token") and not sender.get("clearToken"):
            sender["token"] = old.get(str(sender.get("senderId")), "")
        sender.pop("clearToken", None)
    return result


def save_agent_management(path: Path, value: AgentManagement) -> None:
    payload = {
        "agents": [
            {"agentId": a.agent_id, "displayName": a.display_name, "adapter": a.adapter,
             "mentionId": a.mention_id, "senderId": a.sender_id, "enabled": a.enabled,
             "available": a.available, "budgetChars": a.budget_chars, "maxCalls": a.max_calls}
            for a in value.agents
        ],
        "senders": [
            {"senderId": s.sender_id, "label": s.label, "botUserId": s.bot_user_id,
             "token": s.token, "enabled": s.enabled}
            for s in value.senders
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def sender_usage(value: AgentManagement) -> dict[str, list[str]]:
    usage: dict[str, list[str]] = {}
    for agent in value.agents:
        if agent.enabled:
            usage.setdefault(agent.sender_id, []).append(agent.agent_id)
    return usage


def validate_binding_associations(value: AgentManagement, raw: Mapping[str, Any]) -> None:
    logical: dict[str, list[tuple[Any, Any]]] = {}
    for entry in raw.get("logicalBindings", []):
        if isinstance(entry, Mapping):
            logical.setdefault(str(entry.get("agentId")), []).append(
                (entry.get("bindingId"), entry.get("activeGeneration"))
            )
    native = {
        "codex": {(entry.get("bindingId"), entry.get("generation")) for entry in raw.get("codexBindings", []) if isinstance(entry, Mapping)},
        "antigravity": {(entry.get("bindingId"), entry.get("generation")) for entry in raw.get("antigravityBindings", []) if isinstance(entry, Mapping)},
    }
    errors = []
    for agent in value.agents:
        for ref in logical.get(agent.agent_id, []):
            if ref not in native[agent.adapter]:
                errors.append(f"agents[{agent.agent_id}].adapter: 現有 Binding 缺少對應 native mapping")
    if errors:
        raise ValueError("\n".join(errors))


def _text(value: Any, field: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field}: 必填")
        return ""
    return value.strip()


def _snowflake(value: Any, field: str, errors: list[str]) -> str:
    result = _text(value, field, errors)
    if result and (not result.isdigit() or not 15 <= len(result) <= 22):
        errors.append(f"{field}: Discord User ID 格式錯誤")
    return result


def _bool(value: Any, field: str, errors: list[str]) -> bool:
    if not isinstance(value, bool):
        errors.append(f"{field}: 必須是布林值")
        return False
    return value


def _positive(value: Any, field: str, errors: list[str]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        errors.append(f"{field}: 必須是正整數")
        return 0
    return value
