import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_bridge import ConversationPolicy
from agent_bridge.discord_delivery import DiscordRoomDelivery
from agent_bridge.orchestrator import BindingSnapshot, SharedOrchestrator
from agent_bridge.root_shared import RootSharedHumanTurn, safe_failure_message
from codex_adapter.root_composition import compose_existing_codex_root


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


class RootSharedHumanTurnTests(unittest.TestCase):
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

    def test_controls_are_unavailable_in_human_turn_slice(self):
        bridge, adapter, _ = runtime()
        for text in ("!discuss <@1> <@2> -- goal", "!stop"):
            result = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text=text, mentions_agent=True))
            self.assertEqual(result.action, "control-unavailable")
        self.assertFalse(adapter.requests)

    def test_missing_binding_fails_closed_without_adapter_execution(self):
        bridge, adapter, _ = runtime(bindings={})
        result = asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="room-1", author_id="human", display_name="Human", text="hello", mentions_agent=True))
        self.assertEqual(result.outcomes[0].action, "binding-unavailable")
        self.assertFalse(adapter.requests)
        self.assertIn("既有對話綁定", safe_failure_message(result.outcomes))

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


if __name__ == "__main__":
    unittest.main()
