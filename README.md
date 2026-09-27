# Agent Discord Bridge

將私人 Discord 頻道中的訊息安全地轉交給本機 AI agent runtime。專案目標是讓不同 runtime 共用同一套 Discord 存取邊界、對話政策與公開契約，只在 adapter 層處理各家差異。

> 本專案不是 Discord、OpenAI 或 Google 官方工具。目前正從既有 Codex 與 Antigravity 實作遷移；尚未完成的 adapter 不應視為已支援。

## 目前狀態

- Codex：已遷入既有、通過測試的 Discord bridge 與 persistent adapter core。
- Antigravity：尚未遷入；既有 fork 仍是唯讀遷移來源。
- 共用 conversation policy：已依 frozen contract 建立 deterministic discussion state machine 與 contract tests，但尚未接上 Codex production 入口；Antigravity adapter 也尚未遷入。
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
- `agent_bridge/conversation_policy.py`：尚未接線的 core-owned bounded-discussion scheduler、closing check、quota accounting 與 restart／stop state machine。
- `tests/`：routing policy 與 Codex adapter tests。
- `shared_workspace/`：預設隔離工作目錄說明。

Shared state machine 已實作 `continue`／`complete`／`abstain` 的 discussion lifecycle；但 Codex persistent adapter 目前對可靠非空 final 仍只回傳 `continue`，且 production Discord 入口尚未接上 shared policy。因此目前不能宣稱端到端 `bounded-discussion` 已可使用。

## 本機設定

需要 Python 3.11+、Discord Bot 與對應 runtime。Discord Developer Portal 必須啟用 Message Content Intent；Bot 原則上只需 View Channel、Send Messages、Read Message History 與 Add Reactions，不應授予 Administrator。

Codex baseline 的設定方式：

```powershell
Copy-Item .env.example .env
Copy-Item config.example.json config.json
Copy-Item persona.example.md persona.md
python -m pip install -r requirements.txt
```

真實 Token、numeric ID 與 persona 不得提交。

## 驗證

```powershell
python -m py_compile bot.py conversation_policy.py agent_bridge\conversation_policy.py
python -m unittest discover -s tests -v
git diff --check
```

自動測試不會登入 Discord，也不會使用真實 Token。正式接上 Discord 前仍需另外完成 adapter wiring、delivery 與端到端人工驗收。

## 授權與來源

本專案以 MIT License 發布。遷移來源與保留的署名請見 [`SOURCE.md`](SOURCE.md)。
