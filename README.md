# Agent Discord Bridge

將私人 Discord 頻道中的訊息安全地轉交給本機 AI agent runtime。專案目標是讓不同 runtime 共用同一套 Discord 存取邊界、對話政策與公開契約，只在 adapter 層處理各家差異。

> 本專案不是 Discord、OpenAI 或 Google 官方工具。目前正從既有 Codex 與 Antigravity 實作遷移；尚未完成的 adapter 不應視為已支援。

## 目前狀態

- Codex：已遷入既有、通過測試的 Discord bridge 與 persistent adapter core。
- Antigravity：persistent adapter、exact existing-conversation resolver、authenticated Sidecar client、bounded transcript result recovery 與 heterogeneous composition 已在本機實作並通過自動測試；Codex + Antigravity cross-adapter 真人 Discord E2E 已通過。
- 共用 shared core：已依 frozen contract 建立 canonical event log、persistent event-delta cursor、human-turn／bounded-discussion orchestration、AgentResult validation 與 delivery/contextCommit fence；root composition 可依 participant 設定選用 Codex 或 Antigravity adapter。
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
- `antigravity_adapter/`：existing-conversation resolver、Sidecar transport 與 persistent shared-lane adapter。
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

`bindings.local.json` 只載入人類事先建立的 existing binding；shared state 僅保留 logical identity，Codex thread ID／Antigravity conversation ID 只存在各自 adapter-local resolver。設定矛盾會 fail closed，不會建立或替換 native session。真實 Token、numeric ID、native session ID 與 persona 不得提交。

Antigravity Sidecar 的追蹤範例位於 `antigravity_adapter/sidecar/sidecar.example.json`；安裝時須把 rendezvous 與 transcript root placeholders 換成本機設定。transcript root 必須明確指向包含各 conversation subtree 的 Antigravity brain root；worker 不猜 home、使用者名稱或私人路徑。`agentapi send-message` 負責 dispatch，`/result` 以 dispatch 前 byte offset、file identity、唯一 request marker 與 bounded JSONL delta 恢復 final；Stop hook 可用相同 extractor 提前完成，但只是 optional fast path。worker 只監聽 `127.0.0.1`，每次啟動輪替 bearer token 與 instance identity，並從 Antigravity 注入的 PATH discovery `agentapi`；不支援的 wrapper 會拒絕啟動，不會退回 CDP 或猜測安裝路徑。Windows runtime 另以 opaque named mutex、exclusive loopback socket 與經 creation-time驗證的 parent process handle保護單一 instance；parent watcher以 owned cancellation event安全退出，parent結束時 worker自行 shutdown。Rendezvous以 Windows atomic no-replace primitive發布；既有 live、stale 或無法判定 ownership 的 artifact一律 fail closed，worker不會覆寫它，也不會掃描或終止未知 process。

設定 `SHARED_CORE_ENABLED=true` 與明確的 `SHARED_AGENT_ID` 後，root bot 的 authorized human-turn 使用 bootstrap existing binding 與 shared canonical event-delta；此模式不讀 Discord recent history、不建立 native session，也不在失敗時 fallback legacy execution。`config.json` 的 participant 以 `adapter: codex|antigravity` 選擇 composition；Antigravity 另需把 `ANTIGRAVITY_RENDEZVOUS_PATH` 指向 Sidecar 每次啟動 atomic 發布的 gitignored runtime file。`!stop` 仍只走 shared invalidation；Antigravity 目前準確宣告 `canCancelInFlight=false`，late result 不能越過 core token。cross-adapter 真人 E2E 已完成；chunking 與 durable persistence尚未完成。

## 驗證

### 本機 Agent 管理前台

先複製 `agent-management.example.json` 為 gitignored 的
`agent-management.local.json`，再啟動：

```powershell
python -m agent_bridge.admin_ui
```

前台只監聽 `127.0.0.1:8766`，可編輯 Agent、adapter、討論額度與
Discord Sender。既有 Agent ID 為唯讀；Binding 只顯示狀態，不提供 create、
rebind、retire 或 generation mutation。Token 只會顯示「已設定」；只有 Sender ID
未變且 Token 已設定時，留白才會保留原值，新增或更名的 Sender 必須輸入 Token。
儲存採完整驗證後的單檔 atomic replace，並於重新啟動 Bot 後生效。可用
`127.0.0.1` 或 `localhost` 開啟；關閉前台請回到終端機按 `Ctrl+C`。

Agent 的「啟用」是正式參與開關。內部 `available` 欄位保留給 runtime 狀態，
不由管理前台手動編輯。有 Binding 的 Agent 只能停用，不能從管理設定移除。
Sender 仍被 Agent 引用時也不能移除。移除只更新管理清單，不會修改 Binding；
移除 Sender 也不會影響既有 AI 對話。

`Agent Alias` 是人在共用 ingress Bot 後選擇 Agent 的穩定名稱，只能使用小寫
英文字母開頭，後接小寫字母、數字、`_` 或 `-`，最多 32 字元；啟用中的 Alias
必須唯一。只有一個啟用 Agent 時仍可直接使用 `@Bot 訊息`。同一個 ingress Bot
有多個啟用 Agent 時，使用 `@Bot planner: 訊息`；bounded discussion 則使用
`!discuss planner coder -- 討論目標`。缺少或無法辨識 Alias 時會拒絕執行，不會
猜測或呼叫模型。Selector 只負責 ingress routing，不會寫進 canonical human
content；Discord 回覆前綴仍使用顯示名稱。回覆 Bot 訊息時可省略再次 mention；
共用 Bot 仍須以 `planner: 訊息` 指定 Agent。

「討論字元額度」是單場 bounded discussion 中該 Agent 已確認送達 Discord 的
完整 Unicode 字元數；某次合法回覆可超過剩餘額度，但會阻止下一次排程。「討論
呼叫上限」是該 Agent 在同一場討論可啟動的模型呼叫數。兩者都不套用於一般
human-turn。Binding 欄位分別顯示穩定 logical `bindingId`、目前 generation，及
該版本是否存在可用的既有 AI 對話 mapping；前台不會建立或更換它們。

若沒有 `agent-management.local.json`，既有 `config.json` participants 與
`DISCORD_TOKEN` / `BOT_USER_ID` 會組成相容的 `legacy-default` sender。多個啟用
Agent 共用同一 Sender 時，Discord 顯示會自動加上 `<displayName>: `；專屬
Sender 不強制署名。Sender 不存在、未登入或無法存取頻道時一律 fail closed，
不會改用其他 Bot 冒名發送。

```powershell
python -m py_compile bot.py conversation_policy.py agent_bridge\conversation_policy.py
python -m unittest discover -s tests -v
git diff --check
```

自動測試不會登入 Discord，也不會使用真實 Token。Codex human-turn root path 與 Codex + Antigravity bounded discussion（含 human intervention 與 in-flight `!stop`）均已完成一次真人 Manual E2E。這些結果不代表已部署、CI verified、durable crash recovery 或完整 production rollout。

## 授權與來源

本專案以 MIT License 發布。遷移來源與保留的署名請見 [`SOURCE.md`](SOURCE.md)。
