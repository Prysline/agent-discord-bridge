"""Validation helpers for the frozen AgentRequest / AgentResult v1 boundary."""

from __future__ import annotations

import json
from typing import Any, Mapping

from agent_bridge.contracts import (
    ContractError,
    ERROR_CODES,
    validate_agent_request,
)


CAPABILITIES = {
    "sessionMode": "persistent",
    "maxInFlight": 1,
    "canCancelInFlight": True,
}


def validate_persistent_request(request: Mapping[str, Any]) -> dict[str, Any]:
    """Validate only the persistent event-delta path; reject every other mode."""
    return validate_agent_request(request)


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


def final_text_result(
    request_id: str, mode: str, text: str, *, adapter: str
) -> dict[str, Any]:
    """Convert a native final into the frozen AgentResult status contract."""
    if mode == "human-turn":
        return continue_result(request_id, text)
    if mode != "bounded-discussion":
        raise ContractError("result mode is unsupported")
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError("bounded discussion final must be one JSON object") from exc
    if not isinstance(value, dict) or set(value) - {"status", "text"}:
        raise ContractError("bounded discussion final has invalid fields")
    status = value.get("status")
    body = value.get("text")
    if status in {"continue", "complete", "await-human"}:
        if not isinstance(body, str) or not body.strip():
            raise ContractError(f"{status} result requires non-empty text")
        return {
            "requestId": request_id,
            "contextCommit": "committed",
            "status": status,
            "text": body.strip(),
            "diagnostics": {"adapter": adapter, "stage": "completed"},
        }
    if status == "abstain" and "text" not in value:
        return {
            "requestId": request_id,
            "contextCommit": "committed",
            "status": "abstain",
            "diagnostics": {"adapter": adapter, "stage": "completed"},
        }
    raise ContractError("bounded discussion final status is invalid")
