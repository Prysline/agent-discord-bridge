import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_bridge.binding_operations import (
    BindingAdminControl,
    BindingOperations,
    NativeCreatedUnmanaged,
)
from agent_bridge.conversation_policy import ConversationPolicy, Participant
from agent_bridge.orchestrator import BindingSnapshot, SharedOrchestrator
from agent_bridge.root_shared import RootSharedHumanTurn


class Adapter:
    capabilities = {"sessionMode": "persistent", "maxInFlight": 1, "canCancelInFlight": True}

    def __init__(self): self.requests = []
    async def execute(self, request, *, observer):
        self.requests.append(request)
        await observer.on_invocation_started(request["requestId"], "confirmed")
        return {"requestId": request["requestId"], "contextCommit": "committed", "status": "continue", "text": "ok"}
    async def cancel(self, request_id): return None


class Delivery:
    async def deliver(self, room_id, agent_id, text, request_id): return "delivered"


class NativeControl:
    adapter = "codex"
    mapping_field = "codexBindings"
    can_create = True

    def __init__(self, *, created="native-created", existing=None, validation_error=None):
        self.created = created
        self.existing = existing
        self.validation_error = validation_error
        self.calls = []
        self.published = []

    async def validate_existing(self, native_reference):
        self.calls.append(("validate", native_reference))
        if self.validation_error is not None:
            raise self.validation_error

    async def create(self, name=None):
        self.calls.append(("create", name))
        if isinstance(self.created, Exception):
            raise self.created
        return self.created

    def find_mapping(self, raw, native_reference):
        self.calls.append(("find", native_reference))
        return self.existing

    def mapping_entry(self, binding_id, generation, native_reference):
        return {"bindingId": binding_id, "generation": generation, "threadId": native_reference}

    def validate_publication(self, binding_id, generation, native_reference):
        self.calls.append(("validate-publication", binding_id, generation, native_reference))

    def publish(self, binding_id, generation, native_reference):
        self.calls.append(("publish", binding_id, generation, native_reference))
        self.published.append((binding_id, generation, native_reference))


def root(bindings):
    adapter = Adapter()
    core = SharedOrchestrator(
        policy=ConversationPolicy([Participant("a", "1", 100, 2)], global_max_dispatches=4),
        adapters={"a": adapter}, bindings=bindings, delivery=Delivery(),
    )
    return RootSharedHumanTurn(core, "a"), adapter


class BindingOperationsTests(unittest.TestCase):
    @staticmethod
    def pending(path, *, native=None, binding_id_factory=lambda: "logical-new"):
        bridge, adapter = root({})
        asyncio.run(bridge.handle(
            allowed=True, is_peer=False, room_id="new", author_id="h",
            display_name="H", text="first message", mentions_agent=True,
        ))
        control = BindingAdminControl(
            BindingOperations(path, bridge),
            adapter_types={"a": "codex"},
            display_names={"a": "Agent A"},
            native_controls={"a": native} if native else {},
            room_names={"new": "New room"},
            binding_id_factory=binding_id_factory,
        )
        return bridge, adapter, control

    @staticmethod
    def seed_document():
        return {
            "bindingLineages": [
                {"bindingId": "detached", "agentId": "a", "generations": [3]}
            ],
            "activeBindings": [],
            "codexBindings": [
                {"bindingId": "detached", "generation": 3, "threadId": "native-old"}
            ],
            "antigravityBindings": [],
        }

    def test_bind_existing_new_native_persists_publishes_then_resumes_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl()
            bridge, adapter, control = self.pending(path, native=native)

            result = asyncio.run(control.bind_existing("new", "a", "native-new"))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result.action, "handled")
            self.assertEqual(len(adapter.requests), 1)
            self.assertEqual(saved["activeBindings"][0]["bindingId"], "logical-new")
            self.assertEqual(saved["codexBindings"][-1]["threadId"], "native-new")
            self.assertEqual(native.published, [("logical-new", 1, "native-new")])
            self.assertNotEqual(saved["activeBindings"][0]["bindingId"], "native-new")
            self.assertEqual(bridge.pending_onboarding(), ())

    def test_bind_existing_managed_native_reuses_detached_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl(existing=("detached", 3))
            _, adapter, control = self.pending(path, native=native)

            asyncio.run(control.bind_existing("new", "a", "native-old"))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["activeBindings"][0]["bindingId"], "detached")
            self.assertEqual(saved["bindingLineages"], self.seed_document()["bindingLineages"])
            self.assertEqual(native.published, [])
            self.assertEqual(len(adapter.requests), 1)

    def test_bind_existing_managed_native_active_elsewhere_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            document = self.seed_document()
            document["activeBindings"] = [{
                "roomId": "old",
                "agentId": "a",
                "bindingId": "detached",
                "activeGeneration": 3,
            }]
            path.write_text(json.dumps(document), encoding="utf-8")
            native = NativeControl(existing=("detached", 3))
            _, adapter, control = self.pending(path, native=native)

            with self.assertRaisesRegex(ValueError, "active in only one room"):
                asyncio.run(control.bind_existing("new", "a", "native-old"))

            self.assertEqual(len(adapter.requests), 0)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)

    def test_create_requires_confirmation_then_registers_confirmed_native_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl(created="created-thread")
            _, adapter, control = self.pending(path, native=native)
            self.assertTrue(control.state()["pendingOnboarding"][0]["canCreate"])

            asyncio.run(control.create_pending("new", "a"))

            self.assertEqual(native.calls[0], ("create", "New room · Agent A"))
            self.assertEqual(len(adapter.requests), 1)
            self.assertEqual(native.published, [("logical-new", 1, "created-thread")])

    def test_native_control_can_offer_bind_existing_without_create(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl()
            native.can_create = False
            _, _, control = self.pending(path, native=native)

            pending = control.state()["pendingOnboarding"][0]

            self.assertTrue(pending["canBindExisting"])
            self.assertFalse(pending["canCreate"])

    def test_direct_bind_existing_creates_binding_without_human_event_or_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl()
            bridge, adapter = root({})
            control = BindingAdminControl(
                BindingOperations(path, bridge),
                adapter_types={"a": "codex"},
                display_names={"a": "Agent A"},
                native_controls={"a": native},
                enabled_agents={"a"},
                room_names={"channel": "Support"},
                binding_id_factory=lambda: "logical-direct",
            )

            asyncio.run(control.bind_existing_direct("channel", "a", "thread-direct"))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["activeBindings"][0]["roomId"], "channel")
            self.assertEqual(saved["codexBindings"][-1]["threadId"], "thread-direct")
            self.assertEqual(
                bridge.core.binding("channel", "a"),
                BindingSnapshot("logical-direct", 1),
            )
            self.assertEqual(bridge.core.log.events("channel"), ())
            self.assertEqual(adapter.requests, [])
            self.assertEqual(bridge.pending_onboarding(), ())

            result = asyncio.run(bridge.handle(
                allowed=True, is_peer=False, room_id="channel", author_id="h",
                display_name="H", text="first real message", mentions_agent=True,
            ))
            events = bridge.core.log.events("channel")
            self.assertEqual(result.action, "handled")
            self.assertEqual(len(adapter.requests), 1)
            self.assertEqual(events[0].author_type, "human")
            self.assertEqual(events[0].text, "first real message")

    def test_direct_create_registers_confirmed_native_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl(created="created-direct")
            bridge, adapter = root({})
            control = BindingAdminControl(
                BindingOperations(path, bridge),
                adapter_types={"a": "codex"},
                display_names={"a": "Agent A"},
                native_controls={"a": native},
                enabled_agents={"a"},
                room_names={"channel": "Support"},
                binding_id_factory=lambda: "logical-direct",
            )

            asyncio.run(control.create_direct("channel", "a"))

            self.assertEqual(native.calls[0], ("create", "Support · Agent A"))
            self.assertEqual(native.published, [("logical-direct", 1, "created-direct")])
            self.assertEqual(bridge.core.log.events("channel"), ())
            self.assertEqual(adapter.requests, [])

    def test_direct_binding_rejects_disabled_agent_and_pending_human_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl()
            bridge, adapter = root({})
            disabled = BindingAdminControl(
                BindingOperations(path, bridge),
                adapter_types={"a": "codex"}, display_names={"a": "Agent A"},
                native_controls={"a": native}, enabled_agents=set(),
            )
            with self.assertRaisesRegex(ValueError, "未啟用"):
                asyncio.run(disabled.bind_existing_direct("channel", "a", "thread"))

            asyncio.run(bridge.handle(
                allowed=True, is_peer=False, room_id="channel", author_id="h",
                display_name="H", text="retained human turn", mentions_agent=True,
            ))
            enabled = BindingAdminControl(
                BindingOperations(path, bridge),
                adapter_types={"a": "codex"}, display_names={"a": "Agent A"},
                native_controls={"a": native}, enabled_agents={"a"},
            )
            with self.assertRaisesRegex(ValueError, "pending onboarding"):
                asyncio.run(enabled.create_direct("channel", "a"))

            self.assertEqual(native.calls, [])
            self.assertEqual(adapter.requests, [])
            self.assertEqual(len(bridge.pending_onboarding()), 1)

    def test_created_native_persistence_failure_stays_unmanaged_and_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl(created="created-thread")
            bridge, adapter, control = self.pending(path, native=native)

            with patch("agent_bridge.binding_operations.save_binding_document", side_effect=OSError("disk full")):
                with self.assertRaises(NativeCreatedUnmanaged):
                    asyncio.run(control.create_pending("new", "a"))

            self.assertEqual(native.published, [])
            self.assertEqual(len(adapter.requests), 0)
            self.assertEqual(len(bridge.pending_onboarding()), 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), self.seed_document())

    def test_create_failure_does_not_persist_publish_or_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl(created=RuntimeError("ambiguous"))
            bridge, adapter, control = self.pending(path, native=native)

            with self.assertRaisesRegex(RuntimeError, "ambiguous"):
                asyncio.run(control.create_pending("new", "a"))

            self.assertEqual(native.published, [])
            self.assertEqual(len(adapter.requests), 0)
            self.assertEqual(len(bridge.pending_onboarding()), 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), self.seed_document())

    def test_stale_create_action_is_rejected_before_native_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(self.seed_document()), encoding="utf-8")
            native = NativeControl()
            bridge, _, control = self.pending(path, native=native)
            self.assertTrue(bridge.cancel_onboarding("new", "a"))

            with self.assertRaisesRegex(ValueError, "no pending onboarding"):
                asyncio.run(control.create_pending("new", "a"))

            self.assertEqual(native.calls, [])

    def test_admin_control_snapshot_hides_native_references(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps({
                "bindingLineages": [{"bindingId": "b", "agentId": "a", "generations": [3]}],
                "activeBindings": [],
                "codexBindings": [{"bindingId": "b", "generation": 3, "threadId": "private-thread"}],
                "antigravityBindings": [],
            }), encoding="utf-8")
            bridge, _ = root({})
            asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="new", author_id="h", display_name="H", text="private human text", mentions_agent=True))
            control = BindingAdminControl(
                BindingOperations(path, bridge),
                adapter_types={"a": "codex"}, display_names={"a": "Agent A"},
            )
            state = control.state()
            serialized = json.dumps(state)
            self.assertNotIn("private-thread", serialized)
            self.assertTrue(state["bindings"][0]["nativeMappingAvailable"])
            self.assertEqual(state["pendingOnboarding"][0]["roomId"], "new")

    def test_admin_control_serializes_attach_and_cancel(self):
        async def scenario(path, bridge):
            control = BindingAdminControl(
                BindingOperations(path, bridge),
                adapter_types={"a": "codex"}, display_names={"a": "Agent A"},
                native_controls={"a": NativeControl()},
            )
            result = await control.attach_pending("new", "a", "b", 3)
            self.assertEqual(result.action, "handled")
            self.assertFalse(await control.cancel_pending("new", "a"))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps({
                "bindingLineages": [{"bindingId": "b", "agentId": "a", "generations": [3]}],
                "activeBindings": [],
                "codexBindings": [{"bindingId": "b", "generation": 3, "threadId": "private"}],
                "antigravityBindings": [],
            }), encoding="utf-8")
            bridge, _ = root({})
            asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="new", author_id="h", display_name="H", text="hello", mentions_agent=True))
            asyncio.run(scenario(path, bridge))

    def test_attach_rejects_missing_wrong_or_unhealthy_native_mapping_before_change(self):
        cases = (
            ("missing", [], [], NativeControl(), "exactly one"),
            (
                "wrong-adapter",
                [],
                [{"bindingId": "b", "generation": 3, "conversationId": "conversation"}],
                NativeControl(),
                "exactly one",
            ),
            (
                "unhealthy",
                [{"bindingId": "b", "generation": 3, "threadId": "private"}],
                [],
                NativeControl(validation_error=ValueError("invalid native thread")),
                "invalid native thread",
            ),
        )
        for name, codex, antigravity, native, message in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bindings.local.json"
                document = {
                    "bindingLineages": [{"bindingId": "b", "agentId": "a", "generations": [3]}],
                    "activeBindings": [],
                    "codexBindings": codex,
                    "antigravityBindings": antigravity,
                }
                path.write_text(json.dumps(document), encoding="utf-8")
                bridge, adapter, control = self.pending(path, native=native)

                with self.assertRaisesRegex(ValueError, message):
                    asyncio.run(control.attach_pending("new", "a", "b", 3))

                self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)
                self.assertEqual(adapter.requests, [])
                self.assertEqual(len(bridge.pending_onboarding()), 1)

    def test_attach_rejects_duplicate_native_ownership_before_change(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            document = {
                "bindingLineages": [
                    {"bindingId": "b", "agentId": "a", "generations": [3]},
                    {"bindingId": "other", "agentId": "a", "generations": [1]},
                ],
                "activeBindings": [],
                "codexBindings": [
                    {"bindingId": "b", "generation": 3, "threadId": "private"},
                    {"bindingId": "other", "generation": 1, "threadId": "private"},
                ],
                "antigravityBindings": [],
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            bridge, adapter, control = self.pending(path, native=NativeControl())

            with self.assertRaisesRegex(ValueError, "duplicate mappings"):
                asyncio.run(control.attach_pending("new", "a", "b", 3))

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)
            self.assertEqual(adapter.requests, [])
            self.assertEqual(len(bridge.pending_onboarding()), 1)

    def test_move_rejects_missing_native_mapping_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            document = {
                "bindingLineages": [{"bindingId": "b", "agentId": "a", "generations": [3]}],
                "activeBindings": [{"roomId": "old", "agentId": "a", "bindingId": "b", "activeGeneration": 3}],
                "codexBindings": [],
                "antigravityBindings": [],
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            bridge, _ = root({("old", "a"): BindingSnapshot("b", 3)})
            control = BindingAdminControl(
                BindingOperations(path, bridge),
                adapter_types={"a": "codex"}, display_names={"a": "Agent A"},
                native_controls={"a": NativeControl()},
            )

            with self.assertRaisesRegex(ValueError, "exactly one"):
                asyncio.run(control.move("old", "new", "a"))

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)
            self.assertIsNotNone(bridge.core.binding("old", "a"))
            self.assertIsNone(bridge.core.binding("new", "a"))

    def test_room_move_persists_and_publishes_all_bindings_together(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            document = {
                "bindingLineages": [
                    {"bindingId": "ba", "agentId": "a", "generations": [1]},
                    {"bindingId": "bb", "agentId": "b", "generations": [2]},
                ],
                "activeBindings": [
                    {"roomId": "old", "agentId": "a", "bindingId": "ba", "activeGeneration": 1},
                    {"roomId": "old", "agentId": "b", "bindingId": "bb", "activeGeneration": 2},
                ],
                "codexBindings": [
                    {"bindingId": "ba", "generation": 1, "threadId": "ta"},
                    {"bindingId": "bb", "generation": 2, "threadId": "tb"},
                ], "antigravityBindings": [],
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            participants = [Participant("a", "1", 100, 2), Participant("b", "2", 100, 2)]
            core = SharedOrchestrator(policy=ConversationPolicy(participants, global_max_dispatches=4), adapters={"a":Adapter(),"b":Adapter()},
                bindings={("old","a"):BindingSnapshot("ba",1),("old","b"):BindingSnapshot("bb",2)}, delivery=Delivery())
            bridge = RootSharedHumanTurn(core, "a", participants)
            control = BindingAdminControl(BindingOperations(path, bridge), adapter_types={"a":"codex","b":"codex"},
                display_names={"a":"A","b":"B"}, native_controls={"a":NativeControl(),"b":NativeControl()})

            asyncio.run(control.move_room("old", "new"))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({entry["roomId"] for entry in saved["activeBindings"]}, {"new"})
            self.assertIsNone(core.binding("old", "a")); self.assertIsNone(core.binding("old", "b"))
            self.assertEqual(core.binding("new", "a"), BindingSnapshot("ba", 1))
            self.assertEqual(core.binding("new", "b"), BindingSnapshot("bb", 2))

    def test_room_move_failure_changes_neither_persistence_nor_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            document = {
                "bindingLineages": [{"bindingId":"ba","agentId":"a","generations":[1]},{"bindingId":"bb","agentId":"b","generations":[1]}],
                "activeBindings": [{"roomId":"old","agentId":"a","bindingId":"ba","activeGeneration":1},{"roomId":"old","agentId":"b","bindingId":"bb","activeGeneration":1}],
                "codexBindings": [{"bindingId":"ba","generation":1,"threadId":"ta"}], "antigravityBindings": [],
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            participants=[Participant("a","1",100,2),Participant("b","2",100,2)]
            core=SharedOrchestrator(policy=ConversationPolicy(participants,global_max_dispatches=4),adapters={"a":Adapter(),"b":Adapter()},bindings={("old","a"):BindingSnapshot("ba",1),("old","b"):BindingSnapshot("bb",1)},delivery=Delivery())
            bridge=RootSharedHumanTurn(core,"a",participants)
            control=BindingAdminControl(BindingOperations(path,bridge),adapter_types={"a":"codex","b":"codex"},display_names={},native_controls={"a":NativeControl(),"b":NativeControl()})
            with self.assertRaisesRegex(ValueError, "exactly one"):
                asyncio.run(control.move_room("old", "new"))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)
            self.assertIsNotNone(core.binding("old","a")); self.assertIsNotNone(core.binding("old","b"))
            self.assertIsNone(core.binding("new","a")); self.assertIsNone(core.binding("new","b"))

    def test_room_move_persistence_failure_does_not_publish_runtime_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            document = {
                "bindingLineages": [{"bindingId":"ba","agentId":"a","generations":[1]},{"bindingId":"bb","agentId":"b","generations":[1]}],
                "activeBindings": [{"roomId":"old","agentId":"a","bindingId":"ba","activeGeneration":1},{"roomId":"old","agentId":"b","bindingId":"bb","activeGeneration":1}],
                "codexBindings": [{"bindingId":"ba","generation":1,"threadId":"ta"},{"bindingId":"bb","generation":1,"threadId":"tb"}],
                "antigravityBindings": [],
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            participants=[Participant("a","1",100,2),Participant("b","2",100,2)]
            core=SharedOrchestrator(policy=ConversationPolicy(participants,global_max_dispatches=4),adapters={"a":Adapter(),"b":Adapter()},bindings={("old","a"):BindingSnapshot("ba",1),("old","b"):BindingSnapshot("bb",1)},delivery=Delivery())
            bridge=RootSharedHumanTurn(core,"a",participants)
            control=BindingAdminControl(BindingOperations(path,bridge),adapter_types={"a":"codex","b":"codex"},display_names={},native_controls={"a":NativeControl(),"b":NativeControl()})

            with patch("agent_bridge.binding_operations.save_binding_document", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    asyncio.run(control.move_room("old", "new"))

            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), document)
            self.assertIsNotNone(core.binding("old","a")); self.assertIsNotNone(core.binding("old","b"))
            self.assertIsNone(core.binding("new","a")); self.assertIsNone(core.binding("new","b"))

    def test_unbind_then_next_human_turn_enters_onboarding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            document = {
                "bindingLineages": [{"bindingId": "b", "agentId": "a", "generations": [3]}],
                "activeBindings": [{"roomId": "old", "agentId": "a", "bindingId": "b", "activeGeneration": 3}],
                "codexBindings": [{"bindingId": "b", "generation": 3, "threadId": "private"}],
                "antigravityBindings": [],
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            bridge, adapter = root({("old", "a"): BindingSnapshot("b", 3)})
            control = BindingAdminControl(
                BindingOperations(path, bridge),
                adapter_types={"a": "codex"}, display_names={"a": "Agent A"},
                native_controls={"a": NativeControl()},
            )

            asyncio.run(control.unbind("old", "a"))
            result = asyncio.run(bridge.handle(
                allowed=True, is_peer=False, room_id="old", author_id="h",
                display_name="H", text="after unbind", mentions_agent=True,
            ))

            self.assertEqual(result.action, "onboarding-required")
            self.assertEqual(adapter.requests, [])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bindingLineages"], document["bindingLineages"])
            self.assertEqual(saved["codexBindings"], document["codexBindings"])

    def test_persistence_failure_does_not_publish_or_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps({
                "bindingLineages": [{"bindingId": "b", "agentId": "a", "generations": [3]}],
                "activeBindings": [],
                "codexBindings": [{"bindingId": "b", "generation": 3, "threadId": "private"}],
                "antigravityBindings": [],
            }), encoding="utf-8")
            bridge, adapter = root({})
            asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="new", author_id="h", display_name="H", text="hello", mentions_agent=True))
            with patch("agent_bridge.binding_operations.save_binding_document", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    asyncio.run(BindingOperations(path, bridge).attach_detached("new", "a", "b", 3))
            self.assertIsNone(bridge.core.binding("new", "a"))
            self.assertEqual(len(adapter.requests), 0)
            self.assertEqual(len(bridge.pending_onboarding()), 1)

    def test_attach_persists_then_resumes_pending_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps({
                "bindingLineages": [{"bindingId": "b", "agentId": "a", "generations": [3]}],
                "activeBindings": [],
                "codexBindings": [{"bindingId": "b", "generation": 3, "threadId": "private"}],
                "antigravityBindings": [],
            }), encoding="utf-8")
            bridge, adapter = root({})
            asyncio.run(bridge.handle(allowed=True, is_peer=False, room_id="new", author_id="h", display_name="H", text="hello", mentions_agent=True))
            result = asyncio.run(BindingOperations(path, bridge).attach_detached("new", "a", "b", 3))
            self.assertEqual(result.action, "handled")
            self.assertEqual(len(adapter.requests), 1)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["activeBindings"][0]["roomId"], "new")

    def test_move_and_unbind_preserve_binding_identity_and_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            original = {
                "bindingLineages": [{"bindingId": "b", "agentId": "a", "generations": [3]}],
                "activeBindings": [{"roomId": "old", "agentId": "a", "bindingId": "b", "activeGeneration": 3}],
                "codexBindings": [{"bindingId": "b", "generation": 3, "threadId": "private"}],
                "antigravityBindings": [],
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            bridge, _ = root({("old", "a"): BindingSnapshot("b", 3)})
            operations = BindingOperations(path, bridge)
            operations.move("old", "new", "a")
            operations.unbind("new", "a")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["activeBindings"], [])
            self.assertEqual(saved["bindingLineages"][0]["bindingId"], "b")
            self.assertEqual(saved["bindingLineages"][0]["generations"], [3])
            self.assertEqual(saved["codexBindings"], original["codexBindings"])

    def test_busy_binding_rejects_before_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            original = {
                "bindingLineages": [
                    {"bindingId": "b", "agentId": "a", "generations": [3]},
                    {"bindingId": "c", "agentId": "b", "generations": [1]},
                ],
                "activeBindings": [
                    {"roomId": "old", "agentId": "a", "bindingId": "b", "activeGeneration": 3},
                    {"roomId": "old", "agentId": "b", "bindingId": "c", "activeGeneration": 1},
                ],
                "codexBindings": [
                    {"bindingId": "b", "generation": 3, "threadId": "private"},
                    {"bindingId": "c", "generation": 1, "threadId": "private-b"},
                ],
                "antigravityBindings": [],
            }
            path.write_text(json.dumps(original), encoding="utf-8")
            core = SharedOrchestrator(
                policy=ConversationPolicy([Participant("a", "1", 100, 2), Participant("b", "2", 100, 2)], global_max_dispatches=4),
                adapters={"a": Adapter(), "b": Adapter()},
                bindings={
                    ("old", "a"): BindingSnapshot("b", 3),
                    ("old", "b"): BindingSnapshot("c", 1),
                },
                delivery=Delivery(),
            )
            bridge = RootSharedHumanTurn(core, "a")
            self.assertEqual(core.ingest_human("old", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal").action, "started")
            operations = BindingOperations(path, bridge)
            with self.assertRaisesRegex(ValueError, "active work"):
                operations.move("old", "new", "a")
            with self.assertRaisesRegex(ValueError, "active work"):
                operations.unbind("old", "a")
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), original)
            self.assertIsNotNone(bridge.core.binding("old", "a"))


if __name__ == "__main__": unittest.main()
