"""Minimal one-message Discord delivery boundary for the shared core."""

from __future__ import annotations

from typing import Any

from .agent_management import AgentManagement, sender_usage


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


class DiscordSenderDelivery:
    """Route outbound messages by Agent identity without changing canonical text."""

    def __init__(self, config: AgentManagement, *, message_limit: int = 2000) -> None:
        self.message_limit = message_limit
        self._agents = {item.agent_id: item for item in config.agents}
        self._senders = {item.sender_id: item for item in config.senders}
        self._shared = {key for key, agents in sender_usage(config).items() if len(agents) >= 2}
        self._clients: dict[str, Any] = {}
        self._rooms: dict[str, int] = {}

    def register_sender(self, sender_id: str, client: Any) -> None:
        if sender_id not in self._senders:
            raise ValueError("unknown sender")
        self._clients[sender_id] = client

    def register_room(self, room_id: str, channel_id: int) -> None:
        self._rooms[room_id] = channel_id

    def register(self, room_id: str, channel: Any) -> None:
        self.register_room(room_id, int(channel.id))

    async def deliver(self, room_id: str, agent_id: str, text: str, request_id: str) -> str:
        agent = self._agents.get(agent_id)
        if agent is None or not agent.enabled or not isinstance(text, str) or not text.strip():
            return "not_delivered"
        sender = self._senders.get(agent.sender_id)
        client = self._clients.get(agent.sender_id)
        channel_id = self._rooms.get(room_id)
        if sender is None or not sender.enabled or client is None or channel_id is None:
            return "not_delivered"
        body = f"{agent.display_name}: {text}" if agent.sender_id in self._shared else text
        if len(body) > self.message_limit:
            return "not_delivered"
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                channel = await client.fetch_channel(channel_id)
        except Exception:
            return "not_delivered"
        try:
            sent = await channel.send(body)
        except Exception:
            return "unknown"
        return "delivered" if sent is not None else "unknown"
