"""Bounded correlation for replies to managed Discord Bot messages."""

from __future__ import annotations

from dataclasses import dataclass


def system_message(text: str) -> str:
    return f"[SYSTEM] {text}"


@dataclass(frozen=True)
class HumanIngressRoute:
    target_bot_id: int | None
    allow_without_mention: bool = False
    failure: str | None = None


def resolve_human_ingress_route(
    mentioned_bot_ids: tuple[int, ...],
    *,
    has_reply: bool,
    replied_bot_id: int | None,
    default_bot_id: int,
) -> HumanIngressRoute:
    """Select a Bot identity without degrading an unresolved reply to default."""
    if len(mentioned_bot_ids) > 1:
        return HumanIngressRoute(None, failure="multiple_mentions")
    if mentioned_bot_ids:
        return HumanIngressRoute(mentioned_bot_ids[0])
    if has_reply:
        if replied_bot_id is None:
            return HumanIngressRoute(None, failure="unresolved_reply")
        return HumanIngressRoute(replied_bot_id, allow_without_mention=True)
    return HumanIngressRoute(default_bot_id, allow_without_mention=True)


class ManagedBotReplyIndex:
    def __init__(self, managed_bot_ids: set[int], maximum: int = 200) -> None:
        if maximum < 1:
            raise ValueError("maximum must be positive")
        self._managed_bot_ids = frozenset(managed_bot_ids)
        self._maximum = maximum
        self._bot_by_message: dict[int, int] = {}

    def remember(self, message_id: int, bot_user_id: int) -> None:
        if bot_user_id not in self._managed_bot_ids:
            return
        self._bot_by_message.pop(message_id, None)
        self._bot_by_message[message_id] = bot_user_id
        while len(self._bot_by_message) > self._maximum:
            self._bot_by_message.pop(next(iter(self._bot_by_message)))

    def resolve(
        self, message_id: int | None, resolved_author_id: int | None
    ) -> int | None:
        if resolved_author_id is not None:
            return (
                resolved_author_id
                if resolved_author_id in self._managed_bot_ids
                else None
            )
        if message_id is None:
            return None
        return self._bot_by_message.get(message_id)
