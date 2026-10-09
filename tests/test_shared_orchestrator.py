import asyncio
import unittest

from agent_bridge import ConversationPolicy, Participant
from agent_bridge.contracts import ContractError, validate_agent_result
from agent_bridge.orchestrator import BindingSnapshot, CanonicalLog, SharedOrchestrator
from codex_adapter.app_server import AppServerRpcError, TurnReference
from codex_adapter.binding import BindingUnavailable, ResolvedBinding
from codex_adapter.persistent import CodexPersistentAdapter


class FakeAdapter:
    capabilities = {"sessionMode": "persistent", "maxInFlight": 1, "canCancelInFlight": True}

    def __init__(self, status="continue", *, text="reply", context_commit="committed", order=None):
        self.status = status
        self.text = text
        self.context_commit = context_commit
        self.requests = []
        self.cancelled = []
        self.order = order
        self.entered = None
        self.release = None

    async def execute(self, request, *, observer):
        self.requests.append(request)
        if self.order is not None:
            self.order.append(request["agentId"])
        await observer.on_invocation_started(request["requestId"], "confirmed")
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        result = {
            "requestId": request["requestId"],
            "contextCommit": self.context_commit,
            "status": self.status,
        }
        if self.status in {"continue", "complete", "await-human"}:
            result["text"] = self.text
        if self.status == "error":
            result["error"] = {
                "code": "execution_error",
                "message": "failed",
                "retryable": False,
            }
        return result

    async def cancel(self, request_id):
        self.cancelled.append(request_id)


class FakeDelivery:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes) or ["delivered"]
        self.calls = []

    async def deliver(self, room_id, agent_id, text, request_id):
        self.calls.append((room_id, agent_id, text, request_id))
        return self.outcomes.pop(0) if self.outcomes else "delivered"


class BlockingDelivery(FakeDelivery):
    def __init__(self, outcome):
        super().__init__(outcome)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def deliver(self, room_id, agent_id, text, request_id):
        self.calls.append((room_id, agent_id, text, request_id))
        self.entered.set()
        await self.release.wait()
        return self.outcomes.pop(0)


class ExistingBindingResolver:
    async def resolve(self, binding_id, generation):
        return ResolvedBinding(binding_id, generation, "thread-placeholder")


class MissingBindingResolver:
    async def resolve(self, binding_id, generation):
        raise BindingUnavailable("missing")


class MinimalCodexClient:
    def __init__(self):
        self.request_id = None

    async def connect(self):
        return None

    async def reconnect(self):
        return None

    async def resume_thread(self, thread_id):
        return {"thread": {"id": thread_id}}

    async def start_turn(self, thread_id, text, client_user_message_id):
        self.request_id = client_user_message_id
        return TurnReference("turn-placeholder", "inProgress")

    async def wait_for_turn(self, turn_id, timeout_ms):
        return {
            "id": turn_id,
            "status": "completed",
            "items": [
                {"type": "userMessage", "clientId": self.request_id, "text": "redacted"},
                {"type": "agentMessage", "phase": "final_answer", "text": "Codex final"},
            ],
        }

    async def list_turns(self, thread_id):
        return []

    async def interrupt(self, thread_id, turn_id):
        return {}


def policy():
    return ConversationPolicy(
        [
            Participant("a", "1", 2000, 5),
            Participant("b", "2", 2000, 5),
            Participant("c", "3", 2000, 5),
        ],
        global_max_dispatches=12,
    )


class SharedOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def make_core(self, adapters=None, delivery=None, bindings=None, *, timeout_ms=120_000):
        ids = iter(f"id-{value}" for value in range(1, 100))
        requests = iter(f"req-{value}" for value in range(1, 100))
        adapters = adapters or {"a": FakeAdapter(), "b": FakeAdapter(), "c": FakeAdapter()}
        if bindings is None:
            bindings = {
                ("room", name): BindingSnapshot(f"binding-{name}", 1)
                for name in adapters
            }
        return SharedOrchestrator(
            policy=policy(),
            adapters=adapters,
            bindings=bindings,
            delivery=delivery or FakeDelivery(),
            timeout_ms=timeout_ms,
            display_names={"a": "A", "b": "B", "c": "C"},
            event_id_factory=lambda: next(ids),
            request_id_factory=lambda: next(requests),
        )

    def test_canonical_events_have_core_identity_and_strict_seq(self):
        log = CanonicalLog(iter(["first", "second"]).__next__)
        first = log.append_message(
            "room", author_type="human", author_id="h", display_name="H", text="one"
        )
        second = log.append_core("room", core_type="discussion_started", text="goal")
        self.assertEqual((first.event_id, second.event_id), ("first", "second"))
        self.assertEqual((first.seq, second.seq), (1, 2))

    def test_control_commands_are_not_ordinary_canonical_messages(self):
        core = self.make_core()
        started = core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- goal"
        )
        self.assertEqual(started.action, "started")
        self.assertEqual(len(core.log.events("room")), 1)
        self.assertEqual(core.log.events("room")[0].core_type, "discussion_started")

        core = self.make_core()
        rejected = core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> -- goal"
        )
        self.assertEqual(rejected.action, "rejected")
        self.assertEqual(core.log.events("room"), ())

        core = self.make_core()
        core.ingest_human("room", author_id="h", display_name="H", text="ordinary")
        self.assertEqual(len(core.log.events("room")), 1)
        self.assertEqual(core.log.events("room")[0].text, "ordinary")
        core.ingest_human("room", author_id="h", display_name="H", text="!stop")
        self.assertEqual(len(core.log.events("room")), 1)

    async def test_human_turn_dispatches_mentions_once_in_stable_order(self):
        order = []
        adapters = {name: FakeAdapter(order=order) for name in ("a", "b", "c")}
        core = self.make_core(adapters)
        core.ingest_human(
            "room", author_id="h", display_name="H", text="hello", mentioned_agents=("b", "a", "b")
        )
        outcomes = await core.run_human_turn("room", ("b", "a", "b"))
        self.assertEqual(order, ["b", "a"])
        self.assertEqual([item.action for item in outcomes], ["delivered", "delivered"])

    async def test_human_turn_second_agent_sees_first_confirmed_output(self):
        adapters = {"a": FakeAdapter(text="A delivered"), "b": FakeAdapter(text="B delivered")}
        core = self.make_core(adapters)
        core.ingest_human(
            "room", author_id="h", display_name="H", text="hello", mentioned_agents=("a", "b")
        )
        await core.run_human_turn("room", ("a", "b"))
        texts = [event["text"] for event in adapters["b"].requests[0]["context"]["events"]]
        self.assertIn("A delivered", texts)

    async def test_real_codex_persistent_adapter_accepts_shared_request(self):
        adapter = CodexPersistentAdapter(MinimalCodexClient(), ExistingBindingResolver())
        core = self.make_core(
            {"a": adapter},
            bindings={("room", "a"): BindingSnapshot("existing-binding", 7)},
        )
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        outcome = await core.run_human_turn("room", ("a",))
        self.assertEqual(outcome[0].action, "delivered")
        self.assertEqual(core.log.events("room")[-1].text, "Codex final")

    async def test_peer_output_is_context_only_and_never_dispatches(self):
        adapter = FakeAdapter()
        core = self.make_core({"a": adapter})
        core.observe_agent_message("room", agent_id="a", display_name="A", text="peer")
        self.assertEqual(adapter.requests, [])
        self.assertEqual(core.log.events("room")[0].author_type, "agent")

    async def test_dispatch_snapshot_is_immutable_and_next_request_sees_new_event(self):
        adapter = FakeAdapter()
        adapter.entered = asyncio.Event()
        adapter.release = asyncio.Event()
        core = self.make_core({"a": adapter})
        core.ingest_human("room", author_id="h", display_name="H", text="first")
        running = asyncio.create_task(core.run_human_turn("room", ("a",)))
        await adapter.entered.wait()
        core.ingest_human("room", author_id="h", display_name="H", text="second")
        adapter.release.set()
        await running
        self.assertEqual(adapter.requests[0]["context"]["cursor"]["throughSeqInclusive"], 1)
        adapter.entered = None
        adapter.release = None
        await core.run_human_turn("room", ("a",))
        self.assertEqual(
            [event["text"] for event in adapter.requests[1]["context"]["events"]],
            ["second"],
        )

    async def test_native_known_output_is_omitted_only_for_its_author(self):
        adapters = {"a": FakeAdapter(text="from A"), "b": FakeAdapter(text="from B")}
        core = self.make_core(adapters)
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        await core.run_human_turn("room", ("a",))
        await core.run_human_turn("room", ("a", "b"))
        a_texts = [event["text"] for event in adapters["a"].requests[-1]["context"]["events"]]
        b_texts = [event["text"] for event in adapters["b"].requests[-1]["context"]["events"]]
        self.assertNotIn("from A", a_texts)
        self.assertIn("from A", b_texts)

    async def test_context_commit_advances_only_on_committed(self):
        adapter = FakeAdapter(context_commit="not_committed")
        core = self.make_core({"a": adapter})
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        await core.run_human_turn("room", ("a",))
        self.assertEqual(core.cursor("room", "a").last_canonical_synced_seq, 0)
        adapter.context_commit = "committed"
        await core.run_human_turn("room", ("a",))
        self.assertEqual(core.cursor("room", "a").last_canonical_synced_seq, 1)

    async def test_unknown_context_commit_keeps_fence_and_is_not_replayed(self):
        adapter = FakeAdapter(context_commit="unknown")
        core = self.make_core({"a": adapter})
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        first = await core.run_human_turn("room", ("a",))
        second = await core.run_human_turn("room", ("a",))
        self.assertEqual(first[0].action, "context-unknown")
        self.assertEqual(second[0].action, "blocked")
        self.assertEqual(len(adapter.requests), 1)

    async def test_discussion_start_adds_core_event_and_policy_selects_first(self):
        core = self.make_core()
        outcome = core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@2> <@1>\ngoal"
        )
        self.assertEqual(outcome.action, "started")
        self.assertEqual(core.log.events("room")[-1].core_type, "discussion_started")
        turn = await core.run_discussion_turn("room")
        self.assertEqual(turn.agent_id, "b")

    async def test_fake_complete_continue_and_closing_check_are_wired(self):
        adapters = {"a": FakeAdapter("complete", text="done"), "b": FakeAdapter("continue", text="not yet")}
        core = self.make_core(adapters)
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal")
        first = await core.run_discussion_turn("room")
        second = await core.run_discussion_turn("room")
        self.assertEqual(first.action, "closing-check")
        self.assertEqual(second.action, "closing-cancelled")
        self.assertEqual(core.policy.state("room").phase, "active")

    async def test_fake_abstain_has_no_delivery_or_canonical_output(self):
        adapter = FakeAdapter("abstain", text="")
        delivery = FakeDelivery()
        core = self.make_core({"a": adapter, "b": FakeAdapter()}, delivery)
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal")
        before = len(core.log.events("room"))
        outcome = await core.run_discussion_turn("room")
        self.assertEqual(outcome.action, "abstained")
        self.assertEqual(delivery.calls, [])
        self.assertEqual(len(core.log.events("room")), before)

    async def test_delivery_is_committed_before_next_agent_snapshot(self):
        adapters = {"a": FakeAdapter(text="A output"), "b": FakeAdapter(text="B output")}
        core = self.make_core(adapters)
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal")
        await core.run_discussion_turn("room")
        await core.run_discussion_turn("room")
        texts = [event["text"] for event in adapters["b"].requests[0]["context"]["events"]]
        self.assertIn("A output", texts)

    async def test_unknown_delivery_creates_no_ghost_and_blocks_next_ai(self):
        core = self.make_core(delivery=FakeDelivery("unknown"))
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal")
        before = len(core.log.events("room"))
        first = await core.run_discussion_turn("room")
        second = await core.run_discussion_turn("room")
        self.assertEqual(first.action, "delivery-unknown")
        self.assertEqual(second.action, "blocked")
        self.assertEqual(len(core.log.events("room")), before)

    async def test_unknown_delivery_blocks_other_agent_in_same_room(self):
        core = self.make_core(
            {"a": FakeAdapter(text="A"), "b": FakeAdapter(text="B")},
            FakeDelivery("unknown"),
        )
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        await core.run_human_turn("room", ("a",))
        outcome = await core.run_human_turn("room", ("b",))
        self.assertEqual(outcome[0].action, "blocked")
        self.assertEqual(core.adapters["b"].requests, [])

    async def test_not_delivered_creates_no_ghost_and_suspends(self):
        core = self.make_core(delivery=FakeDelivery("not_delivered"))
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal")
        before = len(core.log.events("room"))
        outcome = await core.run_discussion_turn("room")
        self.assertEqual(outcome.action, "suspended")
        self.assertEqual(core.policy.state("room").phase, "suspended")
        self.assertEqual(len(core.log.events("room")), before)

    async def test_missing_binding_fails_closed_without_adapter_call(self):
        adapter = FakeAdapter()
        core = self.make_core({"a": adapter}, bindings={})
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        outcome = await core.run_human_turn("room", ("a",))
        self.assertEqual(outcome[0].action, "binding-unavailable")
        self.assertEqual(adapter.requests, [])

    async def test_missing_discussion_binding_rejects_before_dispatch(self):
        adapter = FakeAdapter()
        core = self.make_core(
            {"a": adapter, "b": FakeAdapter()},
            bindings={("room", "b"): BindingSnapshot("binding-b", 1)},
        )
        outcome = core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal"
        )
        self.assertEqual(outcome.action, "binding-unavailable")
        self.assertEqual(core.policy.state("room").phase, "stopped")
        self.assertEqual(core.policy.state("room").quotas["a"].used_calls, 0)
        self.assertEqual(core.policy.state("room").safety.dispatched_calls, 0)
        self.assertFalse(core.log.events("room"))
        self.assertEqual(adapter.requests, [])

    async def test_adapter_binding_rejection_does_not_charge_model_call(self):
        adapter = CodexPersistentAdapter(MinimalCodexClient(), MissingBindingResolver())
        core = self.make_core(
            {"a": adapter, "b": FakeAdapter()},
            bindings={
                ("room", "a"): BindingSnapshot("missing-binding", 1),
                ("room", "b"): BindingSnapshot("binding-b", 1),
            },
        )
        core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal"
        )
        outcome = await core.run_discussion_turn("room")
        state = core.policy.state("room")
        self.assertEqual(outcome.action, "not-committed")
        self.assertEqual(state.quotas["a"].used_calls, 0)
        self.assertEqual(state.safety.dispatched_calls, 1)

    async def test_request_validation_failure_does_not_charge_model_call(self):
        adapter = FakeAdapter()
        core = self.make_core({"a": adapter, "b": FakeAdapter()}, timeout_ms=0)
        core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- goal"
        )
        outcome = await core.run_discussion_turn("room")
        state = core.policy.state("room")
        self.assertEqual(outcome.action, "invalid-response")
        self.assertEqual(state.quotas["a"].used_calls, 0)
        self.assertEqual(state.safety.dispatched_calls, 1)
        self.assertEqual(adapter.requests, [])

    async def test_confirmed_invocation_is_charged_once_even_when_execution_errors(self):
        adapter = FakeAdapter("error")
        core = self.make_core({"a": adapter, "b": FakeAdapter()})
        core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- goal"
        )
        outcome = await core.run_discussion_turn("room")
        self.assertEqual(outcome.action, "adapter-error")
        self.assertEqual(core.policy.state("room").quotas["a"].used_calls, 1)

    async def test_ambiguous_invocation_is_charged_and_never_rolled_back(self):
        class AmbiguousAdapter(FakeAdapter):
            async def execute(self, request, *, observer):
                self.requests.append(request)
                await observer.on_invocation_started(request["requestId"], "ambiguous")
                return {
                    "requestId": request["requestId"],
                    "contextCommit": "unknown",
                    "status": "error",
                    "error": {"code": "transport_error", "message": "unknown", "retryable": True},
                }

        core = self.make_core({"a": AmbiguousAdapter(), "b": FakeAdapter()})
        core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- goal"
        )
        outcome = await core.run_discussion_turn("room")
        self.assertEqual(outcome.action, "context-unknown")
        self.assertEqual(core.policy.state("room").quotas["a"].used_calls, 1)

    async def test_explicit_codex_turn_start_rejection_does_not_charge_model_call(self):
        class RejectingClient(MinimalCodexClient):
            async def start_turn(self, thread_id, text, client_user_message_id):
                raise AppServerRpcError(-32000, "rejected")

        adapter = CodexPersistentAdapter(RejectingClient(), ExistingBindingResolver())
        core = self.make_core(
            {"a": adapter, "b": FakeAdapter()},
            bindings={
                ("room", "a"): BindingSnapshot("existing-binding", 7),
                ("room", "b"): BindingSnapshot("binding-b", 1),
            },
        )
        core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- goal"
        )
        outcome = await core.run_discussion_turn("room")
        self.assertEqual(outcome.action, "not-committed")
        self.assertEqual(core.policy.state("room").quotas["a"].used_calls, 0)

    def test_shared_validator_rejects_error_with_text_even_when_empty(self):
        base = {
            "requestId": "request",
            "contextCommit": "committed",
            "status": "error",
            "error": {"code": "execution_error", "message": "failed", "retryable": False},
        }
        for text in ("body", ""):
            with self.subTest(text=text):
                with self.assertRaises(ContractError):
                    validate_agent_result(
                        {**base, "text": text}, request_id="request", mode="human-turn"
                    )

    async def test_invalid_results_fail_closed(self):
        class BadAdapter(FakeAdapter):
            async def execute(self, request, *, observer):
                await observer.on_invocation_started(request["requestId"], "confirmed")
                return {"requestId": "wrong", "contextCommit": "committed", "status": "continue", "text": "x"}

        core = self.make_core({"a": BadAdapter()})
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        outcome = await core.run_human_turn("room", ("a",))
        self.assertEqual(outcome[0].action, "invalid-response")
        blocked = await core.run_human_turn("room", ("a",))
        self.assertEqual(blocked[0].action, "blocked")

    async def test_error_with_text_is_rejected_before_delivery(self):
        class ErrorWithTextAdapter(FakeAdapter):
            async def execute(self, request, *, observer):
                await observer.on_invocation_started(request["requestId"], "confirmed")
                return {
                    "requestId": request["requestId"],
                    "contextCommit": "committed",
                    "status": "error",
                    "text": "must not be delivered",
                    "error": {"code": "execution_error", "message": "failed", "retryable": False},
                }

        delivery = FakeDelivery()
        core = self.make_core({"a": ErrorWithTextAdapter()}, delivery)
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        outcome = await core.run_human_turn("room", ("a",))
        self.assertEqual(outcome[0].action, "invalid-response")
        self.assertEqual(delivery.calls, [])

    async def test_discussion_only_statuses_are_invalid_in_human_turn(self):
        for status, text in (
            ("complete", "done"),
            ("await-human", "need input"),
            ("abstain", "not empty"),
        ):
            core = self.make_core({"a": FakeAdapter(status, text=text)})
            core.ingest_human("room", author_id="h", display_name="H", text="hello")
            outcome = await core.run_human_turn("room", ("a",))
            self.assertEqual(outcome[0].action, "invalid-response")

    async def test_persistent_not_applicable_context_commit_is_invalid(self):
        adapter = FakeAdapter()
        adapter.context_commit = "not_applicable"
        core = self.make_core({"a": adapter})
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        outcome = await core.run_human_turn("room", ("a",))
        self.assertEqual(outcome[0].action, "invalid-response")

    async def test_stop_invalidates_inflight_and_best_effort_cancels(self):
        adapter = FakeAdapter()
        adapter.entered = asyncio.Event()
        adapter.release = asyncio.Event()
        delivery = FakeDelivery()
        core = self.make_core({"a": adapter, "b": FakeAdapter()}, delivery)
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal")
        running = asyncio.create_task(core.run_discussion_turn("room"))
        await adapter.entered.wait()
        await core.stop("room")
        adapter.release.set()
        outcome = await running
        self.assertEqual(core.policy.state("room").phase, "stopped")
        self.assertEqual(len(adapter.cancelled), 1)
        self.assertEqual(core.adapters["b"].cancelled, [])
        self.assertEqual(delivery.calls, [])
        self.assertIn(outcome.action, {"invalidated", "ignored"})
        self.assertEqual(len(core.log.events("room")), 1)
        self.assertEqual(core.log.events("room")[0].core_type, "discussion_started")

    async def test_stop_preserves_attempted_unknown_delivery_until_delivered_reconciliation(self):
        delivery = BlockingDelivery("unknown")
        core = self.make_core({"a": FakeAdapter(text="already attempted"), "b": FakeAdapter()}, delivery)
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- goal")
        running = asyncio.create_task(core.run_discussion_turn("room"))
        await delivery.entered.wait()
        await core.stop("room")
        delivery.release.set()
        self.assertEqual((await running).action, "delivery-unknown")
        self.assertEqual(core.policy.state("room").phase, "stopped")

        reconciled = await core.resolve_pending_delivery("room", "a", "delivered")
        self.assertEqual(reconciled.action, "delivery-reconciled")
        self.assertEqual(core.log.events("room")[-1].text, "already attempted")
        state = core.policy.state("room")
        self.assertEqual(state.phase, "stopped")
        self.assertEqual(state.quotas["a"].used_chars, len("already attempted"))
        self.assertFalse(core.cursor("room", "a").uncertain)
        restarted = core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- next"
        )
        self.assertEqual(restarted.action, "started")
        core.delivery = FakeDelivery("delivered")
        self.assertEqual((await core.run_discussion_turn("room")).action, "result-recorded")

    async def test_stop_unknown_delivery_can_resolve_not_delivered_without_resend(self):
        delivery = BlockingDelivery("unknown")
        core = self.make_core({"a": FakeAdapter(text="not sent"), "b": FakeAdapter()}, delivery)
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- goal")
        running = asyncio.create_task(core.run_discussion_turn("room"))
        await delivery.entered.wait()
        await core.stop("room")
        delivery.release.set()
        await running
        before = tuple(core.log.events("room"))

        resolved = await core.resolve_pending_delivery("room", "a", "not_delivered")
        self.assertEqual(resolved.action, "stopped")
        self.assertEqual(tuple(core.log.events("room")), before)
        self.assertEqual(len(delivery.calls), 1)
        self.assertEqual(core.policy.state("room").quotas["a"].used_chars, 0)
        self.assertFalse(core.cursor("room", "a").uncertain)
        restarted = core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- next"
        )
        self.assertEqual(restarted.action, "started")

    async def test_stop_unknown_delivery_remains_fenced_while_unresolved(self):
        delivery = BlockingDelivery("unknown")
        core = self.make_core({"a": FakeAdapter(text="uncertain"), "b": FakeAdapter()}, delivery)
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- goal")
        running = asyncio.create_task(core.run_discussion_turn("room"))
        await delivery.entered.wait()
        await core.stop("room")
        delivery.release.set()
        await running
        unresolved = await core.resolve_pending_delivery("room", "a", "unknown")
        self.assertEqual(unresolved.action, "delivery-unknown")
        self.assertTrue(core.cursor("room", "a").uncertain)
        before = len(core.log.events("room"))
        core.ingest_human("room", author_id="h", display_name="H", text="new context")
        self.assertEqual((await core.run_human_turn("room", ("b",)))[0].action, "blocked")
        self.assertEqual(len(core.log.events("room")), before + 1)
        self.assertEqual(core.log.events("room")[-1].text, "new context")
        self.assertEqual(core.adapters["b"].requests, [])
        self.assertEqual(core.policy.state("room").phase, "stopped")

    async def test_unresolved_delivery_blocks_new_discussion_before_reservation(self):
        delivery = BlockingDelivery("unknown")
        core = self.make_core({"a": FakeAdapter(text="uncertain"), "b": FakeAdapter()}, delivery)
        core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- first"
        )
        running = asyncio.create_task(core.run_discussion_turn("room"))
        await delivery.entered.wait()
        await core.stop("room")
        delivery.release.set()
        await running

        before_events = tuple(core.log.events("room"))
        before_dispatches = core.policy.state("room").safety.dispatched_calls
        direct = await core.run_discussion_turn("room")
        rejected = core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2> -- second"
        )
        state = core.policy.state("room")

        self.assertEqual(direct.action, "blocked")
        self.assertEqual(rejected.action, "rejected")
        self.assertEqual(rejected.reason, "delivery pending")
        self.assertEqual(tuple(core.log.events("room")), before_events)
        self.assertEqual(state.phase, "stopped")
        self.assertEqual(state.safety.dispatched_calls, before_dispatches)
        self.assertIsNone(state.in_flight)

    async def test_restart_suspends_discussion_and_stale_result_cannot_deliver(self):
        adapter = FakeAdapter()
        adapter.entered = asyncio.Event()
        adapter.release = asyncio.Event()
        delivery = FakeDelivery()
        core = self.make_core({"a": adapter, "b": FakeAdapter()}, delivery)
        core.ingest_human("room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal")
        running = asyncio.create_task(core.run_discussion_turn("room"))
        await adapter.entered.wait()
        core.on_process_restart()
        adapter.release.set()
        await running
        self.assertEqual(core.policy.state("room").phase, "suspended")
        self.assertEqual(delivery.calls, [])

    async def test_discussion_snapshots_room_specific_binding_at_start(self):
        adapters = {"a": FakeAdapter(), "b": FakeAdapter()}
        bindings = {
            ("room", "a"): BindingSnapshot("room-a", 3),
            ("room", "b"): BindingSnapshot("room-b", 4),
            ("other", "a"): BindingSnapshot("other-a", 9),
        }
        core = self.make_core(adapters, bindings=bindings)
        core.ingest_human(
            "room", author_id="h", display_name="H", text="!discuss <@1> <@2>\ngoal"
        )
        core.bindings[("room", "a")] = BindingSnapshot("rebound", 99)
        await core.run_discussion_turn("room")
        self.assertEqual(adapters["a"].requests[0]["binding"], {"bindingId": "room-a", "generation": 3})

    async def test_restart_marks_lost_pending_delivery_as_uncertain(self):
        core = self.make_core({"a": FakeAdapter()}, FakeDelivery("unknown"))
        core.ingest_human("room", author_id="h", display_name="H", text="hello")
        await core.run_human_turn("room", ("a",))
        core.on_process_restart()
        self.assertTrue(core.cursor("room", "a").uncertain)
        outcome = await core.run_human_turn("room", ("a",))
        self.assertEqual(outcome[0].action, "blocked")


if __name__ == "__main__":
    unittest.main()
