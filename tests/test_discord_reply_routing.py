import unittest

from agent_bridge.discord_reply_routing import (
    ManagedBotReplyIndex,
    resolve_human_ingress_route,
    system_message,
)


class ManagedBotReplyIndexTests(unittest.TestCase):
    def test_unresolved_reply_uses_recorded_dedicated_bot_identity(self):
        index = ManagedBotReplyIndex({101, 202})
        index.remember(9001, 202)
        self.assertEqual(index.resolve(9001, None), 202)

    def test_unknown_unresolved_reply_fails_closed(self):
        index = ManagedBotReplyIndex({101, 202})
        self.assertIsNone(index.resolve(9999, None))

    def test_resolved_author_is_authoritative_and_history_is_bounded(self):
        index = ManagedBotReplyIndex({101, 202}, maximum=1)
        index.remember(1, 101)
        index.remember(2, 202)
        self.assertIsNone(index.resolve(1, None))
        self.assertEqual(index.resolve(2, None), 202)
        self.assertIsNone(index.resolve(2, 303))


class HumanIngressRouteTests(unittest.TestCase):
    def test_system_messages_are_visibly_distinct_from_agent_output(self):
        self.assertEqual(system_message("設定尚未完成。"), "[SYSTEM] 設定尚未完成。")
    def test_unknown_reply_without_explicit_mention_cannot_fall_back_to_default(self):
        route = resolve_human_ingress_route(
            (), has_reply=True, replied_bot_id=None, default_bot_id=101
        )
        self.assertEqual(route.failure, "unresolved_reply")
        self.assertIsNone(route.target_bot_id)
        self.assertFalse(route.allow_without_mention)

    def test_explicit_managed_bot_mention_overrides_unknown_reply_identity(self):
        route = resolve_human_ingress_route(
            (202,), has_reply=True, replied_bot_id=None, default_bot_id=101
        )
        self.assertEqual(route.target_bot_id, 202)
        self.assertIsNone(route.failure)
        self.assertFalse(route.allow_without_mention)

    def test_known_reply_routes_to_its_bot_without_requiring_a_mention(self):
        route = resolve_human_ingress_route(
            (), has_reply=True, replied_bot_id=202, default_bot_id=101
        )
        self.assertEqual(route.target_bot_id, 202)
        self.assertTrue(route.allow_without_mention)

    def test_non_reply_without_mention_preserves_default_bot_routing(self):
        route = resolve_human_ingress_route(
            (), has_reply=False, replied_bot_id=None, default_bot_id=101
        )
        self.assertEqual(route.target_bot_id, 101)
        self.assertTrue(route.allow_without_mention)

    def test_multiple_managed_bot_mentions_are_rejected_before_selection(self):
        route = resolve_human_ingress_route(
            (101, 202), has_reply=False, replied_bot_id=None, default_bot_id=101
        )
        self.assertEqual(route.failure, "multiple_mentions")
        self.assertIsNone(route.target_bot_id)


if __name__ == "__main__":
    unittest.main()
