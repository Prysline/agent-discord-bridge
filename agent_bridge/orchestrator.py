"""Memory-only shared-core orchestration boundaries.

Discord authorization and physical delivery stay outside this module.  Inputs
to this core are already authorized; adapters receive only frozen requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Protocol
from uuid import uuid4

from .contracts import (
    InvocationCertainty,
    InvocationObserver,
    validate_agent_request,
    validate_agent_result,
)
from .conversation_policy import ConversationPolicy, DispatchToken, Event


DeliveryStatus = Literal["delivered", "not_delivered", "unknown"]


class AgentAdapter(Protocol):
    capabilities: Mapping[str, Any]

    async def execute(
        self, request: Mapping[str, Any], *, observer: InvocationObserver
    ) -> dict[str, Any]: ...

    async def cancel(self, request_id: str) -> Any: ...


class DeliveryPort(Protocol):
    async def deliver(
        self, room_id: str, agent_id: str, text: str, request_id: str
    ) -> DeliveryStatus: ...


@dataclass(frozen=True)
class BindingSnapshot:
    binding_id: str
    generation: int


@dataclass(frozen=True)
class CanonicalEvent:
    event_id: str
    seq: int
    kind: Literal["message", "core"]
    text: str
    author_type: Literal["human", "agent"] | None = None
    author_id: str | None = None
    author_display_name: str | None = None
    mentions: tuple[str, ...] = ()
    reply_to_event_id: str | None = None
    core_type: Literal["discussion_started", "discussion_resumed"] | None = None
    timestamp: str | None = None

    def as_contract_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "eventId": self.event_id,
            "seq": self.seq,
            "kind": self.kind,
            "text": self.text,
        }
        if self.kind == "message":
            value["author"] = {
                "type": self.author_type,
                "id": self.author_id,
                "displayName": self.author_display_name,
            }
            value["mentions"] = list(self.mentions)
            if self.reply_to_event_id is not None:
                value["replyToEventId"] = self.reply_to_event_id
        else:
            value["type"] = self.core_type
        if self.timestamp is not None:
            value["timestamp"] = self.timestamp
        return value


@dataclass
class CursorState:
    generation: int
    last_canonical_synced_seq: int = 0
    native_known_seqs: set[int] = field(default_factory=set)
    pending_request_id: str | None = None
    pending_from_seq_exclusive: int | None = None
    pending_through_seq_inclusive: int | None = None
    uncertain: bool = False


@dataclass(frozen=True)
class CoreOutcome:
    action: str
    request_id: str | None = None
    agent_id: str | None = None
    reason: str = ""


@dataclass
class _PendingDelivery:
    room_id: str
    agent_id: str
    request_id: str
    mode: str
    result: dict[str, Any]
    token: DispatchToken | None
    binding: BindingSnapshot
    send_attempted: bool = False
    lifecycle_invalidated: bool = False


@dataclass
class _InvocationTracker:
    policy: ConversationPolicy
    room_id: str
    request_id: str
    token: DispatchToken | None
    notified: bool = False

    async def on_invocation_started(
        self, request_id: str, certainty: InvocationCertainty
    ) -> None:
        if request_id != self.request_id:
            raise ValueError("invocation signal requestId mismatch")
        if certainty not in {"confirmed", "ambiguous"}:
            raise ValueError("invalid invocation certainty")
        self.notified = True
        if self.token is not None:
            self.policy.record_invocation_started(self.room_id, self.token)


class CanonicalLog:
    def __init__(self, event_id_factory: Callable[[], str] | None = None) -> None:
        self._event_id_factory = event_id_factory or (lambda: uuid4().hex)
        self._events: dict[str, list[CanonicalEvent]] = {}

    def events(self, room_id: str) -> tuple[CanonicalEvent, ...]:
        return tuple(self._events.get(room_id, ()))

    def high_watermark(self, room_id: str) -> int:
        return len(self._events.get(room_id, ()))

    def append_message(
        self,
        room_id: str,
        *,
        author_type: Literal["human", "agent"],
        author_id: str,
        display_name: str,
        text: str,
        mentions: tuple[str, ...] = (),
        reply_to_event_id: str | None = None,
        timestamp: str | None = None,
    ) -> CanonicalEvent:
        return self._append(
            room_id,
            CanonicalEvent(
                self._event_id_factory(),
                self.high_watermark(room_id) + 1,
                "message",
                text,
                author_type,
                author_id,
                display_name,
                mentions,
                reply_to_event_id,
                timestamp=timestamp,
            ),
        )

    def append_core(
        self,
        room_id: str,
        *,
        core_type: Literal["discussion_started", "discussion_resumed"],
        text: str,
        timestamp: str | None = None,
    ) -> CanonicalEvent:
        return self._append(
            room_id,
            CanonicalEvent(
                self._event_id_factory(),
                self.high_watermark(room_id) + 1,
                "core",
                text,
                core_type=core_type,
                timestamp=timestamp,
            ),
        )

    def _append(self, room_id: str, event: CanonicalEvent) -> CanonicalEvent:
        events = self._events.setdefault(room_id, [])
        if event.seq != len(events) + 1:
            raise RuntimeError("canonical seq must be strictly contiguous")
        events.append(event)
        return event


class SharedOrchestrator:
    """Coordinates canonical context, policy, adapters, and delivery."""

    def __init__(
        self,
        *,
        policy: ConversationPolicy,
        adapters: Mapping[str, AgentAdapter],
        bindings: Mapping[tuple[str, str], BindingSnapshot],
        delivery: DeliveryPort,
        timeout_ms: int = 120_000,
        display_names: Mapping[str, str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        request_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.policy = policy
        self.adapters = dict(adapters)
        self.bindings = dict(bindings)
        self.delivery = delivery
        self.timeout_ms = timeout_ms
        self.display_names = dict(display_names or {})
        self.log = CanonicalLog(event_id_factory)
        self._request_id_factory = request_id_factory or (lambda: uuid4().hex)
        self._cursors: dict[tuple[str, str], CursorState] = {}
        self._pending_delivery: dict[tuple[str, str], _PendingDelivery] = {}
        self._discussion_requests: dict[str, tuple[str, str, DispatchToken]] = {}
        self._discussion_bindings: dict[str, dict[str, BindingSnapshot]] = {}

    def cursor(self, room_id: str, agent_id: str) -> CursorState | None:
        value = self._cursors.get((room_id, agent_id))
        if value is None:
            return None
        return CursorState(
            value.generation,
            value.last_canonical_synced_seq,
            set(value.native_known_seqs),
            value.pending_request_id,
            value.pending_from_seq_exclusive,
            value.pending_through_seq_inclusive,
            value.uncertain,
        )

    def ingest_human(
        self,
        room_id: str,
        *,
        author_id: str,
        display_name: str,
        text: str,
        mentioned_agents: tuple[str, ...] = (),
        reply_to_event_id: str | None = None,
        timestamp: str | None = None,
    ) -> CoreOutcome:
        control = self.policy.classify_control(text)
        if control == "start" and self._has_unresolved_delivery(room_id):
            return CoreOutcome("rejected", reason="delivery pending")
        if control is None:
            self.log.append_message(
                room_id,
                author_type="human",
                author_id=author_id,
                display_name=display_name,
                text=text,
                mentions=mentioned_agents,
                reply_to_event_id=reply_to_event_id,
                timestamp=timestamp,
            )
        transition = self.policy.handle_event(Event(room_id, author_id, False, text))
        if transition.action == "started":
            state = self.policy.state(room_id)
            snapshots = {
                agent_id: binding
                for agent_id in state.participants
                if (binding := self.bindings.get((room_id, agent_id))) is not None
            }
            if len(snapshots) != len(state.participants) or any(
                agent_id not in self.adapters for agent_id in state.participants
            ):
                self.policy.handle_event(Event(room_id, author_id, False, "!stop"))
                self._discussion_bindings.pop(room_id, None)
                return CoreOutcome(
                    "binding-unavailable", reason="participant binding unavailable"
                )
            self._discussion_bindings[room_id] = snapshots
            self.log.append_core(
                room_id, core_type="discussion_started", text=state.goal
            )
        if transition.action == "stopped":
            self._invalidate_room(room_id)
        return CoreOutcome(transition.action, reason=transition.reason)

    def observe_agent_message(
        self, room_id: str, *, agent_id: str, display_name: str, text: str
    ) -> CanonicalEvent:
        """Observe an already-authorized peer output without scheduling AI."""
        event = self.log.append_message(
            room_id,
            author_type="agent",
            author_id=agent_id,
            display_name=display_name,
            text=text,
        )
        self.policy.handle_event(Event(room_id, agent_id, True, text))
        return event

    async def run_human_turn(
        self, room_id: str, mentioned_agents: tuple[str, ...]
    ) -> list[CoreOutcome]:
        outcomes: list[CoreOutcome] = []
        seen: set[str] = set()
        for agent_id in mentioned_agents:
            if agent_id in seen:
                continue
            seen.add(agent_id)
            outcome = await self._execute(room_id, agent_id, "human-turn", None)
            outcomes.append(outcome)
            if outcome.action in {"context-unknown", "delivery-unknown"}:
                break
        return outcomes

    async def run_discussion_turn(self, room_id: str) -> CoreOutcome:
        if self._has_unresolved_delivery(room_id):
            return CoreOutcome("blocked", reason="delivery pending")
        transition = self.policy.begin_dispatch(room_id)
        if not transition.accepted or transition.agent_id is None or transition.token is None:
            return CoreOutcome(transition.action, reason=transition.reason)
        return await self._execute(
            room_id, transition.agent_id, "bounded-discussion", transition.token
        )

    async def resolve_pending_delivery(
        self, room_id: str, agent_id: str, delivery: DeliveryStatus
    ) -> CoreOutcome:
        pending = self._pending_delivery.get((room_id, agent_id))
        if pending is None:
            return CoreOutcome("ignored", agent_id=agent_id, reason="no pending delivery")
        if delivery == "unknown":
            return CoreOutcome("delivery-unknown", pending.request_id, agent_id)
        if delivery == "not_delivered":
            self._pending_delivery.pop((room_id, agent_id), None)
            self._discussion_requests.pop(pending.request_id, None)
            if pending.token is not None and not pending.lifecycle_invalidated:
                self.policy.resolve_delivery(room_id, pending.token, delivery="not_delivered")
            if pending.lifecycle_invalidated:
                cursor = self._cursors.get((room_id, agent_id))
                if cursor is not None:
                    cursor.uncertain = False
            action = "stopped" if pending.lifecycle_invalidated else "suspended"
            return CoreOutcome(action, pending.request_id, agent_id, "delivery failed")
        return self._commit_delivery(pending)

    async def stop(self, room_id: str, author_id: str = "human") -> CoreOutcome:
        outcome = self.ingest_human(
            room_id, author_id=author_id, display_name=author_id, text="!stop"
        )
        for request_id, (request_room, agent_id, _token) in list(
            self._discussion_requests.items()
        ):
            if request_room != room_id:
                continue
            adapter = self.adapters.get(agent_id)
            if adapter is not None and adapter.capabilities.get("canCancelInFlight") is True:
                try:
                    await adapter.cancel(request_id)
                except Exception:
                    pass
            self._discussion_requests.pop(request_id, None)
        return outcome

    def on_process_restart(self) -> None:
        self.policy.on_process_restart()
        for pending in self._pending_delivery.values():
            cursor = self._cursors.get((pending.room_id, pending.agent_id))
            if cursor is not None:
                cursor.uncertain = True
        self._pending_delivery.clear()
        self._discussion_requests.clear()
        for cursor in self._cursors.values():
            if cursor.pending_request_id is not None:
                cursor.uncertain = True

    async def _execute(
        self,
        room_id: str,
        agent_id: str,
        mode: str,
        token: DispatchToken | None,
    ) -> CoreOutcome:
        adapter = self.adapters.get(agent_id)
        if mode == "bounded-discussion":
            binding = self._discussion_bindings.get(room_id, {}).get(agent_id)
        else:
            binding = self.bindings.get((room_id, agent_id))
        if adapter is None or binding is None:
            if token is not None:
                self.policy.fail_dispatch(room_id, token, reason="participant_unavailable")
            return CoreOutcome("binding-unavailable", agent_id=agent_id)
        if self._has_unresolved_delivery(room_id):
            return CoreOutcome("blocked", agent_id=agent_id, reason="delivery pending")

        cursor = self._cursor_for(room_id, agent_id, binding)
        if cursor.pending_request_id is not None or cursor.uncertain:
            return CoreOutcome("blocked", agent_id=agent_id, reason="context fence active")
        request_id = self._request_id_factory()
        request = self._build_request(
            room_id, agent_id, mode, binding, cursor, request_id, token
        )
        try:
            validate_agent_request(request)
        except Exception as exc:
            if token is not None:
                self.policy.fail_dispatch(room_id, token, reason="adapter_error")
            return CoreOutcome("invalid-response", request_id, agent_id, type(exc).__name__)
        cursor.pending_request_id = request_id
        cursor.pending_from_seq_exclusive = request["context"]["cursor"]["fromSeqExclusive"]
        cursor.pending_through_seq_inclusive = request["context"]["cursor"]["throughSeqInclusive"]
        if token is not None:
            self._discussion_requests[request_id] = (room_id, agent_id, token)

        observer = _InvocationTracker(self.policy, room_id, request_id, token)
        try:
            raw_result = await adapter.execute(request, observer=observer)
            result = validate_agent_result(raw_result, request_id=request_id, mode=mode)
        except Exception as exc:
            cursor.uncertain = True
            self._discussion_requests.pop(request_id, None)
            if token is not None:
                self.policy.fail_dispatch(room_id, token, reason="adapter_error")
            return CoreOutcome("invalid-response", request_id, agent_id, type(exc).__name__)

        if token is not None and not observer.notified and (
            result["status"] != "error" or result["contextCommit"] != "not_committed"
        ):
            cursor.uncertain = result["contextCommit"] == "unknown"
            self._discussion_requests.pop(request_id, None)
            self.policy.fail_dispatch(room_id, token, reason="adapter_error")
            return CoreOutcome(
                "invalid-response", request_id, agent_id, "missing invocation lifecycle signal"
            )

        context_commit = result["contextCommit"]
        if context_commit == "unknown":
            cursor.uncertain = True
            return CoreOutcome("context-unknown", request_id, agent_id)
        if context_commit == "not_committed":
            self._clear_context_pending(cursor)
            self._discussion_requests.pop(request_id, None)
            if token is not None:
                self.policy.fail_dispatch(room_id, token, reason="execution_failure")
            return CoreOutcome("not-committed", request_id, agent_id)
        cursor.last_canonical_synced_seq = cursor.pending_through_seq_inclusive or 0
        cursor.native_known_seqs = {
            seq for seq in cursor.native_known_seqs if seq > cursor.last_canonical_synced_seq
        }
        self._clear_context_pending(cursor)

        if result["status"] == "error":
            self._discussion_requests.pop(request_id, None)
            if token is not None:
                self.policy.fail_dispatch(room_id, token, reason="adapter_error")
            return CoreOutcome("adapter-error", request_id, agent_id)
        if token is not None:
            recorded = self.policy.record_result(
                room_id,
                token,
                status=result["status"],
                text=result.get("text", ""),
            )
            if not recorded.accepted:
                return CoreOutcome(recorded.action, request_id, agent_id, recorded.reason)
        if result["status"] == "abstain":
            self._discussion_requests.pop(request_id, None)
            return CoreOutcome("abstained", request_id, agent_id)

        pending = _PendingDelivery(
            room_id, agent_id, request_id, mode, result, token, binding
        )
        self._pending_delivery[(room_id, agent_id)] = pending
        pending.send_attempted = True
        delivery = await self.delivery.deliver(
            room_id, agent_id, result["text"], request_id
        )
        if delivery == "unknown":
            return CoreOutcome("delivery-unknown", request_id, agent_id)
        if delivery == "not_delivered":
            self._pending_delivery.pop((room_id, agent_id), None)
            self._discussion_requests.pop(request_id, None)
            if token is not None and not pending.lifecycle_invalidated:
                self.policy.resolve_delivery(room_id, token, delivery="not_delivered")
            if pending.lifecycle_invalidated:
                cursor.uncertain = False
            action = "stopped" if pending.lifecycle_invalidated else "suspended"
            return CoreOutcome(action, request_id, agent_id, "delivery failed")
        return self._commit_delivery(pending)

    def _commit_delivery(self, pending: _PendingDelivery) -> CoreOutcome:
        key = (pending.room_id, pending.agent_id)
        if self._pending_delivery.get(key) is not pending:
            return CoreOutcome("ignored", pending.request_id, pending.agent_id)
        event = self.log.append_message(
            pending.room_id,
            author_type="agent",
            author_id=pending.agent_id,
            display_name=self.display_names.get(pending.agent_id, pending.agent_id),
            text=pending.result["text"],
        )
        cursor = self._cursor_for(pending.room_id, pending.agent_id, pending.binding)
        cursor.native_known_seqs.add(event.seq)
        if pending.lifecycle_invalidated:
            cursor.uncertain = False
        self._pending_delivery.pop(key, None)
        self._discussion_requests.pop(pending.request_id, None)
        if pending.token is not None:
            if pending.lifecycle_invalidated:
                self.policy.record_reconciled_delivery(
                    pending.room_id, pending.token, text=pending.result["text"]
                )
                return CoreOutcome(
                    "delivery-reconciled",
                    pending.request_id,
                    pending.agent_id,
                    "stopped lifecycle remains invalidated",
                )
            transition = self.policy.resolve_delivery(
                pending.room_id, pending.token, delivery="delivered"
            )
            return CoreOutcome(
                transition.action,
                pending.request_id,
                pending.agent_id,
                transition.reason,
            )
        return CoreOutcome("delivered", pending.request_id, pending.agent_id)

    def _build_request(
        self,
        room_id: str,
        agent_id: str,
        mode: str,
        binding: BindingSnapshot,
        cursor: CursorState,
        request_id: str,
        token: DispatchToken | None,
    ) -> dict[str, Any]:
        through = self.log.high_watermark(room_id)
        events = [
            event.as_contract_dict()
            for event in self.log.events(room_id)
            if cursor.last_canonical_synced_seq < event.seq <= through
            and event.seq not in cursor.native_known_seqs
        ]
        request: dict[str, Any] = {
            "requestId": request_id,
            "agentId": agent_id,
            "mode": mode,
            "binding": {
                "bindingId": binding.binding_id,
                "generation": binding.generation,
            },
            "context": {
                "kind": "event-delta",
                "events": events,
                "cursor": {
                    "fromSeqExclusive": cursor.last_canonical_synced_seq,
                    "throughSeqInclusive": through,
                },
            },
            "constraints": {"timeoutMs": self.timeout_ms},
        }
        if token is not None:
            state = self.policy.state(room_id)
            request["discussion"] = {
                "discussionId": token.discussion_id,
                "goal": state.goal,
                "turnIndex": token.dispatch_index,
                "phase": token.phase,
            }
        return request

    def _cursor_for(
        self, room_id: str, agent_id: str, binding: BindingSnapshot
    ) -> CursorState:
        key = (room_id, agent_id)
        cursor = self._cursors.get(key)
        if cursor is None or cursor.generation != binding.generation:
            cursor = CursorState(binding.generation)
            self._cursors[key] = cursor
        return cursor

    @staticmethod
    def _clear_context_pending(cursor: CursorState) -> None:
        cursor.pending_request_id = None
        cursor.pending_from_seq_exclusive = None
        cursor.pending_through_seq_inclusive = None

    def _invalidate_room(self, room_id: str) -> None:
        for key in [key for key in self._pending_delivery if key[0] == room_id]:
            cursor = self._cursors.get(key)
            if cursor is not None:
                cursor.uncertain = True
            pending = self._pending_delivery[key]
            if pending.send_attempted:
                pending.lifecycle_invalidated = True
            else:
                self._pending_delivery.pop(key, None)

    def _has_unresolved_delivery(self, room_id: str) -> bool:
        return any(key[0] == room_id for key in self._pending_delivery)

