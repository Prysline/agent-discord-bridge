import unittest

from conversation_policy import MessageEnvelope, PeerTurnLimiter, decide_access, validate_config


CONFIG = {
    "dmPolicy": "allowlist",
    "allowFrom": ["100"],
    "channels": {
        "300": {
            "requireMention": True,
            "allowFrom": ["100"],
            "allowBotMention": True,
            "allowBotFrom": ["200"],
        }
    },
}


def envelope(**overrides):
    values = {
        "author_id": "100",
        "author_is_bot": False,
        "bot_user_id": "999",
        "channel_id": "300",
        "is_dm": False,
        "mentions_bot": True,
        "replies_to_bot": False,
    }
    values.update(overrides)
    return MessageEnvelope(**values)


class ConversationPolicyTests(unittest.TestCase):
    def test_valid_config(self):
        validate_config(CONFIG)

    def test_channel_allowlist_is_required(self):
        bad = {**CONFIG, "channels": {"300": {"allowFrom": []}}}
        with self.assertRaises(ValueError):
            validate_config(bad)

    def test_allowed_human_must_mention_or_reply(self):
        self.assertTrue(decide_access(CONFIG, envelope()).allowed)
        self.assertTrue(
            decide_access(
                CONFIG,
                envelope(mentions_bot=False, replies_to_bot=True),
            ).allowed
        )
        self.assertFalse(
            decide_access(
                CONFIG,
                envelope(mentions_bot=False, replies_to_bot=False),
            ).allowed
        )

    def test_authorized_shared_control_does_not_require_extra_bot_mention(self):
        decision = decide_access(
            CONFIG,
            envelope(
                mentions_bot=False,
                replies_to_bot=False,
                is_control_command=True,
            ),
        )
        self.assertTrue(decision.allowed)
        self.assertFalse(
            decide_access(
                CONFIG,
                envelope(
                    author_id="101",
                    mentions_bot=False,
                    is_control_command=True,
                ),
            ).allowed
        )

    def test_unknown_human_is_rejected(self):
        self.assertFalse(decide_access(CONFIG, envelope(author_id="101")).allowed)

    def test_only_explicit_peer_bot_mentions_are_allowed(self):
        decision = decide_access(
            CONFIG,
            envelope(author_id="200", author_is_bot=True),
        )
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.is_peer)
        self.assertFalse(
            decide_access(
                CONFIG,
                envelope(author_id="201", author_is_bot=True),
            ).allowed
        )
        self.assertFalse(
            decide_access(
                CONFIG,
                envelope(
                    author_id="200",
                    author_is_bot=True,
                    mentions_bot=False,
                ),
            ).allowed
        )

    def test_peer_turns_stop_until_human_resets_them(self):
        limiter = PeerTurnLimiter(2)
        self.assertTrue(limiter.allow_peer("300"))
        self.assertTrue(limiter.allow_peer("300"))
        self.assertFalse(limiter.allow_peer("300"))
        limiter.note_human("300")
        self.assertTrue(limiter.allow_peer("300"))


if __name__ == "__main__":
    unittest.main()
