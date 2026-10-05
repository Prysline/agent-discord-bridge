"""Deterministic bounded-discussion state machine owned by the core.

All events passed here are assumed to have already passed Discord allowlist
checks.  Peer Discord output is observable context only; it never schedules
another agent.  Runtime adapters do not own any state represented here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Literal
import re


Phase = Literal[
    "idle", "active", "closing-check", "completed", "stopped", "suspended"
]
ResultStatus = Literal["continue", "complete", "abstain"]
DeliveryStatus = Literal["delivered", "not_delivered", "unknown"]
FailureReason = Literal[
    "timeout",
    "adapter_error",
    "execution_failure",
    "participant_unavailable",
    "discord_delivery_failure",
]

MENTION_RE = re.compile(r"^<@!?(\d+)>[ \t]*")
ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class Participant:
    agent_id: str
    mention_id: str
    budget_chars: int
    max_calls: int
    enabled: bool = True
    available: bool = True
    agent_alias: str | None = None

    def __post_init__(self) -> None:
        if not self.agent_id or not self.mention_id:
            raise ValueError("participant identity must not be empty")
        if self.budget_chars < 1 or self.max_calls < 1:
            raise ValueError("participant quota must be positive")
        alias = self.agent_alias or self.agent_id
        if not ALIAS_RE.fullmatch(alias) or alias in {"discuss", "stop"}:
            raise ValueError("participant alias is invalid")


@dataclass
class AgentDiscussionQuota:
    budget_chars: int
    max_calls: int
    used_chars: int = 0
    used_calls: int = 0

    def can_dispatch(self) -> bool:
        return self.used_chars < self.budget_chars and self.used_calls < self.max_calls


@dataclass
class DiscussionSafety:
    global_max_dispatches: int
    dispatched_calls: int = 0

    def can_dispatch(self) -> bool:
        return self.dispatched_calls < self.global_max_dispatches


@dataclass(frozen=True)
class Event:
    channel_id: str
    author_id: str
    author_is_bot: bool
    content: str


@dataclass(frozen=True)
class DispatchToken:
    discussion_id: str
    dispatch_index: int
    agent_id: str
    phase: Literal["active", "closing-check"]
    context_revision: int
    invalidation_version: int


@dataclass(frozen=True)
class Transition:
    accepted: bool
    action: str
    reason: str = ""
    agent_id: str | None = None
    token: DispatchToken | None = None


@dataclass(frozen=True)
class PendingResult:
    token: DispatchToken
    status: ResultStatus
    text: str


@dataclass
class DiscussionState:
    phase: Phase = "idle"
    discussion_id: str | None = None
    goal: str = ""
    participants: tuple[str, ...] = ()
    next_index: int = 0
    closing_remaining: list[str] = field(default_factory=list)
    quotas: dict[str, AgentDiscussionQuota] = field(default_factory=dict)
    safety: DiscussionSafety | None = None
    context_revision: int = 0
    invalidation_version: int = 0
    in_flight: DispatchToken | None = None
    invocation_started: bool = False
    pending_result: PendingResult | None = None
    last_speaker: str | None = None
    suspension_reason: str = ""


class ConversationPolicy:
    """Pure state machine for bounded discussion scheduling and accounting."""

    def __init__(
        self,
        participants: list[Participant],
        *,
        global_max_dispatches: int,
        start_command: str = "!discuss",
        stop_command: str = "!stop",
    ) -> None:
        if global_max_dispatches < 1:
            raise ValueError("global_max_dispatches must be positive")
        by_agent = {item.agent_id: item for item in participants}
        if len(by_agent) != len(participants):
            raise ValueError("participant agent_id must be unique")
        enabled_aliases = [item.agent_alias or item.agent_id for item in participants if item.enabled]
        if len(set(enabled_aliases)) != len(enabled_aliases):
            raise ValueError("enabled participant alias must be unique")
        self._participants = by_agent
        alias_groups: dict[str, list[Participant]] = {}
        for item in participants:
            alias_groups.setdefault(item.agent_alias or item.agent_id, []).append(item)
        self._aliases = {
            alias: next((item for item in values if item.enabled), values[0])
            for alias, values in alias_groups.items()
        }
        mention_groups: dict[str, list[Participant]] = {}
        for item in participants:
            mention_groups.setdefault(item.mention_id, []).append(item)
        self._mentions = {key: values[0] for key, values in mention_groups.items() if len(values) == 1}
        self.global_max_dispatches = global_max_dispatches
        self.start_command = start_command
        self.stop_command = stop_command
        self._states: dict[str, DiscussionState] = {}
        self._discussion_counter = 0

    def state(self, channel_id: str) -> DiscussionState:
        """Return a detached inspection snapshot, never the mutable live state."""
        return deepcopy(self._state(channel_id))

    def _state(self, channel_id: str) -> DiscussionState:
        return self._states.setdefault(channel_id, DiscussionState())

    def handle_event(self, event: Event, *, allow_control: bool = True) -> Transition:
        """Apply an authorized Discord event without dispatching a model."""
        state = self._state(event.channel_id)
        content = event.content.strip()

        if event.author_is_bot:
            state.context_revision += 1
            return Transition(False, "context-observed", "peer output cannot schedule AI")

        control = self.classify_control(content) if allow_control else None
        if control == "stop":
            return self._stop(state)

        if control == "start":
            if state.phase in {"active", "closing-check"}:
                return Transition(False, "rejected", "discussion active; use !stop first")
            return self._start(state, event.channel_id, content)

        state.context_revision += 1
        if state.phase == "closing-check":
            state.phase = "active"
            state.closing_remaining.clear()
            state.invalidation_version += 1
            state.in_flight = None
            state.pending_result = None
            return Transition(True, "context-intervention", "closing check cancelled")
        if state.phase == "active":
            return Transition(True, "context-intervention", "discussion remains active")
        return Transition(True, "human-context", "no active discussion")

    def classify_control(self, content: str) -> Literal["start", "stop"] | None:
        """Classify only the command boundary; validation remains in handle_event."""
        value = content.strip()
        if value == self.stop_command or value.startswith(self.stop_command + " "):
            return "stop"
        if value == self.start_command or value.startswith(self.start_command + " "):
            return "start"
        return None

    def next_dispatch(self, channel_id: str) -> Transition:
        state = self._state(channel_id)
        if state.in_flight is not None or state.pending_result is not None:
            return Transition(False, "blocked", "dispatch or delivery already pending")
        if state.phase not in {"active", "closing-check"}:
            return Transition(False, "blocked", f"discussion is {state.phase}")
        if state.safety is None or not state.safety.can_dispatch():
            return self._suspend(state, "global dispatch limit reached")

        if state.phase == "closing-check":
            if not state.closing_remaining:
                state.phase = "completed"
                return Transition(False, "completed", "closing check finished")
            agent_id = state.closing_remaining[0]
            if not self._eligible(state, agent_id):
                return self._suspend(
                    state, "closing participant quota exhausted or unavailable"
                )
            return Transition(True, "dispatch-ready", agent_id=agent_id)

        for offset in range(len(state.participants)):
            index = (state.next_index + offset) % len(state.participants)
            agent_id = state.participants[index]
            if self._eligible(state, agent_id) and agent_id != state.last_speaker:
                return Transition(True, "dispatch-ready", agent_id=agent_id)
        return self._suspend(state, "quota/no eligible next participant")

    def begin_dispatch(self, channel_id: str) -> Transition:
        ready = self.next_dispatch(channel_id)
        if not ready.accepted or ready.agent_id is None:
            return ready

        state = self._state(channel_id)
        agent_id = ready.agent_id
        assert state.safety is not None
        state.safety.dispatched_calls += 1

        token = DispatchToken(
            discussion_id=state.discussion_id or "",
            dispatch_index=state.safety.dispatched_calls,
            agent_id=agent_id,
            phase=state.phase,
            context_revision=state.context_revision,
            invalidation_version=state.invalidation_version,
        )
        state.in_flight = token
        state.invocation_started = False
        if state.phase == "active":
            state.next_index = (state.participants.index(agent_id) + 1) % len(
                state.participants
            )
        return Transition(True, "dispatch-started", agent_id=agent_id, token=token)

    def record_invocation_started(
        self, channel_id: str, token: DispatchToken
    ) -> Transition:
        """Charge one model call at the adapter's invocation-start boundary."""
        state = self._state(channel_id)
        invalid = self._validate_current_token(state, token)
        if invalid is not None:
            return invalid
        if state.invocation_started:
            return Transition(
                True, "invocation-already-recorded", agent_id=token.agent_id, token=token
            )
        state.quotas[token.agent_id].used_calls += 1
        state.invocation_started = True
        return Transition(True, "invocation-recorded", agent_id=token.agent_id, token=token)

    def record_result(
        self,
        channel_id: str,
        token: DispatchToken,
        *,
        status: ResultStatus,
        text: str = "",
    ) -> Transition:
        state = self._state(channel_id)
        invalid = self._validate_current_token(state, token)
        if invalid is not None:
            return invalid
        self._validate_result(status, text)
        if state.pending_result is not None:
            return Transition(False, "blocked", "result already pending delivery")
        result = PendingResult(token, status, text)
        if status == "abstain":
            state.in_flight = None
            state.invocation_started = False
            state.last_speaker = token.agent_id
            return self._apply_result_semantics(state, result)
        state.pending_result = result
        return Transition(True, "delivery-pending", agent_id=token.agent_id, token=token)

    def resolve_delivery(
        self,
        channel_id: str,
        token: DispatchToken,
        *,
        delivery: DeliveryStatus,
    ) -> Transition:
        state = self._state(channel_id)
        invalid = self._validate_current_token(state, token)
        if invalid is not None:
            return invalid
        pending = state.pending_result
        if pending is None or pending.token != token:
            return Transition(False, "blocked", "no matching result pending delivery")
        if delivery == "unknown":
            return Transition(False, "delivery-pending", "delivery outcome unknown")
        if delivery == "not_delivered":
            return self._suspend(state, "delivery failed / not delivered")
        if delivery != "delivered":
            raise ValueError(f"invalid delivery status: {delivery}")

        state.in_flight = None
        state.invocation_started = False
        state.pending_result = None
        if pending.text:
            state.quotas[token.agent_id].used_chars += len(pending.text)
        state.last_speaker = token.agent_id
        return self._apply_result_semantics(state, pending)

    def fail_dispatch(
        self, channel_id: str, token: DispatchToken, *, reason: FailureReason
    ) -> Transition:
        state = self._state(channel_id)
        invalid = self._validate_current_token(state, token)
        if invalid is not None:
            return invalid
        if reason not in {
            "timeout",
            "adapter_error",
            "execution_failure",
            "participant_unavailable",
            "discord_delivery_failure",
        }:
            raise ValueError(f"invalid failure reason: {reason}")
        return self._suspend(state, reason.replace("_", " "))

    def record_reconciled_delivery(
        self, channel_id: str, token: DispatchToken, *, text: str
    ) -> Transition:
        """Account a pre-invalidation send later proven delivered, without revival."""
        state = self._state(channel_id)
        if state.discussion_id != token.discussion_id or token.agent_id not in state.quotas:
            return Transition(False, "ignored", "delivery does not belong to discussion")
        state.quotas[token.agent_id].used_chars += len(text)
        return Transition(False, "delivery-reconciled", agent_id=token.agent_id, token=token)

    def _apply_result_semantics(
        self, state: DiscussionState, pending: PendingResult
    ) -> Transition:
        token = pending.token

        if token.phase == "active":
            if pending.status == "complete":
                state.phase = "closing-check"
                state.closing_remaining = self._after(
                    state.participants, token.agent_id
                )[:-1]
                if not state.closing_remaining:
                    state.phase = "completed"
                    return Transition(True, "completed", "no remaining participants")
                return Transition(True, "closing-check", agent_id=token.agent_id)
            return Transition(True, "result-recorded", agent_id=token.agent_id)

        if not state.closing_remaining or state.closing_remaining[0] != token.agent_id:
            return self._suspend(state, "closing-check state mismatch")
        state.closing_remaining.pop(0)
        state.next_index = (state.participants.index(token.agent_id) + 1) % len(
            state.participants
        )
        if pending.status == "continue":
            state.phase = "active"
            state.closing_remaining.clear()
            return Transition(True, "closing-cancelled", agent_id=token.agent_id)
        if not state.closing_remaining:
            state.phase = "completed"
            return Transition(True, "completed", "closing check finished")
        return Transition(True, "closing-recorded", agent_id=token.agent_id)

    def on_process_restart(self) -> None:
        for state in self._states.values():
            if state.phase in {"active", "closing-check"}:
                self._suspend(state, "process restarted")

    def _start(
        self, state: DiscussionState, channel_id: str, content: str
    ) -> Transition:
        parsed = self._parse_start(content)
        if isinstance(parsed, str):
            return Transition(False, "rejected", parsed)
        selectors, goal = parsed
        if len(selectors) < 2:
            return Transition(False, "rejected", "at least two participants required")

        participants: list[Participant] = []
        for selector in selectors:
            mention = MENTION_RE.fullmatch(selector)
            participant = (
                self._mentions.get(mention.group(1))
                if mention else self._aliases.get(selector)
            )
            if participant is None:
                return Transition(False, "rejected", "unknown participant")
            if not participant.enabled:
                return Transition(False, "rejected", "disabled participant")
            if not participant.available:
                return Transition(False, "rejected", "unavailable participant")
            participants.append(participant)
        if len({item.agent_id for item in participants}) != len(participants):
            return Transition(False, "rejected", "duplicate participant")

        self._discussion_counter += 1
        state.phase = "active"
        state.discussion_id = f"{channel_id}:{self._discussion_counter}"
        state.goal = goal
        state.participants = tuple(item.agent_id for item in participants)
        state.next_index = 0
        state.closing_remaining.clear()
        state.quotas = {
            item.agent_id: AgentDiscussionQuota(item.budget_chars, item.max_calls)
            for item in participants
        }
        state.safety = DiscussionSafety(self.global_max_dispatches)
        state.context_revision += 1
        state.invalidation_version += 1
        state.in_flight = None
        state.invocation_started = False
        state.pending_result = None
        state.last_speaker = None
        state.suspension_reason = ""
        return Transition(True, "started", agent_id=state.participants[0])

    def _parse_start(self, content: str) -> tuple[list[str], str] | str:
        body = content[len(self.start_command) :].lstrip(" \t")
        first_line, separator, remainder = body.partition("\n")
        first_line = first_line.rstrip("\r")
        if "--" in first_line:
            participant_text, goal_on_first = first_line.split("--", 1)
            goal = (goal_on_first + (("\n" + remainder) if separator else "")).strip()
        else:
            participant_text = first_line
            goal = remainder.strip() if separator else ""

        selectors = participant_text.split()
        if any(
            not MENTION_RE.fullmatch(selector) and not ALIAS_RE.fullmatch(selector)
            for selector in selectors
        ):
            return "participant header must contain only aliases"
        if not goal:
            return "goal must not be empty"
        return selectors, goal

    def _stop(self, state: DiscussionState) -> Transition:
        state.phase = "stopped"
        state.closing_remaining.clear()
        state.invalidation_version += 1
        state.in_flight = None
        state.invocation_started = False
        state.pending_result = None
        return Transition(True, "stopped", "discussion invalidated")

    def _suspend(self, state: DiscussionState, reason: str) -> Transition:
        state.phase = "suspended"
        state.suspension_reason = reason
        state.invalidation_version += 1
        state.in_flight = None
        state.invocation_started = False
        state.pending_result = None
        return Transition(False, "suspended", reason)

    @staticmethod
    def _validate_current_token(
        state: DiscussionState, token: DispatchToken
    ) -> Transition | None:
        if state.in_flight != token:
            if token.invalidation_version != state.invalidation_version:
                return Transition(False, "invalidated", "late result cannot change state")
            return Transition(False, "ignored", "dispatch token is not current")
        if (
            token.invalidation_version != state.invalidation_version
            or state.phase not in {"active", "closing-check"}
            or token.phase != state.phase
        ):
            return Transition(False, "invalidated", "late result cannot change state")
        return None

    def _eligible(self, state: DiscussionState, agent_id: str) -> bool:
        participant = self._participants[agent_id]
        return (
            participant.enabled
            and participant.available
            and state.quotas[agent_id].can_dispatch()
        )

    @staticmethod
    def _after(items: tuple[str, ...], item: str) -> list[str]:
        index = items.index(item)
        return list(items[index + 1 :] + items[: index + 1])

    @staticmethod
    def _validate_result(status: ResultStatus, text: str) -> None:
        if status not in {"continue", "complete", "abstain"}:
            raise ValueError(f"invalid result status: {status}")
        if status in {"continue", "complete"} and not text.strip():
            raise ValueError(f"{status} requires non-empty text")
        if status == "abstain" and text:
            raise ValueError("abstain must not include text")
