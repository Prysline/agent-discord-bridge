# Agent Discord Bridge

將私人 Discord 頻道中的訊息安全地轉交給本機 AI agent runtime。專案目標是讓不同 runtime 共用同一套 Discord 存取邊界、對話政策與公開契約，只在 adapter 層處理各家差異。

> 本專案不是 Discord、OpenAI 或 Google 官方工具。目前正從既有 Codex 與 Antigravity 實作遷移；尚未完成的 adapter 不應視為已支援。

## 目前狀態

- Codex：已遷入既有、通過測試的 Discord bridge 與 persistent adapter core。
- Antigravity：尚未遷入；既有 fork 仍是唯讀遷移來源。
- 共用 shared core：已依 frozen contract 建立 canonical event log、persistent event-delta cursor、human-turn／bounded-discussion orchestration、AgentResult validation 與 delivery/contextCommit fence；Codex root 已有 opt-in human-turn 與 bounded-discussion wiring，Antigravity adapter 尚未遷入。
- Claude Code：未 bundled、未驗證；未來可依公開 adapter contract 由有環境者提交 PR。

目前根目錄程式仍代表 Codex migration baseline。它可以用來驗證既有行為，但目錄結構不是最終公開 API。

## 安全模型

- Discord 訊息是不受信任的對話資料，不是系統指令或工具授權。
- 人類帳號、頻道與 peer Bot 均採明確 allowlist；設定不完整時拒絕啟動。
- Token、Discord ID、persona、thread mapping 與私人工作資料只放在不納入版控的本機檔案。
- Adapter 不得自行擴張到檔案、shell、網路、外部發布或共同記憶權限。
- 多 Agent 討論必須有可驗證的停止條件，不能讓單一 Agent 無限占用回合或額度。

## Codex migration baseline

既有檔案包含：

- `bot.py`：Discord 與 Codex app-server 橋接程式。
- `conversation_policy.py`：目前 Codex 版的 allowlist、提及／回覆與 peer turn limiter。
- `codex_adapter/`：persistent shared-lane adapter core。
- `agent_bridge/contracts.py`：跨 adapter 的 AgentRequest／AgentResult v1 validation。
- `agent_bridge/conversation_policy.py`：core-owned bounded-discussion scheduler、closing check、quota accounting 與 restart／stop state machine。
- `agent_bridge/binding_control.py` 與 `agent_bridge/binding_bootstrap.py`：memory-only logical binding state 與本機 existing-binding bootstrap parser。
- `agent_bridge/orchestrator.py`：memory-only canonical log、human-turn、discussion、cursor、delivery 與 contextCommit coordination。
- `tests/`：routing policy 與 Codex adapter tests。
- `shared_workspace/`：預設隔離工作目錄說明。

Shared orchestration core 已能以 fake adapter 驗證 `continue`／`complete`／`abstain` lifecycle，並讓 Codex persistent adapter 重用 shared request validator；本機 bootstrap 以 exact `bindingId + generation` 載入既有 Codex thread。Opt-in human-turn root path 已完成真人 Discord E2E；bounded discussion 已接上 root，但只有自動測試，尚未完成真人 Discord E2E。Codex adapter 對真實模型 final 仍只回傳 `continue`，durable restart、完整 production rollout 與端到端 closing-check 仍未完成。

## 本機設定

需要 Python 3.11+、Discord Bot 與對應 runtime。Discord Developer Portal 必須啟用 Message Content Intent；Bot 原則上只需 View Channel、Send Messages、Read Message History 與 Add Reactions，不應授予 Administrator。

Codex baseline 的設定方式：

```powershell
Copy-Item .env.example .env
Copy-Item config.example.json config.json
Copy-Item persona.example.md persona.md
Copy-Item bindings.example.json bindings.local.json
python -m pip install -r requirements.txt
```

`bindings.local.json` 只載入人類事先建立的 existing binding；shared state 僅保留 logical identity，Codex thread ID 只存在 adapter-local resolver。設定矛盾會在 bootstrap 時拒絕載入，不會建立或替換 thread。真實 Token、numeric ID、native thread ID 與 persona 不得提交。

設定 `SHARED_CORE_ENABLED=true` 與明確的 `SHARED_AGENT_ID` 後，root bot 的 authorized human-turn 會使用 bootstrap existing binding、shared canonical event-delta 與 `CodexPersistentAdapter`；此模式不讀 Discord recent history、不建立 thread，也不在失敗時 fallback legacy execution。`config.json` 的 `sharedDiscussion` 明確列出可參與 `!discuss` 的 Discord mention、logical `agentId`、個別字元／呼叫額度與 unattended dispatch hard limit；每位 participant 都必須在 `bindings.local.json` 有 existing binding。`!stop` 會進 shared stop lifecycle，root 不另寫 cancellation semantics。目前仍只支援單則 Discord delivery，peer ingestion、chunking 與 durable persistence 尚未接線。

## 驗證

```powershell
python -m py_compile bot.py conversation_policy.py agent_bridge\conversation_policy.py
python -m unittest discover -s tests -v
git diff --check
```

自動測試不會登入 Discord，也不會使用真實 Token。Human-turn root path 已完成一次真人 Manual E2E；bounded discussion 與 `!stop` 仍需使用真實 participant bindings 另做 Manual E2E，不能由本機測試推定完成。

## 授權與來源

本專案以 MIT License 發布。遷移來源與保留的署名請見 [`SOURCE.md`](SOURCE.md)。
