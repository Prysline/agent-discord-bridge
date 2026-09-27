import unittest

from agent_bridge.conversation_policy import ConversationPolicy, Event


CONFIG = {
    "dmPolicy": "allowlist",
    "allowFrom": ["100"],
    "conversation": {
        "defaultMode": "human-turn",
        "discussion": {
            "startCommand": "!discuss",
            "stopCommand": "!stop",
            "maxTurnsPerBot": 5,
            "maxTotalCharacters": 2000,
        },
    },
    "channels": {
        "300": {
            "mode": "human-turn",
            "requireMention": True,
            "allowFrom": ["100"],
            "allowBotFrom": ["200"],
        },
        "301": {
            "mode": "bounded-discussion",
            "requireMention": True,
            "allowFrom": ["100"],
            "allowBotFrom": ["200"],
        },
    },
}


def event(**changes):
    values = dict(
        channel_id="300",
        author_id="100",
        author_is_bot=False,
        bot_user_id="999",
        is_dm=False,
        content="hello",
        mention_ids=("999",),
    )
    values.update(changes)
    return Event(**values)


class SharedConversationPolicyTests(unittest.TestCase):
    def test_human_turn_rejects_peer_to_prevent_unbounded_agent_chatter(self):
        policy = ConversationPolicy(CONFIG)
        decision = policy.observe_and_decide(
            event(author_id="200", author_is_bot=True)
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "peer not allowed")

    def test_first_mentioned_bot_starts_bounded_discussion(self):
        policy = ConversationPolicy(CONFIG)
        first = policy.observe_and_decide(
            event(
                channel_id="301",
                content="!discuss topic",
                mention_ids=("999", "200"),
            )
        )
        waiting = ConversationPolicy(CONFIG).observe_and_decide(
            event(
                channel_id="301",
                content="!discuss topic",
                mention_ids=("200", "999"),
            )
        )
        self.assertTrue(first.allowed)
        self.assertFalse(waiting.allowed)
        self.assertEqual(waiting.reason, "waiting for first bot")

    def test_peer_requires_active_discussion_and_explicit_mention(self):
        policy = ConversationPolicy(CONFIG)
        peer = event(
            channel_id="301",
            author_id="200",
            author_is_bot=True,
            mention_ids=("999",),
        )
        self.assertFalse(policy.observe_and_decide(peer).allowed)
        policy.observe_and_decide(
            event(channel_id="301", content="!discuss topic", mention_ids=("999",))
        )
        self.assertTrue(policy.observe_and_decide(peer).allowed)

    def test_character_budget_stops_before_another_agent_reply(self):
        config = {
            **CONFIG,
            "conversation": {
                "defaultMode": "human-turn",
                "discussion": {
                    "maxTurnsPerBot": 5,
                    "maxTotalCharacters": 3,
                },
            },
        }
        policy = ConversationPolicy(config)
        policy.observe_and_decide(
            event(channel_id="301", content="!discuss topic", mention_ids=("999",))
        )
        decision = policy.observe_and_decide(
            event(
                channel_id="301",
                author_id="200",
                author_is_bot=True,
                content="abcd",
                mention_ids=("999",),
            )
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "discussion limit reached")

    def test_turn_budget_is_per_agent(self):
        policy = ConversationPolicy(CONFIG)
        policy.observe_and_decide(
            event(channel_id="301", content="!discuss topic", mention_ids=("999",))
        )
        own = event(
            channel_id="301",
            author_id="999",
            author_is_bot=True,
            content="turn",
            mention_ids=("200",),
        )
        for _ in range(5):
            policy.observe_and_decide(own)
        peer = event(
            channel_id="301",
            author_id="200",
            author_is_bot=True,
            content="next",
            mention_ids=("999",),
        )
        self.assertFalse(policy.observe_and_decide(peer).allowed)

    def test_allowed_human_can_stop_discussion(self):
        policy = ConversationPolicy(CONFIG)
        policy.observe_and_decide(
            event(channel_id="301", content="!discuss topic", mention_ids=("999",))
        )
        stopped = policy.observe_and_decide(
            event(channel_id="301", content="!stop", mention_ids=())
        )
        self.assertFalse(stopped.allowed)
        self.assertEqual(stopped.reason, "discussion stopped")


if __name__ == "__main__":
    unittest.main()
