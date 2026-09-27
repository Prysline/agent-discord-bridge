"""Deterministic routing and bounded peer-discussion policy."""

from dataclasses import dataclass, field
import re


VALID_MODES = {"human-turn", "bounded-discussion"}
MENTION_RE = re.compile(r"<@!?\d+>")


def validate_config(config: dict) -> None:
    if config.get("dmPolicy", "disabled") not in {"disabled", "allowlist"}:
        raise ValueError("dmPolicy must be 'disabled' or 'allowlist'")
    if not isinstance(config.get("allowFrom", []), list):
        raise ValueError("allowFrom must be a list")

    channels = config.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ValueError("channels must contain at least one explicit channel")
    for channel_id, policy in channels.items():
        if not str(channel_id).isdigit():
            raise ValueError(f"channel id must be numeric: {channel_id}")
        if not isinstance(policy, dict):
            raise ValueError(f"channel policy must be an object: {channel_id}")
        humans = policy.get("allowFrom")
        if not isinstance(humans, list) or not humans:
            raise ValueError(f"channel {channel_id} requires a non-empty allowFrom")
        mode = policy.get("mode", config.get("conversation", {}).get("defaultMode"))
        if mode not in VALID_MODES:
            raise ValueError(f"channel {channel_id} has invalid mode: {mode}")
        peers = policy.get("allowBotFrom", [])
        if not isinstance(peers, list):
            raise ValueError(f"channel {channel_id} allowBotFrom must be a list")
        if mode == "bounded-discussion" and not peers:
            raise ValueError(f"channel {channel_id} discussion mode requires allowBotFrom")


@dataclass(frozen=True)
class Event:
    channel_id: str
    author_id: str
    author_is_bot: bool
    bot_user_id: str
    is_dm: bool
    content: str
    mention_ids: tuple[str, ...]
    replies_to_bot: bool = False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    is_peer: bool = False
    discussion_active: bool = False
    reason: str = ""


@dataclass
class DiscussionState:
    active: bool = False
    turns: dict[str, int] = field(default_factory=dict)
    total_characters: int = 0


class ConversationPolicy:
    def __init__(self, config: dict):
        validate_config(config)
        self.config = config
        discussion = config.get("conversation", {}).get("discussion", {})
        self.start_command = str(discussion.get("startCommand", "!discuss"))
        self.stop_command = str(discussion.get("stopCommand", "!stop"))
        self.max_turns = int(discussion.get("maxTurnsPerBot", 5))
        self.max_characters = int(discussion.get("maxTotalCharacters", 2000))
        if self.max_turns < 1 or self.max_characters < 1:
            raise ValueError("discussion limits must be positive")
        self._states: dict[str, DiscussionState] = {}

    def state(self, channel_id: str) -> DiscussionState:
        return self._states.setdefault(channel_id, DiscussionState())

    def _channel_policy(self, event: Event) -> dict | None:
        return self.config.get("channels", {}).get(event.channel_id)

    def _mode(self, policy: dict) -> str:
        return policy.get("mode", self.config.get("conversation", {}).get("defaultMode"))

    def observe_and_decide(self, event: Event) -> Decision:
        if event.author_id == event.bot_user_id:
            self._observe_bot(event)
            return Decision(False, reason="self")

        if event.is_dm:
            if event.author_is_bot:
                return Decision(False, reason="bot dm")
            allowed = {str(value) for value in self.config.get("allowFrom", [])}
            if self.config.get("dmPolicy") != "allowlist" or event.author_id not in allowed:
                return Decision(False, reason="dm not allowed")
            return Decision(True, reason="allowed dm")

        policy = self._channel_policy(event)
        if not policy:
            return Decision(False, reason="channel not allowed")
        mode = self._mode(policy)
        state = self.state(event.channel_id)

        if event.author_is_bot:
            allowed_peers = {str(value) for value in policy.get("allowBotFrom", [])}
            if mode != "bounded-discussion" or event.author_id not in allowed_peers:
                return Decision(False, reason="peer not allowed")
            self._observe_bot(event)
            if not state.active:
                return Decision(False, reason="no active discussion")
            if event.bot_user_id not in event.mention_ids:
                return Decision(False, reason="peer did not mention bot")
            if self._limit_reached(state):
                state.active = False
                return Decision(False, reason="discussion limit reached")
            return Decision(True, is_peer=True, discussion_active=True, reason="peer turn")

        allowed_humans = {str(value) for value in policy.get("allowFrom", [])}
        if event.author_id not in allowed_humans:
            return Decision(False, reason="human not allowed")

        content = event.content.strip()
        if mode == "bounded-discussion" and content.startswith(self.stop_command):
            state.active = False
            return Decision(False, reason="discussion stopped")
        if mode == "bounded-discussion" and content.startswith(self.start_command):
            state.active = True
            state.turns.clear()
            state.total_characters = 0
            first_mention = event.mention_ids[0] if event.mention_ids else None
            return Decision(
                first_mention == event.bot_user_id,
                discussion_active=True,
                reason="discussion started" if first_mention == event.bot_user_id else "waiting for first bot",
            )

        state.active = False
        mentioned = event.bot_user_id in event.mention_ids
        if policy.get("requireMention", True) and not (mentioned or event.replies_to_bot):
            return Decision(False, reason="mention required")
        return Decision(True, reason="human turn")

    def _observe_bot(self, event: Event) -> None:
        state = self.state(event.channel_id)
        if not state.active:
            return
        state.turns[event.author_id] = state.turns.get(event.author_id, 0) + 1
        state.total_characters += len(MENTION_RE.sub("", event.content).strip())

    def _limit_reached(self, state: DiscussionState) -> bool:
        return (
            state.total_characters >= self.max_characters
            or any(turns >= self.max_turns for turns in state.turns.values())
        )
