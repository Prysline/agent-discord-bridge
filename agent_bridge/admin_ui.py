"""Local browser UI for Agent and Discord sender configuration."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

from .agent_management import (
    load_agent_management, merge_secret_placeholders, parse_agent_management,
    save_agent_management, validate_binding_associations, validate_removals,
)
from .binding_bootstrap import build_binding_control
from .binding_operations import BindingAdminControl


HTML = r"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Agent 管理</title>
<style>body{font:15px system-ui;max-width:1100px;margin:32px auto;padding:0 18px;color:#202124}h1{margin-bottom:4px}.note{color:#666}table{border-collapse:collapse;width:100%;margin:12px 0 28px}th,td{border:1px solid #ddd;padding:8px;text-align:left}th{white-space:nowrap}input,select{width:100%;box-sizing:border-box;padding:6px}input[type=checkbox]{width:auto}.error{color:#b3261e;white-space:pre-wrap}.ok{color:#137333}button{padding:8px 14px;margin-right:8px;white-space:nowrap}.action{width:1%;white-space:nowrap}</style>
<h1>Agent 與 Discord 發言身分</h1><p class=note>Agent／Sender 設定儲存後需重新啟動 Bot；Binding 操作在 Bot 內嵌前台會立即生效。獨立啟動前台時 Binding 操作會被拒絕。關閉前台請停止對應的 Bot 或回到獨立前台終端機按 Ctrl+C。</p>
<h2>Agents</h2><p class=note>Alias 是共用 Bot 中的人類選擇名稱（例如 <code>@Bot planner: 訊息</code>），限小寫字母開頭及小寫字母、數字、底線、連字號，啟用中的 Alias 不可重複。「討論字元額度」計算一場 bounded discussion 內已送達的回覆字元；「討論呼叫上限」限制該 Agent 的模型啟動次數。一般 human-turn 不使用這兩項額度。</p><table><thead><tr><th>Agent ID</th><th>顯示名稱</th><th>Agent Alias</th><th>Adapter</th><th>Sender</th><th>啟用</th><th>討論字元額度</th><th>討論呼叫上限</th><th>Binding</th><th class=action>移除</th></tr></thead><tbody id=agents></tbody></table>
<h2>Discord Senders</h2><p class=note>只有 Sender ID 未變且 Token 已設定時，Token 留白才會保留原值；新增或更名的 Sender 必須輸入 Token。移除只更新這份管理清單；有 Binding 的 Agent 無法在此移除，移除 Sender 也不會影響 AI 對話。</p><table><thead><tr><th>Sender ID</th><th>標籤</th><th>Bot User ID</th><th>新 Token</th><th>Token</th><th>狀態</th><th>啟用</th><th>使用者</th><th class=action>移除</th></tr></thead><tbody id=senders></tbody></table>
<h2>待完成的頻道綁定</h2><p class=note>可重新連結 Bridge 已管理的聊天窗；支援 control plane 的 Adapter 也可驗證既有 native 聊天窗或明確建立新聊天窗。建立結果不明時不會自動重試。</p><div id=pending-bindings></div>
<h2>Binding 管理</h2><p class=note>搬移會保留原聊天窗上下文，但不搬移 Discord 頻道歷史。解除只移除頻道關聯，不會刪除 Discord 頻道、Agent 本地聊天窗或聊天紀錄。</p><div id=binding-management></div><p id=binding-status></p>
<button onclick="addAgent()">新增 Agent</button><button onclick="addSender()">新增 Sender</button><button onclick="save()">驗證並儲存</button><p id=save-status></p>
<script>const csrf="__CSRF__";let state={agents:[],senders:[]};const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function draw(){agents.innerHTML=state.agents.map((a,i)=>`<tr><td><input value="${esc(a.agentId)}" ${a._new?'':'readonly'} data-a="${i}" data-k="agentId"></td><td><input value="${esc(a.displayName)}" data-a="${i}" data-k="displayName"></td><td><input value="${esc(a.agentAlias)}" data-a="${i}" data-k="agentAlias"></td><td><select data-a="${i}" data-k="adapter"><option ${a.adapter==='codex'?'selected':''}>codex</option><option ${a.adapter==='antigravity'?'selected':''}>antigravity</option></select></td><td><select data-a="${i}" data-k="senderId">${state.senders.map(s=>`<option value="${esc(s.senderId)}" ${s.senderId===a.senderId?'selected':''}>${esc(s.label)}</option>`).join('')}</select></td><td><input type=checkbox ${a.enabled?'checked':''} data-a="${i}" data-k="enabled"></td><td><input type=number min=1 value="${a.budgetChars}" data-a="${i}" data-k="budgetChars"></td><td><input type=number min=1 value="${a.maxCalls}" data-a="${i}" data-k="maxCalls"></td><td>${(a.bindings||[]).map(b=>`Binding：${esc(b.bindingId)}<br>版本：${esc(b.generation)}<br>既有 AI 對話：${b.nativeMappingAvailable?'可用':'缺失'}`).join('<br><br>')||'不存在'}</td><td class=action><button ${a.bindings?.length?'disabled title="有 Binding 時只能停用"':''} onclick="removeAgent(${i})">移除</button></td></tr>`).join('');senders.innerHTML=state.senders.map((s,i)=>`<tr><td><input value="${esc(s.senderId)}" data-s="${i}" data-k="senderId"></td><td><input value="${esc(s.label)}" data-s="${i}" data-k="label"></td><td><input value="${esc(s.botUserId)}" data-s="${i}" data-k="botUserId"></td><td><input type=password value="" data-s="${i}" data-k="token" autocomplete=new-password></td><td>${s.tokenConfigured?'是':'否'}</td><td>${esc(s.status)}</td><td><input type=checkbox ${s.enabled?'checked':''} data-s="${i}" data-k="enabled"></td><td>${(s.usedByAgents||[]).map(esc).join(', ')}</td><td class=action><button onclick="removeSender(${i})">移除</button></td></tr>`).join('');drawBindings()}
function drawBindings(){const c=state.bindingControl||{pendingOnboarding:[],bindings:[]};document.getElementById('pending-bindings').innerHTML=c.pendingOnboarding.map((p,i)=>{const choices=c.bindings.filter(b=>b.agentId===p.agentId&&!b.active&&b.nativeMappingAvailable);return `<div><b>${esc(p.agentDisplayName)}</b>｜Room ${esc(p.roomId)}｜${esc(p.createdAt)}<br><small>${esc(p.triggerPreview)}</small><br><select id="pending-choice-${i}">${choices.map(b=>`<option value="${esc(b.bindingId)}|${b.generation}">${esc(b.bindingId)} / ${b.generation}</option>`).join('')}</select><button ${choices.length?'':'disabled'} onclick="attachPending(${i})">連結已管理聊天窗</button> ${p.canBindExisting?`<input id="native-reference-${i}" placeholder="既有 native 聊天窗 ID" style="width:260px"><button onclick="bindExisting(${i})">驗證並綁定</button>`:''}<button ${p.canCreate?'':'disabled title="此 Adapter 尚未提供已驗證的 create control plane"'} onclick="createPending(${i})">建立新聊天窗</button><button onclick="cancelPending(${i})">取消</button></div>`}).join('')||'目前沒有待處理項目。';document.getElementById('binding-management').innerHTML=c.bindings.filter(b=>b.active).map(b=>`<div><b>${esc(b.agentDisplayName)}</b>｜Room ${esc(b.roomId)}｜Binding ${esc(b.bindingId)} / ${b.generation}｜${b.nativeMappingAvailable?'native 可用':'native 缺失'} <button onclick="moveBinding('${esc(b.roomId)}','${esc(b.agentId)}')">搬移到其他頻道</button><button onclick="unbindBinding('${esc(b.roomId)}','${esc(b.agentId)}')">解除頻道綁定</button></div>`).join('')||'目前沒有 active Binding。'}
async function bindingAction(path,body){const f=document.getElementById('binding-status');f.textContent='處理中…';f.className='';try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});const out=await r.json();f.className=r.ok?'ok':'error';f.textContent=r.ok?'操作完成。':(out.error||`操作失敗（HTTP ${r.status}）`);if(r.ok)await load()}catch(e){f.className='error';f.textContent=`操作失敗：${e instanceof Error?e.message:String(e)}`}}
function attachPending(i){const p=state.bindingControl.pendingOnboarding[i],v=document.getElementById(`pending-choice-${i}`).value,[bindingId,g]=v.split('|');bindingAction('/api/bindings/attach',{roomId:p.roomId,agentId:p.agentId,bindingId,generation:Number(g)})}function bindExisting(i){const p=state.bindingControl.pendingOnboarding[i],nativeReference=document.getElementById(`native-reference-${i}`).value.trim();if(nativeReference)bindingAction('/api/bindings/bind-existing',{roomId:p.roomId,agentId:p.agentId,nativeReference})}function createPending(i){const p=state.bindingControl.pendingOnboarding[i];if(confirm(`要為 ${p.agentDisplayName} 建立新的持久聊天窗嗎？`))bindingAction('/api/bindings/create',{roomId:p.roomId,agentId:p.agentId})}function cancelPending(i){const p=state.bindingControl.pendingOnboarding[i];bindingAction('/api/bindings/cancel',{roomId:p.roomId,agentId:p.agentId})}function moveBinding(roomId,agentId){const target=prompt('輸入目標 Discord Channel ID');if(target)bindingAction('/api/bindings/move',{sourceRoomId:roomId,targetRoomId:target,agentId})}function unbindBinding(roomId,agentId){if(confirm('只解除 Bridge 的頻道綁定；不會刪除 Discord 頻道、Agent 本地聊天窗或聊天紀錄。確定繼續？'))bindingAction('/api/bindings/unbind',{roomId,agentId})}
function sync(){document.querySelectorAll('[data-a]').forEach(e=>{let v=e.type==='checkbox'?e.checked:e.type==='number'?Number(e.value):e.value;state.agents[+e.dataset.a][e.dataset.k]=v});document.querySelectorAll('[data-s]').forEach(e=>{let v=e.type==='checkbox'?e.checked:e.value;state.senders[+e.dataset.s][e.dataset.k]=v})}function addAgent(){sync();state.agents.push({_new:true,agentId:'',displayName:'',agentAlias:'',adapter:'codex',mentionId:state.ingressBotUserId||'',senderId:state.senders[0]?.senderId||'',enabled:true,available:true,budgetChars:2000,maxCalls:5,bindings:[]});draw()}function addSender(){sync();state.senders.push({_new:true,senderId:'',label:'',botUserId:'',token:'',tokenConfigured:false,enabled:true,usedByAgents:[]});draw()}function removeAgent(i){sync();let a=state.agents[i];if(a.bindings?.length){alert('有 Binding 的 Agent 不可移除，請改為停用。');return}if(a._new||confirm(`確定從管理設定移除 Agent ${a.agentId}？`)){state.agents.splice(i,1);draw()}}function removeSender(i){sync();let s=state.senders[i];if(state.agents.some(a=>a.senderId===s.senderId)){alert('此 Sender 仍被 Agent 使用，請先改派 Sender。');return}if(s._new||confirm(`確定從管理設定移除 Sender ${s.senderId}？`)){state.senders.splice(i,1);draw()}}
async function save(){const feedback=document.getElementById('save-status');feedback.className='';feedback.textContent='正在驗證並儲存…';try{sync();let body={agents:state.agents.map(({bindings,_new,...x})=>x),senders:state.senders.map(({tokenConfigured,usedByAgents,_new,...x})=>x)};let r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});let out=await r.json();feedback.className=r.ok?'ok':'error';feedback.textContent=r.ok?'設定已儲存，請重新啟動 Bot。':(out.error||`儲存失敗（HTTP ${r.status}）`);if(r.ok)await load()}catch(error){feedback.className='error';feedback.textContent=`儲存失敗：${error instanceof Error?error.message:String(error)}`}}async function load(){state=await(await fetch('/api/state')).json();draw()}load();</script></html>"""


def create_server(config_path: Path, binding_path: Path, legacy_config: dict, legacy_token: str, legacy_bot_id: int, port: int = 8766, *, binding_admin: BindingAdminControl | None = None, event_loop: asyncio.AbstractEventLoop | None = None) -> ThreadingHTTPServer:
    csrf = secrets.token_urlsafe(32)

    def binding_view():
        if not binding_path.exists():
            return {}, {}, {"codex": set(), "antigravity": set()}
        raw = json.loads(binding_path.read_text(encoding="utf-8"))
        active = build_binding_control(raw).snapshot().active_by_room_agent
        native = {
            "codex": {(item.get("bindingId"), item.get("generation")) for item in raw.get("codexBindings", []) if isinstance(item, dict)},
            "antigravity": {(item.get("bindingId"), item.get("generation")) for item in raw.get("antigravityBindings", []) if isinstance(item, dict)},
        }
        return raw, active, native

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                body = HTML.replace("__CSRF__", csrf).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if self.path == "/api/state":
                _, bindings, native_pairs = binding_view()
                value = load_agent_management(config_path, legacy_config, legacy_token, legacy_bot_id)
                state = value.redacted(bindings, native_pairs)
                state["ingressBotUserId"] = str(legacy_bot_id)
                if binding_admin is not None:
                    state["bindingControl"] = binding_admin.state()
                self._json(200, state); return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            allowed_origins = {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            }
            allowed_paths = {"/api/save", "/api/bindings/attach", "/api/bindings/bind-existing", "/api/bindings/create", "/api/bindings/cancel", "/api/bindings/move", "/api/bindings/unbind"}
            if self.path not in allowed_paths or self.headers.get("Origin") not in allowed_origins or self.headers.get("X-CSRF-Token") != csrf or self.headers.get_content_type() != "application/json":
                self._json(403, {"error": "request rejected"}); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 1024 * 1024: raise ValueError("request body 大小錯誤")
                candidate = json.loads(self.rfile.read(length))
                if self.path != "/api/save":
                    if binding_admin is None or event_loop is None:
                        self._json(409, {"error": "Binding runtime control 尚未連線"}); return
                    result = _run_binding_action(binding_admin, event_loop, self.path, candidate)
                    self._json(200, result); return
                current = load_agent_management(config_path, legacy_config, legacy_token, legacy_bot_id)
                binding_raw, _, _ = binding_view()
                value = parse_agent_management(merge_secret_placeholders(candidate, current))
                validate_removals(current, value, binding_raw)
                validate_binding_associations(value, binding_raw)
                save_agent_management(config_path, value)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)}); return
            except Exception:
                self._json(500, {"error": "Binding 操作未完成；請檢查 Bot 狀態後再試。"}); return
            self._json(200, {"saved": True})

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.csrf_token = csrf  # type: ignore[attr-defined]
    return server


def _run_binding_action(admin: BindingAdminControl, loop: asyncio.AbstractEventLoop, path: str, body: object) -> dict[str, object]:
    if not isinstance(body, dict):
        raise ValueError("request body 必須是 object")
    def text(name: str) -> str:
        value = body.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name}: 必填")
        return value.strip()
    if path == "/api/bindings/attach":
        generation = body.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int):
            raise ValueError("generation: 必須是整數")
        coro = admin.attach_pending(text("roomId"), text("agentId"), text("bindingId"), generation)
    elif path == "/api/bindings/bind-existing":
        coro = admin.bind_existing(text("roomId"), text("agentId"), text("nativeReference"))
    elif path == "/api/bindings/create":
        coro = admin.create_pending(text("roomId"), text("agentId"))
    elif path == "/api/bindings/cancel":
        coro = admin.cancel_pending(text("roomId"), text("agentId"))
    elif path == "/api/bindings/move":
        coro = admin.move(text("sourceRoomId"), text("targetRoomId"), text("agentId"))
    else:
        coro = admin.unbind(text("roomId"), text("agentId"))
    asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=180)
    return {"ok": True}


def main() -> None:
    parser = argparse.ArgumentParser(description="本機 Agent 管理前台")
    parser.add_argument("--config", type=Path, default=Path("agent-management.local.json"))
    parser.add_argument("--bindings", type=Path, default=Path("bindings.local.json"))
    parser.add_argument("--legacy-config", type=Path, default=Path("config.json"))
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    load_dotenv(args.env)
    legacy = json.loads(args.legacy_config.read_text(encoding="utf-8"))
    server = create_server(
        args.config.resolve(), args.bindings.resolve(), legacy,
        os.getenv("DISCORD_TOKEN", ""), int(os.getenv("BOT_USER_ID", "0") or "0"),
        args.port,
    )
    print(f"Agent 管理前台：http://127.0.0.1:{server.server_port}")
    print("關閉前台請按 Ctrl+C。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAgent 管理前台已關閉。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
