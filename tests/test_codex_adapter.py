import asyncio
import unittest
from pathlib import Path

from codex_adapter.app_server import (
    AppServerMethodUnsupported,
    AppServerProtocolError,
    AppServerRpcError,
    AppServerTransportError,
    CodexAppServerClient,
    ThreadReference,
    TurnReference,
)
from codex_adapter.control_plane import CodexControlPlane, CodexCreateAmbiguous, CodexCreateRejected
from codex_adapter.binding import (
    BindingGenerationMismatch,
    BindingUnavailable,
    InMemoryBindingResolver,
    ResolvedBinding,
)
from codex_adapter.persistent import CodexPersistentAdapter


THREAD_ID = "thread-placeholder"
REQUEST_ID = "request-placeholder"


def request(*, request_id=REQUEST_ID, generation=3, timeout_ms=100):
    return {
        "requestId": request_id,
        "agentId": "agent-placeholder",
        "mode": "human-turn",
        "binding": {"bindingId": "binding-placeholder", "generation": generation},
        "context": {
            "kind": "event-delta",
            "events": [
                {
                    "eventId": "event-placeholder",
                    "seq": 8,
                    "kind": "message",
                    "author": {
                        "type": "human",
                        "id": "human-placeholder",
                        "displayName": "Human",
                    },
                    "text": "test input",
                    "mentions": ["agent-placeholder"],
                }
            ],
            "cursor": {"fromSeqExclusive": 7, "throughSeqInclusive": 8},
        },
        "constraints": {"timeoutMs": timeout_ms},
    }


def user_item(request_id=REQUEST_ID):
    # app-server exposes turn/start.clientUserMessageId as userMessage.clientId.
    return {"type": "userMessage", "clientId": request_id, "text": "redacted"}


def final_item(text="final text"):
    return {"type": "agentMessage", "phase": "final_answer", "text": text}


def turn(*, status="completed", request_id=REQUEST_ID, text="final text"):
    items = [user_item(request_id)]
    if text is not None:
        items.append(final_item(text))
    return {"id": "turn-placeholder", "status": status, "items": items}


class FakeResolver:
    def __init__(self, outcome=None):
        self.outcome = outcome or ResolvedBinding(
            "binding-placeholder",
            3,
            THREAD_ID,
        )
        self.calls = []

    async def resolve(self, binding_id, generation):
        self.calls.append((binding_id, generation))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class SequenceResolver(FakeResolver):
    def __init__(self, outcomes):
        super().__init__()
        self.outcomes = list(outcomes)

    async def resolve(self, binding_id, generation):
        self.calls.append((binding_id, generation))
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class BlockingResolver(FakeResolver):
    def __init__(self):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.first = True

    async def resolve(self, binding_id, generation):
        self.calls.append((binding_id, generation))
        if self.first:
            self.first = False
            self.entered.set()
            await self.release.wait()
        return self.outcome


class FakeClient:
    def __init__(self):
        self.connect_calls = 0
        self.reconnect_calls = 0
        self.resume_calls = []
        self.resume_outcome = None
        self.start_calls = []
        self.interrupt_calls = []
        self.interrupt_outcome = {}
        self.start_outcome = TurnReference("turn-placeholder", "inProgress")
        self.wait_outcome = turn()
        self.turn_snapshots = [[turn()]]
        self.block_wait = None

    async def connect(self):
        self.connect_calls += 1

    async def reconnect(self):
        self.reconnect_calls += 1

    async def resume_thread(self, thread_id):
        self.resume_calls.append(thread_id)
        if isinstance(self.resume_outcome, Exception):
            raise self.resume_outcome
        return {"thread": {"id": thread_id}}

    async def start_turn(self, thread_id, text, client_user_message_id):
        self.start_calls.append((thread_id, text, client_user_message_id))
        if isinstance(self.start_outcome, Exception):
            raise self.start_outcome
        return self.start_outcome

    async def wait_for_turn(self, turn_id, timeout_ms):
        if self.block_wait is not None:
            await self.block_wait.wait()
        if isinstance(self.wait_outcome, Exception):
            raise self.wait_outcome
        return self.wait_outcome

    async def list_turns(self, thread_id):
        if not self.turn_snapshots:
            return []
        if len(self.turn_snapshots) == 1:
            snapshot = self.turn_snapshots[0]
        else:
            snapshot = self.turn_snapshots.pop(0)
        if isinstance(snapshot, Exception):
            raise snapshot
        return snapshot

    async def interrupt(self, thread_id, turn_id):
        self.interrupt_calls.append((thread_id, turn_id))
        if isinstance(self.interrupt_outcome, Exception):
            raise self.interrupt_outcome
        return self.interrupt_outcome


class RecordingInvocationObserver:
    def __init__(self):
        self.signals = []

    async def on_invocation_started(self, request_id, certainty):
        self.signals.append((request_id, certainty))


class PersistentAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_capabilities_match_persistent_phase_one_contract(self):
        self.assertEqual(
            CodexPersistentAdapter.capabilities,
            {
                "sessionMode": "persistent",
                "maxInFlight": 1,
                "canCancelInFlight": True,
            },
        )

    async def test_snapshot_context_is_rejected_before_binding_resolution(self):
        raw = request()
        raw["context"] = {"kind": "snapshot", "messages": []}
        resolver = FakeResolver()
        client = FakeClient()
        result = await CodexPersistentAdapter(client, resolver).execute(raw)

        self.assertEqual(result["error"]["code"], "invalid_response")
        self.assertEqual(result["contextCommit"], "not_committed")
        self.assertEqual(resolver.calls, [])
        self.assertEqual(client.start_calls, [])

    async def test_pre_start_failures_emit_no_invocation_signal(self):
        cases = []

        invalid = request()
        invalid["constraints"]["timeoutMs"] = 0
        cases.append((invalid, FakeClient(), FakeResolver()))
        cases.append((request(), FakeClient(), FakeResolver(BindingUnavailable("missing"))))
        cases.append(
            (request(), FakeClient(), FakeResolver(BindingGenerationMismatch("changed")))
        )
        resume_failure = FakeClient()
        resume_failure.resume_outcome = AppServerRpcError(-32000, "resume rejected")
        cases.append((request(), resume_failure, FakeResolver()))
        rejected_start = FakeClient()
        rejected_start.start_outcome = AppServerRpcError(-32000, "turn rejected")
        cases.append((request(), rejected_start, FakeResolver()))

        for raw, client, resolver in cases:
            with self.subTest(client=type(client).__name__, resolver=type(resolver).__name__):
                observer = RecordingInvocationObserver()
                await CodexPersistentAdapter(client, resolver).execute(raw, observer=observer)
                self.assertEqual(observer.signals, [])

    async def test_successful_start_emits_one_confirmed_invocation_signal(self):
        observer = RecordingInvocationObserver()
        result = await CodexPersistentAdapter(FakeClient(), FakeResolver()).execute(
            request(), observer=observer
        )
        self.assertEqual(result["status"], "continue")
        self.assertEqual(observer.signals, [(REQUEST_ID, "confirmed")])

    async def test_confirmed_start_stays_counted_when_execution_later_errors(self):
        client = FakeClient()
        client.wait_outcome = AppServerRpcError(-32000, "turn failed")
        observer = RecordingInvocationObserver()
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(
            request(), observer=observer
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["contextCommit"], "committed")
        self.assertEqual(observer.signals, [(REQUEST_ID, "confirmed")])

    async def test_malformed_canonical_event_is_rejected(self):
        raw = request()
        del raw["context"]["events"][0]["author"]
        resolver = FakeResolver()
        result = await CodexPersistentAdapter(FakeClient(), resolver).execute(raw)

        self.assertEqual(result["error"]["code"], "invalid_response")
        self.assertEqual(result["contextCommit"], "not_committed")
        self.assertEqual(resolver.calls, [])

    async def test_valid_binding_resumes_existing_thread(self):
        client = FakeClient()
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(request())

        self.assertEqual(result["status"], "continue")
        self.assertEqual(result["contextCommit"], "committed")
        self.assertEqual(client.resume_calls, [THREAD_ID])
        self.assertEqual(len(client.start_calls), 1)

    async def test_invalid_binding_fails_closed_without_start(self):
        client = FakeClient()
        resolver = FakeResolver(BindingUnavailable("binding unavailable"))
        result = await CodexPersistentAdapter(client, resolver).execute(request())

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "binding_unavailable")
        self.assertEqual(result["contextCommit"], "not_committed")
        self.assertEqual(client.start_calls, [])

    async def test_generation_mismatch_fails_closed(self):
        client = FakeClient()
        resolver = FakeResolver(
            BindingGenerationMismatch("binding generation mismatch")
        )
        result = await CodexPersistentAdapter(client, resolver).execute(
            request(generation=4)
        )

        self.assertEqual(result["error"]["code"], "binding_unavailable")
        self.assertEqual(result["contextCommit"], "not_committed")
        self.assertEqual(client.resume_calls, [])

    async def test_request_id_is_client_user_message_id(self):
        client = FakeClient()
        await CodexPersistentAdapter(client, FakeResolver()).execute(
            request(request_id="correlation-marker")
        )

        self.assertEqual(client.start_calls[0][2], "correlation-marker")
        self.assertIn("event-placeholder", client.start_calls[0][1])

    async def test_ambiguous_start_committed_positive_read_back(self):
        client = FakeClient()
        client.start_outcome = AppServerTransportError(
            "connection lost",
            ambiguous=True,
        )
        client.turn_snapshots = [[turn(text="recovered final")]]
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(request())

        self.assertEqual(result["status"], "continue")
        self.assertEqual(result["text"], "recovered final")
        self.assertEqual(result["contextCommit"], "committed")
        self.assertEqual(client.reconnect_calls, 1)
        self.assertEqual(client.resume_calls, [THREAD_ID, THREAD_ID])

    async def test_ambiguous_absence_is_unknown_and_not_replayed(self):
        client = FakeClient()
        client.start_outcome = AppServerTransportError(
            "connection lost",
            ambiguous=True,
        )
        client.turn_snapshots = [[]]
        adapter = CodexPersistentAdapter(client, FakeResolver())
        observer = RecordingInvocationObserver()
        result = await adapter.execute(request(timeout_ms=10), observer=observer)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["contextCommit"], "unknown")
        self.assertEqual(result["error"]["code"], "transport_error")
        self.assertEqual(len(client.start_calls), 1)
        self.assertEqual(observer.signals, [(REQUEST_ID, "ambiguous")])

        blocked = await adapter.execute(request(request_id="next-request"))
        self.assertEqual(blocked["error"]["code"], "adapter_unavailable")
        duplicate = await adapter.execute(request())
        self.assertEqual(duplicate, result)
        self.assertEqual(len(client.start_calls), 1)

    async def test_turn_start_timeout_is_reconciled_before_return(self):
        client = FakeClient()
        client.start_outcome = asyncio.TimeoutError()
        client.turn_snapshots = [[turn(text="accepted before timeout")]]
        observer = RecordingInvocationObserver()
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(
            request(), observer=observer
        )

        self.assertEqual(result["status"], "continue")
        self.assertEqual(result["contextCommit"], "committed")
        self.assertEqual(result["text"], "accepted before timeout")
        self.assertEqual(len(client.start_calls), 1)
        self.assertEqual(observer.signals, [(REQUEST_ID, "ambiguous")])

    async def test_final_recovery_after_reconnect(self):
        client = FakeClient()
        client.wait_outcome = AppServerTransportError(
            "connection lost",
            ambiguous=True,
        )
        client.turn_snapshots = [[turn(text="recovered after restart")]]
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(request())

        self.assertEqual(result["text"], "recovered after restart")
        self.assertEqual(client.reconnect_calls, 1)
        self.assertEqual(client.resume_calls, [THREAD_ID, THREAD_ID])

    async def test_completed_turn_waits_for_final_to_become_readable(self):
        client = FakeClient()
        client.wait_outcome = {"id": "turn-placeholder", "status": "completed", "items": []}
        client.turn_snapshots = [
            [turn(status="completed", text=None)],
            [turn(status="completed", text="persisted final")],
        ]
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(request())

        self.assertEqual(result["status"], "continue")
        self.assertEqual(result["contextCommit"], "committed")
        self.assertEqual(result["text"], "persisted final")

    async def test_known_commit_does_not_regress_when_read_back_fails(self):
        client = FakeClient()
        client.wait_outcome = asyncio.TimeoutError()
        client.turn_snapshots = [AppServerProtocolError("contains-private-details")]
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(request())

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["contextCommit"], "committed")
        self.assertEqual(result["error"]["code"], "invalid_response")
        self.assertNotIn("contains-private-details", str(result))

    async def test_max_in_flight_is_one(self):
        client = FakeClient()
        client.block_wait = asyncio.Event()
        adapter = CodexPersistentAdapter(client, FakeResolver())
        first = asyncio.create_task(adapter.execute(request(request_id="first")))
        while not client.start_calls:
            await asyncio.sleep(0)

        second = await adapter.execute(request(request_id="second"))
        self.assertEqual(second["error"]["code"], "adapter_unavailable")
        self.assertEqual(second["contextCommit"], "not_committed")

        client.block_wait.set()
        first_result = await first
        self.assertEqual(first_result["status"], "continue")

    async def test_duplicate_request_waits_for_same_terminal_result(self):
        client = FakeClient()
        client.block_wait = asyncio.Event()
        adapter = CodexPersistentAdapter(client, FakeResolver())
        first = asyncio.create_task(adapter.execute(request()))
        while not client.start_calls:
            await asyncio.sleep(0)
        duplicate = asyncio.create_task(adapter.execute(request()))
        await asyncio.sleep(0)
        self.assertEqual(len(client.start_calls), 1)
        client.block_wait.set()
        self.assertEqual(await first, await duplicate)
        self.assertEqual(len(client.start_calls), 1)

    async def test_generation_change_before_dispatch_is_fenced(self):
        valid = ResolvedBinding("binding-placeholder", 3, THREAD_ID)
        resolver = SequenceResolver(
            [valid, BindingGenerationMismatch("generation changed")]
        )
        client = FakeClient()
        result = await CodexPersistentAdapter(client, resolver).execute(request())
        self.assertEqual(result["error"]["code"], "binding_unavailable")
        self.assertEqual(result["contextCommit"], "not_committed")
        self.assertEqual(client.start_calls, [])

    async def test_generation_change_before_delivery_suppresses_body(self):
        valid = ResolvedBinding("binding-placeholder", 3, THREAD_ID)
        resolver = SequenceResolver(
            [valid, valid, BindingGenerationMismatch("generation changed")]
        )
        result = await CodexPersistentAdapter(FakeClient(), resolver).execute(request())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "binding_unavailable")
        self.assertEqual(result["contextCommit"], "committed")
        self.assertNotIn("text", result)

    async def test_multiple_request_id_matches_fail_closed(self):
        client = FakeClient()
        client.start_outcome = AppServerTransportError("connection lost", ambiguous=True)
        client.turn_snapshots = [[turn(), {**turn(), "id": "other-turn"}]]
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(request())
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_response")
        self.assertEqual(result["contextCommit"], "unknown")

    async def test_unexpected_exception_releases_in_flight_slot(self):
        resolver = FakeResolver(RuntimeError("resolver failed"))
        adapter = CodexPersistentAdapter(FakeClient(), resolver)
        with self.assertRaises(RuntimeError):
            await adapter.execute(request())

        resolver.outcome = ResolvedBinding("binding-placeholder", 3, THREAD_ID)
        result = await adapter.execute(request(request_id="after-failure"))
        self.assertEqual(result["status"], "continue")

    async def test_task_cancellation_after_dispatch_keeps_fence_until_reconciled(self):
        client = FakeClient()
        client.block_wait = asyncio.Event()
        adapter = CodexPersistentAdapter(client, FakeResolver())
        running = asyncio.create_task(adapter.execute(request()))
        while adapter._active is None or adapter._active.turn_id is None:
            await asyncio.sleep(0)

        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertIsNotNone(adapter._active)
        blocked = await adapter.execute(request(request_id="blocked-after-cancel"))
        self.assertEqual(blocked["error"]["code"], "adapter_unavailable")

        outcome = await adapter.cancel(REQUEST_ID)
        self.assertEqual(outcome.state, "late_completion")
        self.assertIsNone(adapter._active)
        client.block_wait = None
        result = await adapter.execute(request(request_id="after-cancel"))
        self.assertEqual(result["status"], "continue")

    async def test_interrupt_requires_read_back_reconciliation(self):
        client = FakeClient()
        client.block_wait = asyncio.Event()
        client.turn_snapshots = [[turn(status="interrupted", text=None)]]
        adapter = CodexPersistentAdapter(client, FakeResolver())
        running = asyncio.create_task(adapter.execute(request()))
        while adapter._active is None or adapter._active.turn_id is None:
            await asyncio.sleep(0)

        outcome = await adapter.cancel(REQUEST_ID)
        self.assertEqual(outcome.state, "interrupted")
        self.assertEqual(outcome.context_commit, "committed")
        self.assertEqual(client.interrupt_calls, [(THREAD_ID, "turn-placeholder")])

        client.block_wait.set()
        await running

    async def test_interrupt_rpc_error_still_reconciles_read_back(self):
        client = FakeClient()
        client.block_wait = asyncio.Event()
        client.interrupt_outcome = AppServerTransportError("interrupt ack lost")
        client.turn_snapshots = [[turn(status="interrupted", text=None)]]
        adapter = CodexPersistentAdapter(client, FakeResolver())
        running = asyncio.create_task(adapter.execute(request()))
        while adapter._active is None or adapter._active.turn_id is None:
            await asyncio.sleep(0)
        outcome = await adapter.cancel(REQUEST_ID)
        self.assertEqual(outcome.state, "interrupted")
        self.assertEqual(outcome.context_commit, "committed")
        client.block_wait.set()
        result = await running
        self.assertEqual(result["status"], "error")
        self.assertNotIn("text", result)

    async def test_cancel_before_turn_id_prevents_dispatch(self):
        resolver = BlockingResolver()
        client = FakeClient()
        adapter = CodexPersistentAdapter(client, resolver)
        running = asyncio.create_task(adapter.execute(request()))
        await resolver.entered.wait()
        outcome = await adapter.cancel(REQUEST_ID)
        self.assertEqual(outcome.state, "unknown")
        self.assertEqual(client.interrupt_calls, [])
        resolver.release.set()
        result = await running
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["contextCommit"], "not_committed")
        self.assertEqual(client.start_calls, [])

    async def test_late_interrupt_race_reports_completion(self):
        client = FakeClient()
        client.block_wait = asyncio.Event()
        client.turn_snapshots = [[turn(status="completed")]]
        adapter = CodexPersistentAdapter(client, FakeResolver())
        running = asyncio.create_task(adapter.execute(request()))
        while adapter._active is None or adapter._active.turn_id is None:
            await asyncio.sleep(0)

        outcome = await adapter.cancel(REQUEST_ID)
        self.assertEqual(outcome.state, "late_completion")
        self.assertEqual(outcome.context_commit, "committed")

        client.block_wait.set()
        result = await running
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["contextCommit"], "committed")
        self.assertNotIn("text", result)

    async def test_required_method_unsupported_is_adapter_unavailable(self):
        client = FakeClient()
        client.start_outcome = AppServerMethodUnsupported("method unsupported")
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(request())

        self.assertEqual(result["error"]["code"], "adapter_unavailable")
        self.assertEqual(result["contextCommit"], "not_committed")

    async def test_malformed_read_back_is_invalid_response(self):
        client = FakeClient()
        client.start_outcome = AppServerProtocolError("malformed response")
        client.turn_snapshots = [AppServerProtocolError("malformed read-back")]
        result = await CodexPersistentAdapter(client, FakeResolver()).execute(request())

        self.assertEqual(result["error"]["code"], "invalid_response")
        self.assertEqual(result["contextCommit"], "unknown")

    async def test_process_restart_resumes_same_existing_thread(self):
        client = FakeClient()
        client.wait_outcome = AppServerTransportError("process restarted", ambiguous=True)
        client.turn_snapshots = [[turn()]]
        await CodexPersistentAdapter(client, FakeResolver()).execute(request())

        self.assertEqual(client.reconnect_calls, 1)
        self.assertEqual(client.resume_calls, [THREAD_ID, THREAD_ID])
        self.assertEqual(len(set(client.resume_calls)), 1)


class ScriptedTransport:
    def __init__(self, responder):
        self.responder = responder
        self.on_message = None
        self.on_disconnect = None
        self.open = False
        self.sent = []

    async def start(self, on_message, on_disconnect):
        self.open = True
        self.on_message = on_message
        self.on_disconnect = on_disconnect

    async def send(self, payload):
        self.sent.append(payload)
        if "id" not in payload:
            return
        response = self.responder(payload)
        await self.on_message(response)

    async def close(self):
        self.open = False

    def is_open(self):
        return self.open


class AppServerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrapper_uses_required_method_shapes(self):
        calls = []

        def responder(payload):
            calls.append((payload["method"], payload["params"]))
            method = payload["method"]
            if method == "initialize":
                result = {}
            elif method == "thread/resume":
                result = {"thread": {"id": THREAD_ID}}
            elif method == "thread/start":
                result = {"thread": {"id": "new-thread"}}
            elif method == "turn/start":
                result = {"turn": {"id": "turn-placeholder", "status": "inProgress"}}
            elif method == "thread/read":
                result = {"thread": {"id": THREAD_ID, "turns": []}}
            elif method == "thread/turns/list":
                result = {"data": [], "nextCursor": None}
            elif method == "turn/interrupt":
                result = {}
            else:
                raise AssertionError(method)
            return {"id": payload["id"], "result": result}

        transport = ScriptedTransport(responder)
        client = CodexAppServerClient(lambda: transport)
        await client.connect()
        await client.resume_thread(THREAD_ID)
        created = await client.start_thread(cwd=Path("workspace"), model="model", base_instructions="safe")
        await client.start_turn(THREAD_ID, "prompt", REQUEST_ID)
        await client.read_thread(THREAD_ID)
        await client.list_turns(THREAD_ID)
        await client.interrupt(THREAD_ID, "turn-placeholder")

        methods = [method for method, _ in calls]
        self.assertEqual(
            methods,
            [
                "initialize",
                "thread/resume",
                "thread/start",
                "turn/start",
                "thread/read",
                "thread/turns/list",
                "turn/interrupt",
            ],
        )
        self.assertEqual(created.thread_id, "new-thread")
        thread_start = calls[2][1]
        self.assertFalse(thread_start["ephemeral"])
        self.assertEqual(thread_start["approvalPolicy"], "never")
        turn_start = calls[3][1]
        self.assertEqual(turn_start["clientUserMessageId"], REQUEST_ID)
        turns_list = calls[5][1]
        self.assertEqual(turns_list["itemsView"], "full")
        await client.close()


class FakeControlClient:
    def __init__(self, create_outcome=None, resume_outcome=None):
        self.create_outcome = create_outcome or ThreadReference("new-thread")
        self.resume_outcome = resume_outcome
        self.create_calls = 0
        self.resume_calls = []

    async def start_thread(self, **kwargs):
        self.create_calls += 1
        if isinstance(self.create_outcome, Exception):
            raise self.create_outcome
        return self.create_outcome

    async def resume_thread(self, thread_id):
        self.resume_calls.append(thread_id)
        if isinstance(self.resume_outcome, Exception):
            raise self.resume_outcome
        return {"thread": {"id": thread_id}}


class AppServerAndControlPlaneTests(unittest.IsolatedAsyncioTestCase):
    def control(self, client):
        return CodexControlPlane(
            client,
            cwd=Path("workspace"),
            model="model",
            base_instructions="safe",
            resolver=InMemoryBindingResolver([]),
        )

    async def test_validate_existing_uses_resume_without_model_turn(self):
        client = FakeControlClient()
        await self.control(client).validate_existing("existing-thread")
        self.assertEqual(client.resume_calls, ["existing-thread"])
        self.assertEqual(client.create_calls, 0)

    async def test_confirmed_create_returns_native_reference_once(self):
        client = FakeControlClient()
        self.assertEqual(await self.control(client).create(), "new-thread")
        self.assertEqual(client.create_calls, 1)

    async def test_explicit_rejection_and_ambiguous_transport_are_distinct(self):
        rejected = FakeControlClient(AppServerRpcError(-1, "rejected"))
        with self.assertRaises(CodexCreateRejected):
            await self.control(rejected).create()
        ambiguous = FakeControlClient(AppServerTransportError("lost", ambiguous=True))
        with self.assertRaises(CodexCreateAmbiguous):
            await self.control(ambiguous).create()
        self.assertEqual(ambiguous.create_calls, 1)

    async def test_completion_notification_is_fast_path(self):
        def responder(payload):
            return {"id": payload["id"], "result": {}}

        transport = ScriptedTransport(responder)
        client = CodexAppServerClient(lambda: transport)
        await client.connect()
        waiter = asyncio.create_task(client.wait_for_turn("turn-placeholder", 100))
        await asyncio.sleep(0)
        await client._handle_message(
            {
                "method": "turn/completed",
                "params": {"turn": turn(text="notification final")},
            }
        )
        completed = await waiter
        self.assertEqual(completed["items"][-1]["text"], "notification final")
        self.assertEqual(client.notification_methods, ["turn/completed"])
        await client.close()

    async def test_cancelled_waiter_is_removed(self):
        transport = ScriptedTransport(
            lambda payload: {"id": payload["id"], "result": {}}
        )
        client = CodexAppServerClient(lambda: transport)
        await client.connect()
        waiter = asyncio.create_task(client.wait_for_turn("turn-placeholder", 1000))
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(client._turn_waiters, {})
        await client.close()

    async def test_initialize_failure_does_not_leave_transport_reusable(self):
        transports = []

        def factory():
            index = len(transports)

            def responder(payload):
                if index == 0:
                    return {
                        "id": payload["id"],
                        "error": {"code": -32601, "message": "unsupported"},
                    }
                return {"id": payload["id"], "result": {}}

            transport = ScriptedTransport(responder)
            transports.append(transport)
            return transport

        client = CodexAppServerClient(factory)
        with self.assertRaises(AppServerMethodUnsupported):
            await client.connect()
        self.assertFalse(transports[0].is_open())

        await client.connect()
        self.assertEqual(len(transports), 2)
        self.assertTrue(transports[1].is_open())
        await client.close()

    async def test_repeated_pagination_cursor_is_protocol_error(self):
        def responder(payload):
            if payload["method"] == "initialize":
                result = {}
            else:
                result = {"data": [], "nextCursor": "same-cursor"}
            return {"id": payload["id"], "result": result}

        client = CodexAppServerClient(lambda: ScriptedTransport(responder))
        await client.connect()
        with self.assertRaises(AppServerProtocolError):
            await client.list_turns(THREAD_ID)
        await client.close()

    async def test_cancelled_rpc_is_removed_from_pending(self):
        class NoResponseTransport(ScriptedTransport):
            async def send(self, payload):
                self.sent.append(payload)
                if payload.get("method") == "initialize":
                    await self.on_message({"id": payload["id"], "result": {}})

        transport = NoResponseTransport(lambda payload: None)
        client = CodexAppServerClient(lambda: transport)
        await client.connect()
        pending = asyncio.create_task(client.read_thread(THREAD_ID))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(client._pending, {})
        await client.close()

    async def test_method_not_found_is_typed_unsupported_error(self):
        def responder(payload):
            return {
                "id": payload["id"],
                "error": {"code": -32601, "message": "unsupported"},
            }

        client = CodexAppServerClient(lambda: ScriptedTransport(responder))
        with self.assertRaises(AppServerMethodUnsupported):
            await client.connect()
        await client.close()

    async def test_malformed_result_is_protocol_error(self):
        def responder(payload):
            return {"id": payload["id"], "result": []}

        client = CodexAppServerClient(lambda: ScriptedTransport(responder))
        with self.assertRaises(AppServerProtocolError):
            await client.connect()
        await client.close()

    async def test_response_with_result_and_error_is_protocol_error(self):
        def responder(payload):
            return {
                "id": payload["id"],
                "result": {},
                "error": {"code": -32000, "message": "contradictory"},
            }

        client = CodexAppServerClient(lambda: ScriptedTransport(responder))
        with self.assertRaises(AppServerProtocolError):
            await client.connect()
        await client.close()

    async def test_unknown_response_id_fails_closed(self):
        client = CodexAppServerClient(lambda: ScriptedTransport(lambda payload: None))
        with self.assertRaises(AppServerProtocolError):
            await client._handle_message({"id": 999, "result": {}})

    async def test_unknown_turn_status_is_protocol_error(self):
        def responder(payload):
            if payload["method"] == "initialize":
                result = {}
            else:
                result = {"turn": {"id": "turn-placeholder", "status": "mystery"}}
            return {"id": payload["id"], "result": result}

        client = CodexAppServerClient(lambda: ScriptedTransport(responder))
        await client.connect()
        with self.assertRaises(AppServerProtocolError):
            await client.start_turn(THREAD_ID, "redacted", REQUEST_ID)
        await client.close()

    async def test_duplicate_completion_notification_is_consumed_once(self):
        client = CodexAppServerClient(lambda: ScriptedTransport(lambda payload: None))
        message = {
            "method": "turn/completed",
            "params": {"turn": turn(text="only final")},
        }
        await client._handle_message(message)
        await client._handle_message(message)
        completed = await client.wait_for_turn("turn-placeholder", 10)
        self.assertEqual(completed["items"][-1]["text"], "only final")
        with self.assertRaises(asyncio.TimeoutError):
            await client.wait_for_turn("turn-placeholder", 1)


if __name__ == "__main__":
    unittest.main()
