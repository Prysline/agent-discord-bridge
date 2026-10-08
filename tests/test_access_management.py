import json
import tempfile
import unittest
from pathlib import Path

from agent_bridge.access_management import access_view, save_access_config, update_access_config


def base_config():
    return {
        "dmPolicy": "allowlist", "allowFrom": ["123456789012345678"],
        "conversation": {"contextMessages": 12, "maxPeerTurns": 2},
        "sharedDiscussion": {"globalMaxDispatches": 2, "participants": []},
        "channels": {
            "223456789012345678": {"name": "Room A", "requireMention": True,
                "allowFrom": ["123456789012345678"], "allowBotMention": True,
                "allowBotFrom": ["323456789012345678"]}
        },
        "accessLabels": {"users": {"123456789012345678": "Owner"}, "bots": {"323456789012345678": "Peer"}},
    }


class AccessManagementTests(unittest.TestCase):
    def test_named_view_and_update_preserve_peer_bot_policy(self):
        current = base_config(); view = access_view(current)
        self.assertEqual(view["users"][0], {"userId": "123456789012345678", "label": "Owner"})
        candidate = {
            "dmPolicy": "disabled",
            "users": [{"userId": "123456789012345678", "label": "Primary user"}],
            "channels": [{"channelId": "223456789012345678", "label": "Renamed room",
                          "requireMention": False, "allowedUserIds": ["123456789012345678"]}],
        }
        updated = update_access_config(current, candidate)
        self.assertEqual(updated["allowFrom"], [])
        self.assertEqual(updated["channels"]["223456789012345678"]["name"], "Renamed room")
        self.assertEqual(updated["channels"]["223456789012345678"]["allowBotFrom"], ["323456789012345678"])
        self.assertEqual(updated["accessLabels"]["users"]["123456789012345678"], "Primary user")
        self.assertEqual(updated["accessLabels"]["bots"]["323456789012345678"], "Peer")

    def test_unknown_user_empty_channel_and_duplicate_ids_fail_closed(self):
        for candidate, message in [
            ({"dmPolicy":"disabled","users":[{"userId":"123456789012345678","label":"A"}],"channels":[]}, "至少需要一個"),
            ({"dmPolicy":"disabled","users":[{"userId":"123456789012345678","label":"A"}],"channels":[{"channelId":"223456789012345678","label":"R","allowedUserIds":["999999999999999999"]}]}, "未知使用者"),
            ({"dmPolicy":"disabled","users":[{"userId":"123456789012345678","label":"A"},{"userId":"123456789012345678","label":"B"}],"channels":[{"channelId":"223456789012345678","label":"R","allowedUserIds":["123456789012345678"]}]}, "重複"),
        ]:
            with self.assertRaisesRegex(ValueError, message): update_access_config(base_config(), candidate)

    def test_require_mention_rejects_non_boolean_values(self):
        candidate = {
            "dmPolicy": "disabled",
            "users": [{"userId": "123456789012345678", "label": "A"}],
            "channels": [{"channelId": "223456789012345678", "label": "R",
                          "requireMention": "false", "allowedUserIds": ["123456789012345678"]}],
        }
        with self.assertRaisesRegex(ValueError, "布林值"):
            update_access_config(base_config(), candidate)

    def test_atomic_save_round_trips_unicode_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"; value = base_config()
            value["accessLabels"]["users"]["123456789012345678"] = "主要使用者"
            save_access_config(path, value)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), value)


if __name__ == "__main__": unittest.main()
