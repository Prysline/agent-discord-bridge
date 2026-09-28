"""Validation helpers for the frozen AgentRequest / AgentResult v1 boundary."""

from __future__ import annotations

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
