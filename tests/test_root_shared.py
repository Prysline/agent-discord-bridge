import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_bridge import ConversationPolicy, Participant
from agent_bridge.discord_delivery import DiscordRoomDelivery
from agent_bridge.orchestrator import BindingSnapshot, SharedOrchestrator
from agent_bridge.root_shared import RootSharedHumanTurn, safe_failure_message
from codex_adapter.root_composition import (
    compose_existing_codex_root,
    parse_root_discussion_settings,
)
from codex_adapter.app_server import AppServerRpcError


class FakeAdapter:
    capabilities = {"sessionMode": "persistent", "maxInFlight": 1, "canCancelInFlight": True}

    def __init__(self, result=None):
        self.requests = []
        self.result = result

    async def execute(self, request, *, observer):
        self.requests.append(request)
        await observer.on_invocation_started(request["requestId"], "confirmed")
        return self.result or {"requestId": request["requestId"], "contextCommit": "committed", "status": "continue", "text": "reply"}

    async def cancel(self, request_id):
        return None


class BlockingAdapter(FakeAdapter):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = []

    async def execute(self, request, *, observer):
        self.requests.append(request)
        await observer.on_invocation_started(request["requestId"], "confirmed")
        self.entered.set()
        await self.release.wait()
        return {"requestId": request["requestId"], "contextCommit": "committed", "status": "continue", "text": "late"}

    async def cancel(self, request_id):
        self.cancelled.append(request_id)


class InvalidNativeClient:
    def __init__(self):
        self.start_calls = []

    async def connect(self):
        return None

    async def reconnect(self):
        return None

    async def resume_thread(self, thread_id):
        raise AppServerRpcError(-32000, "invalid existing thread")

    async def start_turn(self, thread_id, text, client_user_message_id):
        self.start_calls.append((thread_id, client_user_message_id))
        raise AssertionError("invalid native mapping must not create or start a turn")

    async def wait_for_turn(self, turn_id, timeout_ms):
        raise AssertionError("no turn should exist")

    async def list_turns(self, thread_id):
        return []

    async def interrupt(self, thread_id, turn_id):
        return {}


class FakeDelivery:
    def __init__(self, outcome="delivered"):
        self.outcome = outcome
        self.calls = []

    async def deliver(self, room_id, agent_id, text, request_id):
        self.calls.append((room_id, agent_id, text, request_id))
        return self.outcome


class FakeChannel:
    def __init__(self, *, result=object(), error=None):
        self.result = result
        self.error = error
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        if self.error:
            raise self.error
        return self.result


def runtime(adapter=None, delivery=None, bindings=None):
    adapter = adapter or FakeAdapter()
    delivery = delivery or FakeDelivery()
    core = SharedOrchestrator(
        policy=ConversationPolicy([], global_max_dispatches=1),
        adapters={"agent-a": adapter},
        bindings=(
            {("room-1", "agent-a"): BindingSnapshot("binding-a", 7)}
            if bindings is None
            else bindings
        ),
        delivery=delivery,
        request_id_factory=lambda: "request-1",
    )
    return RootSharedHumanTurn(core, "agent-a"), adapter, delivery


def discussion_runtime(*, delivery=None, bindings=None, adapters=None, maximum=3):
    participants = [
        Participant("agent-a", "101", 2000, 5, agent_alias="planner"),
        Participant("agent-b", "202", 2000, 5, agent_alias="coder"),
    ]
    adapters = adapters or {"agent-a": FakeAdapter(), "agent-b": FakeAdapter()}
    delivery = delivery or FakeDelivery()
    request_ids = iter(f"request-{index}" for index in range(1, 20))
    core = SharedOrchestrator(
        policy=ConversationPolicy(participants, global_max_dispatches=maximum),
        adapters=adapters,
        bindings=(
            {
                ("room-1", "agent-a"): BindingSnapshot("binding-a", 7),
                ("room-1", "agent-b"): BindingSnapshot("binding-b", 4),
            }
            if bindings is None
            else bindings
        ),
        delivery=delivery,
        request_id_factory=lambda: next(request_ids),
    )
    return RootSharedHumanTurn(core, "agent-a", participants), adapters, delivery


class RootSharedHumanTurnTests(unittest.TestCase):
    def test_single_agent_bot_routes_directly_and_strips_ingress_mention(self):
        bridge, _, _ = runtime()
        selected = bridge.select_human_target("<@999> hello", 999)
        self.assertTrue(selected.accepted)
        self.assertEqual((selected.agent_id, selected.text), ("agent-a", "hello"))

    def test_reply_ingress_preserves_single_and_shared_agent_routing(self):
        single, _, _ = runtime()
        selected = single.select_human_target("reply body", 999, allow_without_mention=True)
        self.assertEqual((selected.accepted, selected.agent_id, selected.text), (True, "agent-a", "reply body"))

        shared, _, _ = discussion_runtime()
        selected = shared.select_human_target("coder: reply body", 999, allow_without_mention=True)
        self.assertEqual((selected.accepted, selected.agent_id, selected.text), (True, "agent-b", "reply body"))
        self.assertFalse(shared.select_human_target("reply body", 999, allow_without_mention=True).accepted)

    def test_disabled_and_unavailable_agents_cannot_be_selected(self):
        base, _, _ = discussion_runtime()
        disabled = RootSharedHumanTurn(
            base.core,
            "agent-a",
            [Participant("agent-a", "999", 10, 1, enabled=False, agent_alias="planner")],
        )
        self.assertFalse(disabled.select_human_target("<@999> hello", 999).accepted)

        unavailable = RootSharedHumanTurn(
            base.core,
            "agent-a",
            [Participant("agent-a", "999", 10, 1, available=False, agent_alias="planner")],
        )
        self.assertFalse(unavailable.select_human_target("<@999> hello", 999).accepted)

    def test_shared_bot_requires_exact_alias_and_strips_only_selector(self):
        bridge, _, _ = discussion_runtime()
        selected = bridge.select_human_target("<@999> coder: 請評論 planner: hello", 999)
        self.assertTrue(selected.accepted)
        self.assertEqual((selected.agent_id, selected.text), ("agent-b", "請評論 planner: hello"))
        for text in ("<@999> hello", "<@999> missing: hello"):
            rejected = bridge.select_human_target(text, 999)
            self.assertFalse(rejected.accepted)
            self.assertIn("planner", rejected.hint)
            self.assertIn("coder", rejected.hint)

    def test_shared_bot_selected_agent_receives_stripped_canonical_text(self):
        bridge, adapters, _ = discussion_runtime()
        selected = bridge.select_human_target("<@999> coder: inspect this", 999)
        result = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text=selected.text, mentions_agent=True, target_agent_id=selected.agent_id))
        self.assertEqual(result.outcomes[0].action, "delivered")
        self.assertFalse(adapters["agent-a"].requests)
        request = adapters["agent-b"].requests[0]
        self.assertEqual(request["context"]["events"][0]["text"], "inspect this")

    def test_selected_prompt_cannot_be_reclassified_as_control(self):
        bridge, adapters, _ = discussion_runtime()
        selected = bridge.select_human_target("<@999> planner: !stop", 999)
        result = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text=selected.text, mentions_agent=True, target_agent_id=selected.agent_id))
        self.assertEqual(result.action, "handled")
        self.assertEqual(bridge.core.policy.state("room-1").phase, "idle")
        self.assertEqual(adapters["agent-a"].requests[0]["context"]["events"][0]["text"], "!stop")

    def test_active_discussion_intervention_needs_no_alias_and_does_not_dispatch(self):
        bridge, adapters, _ = discussion_runtime(maximum=1)
        asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss planner coder -- topic", mentions_agent=True))
        stripped = bridge.strip_ingress_mention("<@999> one more constraint", 999)
        self.assertEqual(stripped, "one more constraint")
        intervention = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text=stripped, mentions_agent=True))
        self.assertEqual(intervention.action, "discussion-context")
        self.assertFalse(adapters["agent-a"].requests)
        self.assertFalse(adapters["agent-b"].requests)

    def test_authorized_human_uses_configured_agent_and_event_delta(self):
        bridge, adapter, delivery = runtime()
        result = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human-1", display_name="Human", text="hello", mentions_agent=True, timestamp="2026-09-28T00:00:00Z"))
        self.assertEqual(result.outcomes[0].action, "delivered")
        request = adapter.requests[0]
        self.assertEqual(request["agentId"], "agent-a")
        self.assertEqual(request["binding"], {"bindingId": "binding-a", "generation": 7})
        self.assertEqual(request["context"]["kind"], "event-delta")
        self.assertEqual(request["context"]["events"][0]["text"], "hello")
        self.assertEqual(delivery.calls[0][:3], ("room-1", "agent-a", "reply"))

    def test_unauthorized_and_peer_messages_do_not_enter_core(self):
        for allowed, peer in ((False, False), (True, True)):
            bridge, adapter, _ = runtime()
            result = asyncio.run(bridge.handle(allowed=allowed, is_peer=peer, room_id="room-1", author_id="x", display_name="X", text="hello", mentions_agent=True))
            self.assertFalse(adapter.requests)
            self.assertFalse(bridge.core.log.events("room-1"))
            self.assertIn(result.action, {"unauthorized", "peer-ignored"})

    def test_authorized_discussion_uses_core_order_and_bounded_mode(self):
        bridge, adapters, delivery = discussion_runtime(maximum=2)
        started = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss <@101> <@202> -- compare", mentions_agent=True))
        outcomes = asyncio.run(bridge.drive_discussion("room-1"))
        self.assertEqual(started.action, "started")
        self.assertEqual([call[1] for call in delivery.calls], ["agent-a", "agent-b"])
        self.assertEqual(adapters["agent-a"].requests[0]["mode"], "bounded-discussion")
        self.assertEqual(adapters["agent-b"].requests[0]["discussion"]["turnIndex"], 2)
        self.assertEqual(outcomes[-1].action, "suspended")

    def test_discussion_keeps_start_binding_snapshot(self):
        bridge, adapters, _ = discussion_runtime(maximum=2)
        asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss <@101> <@202> -- snapshot", mentions_agent=True))
        bridge.core.bindings[("room-1", "agent-b")] = BindingSnapshot("binding-b-new", 9)
        asyncio.run(bridge.drive_discussion("room-1"))
        self.assertEqual(adapters["agent-b"].requests[0]["binding"], {"bindingId": "binding-b", "generation": 4})

    def test_human_intervention_is_context_not_parallel_human_turn(self):
        bridge, adapters, _ = discussion_runtime(maximum=1)
        asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss <@101> <@202> -- topic", mentions_agent=True))
        intervention = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="one more constraint", mentions_agent=True))
        self.assertEqual(intervention.action, "discussion-context")
        self.assertFalse(adapters["agent-a"].requests)
        asyncio.run(bridge.drive_discussion("room-1"))
        texts = [event["text"] for event in adapters["agent-a"].requests[0]["context"]["events"]]
        self.assertIn("one more constraint", texts)

    def test_missing_participant_binding_rejects_whole_start(self):
        bindings = {("room-1", "agent-a"): BindingSnapshot("binding-a", 7)}
        bridge, adapters, _ = discussion_runtime(bindings=bindings)
        result = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss <@101> <@202> -- missing", mentions_agent=True))
        self.assertEqual(result.action, "binding-unavailable")
        self.assertFalse(bridge.core.log.events("room-1"))
        self.assertFalse(adapters["agent-a"].requests)
        self.assertFalse(adapters["agent-b"].requests)

    def test_stop_uses_core_lifecycle_and_prevents_new_turn(self):
        bridge, adapters, _ = discussion_runtime(maximum=3)
        asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss <@101> <@202> -- stop", mentions_agent=True))
        stop = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!stop", mentions_agent=True))
        outcomes = asyncio.run(bridge.drive_discussion("room-1"))
        self.assertEqual(stop.action, "stopped")
        self.assertFalse(outcomes)
        self.assertFalse(adapters["agent-a"].requests)
        self.assertEqual(bridge.core.policy.state("room-1").phase, "stopped")

    def test_stop_during_root_driver_cancels_and_late_result_cannot_continue(self):
        async def run():
            blocker = BlockingAdapter()
            other = FakeAdapter()
            bridge, _, delivery = discussion_runtime(adapters={"agent-a": blocker, "agent-b": other})
            await bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss <@101> <@202> -- interrupt", mentions_agent=True)
            driver = asyncio.create_task(bridge.drive_discussion("room-1"))
            await blocker.entered.wait()
            stopped = await bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!stop", mentions_agent=True)
            blocker.release.set()
            outcomes = await driver
            self.assertEqual(stopped.action, "stopped")
            self.assertEqual(len(blocker.cancelled), 1)
            self.assertFalse(other.requests)
            self.assertFalse(delivery.calls)
            self.assertEqual(bridge.core.policy.state("room-1").phase, "stopped")
            self.assertEqual(outcomes[-1].action, "invalidated")
        asyncio.run(run())

    def test_unknown_delivery_survives_stop_and_blocks_restart(self):
        bridge, _, _ = discussion_runtime(delivery=FakeDelivery("unknown"))
        asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss <@101> <@202> -- fence", mentions_agent=True))
        outcomes = asyncio.run(bridge.drive_discussion("room-1"))
        stopped = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!stop", mentions_agent=True))
        restarted = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss <@101> <@202> -- again", mentions_agent=True))
        self.assertEqual(outcomes[-1].action, "delivery-unknown")
        self.assertEqual(stopped.action, "stopped")
        self.assertEqual(restarted.action, "rejected")
        self.assertEqual(restarted.outcomes[0].reason, "delivery pending")

    def test_missing_binding_fails_closed_without_adapter_execution(self):
        bridge, adapter, _ = runtime(bindings={})
        result = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="hello", mentions_agent=True))
        self.assertEqual(result.action, "onboarding-required")
        self.assertEqual(len(bridge.pending_onboarding()), 1)
        self.assertFalse(adapter.requests)

    def test_onboarding_suppresses_followups_and_resumes_original_once(self):
        bridge, adapter, _ = runtime(bindings={})
        first = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="first", mentions_agent=True))
        second = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="second", mentions_agent=True))

        self.assertEqual(first.action, "onboarding-required")
        self.assertEqual(second.action, "onboarding-pending")
        self.assertEqual([event.text for event in bridge.core.log.events("room-1")], ["first"])
        self.assertFalse(adapter.requests)

        completed = asyncio.run(bridge.complete_onboarding("room-1", "agent-a", BindingSnapshot("binding-a", 7)))
        self.assertEqual(completed.action, "handled")
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(adapter.requests[0]["context"]["events"][0]["text"], "first")
        self.assertFalse(bridge.pending_onboarding())

    def test_unknown_delivery_keeps_orchestrator_fence(self):
        bridge, _, _ = runtime(delivery=FakeDelivery("unknown"))
        first = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="one", mentions_agent=True))
        second = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="two", mentions_agent=True))
        self.assertEqual(first.outcomes[0].action, "delivery-unknown")
        self.assertEqual(second.outcomes[0].action, "blocked")
        self.assertIn("無法確認", safe_failure_message(first.outcomes))

    def test_known_delivery_failure_has_distinct_safe_message(self):
        bridge, _, _ = runtime(delivery=FakeDelivery("not_delivered"))
        result = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="hello", mentions_agent=True))
        self.assertEqual(result.outcomes[0].action, "suspended")
        self.assertIn("無法送到", safe_failure_message(result.outcomes))


class DiscordRoomDeliveryTests(unittest.TestCase):
    def test_delivery_statuses_and_no_chunking(self):
        async def run():
            delivery = DiscordRoomDelivery(message_limit=5)
            self.assertEqual(await delivery.deliver("missing", "a", "ok", "r"), "not_delivered")
            channel = FakeChannel()
            delivery.register("room", channel)
            self.assertEqual(await delivery.deliver("room", "a", "hello", "r"), "delivered")
            self.assertEqual(await delivery.deliver("room", "a", "too long", "r"), "not_delivered")
            self.assertEqual(channel.sent, ["hello"])
            failing = FakeChannel(error=RuntimeError("ambiguous"))
            delivery.register("fail", failing)
            self.assertEqual(await delivery.deliver("fail", "a", "hello", "r"), "unknown")
        asyncio.run(run())


class RootCompositionTests(unittest.TestCase):
    def test_composition_uses_bootstrapped_active_binding(self):
        raw = {"logicalBindings": [{"roomId": "room-1", "agentId": "agent-a", "bindingId": "binding-a", "generations": [7], "activeGeneration": 7}], "codexBindings": [{"bindingId": "binding-a", "generation": 7, "threadId": "thread-placeholder"}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            bridge = compose_existing_codex_root(binding_path=path, agent_id="agent-a", client=object(), delivery=FakeDelivery(), display_name="Agent")
        self.assertEqual(bridge.core.bindings[("room-1", "agent-a")], BindingSnapshot("binding-a", 7))

    def test_discussion_config_and_composition_include_all_existing_participants(self):
        config = {"sharedDiscussion": {"globalMaxDispatches": 6, "participants": [
            {"agentId": "agent-a", "agentAlias": "planner", "adapter": "codex", "mentionId": "999", "displayName": "A", "budgetChars": 1000, "maxCalls": 2},
            {"agentId": "agent-b", "agentAlias": "coder", "adapter": "codex", "mentionId": "999", "displayName": "B", "budgetChars": 1200, "maxCalls": 3},
        ]}}
        settings = parse_root_discussion_settings(config)
        self.assertEqual([item.agent_alias for item in settings.participants], ["planner", "coder"])
        raw = {"logicalBindings": [
            {"roomId": "room-1", "agentId": "agent-a", "bindingId": "binding-a", "generations": [7], "activeGeneration": 7},
            {"roomId": "room-1", "agentId": "agent-b", "bindingId": "binding-b", "generations": [4], "activeGeneration": 4},
        ], "codexBindings": [
            {"bindingId": "binding-a", "generation": 7, "threadId": "thread-a"},
            {"bindingId": "binding-b", "generation": 4, "threadId": "thread-b"},
        ]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            bridge = compose_existing_codex_root(binding_path=path, agent_id="agent-a", client=object(), delivery=FakeDelivery(), display_name="A", participants=list(settings.participants), participant_display_names=dict(settings.display_names), global_max_dispatches=settings.global_max_dispatches)
        self.assertEqual(bridge.core.policy.global_max_dispatches, 6)
        self.assertEqual(bridge.core.bindings[("room-1", "agent-b")], BindingSnapshot("binding-b", 4))

    def test_invalid_native_thread_fails_closed_without_turn_creation(self):
        settings = parse_root_discussion_settings({"sharedDiscussion": {"globalMaxDispatches": 2, "participants": [
            {"agentId": "agent-a", "adapter": "codex", "mentionId": "101", "budgetChars": 1000, "maxCalls": 2},
            {"agentId": "agent-b", "adapter": "codex", "mentionId": "202", "budgetChars": 1000, "maxCalls": 2},
        ]}})
        raw = {"logicalBindings": [
            {"roomId": "room-1", "agentId": "agent-a", "bindingId": "binding-a", "generations": [1], "activeGeneration": 1},
            {"roomId": "room-1", "agentId": "agent-b", "bindingId": "binding-b", "generations": [1], "activeGeneration": 1},
        ], "codexBindings": [
            {"bindingId": "binding-a", "generation": 1, "threadId": "invalid-a"},
            {"bindingId": "binding-b", "generation": 1, "threadId": "invalid-b"},
        ]}
        client = InvalidNativeClient()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            bridge = compose_existing_codex_root(binding_path=path, agent_id="agent-a", client=client, delivery=FakeDelivery(), display_name="A", participants=list(settings.participants), global_max_dispatches=2)
            started = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="!discuss <@101> <@202> -- invalid", mentions_agent=True))
            outcomes = asyncio.run(bridge.drive_discussion("room-1"))
        self.assertEqual(started.action, "started")
        self.assertEqual(outcomes[-1].action, "not-committed")
        self.assertFalse(client.start_calls)


if __name__ == "__main__":
    unittest.main()
