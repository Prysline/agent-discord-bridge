"""Shared AgentRequest / AgentResult v1 validation."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal, Mapping, Protocol


ERROR_CODES = {
    "adapter_unavailable",
    "binding_unavailable",
    "timeout",
    "transport_error",
    "execution_error",
    "invalid_response",
}
CONTEXT_COMMITS = {"committed", "not_committed", "unknown"}
InvocationCertainty = Literal["confirmed", "ambiguous"]


class InvocationObserver(Protocol):
    """Reports the actual or conservatively ambiguous model-start boundary."""

    async def on_invocation_started(
        self, request_id: str, certainty: InvocationCertainty
    ) -> None: ...


class ContractError(ValueError):
    """A shared request or result violates the frozen v1 contract."""


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field} must be an integer")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{field} must be a string")
    return value


def _optional_text(container: Mapping[str, Any], key: str, field: str) -> None:
    if key in container:
        _string(container[key], field)


def validate_agent_request(request: Mapping[str, Any]) -> dict[str, Any]:
    root = _object(request, "request")
    normalized = deepcopy(dict(root))

    _string(root.get("requestId"), "requestId")
    _string(root.get("agentId"), "agentId")
    mode = root.get("mode")
    if mode not in {"human-turn", "bounded-discussion"}:
        raise ContractError("mode is not supported by AgentRequest v1")

    binding = _object(root.get("binding"), "binding")
    _string(binding.get("bindingId"), "binding.bindingId")
    _integer(binding.get("generation"), "binding.generation")

    context = _object(root.get("context"), "context")
    if context.get("kind") != "event-delta":
        raise ContractError("persistent adapter only accepts context.kind=event-delta")
    events = context.get("events")
    if not isinstance(events, list):
        raise ContractError("context.events must be an array")
    cursor = _object(context.get("cursor"), "context.cursor")
    from_seq = _integer(cursor.get("fromSeqExclusive"), "context.cursor.fromSeqExclusive")
    through_seq = _integer(cursor.get("throughSeqInclusive"), "context.cursor.throughSeqInclusive")
    if through_seq < from_seq:
        raise ContractError("context cursor moves backwards")

    previous_seq: int | None = None
    for index, event in enumerate(events):
        item = _object(event, f"context.events[{index}]")
        _string(item.get("eventId"), f"context.events[{index}].eventId")
        seq = _integer(item.get("seq"), f"context.events[{index}].seq")
        if not from_seq < seq <= through_seq:
            raise ContractError(f"context.events[{index}].seq is outside the cursor range")
        if previous_seq is not None and seq <= previous_seq:
            raise ContractError("context.events must be strictly ordered by seq")
        previous_seq = seq
        field = f"context.events[{index}]"
        if item.get("kind") == "message":
            author = _object(item.get("author"), f"{field}.author")
            if author.get("type") not in {"human", "agent"}:
                raise ContractError(f"{field}.author.type is invalid")
            _string(author.get("id"), f"{field}.author.id")
            _string(author.get("displayName"), f"{field}.author.displayName")
            _text(item.get("text"), f"{field}.text")
            mentions = item.get("mentions")
            if not isinstance(mentions, list) or any(
                not isinstance(value, str) or not value.strip() for value in mentions
            ):
                raise ContractError(f"{field}.mentions must be a string array")
            _optional_text(item, "replyToEventId", f"{field}.replyToEventId")
        elif item.get("kind") == "core":
            if item.get("type") not in {"discussion_started", "discussion_resumed"}:
                raise ContractError(f"{field}.type is invalid")
            _text(item.get("text"), f"{field}.text")
        else:
            raise ContractError(f"{field}.kind is invalid")
        _optional_text(item, "timestamp", f"{field}.timestamp")

    constraints = _object(root.get("constraints"), "constraints")
    if _integer(constraints.get("timeoutMs"), "constraints.timeoutMs") <= 0:
        raise ContractError("constraints.timeoutMs must be greater than zero")

    discussion = root.get("discussion")
    if mode == "human-turn" and discussion is not None:
        raise ContractError("discussion is only valid for bounded-discussion")
    if mode == "bounded-discussion":
        value = _object(discussion, "discussion")
        _string(value.get("discussionId"), "discussion.discussionId")
        _string(value.get("goal"), "discussion.goal")
        _integer(value.get("turnIndex"), "discussion.turnIndex")
        if value.get("phase") not in {"active", "closing-check"}:
            raise ContractError("discussion.phase is invalid")
    return normalized


def validate_agent_result(
    result: Mapping[str, Any], *, request_id: str, mode: str
) -> dict[str, Any]:
    root = _object(result, "result")
    normalized = deepcopy(dict(root))
    if root.get("requestId") != request_id:
        raise ContractError("result requestId does not match request")
    if root.get("contextCommit") not in CONTEXT_COMMITS:
        raise ContractError("result contextCommit is invalid")
    status = root.get("status")
    if status not in {"continue", "complete", "abstain", "await-human", "error"}:
        raise ContractError("result status is invalid")

    text = root.get("text")
    if status == "continue":
        _string(text, "result.text")
    elif status in {"complete", "await-human"}:
        if mode != "bounded-discussion":
            raise ContractError(f"{status} is only valid for bounded-discussion")
        _string(text, "result.text")
    elif status == "abstain":
        if mode != "bounded-discussion":
            raise ContractError("abstain is only valid for bounded-discussion")
        if "text" in root:
            raise ContractError("abstain must not include text")
    else:
        if "text" in root:
            raise ContractError("error must not include text")
        error = _object(root.get("error"), "result.error")
        if error.get("code") not in ERROR_CODES:
            raise ContractError("result.error.code is invalid")
        _string(error.get("message"), "result.error.message")
        if not isinstance(error.get("retryable"), bool):
            raise ContractError("result.error.retryable must be a boolean")
    return normalized
