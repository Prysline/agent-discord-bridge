import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib import error, request

from agent_bridge import admin_ui
from agent_bridge.agent_management import (
    load_agent_management, merge_secret_placeholders, parse_agent_management,
    save_agent_management,
    validate_binding_associations,
    validate_removals,
)
from agent_bridge.discord_delivery import DiscordSenderDelivery


def valid_raw():
    return {
        "agents": [
            {"agentId":"a", "displayName":"Agent A", "agentAlias":"planner", "adapter":"codex",
             "mentionId":"123456789012345678", "senderId":"shared", "enabled":True,
             "available":True, "budgetChars":2000, "maxCalls":5},
            {"agentId":"b", "displayName":"Agent B", "agentAlias":"coder", "adapter":"antigravity",
             "mentionId":"223456789012345678", "senderId":"shared", "enabled":True,
             "available":True, "budgetChars":2000, "maxCalls":5},
        ],
        "senders": [
            {"senderId":"shared", "label":"Shared", "botUserId":"323456789012345678",
             "token":"private-test-placeholder", "enabled":True}
        ],
    }


class FakeChannel:
    def __init__(self, error=None): self.sent=[]; self.error=error
    async def send(self, text):
        if self.error: raise self.error
        self.sent.append(text); return object()


class FakeClient:
    def __init__(self, channel=None, fetch_error=None): self.channel=channel; self.fetch_error=fetch_error
    def get_channel(self, channel_id): return self.channel
    async def fetch_channel(self, channel_id):
        if self.fetch_error: raise self.fetch_error
        if self.channel is None: raise RuntimeError("unavailable")
        return self.channel


class FakeBindingAdmin:
    def __init__(self): self.calls=[]
    def state(self):
        return {"pendingOnboarding":[{"roomId":"room","agentId":"a","agentDisplayName":"A","adapter":"codex","createdAt":"now","status":"pending","triggerPreview":"hello","canCreate":True,"canBindExisting":True}],"bindings":[]}
    async def cancel_pending(self, room_id, agent_id): self.calls.append(("cancel",room_id,agent_id)); return True
    async def bind_existing(self, room_id, agent_id, native_reference): self.calls.append(("bind-existing",room_id,agent_id,native_reference))
    async def create_pending(self, room_id, agent_id): self.calls.append(("create",room_id,agent_id))


class AgentManagementTests(unittest.TestCase):
    def test_valid_config_and_redaction(self):
        value=parse_agent_management(valid_raw())
        redacted=value.redacted()
        self.assertTrue(redacted["senders"][0]["tokenConfigured"])
        self.assertNotIn("token", redacted["senders"][0])

    def test_validation_rejects_duplicate_invalid_and_unknown_fields(self):
        cases=[]
        raw=valid_raw(); raw["agents"][1]["agentId"]="a"; cases.append((raw,"重複"))
        raw=valid_raw(); raw["agents"][0]["adapter"]="other"; cases.append((raw,"adapter"))
        raw=valid_raw(); raw["agents"][0]["senderId"]="missing"; cases.append((raw,"找不到"))
        raw=valid_raw(); raw["senders"].append(dict(raw["senders"][0])); cases.append((raw,"重複"))
        raw=valid_raw(); raw["senders"][0]["botUserId"]="bad"; cases.append((raw,"格式"))
        raw=valid_raw(); raw["senders"][0]["token"]=""; cases.append((raw,"Token"))
        raw=valid_raw(); raw["agents"][0]["budgetChars"]=0; cases.append((raw,"正整數"))
        for candidate, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                parse_agent_management(candidate)

    def test_agent_alias_validation_and_enabled_uniqueness(self):
        cases=[]
        raw=valid_raw(); raw["agents"][0]["agentAlias"]=""; cases.append((raw,"agentAlias"))
        raw=valid_raw(); raw["agents"][0]["agentAlias"]="Bad Alias"; cases.append((raw,"格式"))
        raw=valid_raw(); raw["agents"][0]["agentAlias"]="stop"; cases.append((raw,"保留字"))
        raw=valid_raw(); raw["agents"][1]["agentAlias"]="planner"; cases.append((raw,"必須唯一"))
        for candidate, expected in cases:
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, expected):
                parse_agent_management(candidate)

        disabled=valid_raw(); disabled["agents"][1]["agentAlias"]="planner"; disabled["agents"][1]["enabled"]=False
        self.assertEqual(parse_agent_management(disabled).agents[1].agent_alias,"planner")

    def test_secret_blank_keeps_existing_and_explicit_clear_is_validated(self):
        current=parse_agent_management(valid_raw())
        candidate=valid_raw(); candidate["senders"][0]["token"]=""
        merged=merge_secret_placeholders(candidate,current)
        self.assertEqual(merged["senders"][0]["token"],"private-test-placeholder")
        candidate["senders"][0]["clearToken"]=True
        with self.assertRaisesRegex(ValueError,"Token"):
            parse_agent_management(merge_secret_placeholders(candidate,current))

    def test_atomic_save_and_failed_replace_preserve_original(self):
        value=parse_agent_management(valid_raw())
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"local.json"
            path.write_text("original",encoding="utf-8")
            with patch("agent_bridge.agent_management.os.replace",side_effect=OSError("fail")):
                with self.assertRaises(OSError): save_agent_management(path,value)
            self.assertEqual(path.read_text(encoding="utf-8"),"original")
            save_agent_management(path,value)
            self.assertEqual(load_agent_management(path,{},"",0),value)

    def test_legacy_default_sender_requires_no_migration(self):
        legacy={"sharedDiscussion":{"participants":[{"agentId":"a","displayName":"A","adapter":"codex","mentionId":"123456789012345678","budgetChars":100,"maxCalls":1}]}}
        with tempfile.TemporaryDirectory() as directory:
            value=load_agent_management(Path(directory)/"missing.json",legacy,"legacy-secret",323456789012345678)
        self.assertEqual(value.agents[0].sender_id,"legacy-default")
        self.assertEqual(value.agents[0].agent_alias,"a")
        self.assertEqual(value.senders[0].token,"legacy-secret")

    def test_binding_status_is_read_only_and_adapter_mismatch_rejected(self):
        value=parse_agent_management(valid_raw())
        raw={"logicalBindings":[{"agentId":"a","bindingId":"binding-a","activeGeneration":1}],"codexBindings":[{"bindingId":"binding-a","generation":1}]}
        validate_binding_associations(value,raw)
        changed=valid_raw();changed["agents"][0]["adapter"]="antigravity"
        with self.assertRaisesRegex(ValueError,"native mapping"):
            validate_binding_associations(parse_agent_management(changed),raw)

    def test_all_room_bindings_for_an_agent_are_validated(self):
        value=parse_agent_management(valid_raw())
        raw={
            "logicalBindings":[
                {"roomId":"one","agentId":"a","bindingId":"binding-a1","activeGeneration":1},
                {"roomId":"two","agentId":"a","bindingId":"binding-a2","activeGeneration":2},
            ],
            "codexBindings":[{"bindingId":"binding-a2","generation":2}],
        }
        with self.assertRaisesRegex(ValueError,"native mapping"):
            validate_binding_associations(value,raw)

    def test_current_binding_schema_is_validated(self):
        value=parse_agent_management(valid_raw())
        raw={
            "bindingLineages":[{"agentId":"a","bindingId":"binding-a","generations":[3]}],
            "activeBindings":[{"roomId":"one","agentId":"a","bindingId":"binding-a","activeGeneration":3}],
            "codexBindings":[{"bindingId":"binding-a","generation":3}],
        }
        validate_binding_associations(value,raw)
        changed=valid_raw();changed["agents"][0]["adapter"]="antigravity"
        with self.assertRaisesRegex(ValueError,"native mapping"):
            validate_binding_associations(parse_agent_management(changed),raw)

    def test_bound_agent_removal_is_rejected_but_unbound_removal_is_allowed(self):
        current=parse_agent_management(valid_raw())
        candidate=valid_raw(); candidate["agents"]=[candidate["agents"][1]]
        removed=parse_agent_management(candidate)
        bindings={"logicalBindings":[{"roomId":"one","agentId":"a","bindingId":"binding-a","activeGeneration":1}]}
        with self.assertRaisesRegex(ValueError,"有 Binding"):
            validate_removals(current,removed,bindings)
        validate_removals(current,removed,{"logicalBindings":[]})

    def test_detached_lineage_still_blocks_agent_removal(self):
        current=parse_agent_management(valid_raw())
        candidate=valid_raw(); candidate["agents"]=[candidate["agents"][1]]
        removed=parse_agent_management(candidate)
        bindings={"bindingLineages":[{"agentId":"a","bindingId":"binding-a","generations":[1]}],"activeBindings":[]}
        with self.assertRaisesRegex(ValueError,"有 Binding"):
            validate_removals(current,removed,bindings)

    def test_sender_removal_requires_reassigning_agents(self):
        candidate=valid_raw(); candidate["senders"]=[]
        with self.assertRaisesRegex(ValueError,"找不到對應 Sender"):
            parse_agent_management(candidate)

    def test_shared_and_dedicated_identity_routing(self):
        async def run():
            shared=parse_agent_management(valid_raw())
            delivery=DiscordSenderDelivery(shared); channel=FakeChannel(); delivery.register_sender("shared",FakeClient(channel)); delivery.register_room("room",1)
            raw_text="canonical"
            self.assertEqual(await delivery.deliver("room","a",raw_text,"r"),"delivered")
            self.assertEqual(await delivery.deliver("room","b",raw_text,"r"),"delivered")
            self.assertEqual(channel.sent,["Agent A: canonical","Agent B: canonical"])
            self.assertEqual(raw_text,"canonical")

            dedicated=valid_raw(); dedicated["senders"].append({"senderId":"only-b","label":"B","botUserId":"423456789012345678","token":"test-b","enabled":True}); dedicated["agents"][1]["senderId"]="only-b"
            value=parse_agent_management(dedicated); a,b=FakeChannel(),FakeChannel(); routed=DiscordSenderDelivery(value); routed.register_sender("shared",FakeClient(a));routed.register_sender("only-b",FakeClient(b));routed.register_room("room",1)
            await routed.deliver("room","a","one","r1");await routed.deliver("room","b","two","r2")
            self.assertEqual(a.sent,["one"]);self.assertEqual(b.sent,["two"])
        asyncio.run(run())

    def test_missing_sender_client_fails_closed_and_length_includes_prefix(self):
        async def run():
            value=parse_agent_management(valid_raw()); delivery=DiscordSenderDelivery(value,message_limit=10); delivery.register_room("room",1)
            self.assertEqual(await delivery.deliver("room","a","ok","r"),"not_delivered")
            fallback=FakeChannel(); delivery.register_sender("shared",FakeClient(fallback))
            self.assertEqual(await delivery.deliver("room","a","1234","r"),"not_delivered")
            self.assertFalse(fallback.sent)
        asyncio.run(run())

    def test_channel_resolution_failure_is_not_delivered_but_send_failure_is_unknown(self):
        async def run():
            value=parse_agent_management(valid_raw()); delivery=DiscordSenderDelivery(value); delivery.register_room("room",1)
            delivery.register_sender("shared",FakeClient(fetch_error=RuntimeError("not ready")))
            self.assertEqual(await delivery.deliver("room","a","hello","r1"),"not_delivered")
            delivery.register_sender("shared",FakeClient(FakeChannel(RuntimeError("ambiguous send"))))
            self.assertEqual(await delivery.deliver("room","a","hello","r2"),"unknown")
        asyncio.run(run())

    def test_admin_api_masks_token_enforces_origin_and_has_no_binding_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=root/"agent-management.local.json"; save_agent_management(config,parse_agent_management(valid_raw()))
            server=admin_ui.create_server(config,root/"missing-bindings.json",{},"",0,0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f"http://127.0.0.1:{server.server_port}"
            try:
                state=json.loads(request.urlopen(base+"/api/state").read())
                self.assertNotIn("token",state["senders"][0])
                body=json.dumps(valid_raw()).encode()
                bad=request.Request(base+"/api/save",data=body,method="POST",headers={"Content-Type":"application/json"})
                with self.assertRaises(error.HTTPError) as caught: request.urlopen(bad)
                self.assertEqual(caught.exception.code,403)
                self.assertIn("save-status",admin_ui.HTML)
                self.assertIn("Sender ID 未變",admin_ui.HTML)
                self.assertIn("catch(error)",admin_ui.HTML)
                self.assertIn("removeAgent",admin_ui.HTML)
                self.assertIn("removeSender",admin_ui.HTML)
                self.assertNotIn('data-k="available"',admin_ui.HTML)
                self.assertNotIn('data-k="mentionId"',admin_ui.HTML)
                self.assertIn('data-k="agentAlias"',admin_ui.HTML)
                self.assertIn("一般 human-turn 不使用這兩項額度",admin_ui.HTML)
                self.assertIn("white-space:nowrap",admin_ui.HTML)
                state["agents"][0]["displayName"]="Updated Agent"
                state["senders"][0]["token"]=""
                saved=request.Request(
                    base+"/api/save",data=json.dumps(state).encode(),method="POST",
                    headers={"Content-Type":"application/json","Origin":f"http://localhost:{server.server_port}","X-CSRF-Token":server.csrf_token},
                )
                self.assertEqual(json.loads(request.urlopen(saved).read()),{"saved":True})
                updated=load_agent_management(config,{},"",0)
                self.assertEqual(updated.agents[0].display_name,"Updated Agent")
                self.assertEqual(updated.senders[0].token,"private-test-placeholder")
                self.assertNotIn("rebind",admin_ui.HTML.lower());self.assertNotIn("retire",admin_ui.HTML.lower())
            finally:
                server.shutdown();server.server_close();thread.join(2)

    def test_admin_binding_action_requires_runtime_and_uses_bot_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); config=root/"agent-management.local.json"; save_agent_management(config,parse_agent_management(valid_raw()))
            loop=asyncio.new_event_loop(); loop_thread=threading.Thread(target=loop.run_forever,daemon=True);loop_thread.start()
            admin=FakeBindingAdmin()
            server=admin_ui.create_server(config,root/"missing-bindings.json",{},"",0,0,binding_admin=admin,event_loop=loop)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f"http://127.0.0.1:{server.server_port}"
            try:
                state=json.loads(request.urlopen(base+"/api/state").read())
                self.assertEqual(state["bindingControl"]["pendingOnboarding"][0]["roomId"],"room")
                body=json.dumps({"roomId":"room","agentId":"a"}).encode()
                req=request.Request(base+"/api/bindings/cancel",data=body,method="POST",headers={"Content-Type":"application/json","Origin":f"http://localhost:{server.server_port}","X-CSRF-Token":server.csrf_token})
                self.assertEqual(json.loads(request.urlopen(req).read()),{"ok":True})
                for path, payload in [
                    ("bind-existing", {"roomId":"room","agentId":"a","nativeReference":"existing"}),
                    ("create", {"roomId":"room","agentId":"a"}),
                ]:
                    req=request.Request(base+f"/api/bindings/{path}",data=json.dumps(payload).encode(),method="POST",headers={"Content-Type":"application/json","Origin":f"http://localhost:{server.server_port}","X-CSRF-Token":server.csrf_token})
                    self.assertEqual(json.loads(request.urlopen(req).read()),{"ok":True})
                self.assertEqual(admin.calls,[
                    ("cancel","room","a"),
                    ("bind-existing","room","a","existing"),
                    ("create","room","a"),
                ])
            finally:
                server.shutdown();server.server_close();thread.join(2);loop.call_soon_threadsafe(loop.stop);loop_thread.join(2);loop.close()


if __name__ == "__main__": unittest.main()
