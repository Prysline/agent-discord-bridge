"""Local browser UI for Agent and Discord sender configuration."""

from __future__ import annotations

import argparse
import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

from .agent_management import (
    load_agent_management, merge_secret_placeholders, parse_agent_management,
    save_agent_management, validate_binding_associations,
)
from .binding_bootstrap import build_binding_control


HTML = r"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Agent 管理</title>
<style>body{font:15px system-ui;max-width:1100px;margin:32px auto;padding:0 18px;color:#202124}h1{margin-bottom:4px}.note{color:#666}table{border-collapse:collapse;width:100%;margin:12px 0 28px}th,td{border:1px solid #ddd;padding:8px;text-align:left}input,select{width:100%;box-sizing:border-box;padding:6px}input[type=checkbox]{width:auto}.error{color:#b3261e;white-space:pre-wrap}.ok{color:#137333}button{padding:8px 14px;margin-right:8px}</style>
<h1>Agent 與 Discord 發言身分</h1><p class=note>Binding 僅供查看。儲存後需重新啟動 Bot 才生效。</p>
<h2>Agents</h2><table><thead><tr><th>Agent ID</th><th>顯示名稱</th><th>Adapter</th><th>Mention ID</th><th>Sender</th><th>啟用</th><th>可用</th><th>字元</th><th>Calls</th><th>Binding</th></tr></thead><tbody id=agents></tbody></table>
<h2>Discord Senders</h2><table><thead><tr><th>Sender ID</th><th>標籤</th><th>Bot User ID</th><th>新 Token（留白保留）</th><th>Token</th><th>狀態</th><th>啟用</th><th>使用者</th></tr></thead><tbody id=senders></tbody></table>
<button onclick="addAgent()">新增 Agent</button><button onclick="addSender()">新增 Sender</button><button onclick="save()">驗證並儲存</button><p id=status></p>
<script>const csrf="__CSRF__";let state={agents:[],senders:[]};const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function draw(){agents.innerHTML=state.agents.map((a,i)=>`<tr><td><input value="${esc(a.agentId)}" ${a._new?'':'readonly'} data-a="${i}" data-k="agentId"></td><td><input value="${esc(a.displayName)}" data-a="${i}" data-k="displayName"></td><td><select data-a="${i}" data-k="adapter"><option ${a.adapter==='codex'?'selected':''}>codex</option><option ${a.adapter==='antigravity'?'selected':''}>antigravity</option></select></td><td><input value="${esc(a.mentionId)}" data-a="${i}" data-k="mentionId"></td><td><select data-a="${i}" data-k="senderId">${state.senders.map(s=>`<option value="${esc(s.senderId)}" ${s.senderId===a.senderId?'selected':''}>${esc(s.label)}</option>`).join('')}</select></td><td><input type=checkbox ${a.enabled?'checked':''} data-a="${i}" data-k="enabled"></td><td><input type=checkbox ${a.available?'checked':''} data-a="${i}" data-k="available"></td><td><input type=number min=1 value="${a.budgetChars}" data-a="${i}" data-k="budgetChars"></td><td><input type=number min=1 value="${a.maxCalls}" data-a="${i}" data-k="maxCalls"></td><td>${(a.bindings||[]).map(b=>esc(`${b.bindingId} / ${b.generation} / native ${b.nativeMappingAvailable?'可用':'缺失'}`)).join('<br>')||'不存在'}</td></tr>`).join('');senders.innerHTML=state.senders.map((s,i)=>`<tr><td><input value="${esc(s.senderId)}" data-s="${i}" data-k="senderId"></td><td><input value="${esc(s.label)}" data-s="${i}" data-k="label"></td><td><input value="${esc(s.botUserId)}" data-s="${i}" data-k="botUserId"></td><td><input type=password value="" data-s="${i}" data-k="token" autocomplete=new-password></td><td>${s.tokenConfigured?'是':'否'}</td><td>${esc(s.status)}</td><td><input type=checkbox ${s.enabled?'checked':''} data-s="${i}" data-k="enabled"></td><td>${(s.usedByAgents||[]).map(esc).join(', ')}</td></tr>`).join('')}
function sync(){document.querySelectorAll('[data-a]').forEach(e=>{let v=e.type==='checkbox'?e.checked:e.type==='number'?Number(e.value):e.value;state.agents[+e.dataset.a][e.dataset.k]=v});document.querySelectorAll('[data-s]').forEach(e=>{let v=e.type==='checkbox'?e.checked:e.value;state.senders[+e.dataset.s][e.dataset.k]=v})}function addAgent(){sync();state.agents.push({_new:true,agentId:'',displayName:'',adapter:'codex',mentionId:'',senderId:state.senders[0]?.senderId||'',enabled:true,available:true,budgetChars:2000,maxCalls:5,bindings:[]});draw()}function addSender(){sync();state.senders.push({senderId:'',label:'',botUserId:'',token:'',tokenConfigured:false,enabled:true,usedByAgents:[]});draw()}
async function save(){sync();let body={agents:state.agents.map(({bindings,_new,...x})=>x),senders:state.senders.map(({tokenConfigured,usedByAgents,...x})=>x)};let r=await fetch('/api/save',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)});let out=await r.json();status.className=r.ok?'ok':'error';status.textContent=r.ok?'設定已儲存，請重新啟動 Bot。':out.error;if(r.ok)load()}async function load(){state=await(await fetch('/api/state')).json();draw()}load();</script></html>"""


def create_server(config_path: Path, binding_path: Path, legacy_config: dict, legacy_token: str, legacy_bot_id: int, port: int = 8766) -> ThreadingHTTPServer:
    csrf = secrets.token_urlsafe(32)
    bindings = {}
    binding_raw = {}
    native_pairs = {"codex": set(), "antigravity": set()}
    if binding_path.exists():
        binding_raw = json.loads(binding_path.read_text(encoding="utf-8"))
        bindings = build_binding_control(binding_raw).snapshot().active_by_room_agent
        native_pairs = {
            "codex": {(item.get("bindingId"), item.get("generation")) for item in binding_raw.get("codexBindings", []) if isinstance(item, dict)},
            "antigravity": {(item.get("bindingId"), item.get("generation")) for item in binding_raw.get("antigravityBindings", []) if isinstance(item, dict)},
        }

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                body = HTML.replace("__CSRF__", csrf).encode("utf-8")
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
            if self.path == "/api/state":
                value = load_agent_management(config_path, legacy_config, legacy_token, legacy_bot_id)
                self._json(200, value.redacted(bindings, native_pairs)); return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            expected = f"http://127.0.0.1:{self.server.server_port}"
            if self.path != "/api/save" or self.headers.get("Origin") != expected or self.headers.get("X-CSRF-Token") != csrf or self.headers.get_content_type() != "application/json":
                self._json(403, {"error": "request rejected"}); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 1024 * 1024: raise ValueError("request body 大小錯誤")
                candidate = json.loads(self.rfile.read(length))
                current = load_agent_management(config_path, legacy_config, legacy_token, legacy_bot_id)
                value = parse_agent_management(merge_secret_placeholders(candidate, current))
                validate_binding_associations(value, binding_raw)
                save_agent_management(config_path, value)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)}); return
            self._json(200, {"saved": True})

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.csrf_token = csrf  # type: ignore[attr-defined]
    return server


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
    server.serve_forever()


if __name__ == "__main__":
    main()
