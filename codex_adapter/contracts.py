"""Validation helpers for the frozen AgentRequest / AgentResult v1 boundary."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


ERROR_CODES = {
    "adapter_unavailable",
    "binding_unavailable",
    "timeout",
    "transport_error",
    "execution_error",
    "invalid_response",
}

CAPABILITIES = {
    "sessionMode": "persistent",
    "maxInFlight": 1,
    "canCancelInFlight": True,
}


class ContractError(ValueError):
    """AgentRequest does not satisfy the frozen v1 persistent contract."""


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


def validate_persistent_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate only the persistent event-delta path; reject every other mode."""
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
        event_field = f"context.events[{index}]"
        kind = item.get("kind")
        if kind == "message":
            author = _object(item.get("author"), f"{event_field}.author")
            if author.get("type") not in {"human", "agent"}:
                raise ContractError(f"{event_field}.author.type is invalid")
            _string(author.get("id"), f"{event_field}.author.id")
            _string(author.get("displayName"), f"{event_field}.author.displayName")
            _text(item.get("text"), f"{event_field}.text")
            mentions = item.get("mentions")
            if not isinstance(mentions, list) or any(
                not isinstance(mention, str) or not mention.strip()
                for mention in mentions
            ):
                raise ContractError(f"{event_field}.mentions must be a string array")
            _optional_text(item, "replyToEventId", f"{event_field}.replyToEventId")
        elif kind == "core":
            if item.get("type") not in {
                "discussion_started",
                "discussion_resumed",
            }:
                raise ContractError(f"{event_field}.type is invalid")
            _text(item.get("text"), f"{event_field}.text")
        else:
            raise ContractError(f"{event_field}.kind is invalid")
        _optional_text(item, "timestamp", f"{event_field}.timestamp")

    constraints = _object(root.get("constraints"), "constraints")
    timeout_ms = _integer(constraints.get("timeoutMs"), "constraints.timeoutMs")
    if timeout_ms <= 0:
        raise ContractError("constraints.timeoutMs must be greater than zero")

    discussion = root.get("discussion")
    if mode == "human-turn" and discussion is not None:
        raise ContractError("discussion is only valid for bounded-discussion")
    if mode == "bounded-discussion":
        discussion_obj = _object(discussion, "discussion")
        _string(discussion_obj.get("discussionId"), "discussion.discussionId")
        _string(discussion_obj.get("goal"), "discussion.goal")
        _integer(discussion_obj.get("turnIndex"), "discussion.turnIndex")
        if discussion_obj.get("phase") not in {"active", "closing-check"}:
            raise ContractError("discussion.phase is invalid")

    return normalized


def error_result(
    request_id: str,
    context_commit: str,
    code: str,
    message: str,
    retryable: bool,
    stage: str,
) -> dict[str, Any]:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown AgentError code: {code}")
    return {
        "requestId": request_id,
        "contextCommit": context_commit,
        "status": "error",
        "error": {"code": code, "message": message, "retryable": retryable},
        "diagnostics": {"adapter": "codex", "stage": stage},
    }


def continue_result(request_id: str, text: str) -> dict[str, Any]:
    if not isinstance(text, str) or not text.strip():
        raise ContractError("continue result requires non-empty text")
    return {
        "requestId": request_id,
        "contextCommit": "committed",
        "status": "continue",
        "text": text.strip(),
        "diagnostics": {"adapter": "codex", "stage": "completed"},
    }
