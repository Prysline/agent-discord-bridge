import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib import error, request as urllib_request

from agent_bridge import ConversationPolicy, Participant
from agent_bridge.heterogeneous_composition import compose_existing_heterogeneous_root
from agent_bridge.orchestrator import BindingSnapshot, SharedOrchestrator
from antigravity_adapter.binding import (
    BindingGenerationMismatch,
    BindingUnavailable,
    InMemoryBindingResolver,
    ResolvedBinding,
)
from antigravity_adapter.binding_bootstrap import bootstrap_existing_bindings
from antigravity_adapter.control_plane import AntigravityControlPlane
from antigravity_adapter.persistent import AntigravityPersistentAdapter
from antigravity_adapter.transport import SidecarAmbiguous, SidecarHttpClient, SidecarRejected, SidecarUnavailable
from antigravity_adapter.sidecar import worker
from antigravity_adapter.sidecar import lifecycle
from codex_adapter.app_server import TurnReference
from codex_adapter.persistent import CodexPersistentAdapter
from codex_adapter.root_composition import parse_root_discussion_settings


def request(request_id="request-1", *, mode="human-turn"):
    value = {
        "requestId": request_id,
        "agentId": "agent-b",
        "mode": mode,
        "binding": {"bindingId": "binding-b", "generation": 3},
        "context": {
            "kind": "event-delta",
            "events": [{
                "eventId": "event-1", "seq": 1, "kind": "message",
                "author": {"type": "human", "id": "human", "displayName": "Human"},
                "text": "hello", "mentions": ["agent-b"],
            }],
            "cursor": {"fromSeqExclusive": 0, "throughSeqInclusive": 1},
        },
        "constraints": {"timeoutMs": 100},
    }
    if mode == "bounded-discussion":
        value["discussion"] = {"discussionId": "d1", "goal": "compare", "turnIndex": 1, "phase": "active"}
    return value


def bindings():
    return {
        "logicalBindings": [
            {"roomId": "room", "agentId": "agent-b", "bindingId": "binding-b", "generations": [2, 3], "activeGeneration": 3},
        ],
        "antigravityBindings": [
            {"bindingId": "binding-b", "generation": 2, "conversationId": "old-conversation"},
            {"bindingId": "binding-b", "generation": 3, "conversationId": "conversation"},
        ],
    }


class Observer:
    def __init__(self): self.calls = []
    async def on_invocation_started(self, request_id, certainty): self.calls.append((request_id, certainty))


class FakeTransport:
    def __init__(self, results=None, send_error=None, health_error=None):
        self.results = list([{"status": "completed", "text": "reply"}] if results is None else results)
        self.send_error = send_error
        self.health_error = health_error
        self.sends = []
        self.accepted_ids = set()
    async def health(self):
        if self.health_error: raise self.health_error
    async def send(self, conversation_id, message, request_id):
        self.sends.append((conversation_id, message, request_id))
        if self.send_error: raise self.send_error
        self.accepted_ids.add("local-1")
        return "local-1"
    async def result(self, request_id):
        self.last_result_id = request_id
        if request_id not in self.accepted_ids:
            return None
        return self.results.pop(0) if self.results else {"status": "pending"}


class Delivery:
    def __init__(self): self.calls=[]
    async def deliver(self, room_id, agent_id, text, request_id):
        self.calls.append((room_id, agent_id, text, request_id)); return "delivered"


class FakeCodexClient:
    def __init__(self):
        self.resume_calls = []
        self.start_calls = []
        self.current_request_id = None

    async def connect(self): return None
    async def reconnect(self): return None
    async def resume_thread(self, thread_id):
        self.resume_calls.append(thread_id)
        return {"thread":{"id":thread_id}}
    async def start_turn(self, thread_id, text, client_user_message_id):
        self.start_calls.append((thread_id, text, client_user_message_id))
        self.current_request_id = client_user_message_id
        return TurnReference("turn-a", "inProgress")
    async def wait_for_turn(self, turn_id, timeout_ms):
        return self._turn()
    async def list_turns(self, thread_id):
        return [self._turn()]
    async def interrupt(self, thread_id, turn_id): return {}
    def _turn(self):
        return {
            "id":"turn-a", "status":"completed", "items":[
                {"type":"userMessage", "clientId":self.current_request_id, "text":"redacted"},
                {"type":"agentMessage", "phase":"final_answer", "text":"codex reply"},
            ],
        }


class AntigravityAdapterTests(unittest.TestCase):
    def test_exact_generation_and_old_snapshot_resolve(self):
        resolver = bootstrap_existing_bindings(bindings()).binding_resolver
        self.assertEqual(asyncio.run(resolver.resolve("binding-b", 2)).conversation_id, "old-conversation")
        with self.assertRaises(BindingGenerationMismatch): asyncio.run(resolver.resolve("binding-b", 9))
        with self.assertRaises(BindingUnavailable): asyncio.run(resolver.resolve("missing", 3))

    def test_duplicate_native_conversation_across_logical_pairs_fails(self):
        raw = bindings()
        raw["logicalBindings"].append({
            "roomId": "other-room", "agentId": "agent-c", "bindingId": "binding-c",
            "generations": [1], "activeGeneration": 1,
        })
        raw["antigravityBindings"].append({
            "bindingId": "binding-c", "generation": 1, "conversationId": "conversation",
        })
        with self.assertRaisesRegex(ValueError, "already managed"):
            bootstrap_existing_bindings(raw)

    def test_human_and_bounded_requests_preserve_core_request_id(self):
        cases = (
            ("human-turn", "reply"),
            ("bounded-discussion", '{"status":"continue","text":"reply"}'),
        )
        for mode, text in cases:
            transport = FakeTransport(results=[{"status": "completed", "text": text}])
            adapter = AntigravityPersistentAdapter(transport, bootstrap_existing_bindings(bindings()).binding_resolver)
            observer = Observer()
            result = asyncio.run(adapter.execute(request(mode=mode), observer=observer))
            self.assertEqual(result["requestId"], "request-1")
            self.assertEqual(result["status"], "continue")
            self.assertEqual(result["contextCommit"], "committed")
            self.assertEqual(observer.calls, [("request-1", "confirmed")])
            self.assertEqual(transport.last_result_id, "local-1")

    def test_bounded_result_supports_complete_and_abstain(self):
        cases = (
            ('{"status":"complete","text":"conclusion"}', "complete", "conclusion"),
            ('{"status":"await-human","text":"need a choice"}', "await-human", "need a choice"),
            ('{"status":"abstain"}', "abstain", None),
        )
        for text, status, expected_text in cases:
            transport = FakeTransport(results=[{"status": "completed", "text": text}])
            adapter = AntigravityPersistentAdapter(
                transport, bootstrap_existing_bindings(bindings()).binding_resolver
            )
            result = asyncio.run(
                adapter.execute(request(mode="bounded-discussion"), observer=Observer())
            )
            self.assertEqual(result["status"], status)
            self.assertEqual(result.get("text"), expected_text)
            self.assertEqual(result["diagnostics"]["adapter"], "antigravity")

    def test_bounded_plain_text_fails_closed(self):
        adapter = AntigravityPersistentAdapter(
            FakeTransport(results=[{"status": "completed", "text": "looks done"}]),
            bootstrap_existing_bindings(bindings()).binding_resolver,
        )
        result = asyncio.run(
            adapter.execute(request(mode="bounded-discussion"), observer=Observer())
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["contextCommit"], "committed")
        self.assertEqual(result["error"]["code"], "invalid_response")

    def test_preflight_failure_does_not_signal_invocation(self):
        adapter = AntigravityPersistentAdapter(FakeTransport(health_error=SidecarUnavailable("down")), bootstrap_existing_bindings(bindings()).binding_resolver)
        observer = Observer(); result = asyncio.run(adapter.execute(request(), observer=observer))
        self.assertEqual(result["contextCommit"], "not_committed")
        self.assertFalse(observer.calls)

    def test_ambiguous_send_counts_once_and_fails_unknown(self):
        transport = FakeTransport(results=[], send_error=SidecarAmbiguous("lost"))
        adapter = AntigravityPersistentAdapter(transport, bootstrap_existing_bindings(bindings()).binding_resolver)
        observer = Observer(); result = asyncio.run(adapter.execute(request(), observer=observer))
        self.assertEqual(observer.calls, [("request-1", "ambiguous")])
        self.assertEqual(result["contextCommit"], "unknown")
        blocked = asyncio.run(adapter.execute(request("request-2"), observer=Observer()))
        self.assertEqual(blocked["error"]["code"], "adapter_unavailable")

    def test_ambiguous_send_uses_local_identity_for_positive_read_back(self):
        class AcceptedButResponseLost(FakeTransport):
            async def send(self, conversation_id, message, request_id):
                self.sends.append((conversation_id, message, request_id))
                self.accepted_ids.add("sidecar-local")
                raise SidecarAmbiguous("lost", "sidecar-local")

        transport = AcceptedButResponseLost()
        adapter = AntigravityPersistentAdapter(
            transport, bootstrap_existing_bindings(bindings()).binding_resolver
        )
        observer = Observer()
        result = asyncio.run(adapter.execute(request(), observer=observer))
        self.assertEqual(result["contextCommit"], "committed")
        self.assertEqual(result["text"], "reply")
        self.assertEqual(transport.last_result_id, "sidecar-local")
        self.assertEqual(observer.calls, [("request-1", "ambiguous")])

    def test_cancel_is_invalidation_not_claimed_platform_interrupt(self):
        adapter = AntigravityPersistentAdapter(FakeTransport(), bootstrap_existing_bindings(bindings()).binding_resolver)
        outcome = asyncio.run(adapter.cancel("request-1"))
        self.assertEqual(outcome["state"], "invalidated")
        self.assertFalse(adapter.capabilities["canCancelInFlight"])

    def test_late_recovered_final_cannot_cross_core_invalidation(self):
        class BlockingTransport(FakeTransport):
            def __init__(self):
                super().__init__()
                self.polling = asyncio.Event()
                self.release = asyncio.Event()
            async def result(self, request_id):
                self.polling.set()
                await self.release.wait()
                return {"status":"completed", "text":"late reply"}

        async def scenario():
            transport = BlockingTransport()
            adapter = AntigravityPersistentAdapter(
                transport, bootstrap_existing_bindings(bindings()).binding_resolver
            )
            task = asyncio.create_task(adapter.execute(request(), observer=Observer()))
            await transport.polling.wait()
            await adapter.cancel("request-1")
            transport.release.set()
            return await task

        result = asyncio.run(scenario())
        self.assertEqual(result["contextCommit"], "committed")
        self.assertEqual(result["status"], "error")
        self.assertNotIn("text", result)

    def test_shared_orchestrator_commits_antigravity_output_after_delivery(self):
        adapter = AntigravityPersistentAdapter(FakeTransport(), bootstrap_existing_bindings(bindings()).binding_resolver)
        delivery = Delivery()
        core = SharedOrchestrator(policy=ConversationPolicy([], global_max_dispatches=1), adapters={"agent-b": adapter}, bindings={("room", "agent-b"): BindingSnapshot("binding-b", 3)}, delivery=delivery, request_id_factory=lambda: "request-1")
        core.ingest_human("room", author_id="human", display_name="Human", text="hello", mentioned_agents=("agent-b",))
        outcome = asyncio.run(core.run_human_turn("room", ("agent-b",)))[0]
        self.assertEqual(outcome.action, "delivered")
        self.assertEqual(core.log.events("room")[-1].text, "reply")


class HeterogeneousCompositionTests(unittest.TestCase):
    def test_current_binding_schema_composes_both_adapters(self):
        raw = {
            "bindingLineages": [
                {"agentId":"agent-a","bindingId":"binding-a","generations":[1]},
                {"agentId":"agent-b","bindingId":"binding-b","generations":[3]},
            ],
            "activeBindings": [
                {"roomId":"room","agentId":"agent-a","bindingId":"binding-a","activeGeneration":1},
                {"roomId":"room","agentId":"agent-b","bindingId":"binding-b","activeGeneration":3},
            ],
            "codexBindings":[{"bindingId":"binding-a","generation":1,"threadId":"thread"}],
            "antigravityBindings":[{"bindingId":"binding-b","generation":3,"conversationId":"conversation"}],
        }
        participants = [Participant("agent-a","101",100,1), Participant("agent-b","202",100,1)]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bindings.json"; path.write_text(json.dumps(raw), encoding="utf-8")
            bridge=compose_existing_heterogeneous_root(binding_path=path, primary_agent_id="agent-a", adapter_types={"agent-a":"codex","agent-b":"antigravity"}, codex_client=object(), antigravity_transport=FakeTransport(), delivery=Delivery(), participants=participants, display_names={"agent-a":"A","agent-b":"B"}, global_max_dispatches=2)
        self.assertIsInstance(bridge.core.adapters["agent-a"], CodexPersistentAdapter)
        self.assertIsInstance(bridge.core.adapters["agent-b"], AntigravityPersistentAdapter)
        self.assertEqual(set(bridge.core.bindings), {("room", "agent-a"), ("room", "agent-b")})

    def test_composition_selects_adapter_per_agent_without_native_ids_in_core(self):
        raw = {
            "logicalBindings": [
                {"roomId":"room","agentId":"agent-a","bindingId":"binding-a","generations":[1],"activeGeneration":1},
                {"roomId":"room","agentId":"agent-b","bindingId":"binding-b","generations":[3],"activeGeneration":3},
            ],
            "codexBindings":[{"bindingId":"binding-a","generation":1,"threadId":"thread"}],
            "antigravityBindings":[{"bindingId":"binding-b","generation":3,"conversationId":"conversation"}],
        }
        participants = [Participant("agent-a","101",100,1), Participant("agent-b","202",100,1)]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bindings.json"; path.write_text(json.dumps(raw), encoding="utf-8")
            bridge=compose_existing_heterogeneous_root(binding_path=path, primary_agent_id="agent-a", adapter_types={"agent-a":"codex","agent-b":"antigravity"}, codex_client=object(), antigravity_transport=FakeTransport(), delivery=Delivery(), participants=participants, display_names={"agent-a":"A","agent-b":"B"}, global_max_dispatches=2)
        self.assertIsInstance(bridge.core.adapters["agent-b"], AntigravityPersistentAdapter)
        self.assertEqual(vars(bridge.core.bindings[("room","agent-b")]), {"binding_id":"binding-b", "generation":3})

    def test_codex_only_config_does_not_require_antigravity_section(self):
        raw = {
            "logicalBindings":[{"roomId":"room","agentId":"agent-a","bindingId":"binding-a","generations":[1],"activeGeneration":1}],
            "codexBindings":[{"bindingId":"binding-a","generation":1,"threadId":"thread"}],
        }
        participant = Participant("agent-a","101",100,1)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bindings.json"; path.write_text(json.dumps(raw), encoding="utf-8")
            bridge=compose_existing_heterogeneous_root(binding_path=path, primary_agent_id="agent-a", adapter_types={"agent-a":"codex"}, codex_client=object(), antigravity_transport=FakeTransport(), delivery=Delivery(), participants=[participant], display_names={"agent-a":"A"}, global_max_dispatches=1)
        self.assertEqual(bridge.core.adapters["agent-a"].__class__.__name__, "CodexPersistentAdapter")

    def test_missing_binding_reaches_onboarding_without_model_invocation(self):
        raw = {
            "bindingLineages": [],
            "activeBindings": [],
            "codexBindings": [],
            "antigravityBindings": [],
        }
        client = FakeCodexClient()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            bridge = compose_existing_heterogeneous_root(
                binding_path=path,
                primary_agent_id="agent-a",
                adapter_types={"agent-a": "codex"},
                codex_client=client,
                antigravity_transport=FakeTransport(),
                delivery=Delivery(),
                participants=[Participant("agent-a", "101", 100, 1)],
                display_names={"agent-a": "A"},
                global_max_dispatches=1,
            )
            result = asyncio.run(bridge.handle(
                allowed=True,
                is_peer=False,
                room_id="new-room",
                author_id="human",
                display_name="Human",
                text="retain this first message",
                mentions_agent=True,
            ))
        self.assertEqual(result.action, "onboarding-required")
        self.assertEqual(len(bridge.pending_onboarding()), 1)
        self.assertEqual(client.resume_calls, [])
        self.assertEqual(client.start_calls, [])

    def test_human_turn_only_parser_defaults_primary_to_codex(self):
        raw = {
            "logicalBindings":[{"roomId":"room","agentId":"agent-a","bindingId":"binding-a","generations":[1],"activeGeneration":1}],
            "codexBindings":[{"bindingId":"binding-a","generation":1,"threadId":"thread-a"}],
        }
        settings = parse_root_discussion_settings({})
        client = FakeCodexClient()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bindings.json"; path.write_text(json.dumps(raw), encoding="utf-8")
            bridge=compose_existing_heterogeneous_root(
                binding_path=path, primary_agent_id="agent-a",
                adapter_types=settings.adapter_types, codex_client=client,
                antigravity_transport=FakeTransport(), delivery=Delivery(),
                participants=list(settings.participants), display_names=dict(settings.display_names),
                global_max_dispatches=settings.global_max_dispatches,
            )
            result = asyncio.run(bridge.handle(
                allowed=True, is_peer=False, room_id="room", author_id="human",
                display_name="Human", text="hello", mentions_agent=True,
            ))
        self.assertIsInstance(bridge.core.adapters["agent-a"], CodexPersistentAdapter)
        self.assertEqual(result.outcomes[0].action, "delivered")
        self.assertEqual(client.resume_calls, ["thread-a"])

    def test_explicit_discussion_adapter_is_required_and_unknown_fails(self):
        base = {"globalMaxDispatches":2, "participants":[{
            "agentId":"agent-a", "mentionId":"101", "displayName":"A",
            "budgetChars":100, "maxCalls":1,
        }]}
        with self.assertRaisesRegex(ValueError, "adapter is invalid"):
            parse_root_discussion_settings({"sharedDiscussion":base})
        base["participants"][0]["adapter"] = "unknown-runtime"
        with self.assertRaisesRegex(ValueError, "adapter is invalid"):
            parse_root_discussion_settings({"sharedDiscussion":base})

    def test_composition_cannot_default_an_explicit_primary_participant(self):
        raw = {
            "logicalBindings":[{"roomId":"room","agentId":"agent-a","bindingId":"binding-a","generations":[1],"activeGeneration":1}],
            "codexBindings":[{"bindingId":"binding-a","generation":1,"threadId":"thread-a"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bindings.json"; path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "discussion participant"):
                compose_existing_heterogeneous_root(
                    binding_path=path, primary_agent_id="agent-a", adapter_types={},
                    codex_client=FakeCodexClient(), antigravity_transport=FakeTransport(),
                    delivery=Delivery(), participants=[Participant("agent-a","101",100,1)],
                    display_names={"agent-a":"A"}, global_max_dispatches=1,
                )

    def test_missing_antigravity_native_mapping_fails_at_bootstrap(self):
        raw = {
            "logicalBindings":[{"roomId":"room","agentId":"agent-b","bindingId":"binding-b","generations":[3],"activeGeneration":3}],
            "codexBindings":[], "antigravityBindings":[],
        }
        participant = Participant("agent-b","202",100,1)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bindings.json"; path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing"):
                compose_existing_heterogeneous_root(binding_path=path, primary_agent_id="agent-b", adapter_types={"agent-b":"antigravity"}, codex_client=object(), antigravity_transport=FakeTransport(), delivery=Delivery(), participants=[participant], display_names={"agent-b":"B"}, global_max_dispatches=1)

    def test_production_adapters_complete_through_shared_closing_check(self):
        raw = {
            "logicalBindings":[
                {"roomId":"room","agentId":"agent-a","bindingId":"binding-a","generations":[1],"activeGeneration":1},
                {"roomId":"room","agentId":"agent-b","bindingId":"binding-b","generations":[3],"activeGeneration":3},
            ],
            "codexBindings":[{"bindingId":"binding-a","generation":1,"threadId":"thread-a"}],
            "antigravityBindings":[{"bindingId":"binding-b","generation":3,"conversationId":"conversation-b"}],
        }
        client = FakeCodexClient()
        client._turn = lambda: {
            "id":"turn-a", "status":"completed", "items":[
                {"type":"userMessage", "clientId":client.current_request_id, "text":"redacted"},
                {"type":"agentMessage", "phase":"final_answer", "text":'{"status":"complete","text":"codex conclusion"}'},
            ],
        }
        transport = FakeTransport(results=[{
            "status": "completed",
            "text": '{"status":"complete","text":"antigravity confirms"}',
        }])
        participants = [Participant("agent-a","101",1000,2), Participant("agent-b","202",1000,2)]
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"bindings.json"; path.write_text(json.dumps(raw), encoding="utf-8")
            bridge=compose_existing_heterogeneous_root(
                binding_path=path, primary_agent_id="agent-a",
                adapter_types={"agent-a":"codex", "agent-b":"antigravity"},
                codex_client=client, antigravity_transport=transport, delivery=Delivery(),
                participants=participants, display_names={"agent-a":"A", "agent-b":"B"},
                global_max_dispatches=4,
            )
            started = bridge.core.ingest_human(
                "room", author_id="human", display_name="Human",
                text="!discuss <@101> <@202>\ncompare adapters",
            )
            first = asyncio.run(bridge.core.run_discussion_turn("room"))
            second = asyncio.run(bridge.core.run_discussion_turn("room"))
        self.assertEqual(started.action, "started")
        self.assertEqual((first.agent_id, second.agent_id), ("agent-a", "agent-b"))
        self.assertEqual((first.action, second.action), ("closing-check", "completed"))
        self.assertEqual(bridge.core.policy.state("room").phase, "completed")
        self.assertIsInstance(bridge.core.adapters["agent-a"], CodexPersistentAdapter)
        self.assertIsInstance(bridge.core.adapters["agent-b"], AntigravityPersistentAdapter)
        self.assertEqual(client.resume_calls, ["thread-a"])
        self.assertEqual(transport.sends[0][0], "conversation-b")
        self.assertIn('"mode":"bounded-discussion"', client.start_calls[0][1])
        self.assertIn('"mode":"bounded-discussion"', transport.sends[0][1])
        self.assertIn("codex conclusion", transport.sends[0][1])
        self.assertNotIn("conversation-b", client.start_calls[0][1])
        self.assertNotIn("thread-a", transport.sends[0][1])


class AntigravityControlPlaneTests(unittest.TestCase):
    def test_existing_conversation_can_be_validated_published_and_resolved(self):
        class Transport:
            def __init__(self): self.validated = []
            async def validate_conversation(self, conversation_id):
                self.validated.append(conversation_id)

        transport = Transport()
        resolver = InMemoryBindingResolver([])
        control = AntigravityControlPlane(transport, resolver)

        asyncio.run(control.validate_existing("conversation-existing"))
        control.validate_publication("binding-new", 1, "conversation-existing")
        control.publish("binding-new", 1, "conversation-existing")

        self.assertEqual(transport.validated, ["conversation-existing"])
        self.assertFalse(control.can_create)
        self.assertEqual(
            asyncio.run(resolver.resolve("binding-new", 1)),
            ResolvedBinding("binding-new", 1, "conversation-existing"),
        )
        self.assertEqual(
            control.mapping_entry("binding-new", 1, "conversation-existing"),
            {"bindingId":"binding-new", "generation":1, "conversationId":"conversation-existing"},
        )

    def test_validation_failure_does_not_publish_or_enable_create(self):
        class Transport:
            async def validate_conversation(self, conversation_id):
                raise SidecarRejected("conversation_unavailable")

        resolver = InMemoryBindingResolver([])
        control = AntigravityControlPlane(Transport(), resolver)

        with self.assertRaisesRegex(ValueError, "找不到此 Antigravity Conversation"):
            asyncio.run(control.validate_existing("missing"))
        with self.assertRaisesRegex(ValueError, "creation is not supported"):
            asyncio.run(control.create())
        with self.assertRaises(BindingUnavailable):
            asyncio.run(resolver.resolve("binding-new", 1))


class SidecarHttpClientTests(unittest.TestCase):
    def test_validate_conversation_uses_read_only_sidecar_endpoint(self):
        client = SidecarHttpClient("unused.json")
        with patch.object(client, "_call", return_value={"valid": True}) as call:
            asyncio.run(client.validate_conversation("existing-conversation"))
        call.assert_called_once_with(
            "POST",
            "/validate-conversation",
            {"conversationId": "existing-conversation"},
        )

    def test_authenticated_http_validation_does_not_dispatch(self):
        class ValidationState:
            def __init__(self): self.validated = []
            def validate_conversation(self, conversation_id):
                self.validated.append(conversation_id)

        with tempfile.TemporaryDirectory() as directory:
            state = ValidationState()
            server = worker.Server(("127.0.0.1", 0), worker.Handler)
            server.state = state
            server.token = "test-token"
            server.instance_id = "test-instance"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps({
                "host":"127.0.0.1", "port":server.server_address[1],
                "instanceId":"test-instance", "token":"test-token",
            }), encoding="utf-8")
            try:
                asyncio.run(SidecarHttpClient(path).validate_conversation("existing"))
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)
        self.assertEqual(state.validated, ["existing"])

    def test_rendezvous_must_be_loopback_and_diagnostics_hide_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps({"host":"0.0.0.0","port":8765,"instanceId":"instance","token":"secret"}), encoding="utf-8")
            client = SidecarHttpClient(path)
            with self.assertRaises(SidecarUnavailable) as caught:
                asyncio.run(client.health())
        self.assertNotIn("secret", str(caught.exception))

    def test_ambiguous_send_exposes_only_fresh_local_correlation_id(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps({"host":"127.0.0.1","port":8765,"instanceId":"instance","token":"secret"}), encoding="utf-8")
            client = SidecarHttpClient(path)
            with patch.object(client, "_call", side_effect=SidecarAmbiguous("lost")):
                with self.assertRaises(SidecarAmbiguous) as caught:
                    asyncio.run(client.send("conversation", "hello", "core-request"))
        self.assertRegex(caught.exception.request_id or "", r"^[a-f0-9]{32}$")
        self.assertNotEqual(caught.exception.request_id, "core-request")
        self.assertNotIn("secret", str(caught.exception))


class SidecarCorrelationTests(unittest.TestCase):
    def make_state(self, directory):
        root = Path(directory) / "brain"
        root.mkdir(exist_ok=True)
        with patch("antigravity_adapter.sidecar.worker.discover_agentapi", return_value=("agentapi.exe", [], "direct-executable")):
            return worker.State(1, root)

    def transcript(self, state, conversation="conversation"):
        path = state.transcript_root / conversation / ".system_generated" / "logs" / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def dispatch(self, state, transcript=None, request_id="request-1"):
        if transcript is not None and not transcript.exists():
            transcript.write_text("", encoding="utf-8")
        with patch("antigravity_adapter.sidecar.worker.subprocess.run") as run:
            run.return_value.returncode = 0
            return state.send("conversation", "hello", request_id)

    def write_records(self, transcript, records):
        transcript.write_text(
            "\n".join(json.dumps(item) for item in records) + "\n",
            encoding="utf-8",
        )

    def test_conversation_validation_is_read_only_and_requires_existing_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            with self.assertRaisesRegex(worker.SidecarFailure, "conversation_unavailable"):
                state.validate_conversation("missing")
            transcript = self.transcript(state, "existing")
            transcript.write_text("", encoding="utf-8")

            state.validate_conversation("existing")

            self.assertEqual(state.requests, {})
            self.assertIsNone(state.pending_id)

    def final_records(self, pending, text="final"):
        return [
            {"source":"SYSTEM","type":"SYSTEM_MESSAGE","status":"DONE","content":f"[{pending.marker}]\nhello"},
            {"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","content":text},
        ]

    def test_terminal_tool_call_extracts_only_final_text(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            records = [
                {"source":"SYSTEM","type":"SYSTEM_MESSAGE","status":"DONE","content":f"[{pending.marker}]\nhello"},
                {"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","content":"tool", "tool_calls":[{"name":"terminal"}]},
                {"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","content":"final"},
            ]
            transcript.write_text("\n".join(json.dumps(item) for item in records) + "\n", encoding="utf-8")
            outcome = state.stop({"conversationId":"conversation","fullyIdle":True,"error":None,"transcriptPath":str(transcript)})
        self.assertEqual(outcome, "completed")
        self.assertEqual(pending.text, "final")

    def test_hookless_result_polling_recovers_unique_final(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            self.write_records(transcript, self.final_records(pending, "hookless reply"))
            recovered = state.result(pending.request_id)
        self.assertEqual(recovered.status, "completed")
        self.assertEqual(recovered.text, "hookless reply")
        self.assertEqual(recovered.recovery_source, "polling")
        self.assertIsNone(state.pending_id)

    def test_delayed_final_remains_pending_then_completes(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            self.write_records(transcript, self.final_records(pending)[:1])
            self.assertEqual(state.result(pending.request_id).status, "pending")
            self.write_records(transcript, self.final_records(pending))
            self.assertEqual(state.result(pending.request_id).status, "completed")

    def test_dispatch_offset_excludes_old_conversation_history(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            self.write_records(transcript, [
                {"source":"SYSTEM","type":"SYSTEM_MESSAGE","status":"DONE","content":"[SIDECAR_REQUEST_ID_old]"},
                {"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","content":"old reply"},
            ])
            pending = self.dispatch(state, transcript)
            self.assertEqual(state.result(pending.request_id).status, "pending")
            with transcript.open("a", encoding="utf-8") as stream:
                for item in self.final_records(pending, "new reply"):
                    stream.write(json.dumps(item) + "\n")
            recovered = state.result(pending.request_id)
            self.assertEqual(recovered.status, "completed")
            self.assertEqual(recovered.text, "new reply")

    def test_transcript_created_after_dispatch_is_accepted_once(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state)
            self.assertFalse(transcript.exists())
            self.write_records(transcript, self.final_records(pending))
            self.assertEqual(state.result(pending.request_id).status, "completed")

    def test_duplicate_marker_and_multiple_finals_fail_closed(self):
        cases = (
            lambda pending: self.final_records(pending) + self.final_records(pending)[:1],
            lambda pending: self.final_records(pending) + [{"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","content":"second"}],
        )
        for records in cases:
            with self.subTest(case=records), tempfile.TemporaryDirectory() as directory:
                state = self.make_state(directory)
                transcript = self.transcript(state)
                pending = self.dispatch(state, transcript)
                self.write_records(transcript, records(pending))
                recovered = state.result(pending.request_id)
                self.assertEqual(recovered.status, "error")
                self.assertEqual(recovered.error["code"], "correlation_failed")

    def test_incomplete_tail_waits_but_complete_malformed_line_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            raw = "\n".join(json.dumps(item) for item in self.final_records(pending))
            transcript.write_text(raw[:-2], encoding="utf-8")
            self.assertEqual(state.result(pending.request_id).status, "pending")
            transcript.write_text(raw + "\n", encoding="utf-8")
            self.assertEqual(state.result(pending.request_id).status, "completed")

        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            transcript.write_text("{not-json}\n", encoding="utf-8")
            recovered = state.result(pending.request_id)
            self.assertEqual(recovered.status, "error")
            self.assertEqual(recovered.error["code"], "correlation_failed")

    def test_truncation_replacement_and_disappearance_fail_closed(self):
        for mode in ("truncate", "replace", "disappear"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                state = self.make_state(directory)
                transcript = self.transcript(state)
                transcript.write_text("baseline\n", encoding="utf-8")
                pending = self.dispatch(state, transcript)
                if mode == "replace":
                    replacement = transcript.with_suffix(".replacement")
                    replacement.write_text("", encoding="utf-8")
                    replacement.replace(transcript)
                elif mode == "truncate":
                    transcript.write_text("", encoding="utf-8")
                else:
                    transcript.unlink()
                recovered = state.result(pending.request_id)
                self.assertEqual(recovered.status, "error")
                self.assertEqual(recovered.error["code"], "transcript_identity_changed")

    def test_invalid_conversation_references_and_root_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            for value in ("../escape", "nested/path", "nested\\path", "", "C:\\absolute"):
                with self.subTest(value=value), self.assertRaisesRegex(worker.SidecarFailure, "invalid_conversation_reference"):
                    state.send(value, "hello", None)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            outside = Path(directory) / "outside.jsonl"
            outside.write_text("", encoding="utf-8")
            outcome = state.stop({"conversationId":"conversation","fullyIdle":True,"transcriptPath":str(outside)})
            self.assertEqual(outcome, "error")
            self.assertEqual(pending.error["code"], "transcript_unavailable")

    def test_transcript_root_and_symlink_escape_are_rejected_without_path_disclosure(self):
        with patch("antigravity_adapter.sidecar.worker.discover_agentapi", return_value=("agentapi.exe", [], "direct-executable")):
            with self.assertRaisesRegex(worker.SidecarFailure, "transcript_root_invalid") as missing:
                worker.State(1, "private-root-that-does-not-exist")
        self.assertNotIn("private-root", str(missing.exception))

        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            outside = Path(directory) / "outside"
            outside.mkdir()
            with patch.object(type(state.transcript_root), "resolve", return_value=outside.resolve()):
                with self.assertRaisesRegex(worker.SidecarFailure, "invalid_conversation_reference"):
                    state._expected_transcript("conversation")

    def test_deadline_performs_one_final_refresh_before_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            self.write_records(transcript, self.final_records(pending, "at deadline"))
            pending.deadline = time.monotonic() - 1
            state.expire()
            self.assertEqual(pending.status, "completed")
            self.assertEqual(pending.text, "at deadline")

    def test_terminal_request_releases_global_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            first = self.dispatch(state, transcript)
            self.write_records(transcript, self.final_records(first))
            self.assertEqual(state.result(first.request_id).status, "completed")
            second = self.dispatch(state, transcript, "request-2")
            self.assertEqual(second.status, "pending")
            self.assertEqual(state.pending_id, "request-2")

    def test_polling_and_stop_each_win_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            self.write_records(transcript, self.final_records(pending, "poll wins"))
            self.assertEqual(state.result(pending.request_id).status, "completed")
            self.assertEqual(state.stop({"conversationId":"conversation","fullyIdle":True,"transcriptPath":str(transcript)}), "unknown_stop")
            self.assertEqual(pending.text, "poll wins")
            self.assertEqual(pending.recovery_source, "polling")

        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            self.write_records(transcript, self.final_records(pending, "stop wins"))
            self.assertEqual(state.stop({"conversationId":"conversation","fullyIdle":True,"transcriptPath":str(transcript)}), "completed")
            self.assertEqual(state.result(pending.request_id).text, "stop wins")
            self.assertEqual(pending.recovery_source, "stop-hook")

    def test_missing_marker_remains_pending_and_busy_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            transcript.write_text("", encoding="utf-8")
            with patch("antigravity_adapter.sidecar.worker.subprocess.run") as run:
                run.return_value.returncode = 0
                pending = state.send("conversation", "hello", "request-1")
                with self.assertRaisesRegex(worker.SidecarFailure, "busy"):
                    state.send("conversation", "again", "request-2")
            transcript.write_text(json.dumps({"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","content":"guess"})+"\n", encoding="utf-8")
            outcome = state.stop({"conversationId":"conversation","fullyIdle":True,"error":None,"transcriptPath":str(transcript)})
        self.assertEqual(outcome, "pending")
        self.assertEqual(pending.status, "pending")

    def test_timeout_releases_busy_slot_and_late_stop_does_not_revive_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            pending.deadline = time.monotonic() - 1
            state.expire()
            transcript.write_text(
                json.dumps({"source":"MODEL","type":"PLANNER_RESPONSE","status":"DONE","content":"late"}) + "\n",
                encoding="utf-8",
            )
            outcome = state.stop({"conversationId":"conversation", "fullyIdle":True, "transcriptPath":str(transcript)})
        self.assertEqual(outcome, "unknown_stop")
        self.assertEqual(pending.status, "error")
        self.assertEqual(pending.error["code"], "timeout")

    def test_unmatched_stop_does_not_consume_current_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            outcome = state.stop({"conversationId":"different", "fullyIdle":True, "transcriptPath":str(transcript)})
        self.assertEqual(outcome, "unknown_stop")
        self.assertEqual(state.pending_id, pending.request_id)
        self.assertEqual(pending.status, "pending")

    def test_malformed_and_unreadable_transcripts_fail_closed(self):
        for malformed in (True, False):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as directory:
                state = self.make_state(directory)
                transcript = self.transcript(state)
                pending = self.dispatch(state, transcript)
                if malformed:
                    transcript.write_text("{not-json}\n", encoding="utf-8")
                    path = str(transcript)
                else:
                    path = str(Path(directory) / "missing.jsonl")
                outcome = state.stop({"conversationId":"conversation", "fullyIdle":True, "transcriptPath":path})
                self.assertEqual(outcome, "error")
                self.assertEqual(pending.status, "error")
                self.assertIsNone(pending.text)

    def test_sidecar_restart_cannot_complete_stale_request(self):
        with tempfile.TemporaryDirectory() as directory:
            old_state = self.make_state(directory)
            transcript = self.transcript(old_state)
            pending = self.dispatch(old_state, transcript)
            new_state = self.make_state(directory)
            outcome = new_state.stop({"conversationId":"conversation", "fullyIdle":True, "transcriptPath":str(transcript)})
        self.assertEqual(outcome, "unknown_stop")
        self.assertEqual(pending.status, "pending")
        self.assertNotIn("request-1", new_state.requests)

    def test_authenticated_headers_and_rotated_rendezvous_are_read_per_call(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return b'{"status":"ready"}'
        seen = []
        def fake_open(req, timeout):
            seen.append((req.get_header("Authorization"), req.get_header("X-sidecar-instance")))
            return Response()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            client = SidecarHttpClient(path)
            with patch("antigravity_adapter.transport.request.urlopen", fake_open):
                path.write_text(json.dumps({"host":"127.0.0.1","port":8765,"instanceId":"one","token":"first"}), encoding="utf-8")
                asyncio.run(client.health())
                path.write_text(json.dumps({"host":"127.0.0.1","port":8765,"instanceId":"two","token":"second"}), encoding="utf-8")
                asyncio.run(client.health())
        self.assertEqual(seen, [("Bearer first", "one"), ("Bearer second", "two")])

    def test_http_auth_failure_is_explicit_without_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps({"host":"127.0.0.1","port":8765,"instanceId":"instance","token":"secret"}), encoding="utf-8")
            client = SidecarHttpClient(path)
            failure = error.HTTPError("http://127.0.0.1", 401, "unauthorized", {}, None)
            failure.read = lambda: b'{"error":"unauthorized"}'
            with patch("antigravity_adapter.transport.request.urlopen", side_effect=failure):
                with self.assertRaises(SidecarRejected) as caught:
                    asyncio.run(client.health())
        self.assertEqual(str(caught.exception), "unauthorized")
        self.assertNotIn("secret", str(caught.exception))

    def test_http_server_enforces_bearer_and_instance_identity(self):
        class ReadyState:
            command_type = "test"
            pending_id = None
            requests = {}
            def expire(self): return None

        server = worker.Server(("127.0.0.1", 0), worker.Handler)
        server.state = ReadyState()
        server.token = "test-token"
        server.instance_id = "test-instance"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = f"http://127.0.0.1:{server.server_address[1]}/health"
        try:
            with self.assertRaises(error.HTTPError) as missing:
                urllib_request.urlopen(endpoint, timeout=2)
            self.assertEqual(missing.exception.code, 401)

            wrong_instance = urllib_request.Request(
                endpoint,
                headers={"Authorization":"Bearer test-token", "X-Sidecar-Instance":"stale"},
            )
            with self.assertRaises(error.HTTPError) as stale:
                urllib_request.urlopen(wrong_instance, timeout=2)
            self.assertEqual(stale.exception.code, 409)

            valid = urllib_request.Request(
                endpoint,
                headers={"Authorization":"Bearer test-token", "X-Sidecar-Instance":"test-instance"},
            )
            with urllib_request.urlopen(valid, timeout=2) as response:
                body = json.loads(response.read().decode("utf-8"))
            self.assertEqual(body["status"], "ready")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_http_result_endpoint_refreshes_real_transcript_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = self.make_state(directory)
            transcript = self.transcript(state)
            pending = self.dispatch(state, transcript)
            self.write_records(transcript, self.final_records(pending, "through http"))
            server = worker.Server(("127.0.0.1", 0), worker.Handler)
            server.state = state
            server.token = "test-token"
            server.instance_id = "test-instance"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/result/{pending.request_id}"
            req = urllib_request.Request(
                endpoint,
                headers={"Authorization":"Bearer test-token", "X-Sidecar-Instance":"test-instance"},
            )
            try:
                with urllib_request.urlopen(req, timeout=2) as response:
                    body = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
        self.assertEqual(body, {
            "requestId":"request-1",
            "status":"completed",
            "text":"through http",
            "recoverySource":"polling",
        })


@unittest.skipUnless(os.name == "nt", "Windows lifecycle contract")
class SidecarLifecycleTests(unittest.TestCase):
    def test_worker_supports_supervisor_direct_file_entrypoint(self):
        completed = subprocess.run(
            [sys.executable, worker.__file__, "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_named_mutex_rejects_duplicate_and_releases_after_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            name = lifecycle.mutex_name(Path(directory) / "runtime.json")
            first = lifecycle.WindowsMutex(name)
            try:
                with self.assertRaisesRegex(lifecycle.LifecycleFailure, "instance_conflict"):
                    lifecycle.WindowsMutex(name)
            finally:
                first.close()
            replacement = lifecycle.WindowsMutex(name)
            replacement.close()

    def test_mutex_identity_uses_canonical_windows_path(self):
        base = Path.cwd() / "mutex-fixture" / "runtime.json"
        equivalents = [
            base,
            Path(str(base).upper()),
            Path(str(base).replace("\\", "/")),
            base.parent / "." / "child" / ".." / base.name,
            Path(os.path.relpath(base, Path.cwd())),
        ]
        self.assertEqual(len({lifecycle.mutex_name(path) for path in equivalents}), 1)

    def test_exclusive_socket_rejects_existing_listener_without_terminating_it(self):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        try:
            with self.assertRaisesRegex(lifecycle.LifecycleFailure, "port_conflict"):
                worker.create_server(listener.getsockname()[1])
            self.assertIsNotNone(listener.getsockname())
        finally:
            listener.close()

    def test_live_stale_and_ambiguous_rendezvous_fail_closed_untouched(self):
        class ReadyState:
            command_type = "test"
            pending_id = None
            def expire(self): return None

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            server = worker.Server(("127.0.0.1", 0), worker.Handler)
            server.state = ReadyState()
            server.token = "token"
            server.instance_id = "instance"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            live = json.dumps({"host":"127.0.0.1", "port":server.server_address[1], "instanceId":"instance", "token":"token"})
            path.write_text(live, encoding="utf-8")
            try:
                with self.assertRaisesRegex(lifecycle.LifecycleFailure, "live_instance_conflict"):
                    lifecycle.classify_existing_rendezvous(path)
                self.assertEqual(path.read_text(encoding="utf-8"), live)

                mismatch = json.dumps({"host":"127.0.0.1", "port":server.server_address[1], "instanceId":"instance", "token":"wrong"})
                path.write_text(mismatch, encoding="utf-8")
                with self.assertRaisesRegex(lifecycle.LifecycleFailure, "rendezvous_conflict"):
                    lifecycle.classify_existing_rendezvous(path)
                self.assertEqual(path.read_text(encoding="utf-8"), mismatch)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=2)

            probe = socket.socket(); probe.bind(("127.0.0.1", 0)); stale_port = probe.getsockname()[1]; probe.close()
            stale = json.dumps({"host":"127.0.0.1", "port":stale_port, "instanceId":"old", "token":"old"})
            path.write_text(stale, encoding="utf-8")
            refused = error.URLError(ConnectionRefusedError(10061, "refused"))
            with patch("antigravity_adapter.sidecar.lifecycle.urllib.request.urlopen", side_effect=refused):
                with self.assertRaisesRegex(lifecycle.LifecycleFailure, "stale_rendezvous"):
                    lifecycle.classify_existing_rendezvous(path)
            self.assertEqual(path.read_text(encoding="utf-8"), stale)

            path.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(lifecycle.LifecycleFailure, "rendezvous_conflict"):
                lifecycle.classify_existing_rendezvous(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "{}")

    def test_cleanup_requires_both_instance_and_token_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            replacement = {"instanceId":"new", "token":"new"}
            path.write_text(json.dumps(replacement), encoding="utf-8")
            lifecycle.remove_owned_rendezvous(path, "old", "old")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), replacement)
            lifecycle.remove_owned_rendezvous(path, "new", "new")
            self.assertFalse(path.exists())

    def test_atomic_publication_loses_race_without_overwriting_foreign_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            original_move = lifecycle._move_no_replace

            def competing_move(source, destination):
                destination.write_text("foreign", encoding="utf-8")
                return original_move(source, destination)

            with patch("antigravity_adapter.sidecar.lifecycle._move_no_replace", side_effect=competing_move):
                with self.assertRaisesRegex(lifecycle.LifecycleFailure, "rendezvous_conflict"):
                    lifecycle.publish_exclusive(path, "owned")
            self.assertEqual(path.read_text(encoding="utf-8"), "foreign")
            self.assertEqual(list(path.parent.glob("runtime.json.*.tmp")), [])

    def test_cleanup_removes_owned_artifact_before_releasing_mutex(self):
        events = []
        class Server:
            instance_id = "instance"
            token = "token"
            def server_close(self): events.append("server_closed")
        class Resource:
            def __init__(self, name): self.name = name
            def close(self): events.append(self.name)
        owner = lifecycle.Lifecycle(Resource("mutex_released"), Resource("parent_resources_closed"))
        with patch("antigravity_adapter.sidecar.worker.remove_owned_rendezvous", side_effect=lambda *_: events.append("rendezvous_cleanup_completed")):
            worker.cleanup(Server(), Path("unused"), owner)
        self.assertEqual(events, ["server_closed", "rendezvous_cleanup_completed", "parent_resources_closed", "mutex_released"])

    def test_partial_startup_cleanup_touches_only_owned_resources(self):
        events = []
        class Resource:
            def __init__(self, name): self.name = name
            def close(self): events.append(self.name)
        owner = lifecycle.Lifecycle(Resource("mutex"), Resource("parent"))
        with tempfile.TemporaryDirectory() as directory:
            foreign = Path(directory) / "runtime.json"
            foreign.write_text("foreign", encoding="utf-8")
            worker.cleanup(None, foreign, owner)
            self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign")
        self.assertEqual(events, ["parent", "mutex"])

    def test_parent_handle_signals_original_process_object(self):
        process = subprocess.Popen(["powershell", "-NoProfile", "-Command", "Start-Sleep -Milliseconds 300"])
        observed = threading.Event()
        handle = lifecycle.ParentHandle(process.pid, _worker_created=(1 << 64) - 1)
        try:
            handle.watch(observed.set)
            process.wait(timeout=3)
            self.assertTrue(observed.wait(2))
        finally:
            handle.close()
            if process.poll() is None: process.terminate()

    def test_reused_parent_pid_is_rejected_and_opened_handle_closed(self):
        kernel32 = MagicMock()
        kernel32.OpenProcess.return_value = 123
        with patch("antigravity_adapter.sidecar.lifecycle._creation_time", return_value=200):
            with self.assertRaisesRegex(lifecycle.LifecycleFailure, "parent_watch_unavailable"):
                lifecycle.ParentHandle(42, _kernel32=kernel32, _worker_created=100)
        kernel32.CloseHandle.assert_called_once_with(123)
        kernel32.CreateEventW.assert_not_called()

    def test_normal_close_cancels_and_joins_active_parent_waiter(self):
        called = threading.Event()
        handle = lifecycle.ParentHandle(os.getppid())
        thread = handle.watch(called.set)
        handle.close()
        self.assertFalse(thread.is_alive())
        self.assertFalse(called.is_set())

    def test_watcher_start_failure_keeps_cleanup_safe(self):
        handle = lifecycle.ParentHandle(os.getppid())
        with patch("antigravity_adapter.sidecar.lifecycle.threading.Thread.start", side_effect=RuntimeError("start failed")):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                handle.watch(lambda: None)
        handle.close()

    def test_parent_death_and_cleanup_race_joins_before_handle_close(self):
        process = subprocess.Popen(["powershell", "-NoProfile", "-Command", "Start-Sleep -Milliseconds 150"])
        entered, release = threading.Event(), threading.Event()
        handle = lifecycle.ParentHandle(process.pid, _worker_created=(1 << 64) - 1)
        def callback():
            entered.set(); release.wait(2)
        watcher = handle.watch(callback)
        process.wait(timeout=3)
        self.assertTrue(entered.wait(2))
        closer = threading.Thread(target=handle.close)
        closer.start()
        self.assertTrue(closer.is_alive())
        release.set(); closer.join(2)
        self.assertFalse(closer.is_alive())
        self.assertFalse(watcher.is_alive())

    def test_parent_watch_unavailable_fails_closed_and_cleanup_is_idempotent(self):
        with patch("antigravity_adapter.sidecar.lifecycle.ctypes.WinDLL") as dll:
            dll.return_value.OpenProcess.return_value = 0
            with self.assertRaisesRegex(lifecycle.LifecycleFailure, "parent_watch_unavailable"):
                lifecycle.ParentHandle(123)

        class Resource:
            def __init__(self): self.closed = 0
            def close(self): self.closed += 1
        mutex, parent = Resource(), Resource()
        owner = lifecycle.Lifecycle(mutex, parent)
        owner.close(); owner.close()
        self.assertEqual((mutex.closed, parent.closed), (1, 1))

    def test_production_lifecycle_never_kills_or_enumerates_processes(self):
        source = Path(lifecycle.__file__).read_text(encoding="utf-8")
        for forbidden in ("taskkill", "TerminateProcess", "Win32_Process", "python.exe"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__": unittest.main()
