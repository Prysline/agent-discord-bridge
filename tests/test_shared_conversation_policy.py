import unittest

from agent_bridge.conversation_policy import ConversationPolicy, Event, Participant


def participant(agent_id, mention_id, **changes):
    values = dict(budget_chars=100, max_calls=3, enabled=True, available=True)
    values.update(changes)
    return Participant(agent_id, mention_id, **values)


def policy(participants=None, *, global_max=20):
    return ConversationPolicy(
        participants
        or [participant("a", "101"), participant("b", "102"), participant("c", "103")],
        global_max_dispatches=global_max,
    )


def human(content, channel="room"):
    return Event(channel, "human", False, content)


def peer(content="<@102> next", channel="room"):
    return Event(channel, "peer", True, content)


def start(value, content="!discuss <@101> <@102> -- goal"):
    return value.handle_event(human(content))


def finish(value, channel, token, *, status, text="", delivery="delivered"):
    value.record_invocation_started(channel, token)
    recorded = value.record_result(channel, token, status=status, text=text)
    if not recorded.accepted:
        return recorded
    if status == "abstain":
        return recorded
    return value.resolve_delivery(channel, token, delivery=delivery)


class SharedConversationPolicyTests(unittest.TestCase):
    def test_valid_discuss_uses_mention_order_and_first_speaker(self):
        value = policy()
        result = start(value, "!discuss <@103> <@101>\nreview restart")
        self.assertTrue(result.accepted)
        self.assertEqual(value.state("room").participants, ("c", "a"))
        self.assertEqual(value.state("room").goal, "review restart")
        self.assertEqual(value.next_dispatch("room").agent_id, "c")

    def test_goal_mentions_do_not_expand_participants(self):
        cases = [
            ("!discuss <@101> <@102>\n<@103> should answer this?", "<@103> should answer this?"),
            ("!discuss <@101> <@102>\ncompare <@103> with <@104>", "compare <@103> with <@104>"),
            ("!discuss <@101> <@102> -- ask <@103> about this", "ask <@103> about this"),
        ]
        for command, goal in cases:
            with self.subTest(command=command):
                value = policy()
                self.assertTrue(start(value, command).accepted)
                self.assertEqual(value.state("room").participants, ("a", "b"))
                self.assertEqual(value.state("room").goal, goal)

    def test_empty_goal_is_rejected(self):
        self.assertEqual(
            start(policy(), "!discuss <@101> <@102>").reason,
            "goal must not be empty",
        )

    def test_fewer_than_two_participants_is_rejected(self):
        self.assertEqual(
            start(policy(), "!discuss <@101> -- goal").reason,
            "at least two participants required",
        )

    def test_duplicate_participant_is_rejected(self):
        self.assertEqual(
            start(policy(), "!discuss <@101> <@101> -- goal").reason,
            "duplicate participant",
        )

    def test_unknown_disabled_and_unavailable_reject_entire_start(self):
        cases = [
            (policy(), "!discuss <@101> <@999> -- goal", "unknown participant"),
            (
                policy([participant("a", "101"), participant("b", "102", enabled=False)]),
                "!discuss <@101> <@102> -- goal",
                "disabled participant",
            ),
            (
                policy([participant("a", "101"), participant("b", "102", available=False)]),
                "!discuss <@101> <@102> -- goal",
                "unavailable participant",
            ),
        ]
        for value, command, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(start(value, command).reason, reason)
                self.assertEqual(value.state("room").phase, "idle")

    def test_active_discussion_rejects_second_start(self):
        value = policy()
        start(value)
        result = start(value, "!discuss <@102> <@103> -- replacement")
        self.assertFalse(result.accepted)
        self.assertIn("!stop", result.reason)
        self.assertEqual(value.state("room").goal, "goal")

    def test_state_returns_detached_inspection_copy(self):
        value = policy()
        start(value)
        snapshot = value.state("room")
        snapshot.phase = "stopped"
        snapshot.quotas["a"].used_calls = 99
        self.assertEqual(value.state("room").phase, "active")
        self.assertEqual(value.state("room").quotas["a"].used_calls, 0)

    def test_round_robin_is_core_owned(self):
        value = policy()
        start(value, "!discuss <@102> <@101> <@103> -- goal")
        observed = []
        for _ in range(4):
            begun = value.begin_dispatch("room")
            observed.append(begun.agent_id)
            finish(value,
                "room", begun.token, status="continue", text="ok", delivery="delivered"
            )
        self.assertEqual(observed, ["b", "a", "c", "b"])

    def test_peer_discord_message_never_triggers_dispatch(self):
        value = policy()
        start(value)
        before = value.next_dispatch("room").agent_id
        result = value.handle_event(peer())
        self.assertFalse(result.accepted)
        self.assertEqual(result.action, "context-observed")
        self.assertEqual(value.next_dispatch("room").agent_id, before)

    def test_human_intervention_keeps_active_order(self):
        value = policy()
        start(value)
        first = value.begin_dispatch("room")
        finish(value,"room", first.token, status="continue", text="ok")
        result = value.handle_event(human("extra context <@101>"))
        self.assertEqual(result.action, "context-intervention")
        self.assertEqual(value.state("room").phase, "active")
        self.assertEqual(value.next_dispatch("room").agent_id, "b")

    def test_human_intervention_cancels_closing_check(self):
        value = policy()
        start(value)
        first = value.begin_dispatch("room")
        finish(value,"room", first.token, status="complete", text="done")
        self.assertEqual(value.state("room").phase, "closing-check")
        value.handle_event(human("one more fact"))
        self.assertEqual(value.state("room").phase, "active")
        self.assertEqual(value.state("room").closing_remaining, [])

    def test_dispatch_snapshot_does_not_live_inject_later_human_event(self):
        value = policy()
        start(value)
        begun = value.begin_dispatch("room")
        value.handle_event(human("arrived after dispatch"))
        self.assertLess(begun.token.context_revision, value.state("room").context_revision)

    def test_complete_enters_closing_check_in_round_robin_order(self):
        value = policy()
        start(value, "!discuss <@101> <@102> <@103> -- goal")
        begun = value.begin_dispatch("room")
        result = finish(value,"room", begun.token, status="complete", text="done")
        self.assertEqual(result.action, "closing-check")
        self.assertEqual(value.state("room").closing_remaining, ["b", "c"])

    def test_closing_continue_resumes_active_discussion(self):
        value = policy()
        start(value)
        first = value.begin_dispatch("room")
        finish(value,"room", first.token, status="complete", text="done")
        check = value.begin_dispatch("room")
        result = finish(value,"room", check.token, status="continue", text="objection")
        self.assertEqual(result.action, "closing-cancelled")
        self.assertEqual(value.state("room").phase, "active")

    def test_closing_complete_and_abstain_finish_when_no_continue(self):
        for status, text in [("complete", "agreed"), ("abstain", "")]:
            with self.subTest(status=status):
                value = policy()
                start(value)
                first = value.begin_dispatch("room")
                finish(value,"room", first.token, status="complete", text="done")
                check = value.begin_dispatch("room")
                result = finish(value,"room", check.token, status=status, text=text)
                self.assertEqual(result.action, "completed")
                self.assertEqual(value.state("room").phase, "completed")

    def test_three_participant_closing_finishes_after_mixed_no_objection_results(self):
        value = policy()
        start(value, "!discuss <@101> <@102> <@103> -- goal")
        opener = value.begin_dispatch("room")
        finish(value,"room", opener.token, status="complete", text="done")

        second = value.begin_dispatch("room")
        finish(value,"room", second.token, status="complete", text="agreed")
        self.assertEqual(value.state("room").phase, "closing-check")

        third = value.begin_dispatch("room")
        result = finish(value,"room", third.token, status="abstain")
        self.assertEqual(result.action, "completed")
        self.assertEqual(value.state("room").phase, "completed")

    def test_abstain_cannot_carry_text(self):
        value = policy()
        start(value)
        begun = value.begin_dispatch("room")
        with self.assertRaises(ValueError):
            finish(value,"room", begun.token, status="abstain", text="not empty")
        self.assertEqual(value.state("room").in_flight, begun.token)

    def test_active_abstain_advances_without_delivery(self):
        value = policy()
        start(value)
        begun = value.begin_dispatch("room")
        result = value.record_result("room", begun.token, status="abstain")
        state = value.state("room")
        self.assertEqual(result.action, "result-recorded")
        self.assertIsNone(state.pending_result)
        self.assertIsNone(state.in_flight)
        self.assertEqual(state.quotas[begun.agent_id].used_chars, 0)
        self.assertEqual(value.next_dispatch("room").agent_id, "b")

    def test_closing_abstain_advances_without_delivery(self):
        value = policy()
        start(value, "!discuss <@101> <@102> <@103> -- goal")
        opener = value.begin_dispatch("room")
        finish(value, "room", opener.token, status="complete", text="done")

        second = value.begin_dispatch("room")
        first_abstain = value.record_result("room", second.token, status="abstain")
        self.assertEqual(first_abstain.action, "closing-recorded")
        self.assertIsNone(value.state("room").pending_result)
        self.assertEqual(value.state("room").phase, "closing-check")

        third = value.begin_dispatch("room")
        final_abstain = value.record_result("room", third.token, status="abstain")
        self.assertEqual(final_abstain.action, "completed")
        self.assertIsNone(value.state("room").pending_result)
        self.assertEqual(value.state("room").phase, "completed")

    def test_abstain_never_requires_delivery_resolution(self):
        value = policy()
        start(value)
        begun = value.begin_dispatch("room")
        value.record_result("room", begun.token, status="abstain")
        result = value.resolve_delivery("room", begun.token, delivery="delivered")
        self.assertIn(result.action, {"ignored", "invalidated"})
        self.assertEqual(value.state("room").phase, "active")

    def test_per_agent_call_accounting_happens_on_invocation_signal_once(self):
        value = policy()
        start(value)
        begun = value.begin_dispatch("room")
        self.assertEqual(value.state("room").quotas[begun.agent_id].used_calls, 0)
        self.assertEqual(value.state("room").safety.dispatched_calls, 1)
        first = value.record_invocation_started("room", begun.token)
        duplicate = value.record_invocation_started("room", begun.token)
        self.assertEqual(first.action, "invocation-recorded")
        self.assertEqual(duplicate.action, "invocation-already-recorded")
        self.assertEqual(value.state("room").quotas[begun.agent_id].used_calls, 1)
        self.assertEqual(value.state("room").safety.dispatched_calls, 1)

    def test_only_last_speaker_eligible_suspends(self):
        value = policy([
            participant("a", "101", max_calls=1),
            participant("b", "102", max_calls=3),
        ])
        start(value)
        first = value.begin_dispatch("room")
        finish(value,"room", first.token, status="continue", text="a")
        second = value.begin_dispatch("room")
        finish(value,"room", second.token, status="continue", text="b")
        result = value.next_dispatch("room")
        self.assertEqual(result.action, "suspended")
        self.assertEqual(value.state("room").phase, "suspended")

    def test_quota_skip_order_for_three_and_four_participants(self):
        cases = [
            ([participant("a", "101", max_calls=1), participant("b", "102"), participant("c", "103")],
             "!discuss <@101> <@102> <@103> -- goal", ["a", "b", "c", "b"]),
            ([participant("a", "101", max_calls=1), participant("b", "102", max_calls=1),
              participant("c", "103"), participant("d", "104")],
             "!discuss <@101> <@102> <@103> <@104> -- goal", ["a", "b", "c", "d", "c"]),
        ]
        for participants, command, expected in cases:
            with self.subTest(expected=expected):
                value = policy(participants)
                start(value, command)
                observed = []
                for _ in expected:
                    begun = value.begin_dispatch("room")
                    observed.append(begun.agent_id)
                    finish(value, "room", begun.token, status="continue", text="ok")
                self.assertEqual(observed, expected)
                self.assertTrue(all(a != b for a, b in zip(observed, observed[1:])))

    def test_character_accounting_counts_only_confirmed_delivery(self):
        value = policy()
        start(value)
        first = value.begin_dispatch("room")
        finish(value,
            "room", first.token, status="continue", text="😀a", delivery="delivered"
        )
        second = value.begin_dispatch("room")
        finish(value,
            "room", second.token, status="continue", text="ignored", delivery="unknown"
        )
        state = value.state("room")
        self.assertEqual(state.quotas[first.agent_id].used_chars, 2)
        self.assertEqual(state.quotas[second.agent_id].used_chars, 0)

    def test_global_dispatch_limit_suspends_before_next_invocation(self):
        value = policy(global_max=1)
        start(value)
        begun = value.begin_dispatch("room")
        finish(value,"room", begun.token, status="continue", text="ok")
        result = value.begin_dispatch("room")
        self.assertEqual(result.action, "suspended")
        self.assertEqual(value.state("room").phase, "suspended")

    def test_legal_character_overshoot_is_not_truncated(self):
        value = policy([
            participant("a", "101", budget_chars=2),
            participant("b", "102"),
        ])
        start(value)
        begun = value.begin_dispatch("room")
        text = "完整回答"
        finish(value,
            "room", begun.token, status="continue", text=text, delivery="delivered"
        )
        self.assertEqual(value.state("room").quotas["a"].used_chars, len(text))
        second = value.begin_dispatch("room")
        self.assertEqual(second.agent_id, "b")
        finish(value,"room", second.token, status="continue", text="b")
        self.assertEqual(value.next_dispatch("room").action, "suspended")

    def test_stop_invalidates_in_flight_result(self):
        value = policy()
        start(value)
        begun = value.begin_dispatch("room")
        stopped = value.handle_event(human("!stop"))
        late = finish(value,"room", begun.token, status="continue", text="late")
        self.assertEqual(stopped.action, "stopped")
        self.assertEqual(late.action, "invalidated")
        self.assertEqual(value.state("room").phase, "stopped")

    def test_restart_turns_active_discussion_into_suspended(self):
        value = policy()
        start(value)
        value.on_process_restart()
        self.assertEqual(value.state("room").phase, "suspended")
        self.assertEqual(value.state("room").suspension_reason, "process restarted")

    def test_closing_continue_resumes_after_objecting_speaker(self):
        cases = [
            ("!discuss <@101> <@102> -- goal", [], "a"),
            ("!discuss <@101> <@102> <@103> -- goal", [], "c"),
            ("!discuss <@101> <@102> <@103> -- goal", ["complete"], "a"),
            ("!discuss <@101> <@102> <@103> <@104> -- goal", ["complete"], "d"),
            ("!discuss <@101> <@102> <@103> <@104> -- goal", ["complete", "abstain"], "a"),
        ]
        participants = [participant("a", "101"), participant("b", "102"), participant("c", "103"), participant("d", "104")]
        for command, prior, expected in cases:
            with self.subTest(command=command, prior=prior):
                value = policy(participants)
                start(value, command)
                opener = value.begin_dispatch("room")
                finish(value, "room", opener.token, status="complete", text="done")
                for status in prior:
                    check = value.begin_dispatch("room")
                    finish(value, "room", check.token, status=status, text="agreed" if status == "complete" else "")
                check = value.begin_dispatch("room")
                finish(value, "room", check.token, status="continue", text="objection")
                self.assertEqual(value.next_dispatch("room").agent_id, expected)

    def test_closing_quota_exhaustion_suspends(self):
        value = policy([participant("a", "101"), participant("b", "102", max_calls=1)])
        start(value)
        for text in ["a", "b"]:
            begun = value.begin_dispatch("room")
            finish(value, "room", begun.token, status="continue", text=text)
        begun = value.begin_dispatch("room")
        finish(value, "room", begun.token, status="complete", text="done")
        self.assertEqual(value.next_dispatch("room").action, "suspended")
        self.assertNotEqual(value.state("room").phase, "completed")

    def test_closing_failures_suspend(self):
        for reason in ["timeout", "adapter_error", "participant_unavailable"]:
            with self.subTest(reason=reason):
                value = policy()
                start(value)
                opener = value.begin_dispatch("room")
                finish(value, "room", opener.token, status="complete", text="done")
                check = value.begin_dispatch("room")
                result = value.fail_dispatch("room", check.token, reason=reason)
                self.assertEqual(result.action, "suspended")
                self.assertNotEqual(value.state("room").phase, "completed")

    def test_closing_global_hard_cap_suspends(self):
        value = policy(global_max=1)
        start(value)
        opener = value.begin_dispatch("room")
        finish(value, "room", opener.token, status="complete", text="done")
        self.assertEqual(value.next_dispatch("room").action, "suspended")

    def test_result_waits_for_confirmed_delivery(self):
        value = policy()
        start(value)
        begun = value.begin_dispatch("room")
        recorded = value.record_result("room", begun.token, status="complete", text="done")
        self.assertEqual(recorded.action, "delivery-pending")
        self.assertEqual(value.state("room").phase, "active")
        self.assertEqual(value.next_dispatch("room").action, "blocked")
        self.assertEqual(value.resolve_delivery("room", begun.token, delivery="unknown").action, "delivery-pending")
        self.assertEqual(value.state("room").phase, "active")
        self.assertEqual(value.resolve_delivery("room", begun.token, delivery="delivered").action, "closing-check")

    def test_not_delivered_suspends_without_semantic_transition(self):
        value = policy()
        start(value)
        begun = value.begin_dispatch("room")
        value.record_result("room", begun.token, status="complete", text="done")
        result = value.resolve_delivery("room", begun.token, delivery="not_delivered")
        state = value.state("room")
        self.assertEqual(result.action, "suspended")
        self.assertEqual(state.phase, "suspended")
        self.assertEqual(state.quotas[begun.agent_id].used_chars, 0)
        self.assertEqual(state.closing_remaining, [])

    def test_closing_human_intervention_invalidates_old_token(self):
        value = policy()
        start(value)
        opener = value.begin_dispatch("room")
        finish(value, "room", opener.token, status="complete", text="done")
        check = value.begin_dispatch("room")
        value.record_result("room", check.token, status="continue", text="objection")
        value.handle_event(human("new context"))
        self.assertEqual(value.resolve_delivery("room", check.token, delivery="delivered").action, "invalidated")
        self.assertEqual(value.state("room").phase, "active")

    def test_restart_phase_rules_and_stale_token(self):
        value = policy()
        start(value)
        opener = value.begin_dispatch("room")
        finish(value, "room", opener.token, status="complete", text="done")
        check = value.begin_dispatch("room")
        value.record_result("room", check.token, status="continue", text="objection")
        value.on_process_restart()
        self.assertEqual(value.state("room").phase, "suspended")
        self.assertEqual(value.resolve_delivery("room", check.token, delivery="delivered").action, "invalidated")

        for setup, expected in [(None, "idle"), ("completed", "completed"), ("stopped", "stopped")]:
            other = policy()
            if setup == "completed":
                start(other)
                first = other.begin_dispatch("room")
                finish(other, "room", first.token, status="complete", text="done")
                second = other.begin_dispatch("room")
                finish(other, "room", second.token, status="complete", text="agreed")
            elif setup == "stopped":
                start(other)
                other.handle_event(human("!stop"))
            other.on_process_restart()
            self.assertEqual(other.state("room").phase, expected)

    def test_stop_invalidates_pending_delivery(self):
        value = policy()
        start(value)
        begun = value.begin_dispatch("room")
        value.record_result("room", begun.token, status="continue", text="late")
        value.handle_event(human("!stop"))
        self.assertEqual(value.resolve_delivery("room", begun.token, delivery="delivered").action, "invalidated")
        self.assertEqual(value.state("room").phase, "stopped")


if __name__ == "__main__":
    unittest.main()
