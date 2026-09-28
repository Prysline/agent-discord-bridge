"""Minimal one-message Discord delivery boundary for the shared core."""

from __future__ import annotations

from typing import Any


class DiscordRoomDelivery:
    def __init__(self, *, message_limit: int = 2000) -> None:
        self.message_limit = message_limit
        self._channels: dict[str, Any] = {}

    def register(self, room_id: str, channel: Any) -> None:
        self._channels[room_id] = channel

    async def deliver(
        self, room_id: str, agent_id: str, text: str, request_id: str
    ) -> str:
        channel = self._channels.get(room_id)
        if channel is None or not isinstance(text, str) or not text.strip():
            return "not_delivered"
        if len(text) > self.message_limit:
            return "not_delivered"
        try:
            sent = await channel.send(text)
        except Exception:
            return "unknown"
        return "delivered" if sent is not None else "unknown"
