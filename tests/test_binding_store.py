import json
import tempfile
import unittest
from pathlib import Path

from agent_bridge.binding_store import normalize_binding_document, save_binding_document


class BindingStoreTests(unittest.TestCase):
    def test_legacy_document_normalizes_without_identity_changes(self):
        raw = {
            "logicalBindings": [{"roomId": "room", "agentId": "a", "bindingId": "stable", "generations": [7], "activeGeneration": 7}],
            "codexBindings": [{"bindingId": "stable", "generation": 7, "threadId": "private"}],
            "antigravityBindings": [],
        }
        value = normalize_binding_document(raw)
        self.assertEqual(value["bindingLineages"], [{"bindingId": "stable", "agentId": "a", "generations": [7]}])
        self.assertEqual(value["activeBindings"][0]["activeGeneration"], 7)
        self.assertNotIn("logicalBindings", value)

    def test_atomic_save_writes_valid_current_schema(self):
        raw = {
            "bindingLineages": [{"bindingId": "detached", "agentId": "a", "generations": [4]}],
            "activeBindings": [],
            "codexBindings": [{"bindingId": "detached", "generation": 4, "threadId": "private"}],
            "antigravityBindings": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bindings.local.json"
            save_binding_document(path, raw)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved, normalize_binding_document(raw))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
