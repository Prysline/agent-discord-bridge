"""Root human-turn ingress for the opt-in shared-core Discord path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from .conversation_policy import Participant
from .orchestrator import CoreOutcome, SharedOrchestrator


@dataclass(frozen=True)
class RootHumanTurnResult:
    action: str
    outcomes: tuple[CoreOutcome, ...] = ()


@dataclass(frozen=True)
class HumanTarget:
    accepted: bool
    text: str = ""
    agent_id: str | None = None
    hint: str = ""


@dataclass(frozen=True)
class OnboardingPending:
    room_id: str
    agent_id: str
    created_at: str
    trigger_preview: str


class RootSharedHumanTurn:
    def __init__(self, core: SharedOrchestrator, agent_id: str, participants: list[Participant] | None = None) -> None:
        if not agent_id.strip():
            raise ValueError("shared agent id must be non-empty")
        self.core = core
        self.agent_id = agent_id
        if participants is None:
            self._enabled = [Participant(agent_id, "legacy", 1, 1, agent_alias=agent_id)]
        else:
            self._enabled = [item for item in participants if item.enabled]
        self._onboarding: dict[tuple[str, str], OnboardingPending] = {}

    def pending_onboarding(self) -> tuple[OnboardingPending, ...]:
        return tuple(self._onboarding.values())

    def cancel_onboarding(self, room_id: str, agent_id: str) -> bool:
        return self._onboarding.pop((room_id, agent_id), None) is not None

    async def complete_onboarding(
        self, room_id: str, agent_id: str, binding: "BindingSnapshot"
    ) -> RootHumanTurnResult:
        key = (room_id, agent_id)
        if key not in self._onboarding:
            return RootHumanTurnResult("ignored")
        self.core.publish_binding(room_id, agent_id, binding)
        self._onboarding.pop(key)
        outcomes = await self.core.run_human_turn(room_id, (agent_id,))
        return RootHumanTurnResult("handled", tuple(outcomes))

    def select_human_target(
        self, text: str, bot_user_id: int, *, allow_without_mention: bool = False
    ) -> HumanTarget:
        body = self.strip_ingress_mention(text, bot_user_id)
        if body is None:
            if not allow_without_mention:
                return HumanTarget(False, hint="請先 mention 這個 Bot。")
            body = text.strip()
        candidates = [item for item in self._enabled if item.mention_id == str(bot_user_id)]
        if not candidates:
            return HumanTarget(False, hint="這個 Bot 沒有對應的啟用 Agent。")
        if len(candidates) == 1:
            if not candidates[0].available:
                return HumanTarget(False, hint="這個 Agent 暫時不可用。")
            return HumanTarget(bool(body), body, candidates[0].agent_id, "訊息內容不可為空。")
        aliases = {item.agent_alias or item.agent_id: item for item in candidates}
        selector = re.match(r"^([a-z][a-z0-9_-]{0,31})\s*:\s*(.+)$", body, re.DOTALL)
        if selector and selector.group(1) in aliases:
            participant = aliases[selector.group(1)]
            if not participant.available:
                return HumanTarget(False, hint=f"Agent {selector.group(1)} 暫時不可用。")
            return HumanTarget(True, selector.group(2).strip(), participant.agent_id)
        names = "、".join(aliases)
        return HumanTarget(False, hint=f"這個 Bot 有多個 Agent，請指定：{names}\n例如：<@{bot_user_id}> {next(iter(aliases))}: 幫我看看")

    @staticmethod
    def strip_ingress_mention(text: str, bot_user_id: int) -> str | None:
        match = re.search(rf"<@!?{bot_user_id}>", text)
        if match is None:
            return None
        return (text[:match.start()] + text[match.end():]).strip()

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
        target_agent_id: str | None = None,
        mentioned_agent_ids: tuple[str, ...] = (),
        timestamp: str | None = None,
    ) -> RootHumanTurnResult:
        if not allowed:
            return RootHumanTurnResult("unauthorized")
        if is_peer:
            return RootHumanTurnResult("peer-ignored")
        control = None if target_agent_id is not None else self.core.policy.classify_control(text)
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

        selected_agent = target_agent_id or self.agent_id
        if self.core.policy.state(room_id).phase in {"active", "closing-check"}:
            outcome = self.core.ingest_human(
                room_id,
                author_id=author_id,
                display_name=display_name,
                text=text,
                mentioned_agents=(
                    mentioned_agent_ids
                    or ((selected_agent,) if mentions_agent else ())
                ),
                timestamp=timestamp,
                allow_control=False,
            )
            return RootHumanTurnResult("discussion-context", (outcome,))
        key = (room_id, selected_agent)
        if self.core.binding(room_id, selected_agent) is None:
            if key in self._onboarding:
                return RootHumanTurnResult("onboarding-pending")
            outcome = self.core.ingest_human(
                room_id,
                author_id=author_id,
                display_name=display_name,
                text=text,
                mentioned_agents=(selected_agent,) if mentions_agent else (),
                timestamp=timestamp,
                allow_control=False,
            )
            self._onboarding[key] = OnboardingPending(
                room_id,
                selected_agent,
                datetime.now(timezone.utc).isoformat(),
                text[:160],
            )
            return RootHumanTurnResult("onboarding-required", (outcome,))
        outcome = self.core.ingest_human(
            room_id,
            author_id=author_id,
            display_name=display_name,
            text=text,
            mentioned_agents=(selected_agent,) if mentions_agent else (),
            timestamp=timestamp,
            allow_control=target_agent_id is None,
        )
        outcomes = await self.core.run_human_turn(room_id, (selected_agent,))
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
    if (
        action == "adapter-error"
        and outcomes[-1].reason == "codex_model_not_supported_for_chatgpt_account"
    ):
        return "設定的 Codex 模型不支援目前登入的 ChatGPT 帳號。請在本機 Agent 管理前台改選模型，重新啟動 Bot，並為頻道建立新的 Codex Thread。"
    return "後台這次沒有完成回覆，且不會改走舊流程重試。"
