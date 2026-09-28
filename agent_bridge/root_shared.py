"""Root human-turn ingress for the opt-in shared-core Discord path."""

from __future__ import annotations

from dataclasses import dataclass

from .orchestrator import CoreOutcome, SharedOrchestrator


@dataclass(frozen=True)
class RootHumanTurnResult:
    action: str
    outcomes: tuple[CoreOutcome, ...] = ()


class RootSharedHumanTurn:
    def __init__(self, core: SharedOrchestrator, agent_id: str) -> None:
        if not agent_id.strip():
            raise ValueError("shared agent id must be non-empty")
        self.core = core
        self.agent_id = agent_id

    async def handle(
        self,
        *,
        allowed: bool,
        is_peer: bool,
        room_id: str,
        author_id: str,
        display_name: str,
        text: str,
        mentions_agent: bool,
        timestamp: str | None = None,
    ) -> RootHumanTurnResult:
        if not allowed:
            return RootHumanTurnResult("unauthorized")
        if is_peer:
            return RootHumanTurnResult("peer-ignored")
        control = self.core.policy.classify_control(text)
        if control == "start":
            outcome = self.core.ingest_human(
                room_id,
                author_id=author_id,
                display_name=display_name,
                text=text,
                timestamp=timestamp,
            )
            return RootHumanTurnResult(outcome.action, (outcome,))
        if control == "stop":
            outcome = await self.core.stop(room_id, author_id)
            return RootHumanTurnResult(outcome.action, (outcome,))

        outcome = self.core.ingest_human(
            room_id,
            author_id=author_id,
            display_name=display_name,
            text=text,
            mentioned_agents=(self.agent_id,) if mentions_agent else (),
            timestamp=timestamp,
        )
        if self.core.policy.state(room_id).phase in {"active", "closing-check"}:
            return RootHumanTurnResult("discussion-context", (outcome,))
        outcomes = await self.core.run_human_turn(room_id, (self.agent_id,))
        return RootHumanTurnResult("handled", tuple(outcomes))

    async def drive_discussion(self, room_id: str) -> tuple[CoreOutcome, ...]:
        """Run core-selected turns until the discussion reaches a safe stop."""
        outcomes: list[CoreOutcome] = []
        while self.core.policy.state(room_id).phase in {"active", "closing-check"}:
            outcome = await self.core.run_discussion_turn(room_id)
            outcomes.append(outcome)
            if outcome.action in {
                "blocked",
                "binding-unavailable",
                "context-unknown",
                "delivery-unknown",
                "not-committed",
                "invalid-response",
                "adapter-error",
                "suspended",
                "stopped",
            }:
                break
        return tuple(outcomes)


def safe_failure_message(outcomes: tuple[CoreOutcome, ...]) -> str | None:
    if not outcomes or all(item.action == "delivered" for item in outcomes):
        return None
    action = outcomes[-1].action
    if action == "binding-unavailable":
        return "目前沒有可用的既有對話綁定，這次沒有送進後台。"
    if action == "delivery-unknown":
        return "回覆傳送結果目前無法確認，為避免重複送出，我先停止後續處理。"
    if action == "suspended":
        return "回覆無法送到這個頻道，這次沒有改用其他方式重送。"
    return "後台這次沒有完成回覆，且不會改走舊流程重試。"
