import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib import error, request

from agent_bridge import setup_ui
from agent_bridge.agent_management import load_agent_management


def valid_setup():
    return {
        "token": "private-test-token", "botUserId": "123456789012345678",
        "humanUserId": "223456789012345678", "humanLabel": "Owner",
        "channelId": "323456789012345678",
        "channelName": "private-room", "agentId": "agent-one", "agentAlias": "assistant",
        "displayName": "Primary Agent", "senderId": "primary-bot", "budgetChars": 2000,
        "maxCalls": 5, "model": "test-model", "cwd": "shared_workspace", "persona": "私人助理。",
    }


class SetupUiTests(unittest.TestCase):
    def test_first_run_writes_consistent_minimal_codex_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup_ui.save_first_run(root, valid_setup())
            self.assertEqual(set(path.name for path in root.iterdir()), set(setup_ui.TARGETS))
            env = (root / ".env").read_text(encoding="utf-8")
            config = json.loads((root / "config.json").read_text(encoding="utf-8"))
            bindings = json.loads((root / "bindings.local.json").read_text(encoding="utf-8"))
            managed = load_agent_management(root / "agent-management.local.json", {}, "", 0)
            self.assertIn("SHARED_AGENT_ID=agent-one", env)
            self.assertIn("DISCORD_TOKEN=private-test-token", env)
            self.assertIn("ROOT_BOT_LABEL=Discord Bridge", env)
            self.assertIn("LEGACY_AGENT_DISPLAY_NAME=Primary Agent", env)
            self.assertIn("AGENT_ADMIN_PORT=8766", env)
            self.assertNotIn("BOT_DISPLAY_NAME=", env)
            self.assertEqual(config["sharedDiscussion"]["participants"][0]["agentId"], "agent-one")
            self.assertEqual(config["channels"]["323456789012345678"]["allowFrom"], ["223456789012345678"])
            self.assertEqual(config["accessLabels"]["users"]["223456789012345678"], "Owner")
            self.assertEqual(managed.agents[0].mention_id, "123456789012345678")
            self.assertEqual(managed.senders[0].token, "private-test-token")
            self.assertEqual(bindings["bindingLineages"], [])

    def test_invalid_alias_or_id_is_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); raw = valid_setup(); raw["agentAlias"] = "中文"
            with self.assertRaisesRegex(ValueError, "agentAlias"):
                setup_ui.save_first_run(root, raw)
            self.assertEqual(list(root.iterdir()), [])
            raw = valid_setup(); raw["channelId"] = "not-an-id"
            with self.assertRaisesRegex(ValueError, "channelId"):
                setup_ui.save_first_run(root, raw)
            self.assertEqual(list(root.iterdir()), [])

    def test_env_fields_cannot_inject_additional_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); raw = valid_setup(); raw["model"] = "safe\nSHARED_CORE_ENABLED=false"
            with self.assertRaisesRegex(ValueError, "model: 不得包含換行"):
                setup_ui.save_first_run(root, raw)
            self.assertEqual(list(root.iterdir()), [])

    def test_existing_local_configuration_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / ".env").write_text("KEEP=1\n", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "拒絕覆寫"):
                setup_ui.save_first_run(root, valid_setup())
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "KEEP=1\n")
            self.assertFalse((root / "config.json").exists())

    def test_http_setup_masks_state_and_enforces_csrf_and_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); server = setup_ui.create_server(root, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                state = json.loads(request.urlopen(base + "/api/state").read())
                self.assertEqual(state, {"configured": False, "existing": []})
                self.assertNotIn("private-test-token", setup_ui.HTML)
                body = json.dumps(valid_setup()).encode()
                bad = request.Request(base + "/api/setup", data=body, method="POST", headers={"Content-Type": "application/json"})
                with self.assertRaises(error.HTTPError) as caught: request.urlopen(bad)
                self.assertEqual(caught.exception.code, 403)
                good = request.Request(base + "/api/setup", data=body, method="POST", headers={
                    "Content-Type": "application/json", "Origin": f"http://localhost:{server.server_port}",
                    "X-CSRF-Token": server.csrf_token,
                })
                self.assertEqual(json.loads(request.urlopen(good).read()), {"saved": True})
                state = json.loads(request.urlopen(base + "/api/state").read())
                self.assertTrue(state["configured"]); self.assertNotIn("token", state)
            finally:
                server.shutdown(); server.server_close(); thread.join(2)


if __name__ == "__main__": unittest.main()
