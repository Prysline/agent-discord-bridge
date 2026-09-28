import asyncio
import json
import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from agent_bridge.binding_control import BindingControlState, BindingGenerationRecord, BindingLineage, BindingRef
from codex_adapter.binding import BindingGenerationMismatch, BindingUnavailable
from codex_adapter.binding_bootstrap import (
    bootstrap_existing_bindings,
    load_existing_binding_bootstrap,
)


def config():
    return {
        "logicalBindings": [
            {"roomId": "room-a", "agentId": "agent-a", "bindingId": "binding-a", "generations": [3, 7], "activeGeneration": 7},
            {"roomId": "room-a", "agentId": "agent-b", "bindingId": "binding-b", "generations": [4], "activeGeneration": 4},
        ],
        "codexBindings": [
            {"bindingId": "binding-a", "generation": 7, "threadId": "thread-a"},
            {"bindingId": "binding-b", "generation": 4, "threadId": "thread-b"},
        ],
    }


class BindingBootstrapTests(unittest.TestCase):
    def test_gitignored_file_loader_builds_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            path.write_text(json.dumps(config()), encoding="utf-8")
            value = load_existing_binding_bootstrap(path)

        resolved = asyncio.run(value.binding_resolver.resolve("binding-a", 7))
        self.assertEqual(resolved.thread_id, "thread-a")

    def test_valid_config_builds_logical_state_and_exact_resolver(self):
        value = bootstrap_existing_bindings(config())
        self.assertIsInstance(value.binding_control, BindingControlState)
        resolved = asyncio.run(value.binding_resolver.resolve("binding-a", 7))
        self.assertEqual((resolved.binding_id, resolved.generation, resolved.thread_id), ("binding-a", 7, "thread-a"))
        other = asyncio.run(value.binding_resolver.resolve("binding-b", 4))
        self.assertEqual(other.thread_id, "thread-b")

    def test_missing_active_native_mapping_fails(self):
        raw = config()
        raw["codexBindings"] = raw["codexBindings"][1:]
        with self.assertRaisesRegex(ValueError, "missing"):
            bootstrap_existing_bindings(raw)

    def test_native_mapping_must_reference_known_logical_pair(self):
        for binding_id, generation in (("missing", 7), ("binding-a", 99)):
            raw = config()
            raw["codexBindings"][0].update(bindingId=binding_id, generation=generation)
            with self.subTest(binding_id=binding_id, generation=generation):
                with self.assertRaisesRegex(ValueError, "unknown"):
                    bootstrap_existing_bindings(raw)

    def test_duplicate_exact_native_mapping_fails(self):
        raw = config()
        raw["codexBindings"].append(dict(raw["codexBindings"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            bootstrap_existing_bindings(raw)

    def test_duplicate_logical_owner_uses_foundation_invariant(self):
        raw = config()
        duplicate = dict(raw["logicalBindings"][1])
        duplicate.update(roomId="room-a", agentId="agent-a", bindingId="binding-c")
        raw["logicalBindings"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "only one active binding"):
            bootstrap_existing_bindings(raw)

    def test_malformed_or_empty_identifiers_fail(self):
        for field, value in (("roomId", ""), ("agentId", " "), ("bindingId", None)):
            raw = config()
            raw["logicalBindings"][0][field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "non-empty"):
                    bootstrap_existing_bindings(raw)
        raw = config()
        raw["codexBindings"][0]["threadId"] = ""
        with self.assertRaisesRegex(ValueError, "non-empty"):
            bootstrap_existing_bindings(raw)

    def test_wrong_generation_never_falls_back(self):
        resolver = bootstrap_existing_bindings(config()).binding_resolver
        with self.assertRaises(BindingGenerationMismatch):
            asyncio.run(resolver.resolve("binding-a", 3))
        with self.assertRaises(BindingUnavailable):
            asyncio.run(resolver.resolve("unknown", 7))

    def test_shared_types_keep_native_session_out(self):
        self.assertEqual({field.name for field in fields(BindingRef)}, {"binding_id", "generation"})
        self.assertEqual({field.name for field in fields(BindingGenerationRecord)}, {"generation"})
        self.assertEqual({field.name for field in fields(BindingLineage)}, {"binding_id", "room_id", "agent_id", "generations"})
        self.assertFalse(any("thread" in name.lower() for name in vars(BindingControlState)))


if __name__ == "__main__":
    unittest.main()
