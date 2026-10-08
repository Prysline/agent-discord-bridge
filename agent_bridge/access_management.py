"""Validated, display-friendly editing of the Discord access policy."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping

from conversation_policy import validate_config


def access_view(config: Mapping[str, Any]) -> dict[str, Any]:
    labels = config.get("accessLabels", {})
    user_labels = labels.get("users", {}) if isinstance(labels, Mapping) else {}
    ids = {str(value) for value in config.get("allowFrom", [])}
    for policy in config.get("channels", {}).values():
        if isinstance(policy, Mapping):
            ids.update(str(value) for value in policy.get("allowFrom", []))
    return {
        "dmPolicy": config.get("dmPolicy", "disabled"),
        "users": [{"userId": value, "label": str(user_labels.get(value, value))} for value in sorted(ids)],
        "channels": [
            {
                "channelId": str(channel_id), "label": str(policy.get("name", channel_id)),
                "requireMention": bool(policy.get("requireMention", True)),
                "allowedUserIds": [str(value) for value in policy.get("allowFrom", [])],
            }
            for channel_id, policy in config.get("channels", {}).items()
            if isinstance(policy, Mapping)
        ],
    }


def update_access_config(config: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
    raw_users = candidate.get("users")
    raw_channels = candidate.get("channels")
    dm_policy = candidate.get("dmPolicy")
    if dm_policy not in {"disabled", "allowlist"}:
        raise ValueError("dmPolicy: 必須是 disabled 或 allowlist")
    if not isinstance(raw_users, list) or not raw_users:
        raise ValueError("users: 至少需要一位授權使用者")
    if not isinstance(raw_channels, list) or not raw_channels:
        raise ValueError("channels: 至少需要一個授權頻道")
    users: dict[str, str] = {}
    for index, item in enumerate(raw_users):
        if not isinstance(item, Mapping): raise ValueError(f"users[{index}]: 必須是物件")
        user_id = _snowflake(item.get("userId"), f"users[{index}].userId")
        label = _text(item.get("label"), f"users[{index}].label")
        if user_id in users: raise ValueError(f"users[{index}].userId: 重複")
        users[user_id] = label
    current_channels = config.get("channels", {})
    channels: dict[str, Any] = {}
    for index, item in enumerate(raw_channels):
        if not isinstance(item, Mapping): raise ValueError(f"channels[{index}]: 必須是物件")
        channel_id = _snowflake(item.get("channelId"), f"channels[{index}].channelId")
        label = _text(item.get("label"), f"channels[{index}].label")
        allowed = item.get("allowedUserIds")
        if not isinstance(allowed, list) or not allowed:
            raise ValueError(f"channels[{index}].allowedUserIds: 至少選擇一位使用者")
        normalized = [_snowflake(value, f"channels[{index}].allowedUserIds") for value in allowed]
        if len(normalized) != len(set(normalized)): raise ValueError(f"channels[{index}].allowedUserIds: 重複")
        if any(value not in users for value in normalized): raise ValueError(f"channels[{index}].allowedUserIds: 包含未知使用者")
        if channel_id in channels: raise ValueError(f"channels[{index}].channelId: 重複")
        old = current_channels.get(channel_id, {}) if isinstance(current_channels, Mapping) else {}
        channels[channel_id] = {
            "name": label, "requireMention": _boolean(item.get("requireMention", True), f"channels[{index}].requireMention"),
            "allowFrom": normalized, "allowBotMention": bool(old.get("allowBotMention", False)),
            "allowBotFrom": list(old.get("allowBotFrom", [])),
        }
    result = json.loads(json.dumps(config))
    result["dmPolicy"] = dm_policy
    result["allowFrom"] = list(users) if dm_policy == "allowlist" else []
    result["channels"] = channels
    preserved_labels = result.get("accessLabels", {})
    if not isinstance(preserved_labels, dict): preserved_labels = {}
    result["accessLabels"] = {**preserved_labels, "users": users}
    validate_config(result)
    return result


def save_access_config(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2); stream.write("\n")
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip(): raise ValueError(f"{field}: 必填")
    return value.strip()


def _snowflake(value: object, field: str) -> str:
    result = _text(value, field)
    if not result.isdigit() or not 15 <= len(result) <= 22: raise ValueError(f"{field}: Discord ID 格式錯誤")
    return result


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool): raise ValueError(f"{field}: 必須是布林值")
    return value
