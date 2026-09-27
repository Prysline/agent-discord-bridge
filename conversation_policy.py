"""Pure routing policy for a private Discord room."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MessageEnvelope:
    author_id: str
    author_is_bot: bool
    bot_user_id: str
    channel_id: str
    is_dm: bool
    mentions_bot: bool
    replies_to_bot: bool


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    is_peer: bool = False
    reason: str = ""


def validate_config(config: dict) -> None:
    """Fail closed when an allowlist is missing or malformed."""
    if config.get("dmPolicy", "disabled") not in {"disabled", "allowlist"}:
        raise ValueError("dmPolicy must be 'disabled' or 'allowlist'")

    allow_from = config.get("allowFrom", [])
    if not isinstance(allow_from, list):
        raise ValueError("allowFrom must be a list")

    channels = config.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ValueError("channels must contain at least one explicit channel")

    for channel_id, policy in channels.items():
        if not str(channel_id).isdigit():
            raise ValueError(f"channel id must be numeric: {channel_id}")
        if not isinstance(policy, dict):
            raise ValueError(f"channel policy must be an object: {channel_id}")
        human_ids = policy.get("allowFrom")
        if not isinstance(human_ids, list) or not human_ids:
            raise ValueError(f"channel {channel_id} requires a non-empty allowFrom")
        peer_ids = policy.get("allowBotFrom", [])
        if not isinstance(peer_ids, list):
            raise ValueError(f"channel {channel_id} allowBotFrom must be a list")
        if policy.get("allowBotMention", False) and not peer_ids:
            raise ValueError(
                f"channel {channel_id} enables bot mentions but has no allowBotFrom"
            )


def decide_access(config: dict, message: MessageEnvelope) -> AccessDecision:
    if message.author_id == message.bot_user_id:
        return AccessDecision(False, reason="self")

    if message.is_dm:
        if message.author_is_bot:
            return AccessDecision(False, reason="bot dm")
        if config.get("dmPolicy", "disabled") != "allowlist":
            return AccessDecision(False, reason="dm disabled")
        if message.author_id not in {str(value) for value in config.get("allowFrom", [])}:
            return AccessDecision(False, reason="dm author not allowed")
        return AccessDecision(True, reason="allowed dm")

    channel_policy = config.get("channels", {}).get(message.channel_id)
    if not channel_policy:
        return AccessDecision(False, reason="channel not allowed")

    if message.author_is_bot:
        allowed_peers = {str(value) for value in channel_policy.get("allowBotFrom", [])}
        if not channel_policy.get("allowBotMention", False):
            return AccessDecision(False, reason="bot interaction disabled")
        if message.author_id not in allowed_peers:
            return AccessDecision(False, reason="peer bot not allowed")
        if not message.mentions_bot:
            return AccessDecision(False, reason="peer did not mention bot")
        return AccessDecision(True, is_peer=True, reason="allowed peer mention")

    allowed_humans = {str(value) for value in channel_policy.get("allowFrom", [])}
    if message.author_id not in allowed_humans:
        return AccessDecision(False, reason="human not allowed")

    if channel_policy.get("requireMention", True):
        if not (message.mentions_bot or message.replies_to_bot):
            return AccessDecision(False, reason="mention required")

    return AccessDecision(True, reason="allowed human message")


class PeerTurnLimiter:
    """Bound autonomous bot-to-bot exchanges until an allowed human speaks."""

    def __init__(self, max_peer_turns: int):
        if max_peer_turns < 0:
            raise ValueError("max_peer_turns must be >= 0")
        self.max_peer_turns = max_peer_turns
        self._counts: dict[str, int] = {}

    def note_human(self, channel_id: str) -> None:
        self._counts[channel_id] = 0

    def allow_peer(self, channel_id: str) -> bool:
        current = self._counts.get(channel_id, 0)
        if current >= self.max_peer_turns:
            return False
        self._counts[channel_id] = current + 1
        return True
