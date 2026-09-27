# 架構與遷移邊界

## 目標

同一套 Discord 安全邊界、事件格式、對話政策與結果契約，搭配可替換的 runtime adapter。

```text
Discord ingress
  -> shared access policy
  -> shared conversation policy
  -> AgentRequest / AgentResult contract
  -> runtime adapter
       - Codex app-server
       - Antigravity AgentAPI sidecar
       - future adapters
  -> Discord delivery
```

共用層負責 deterministic routing、allowlist、participant snapshot、round-robin、per-agent quota、closing check、停止條件及安全診斷。Runtime adapter 只負責把既有 request 交給對應 agent，並回報可驗證的結果；不得自行選擇 Discord 頻道、改寫共同配額、冒用其他 Bot 或擴張權限。

## Shared conversation policy

`agent_bridge/conversation_policy.py` 是 core-owned deterministic state machine：

- `!discuss` 以 mention 順序固定 participants 與 round-robin；錯誤 participant 使整次 start 失敗。
- Peer Discord output 只成為 context，不觸發下一位 agent。
- 普通 human intervention 不重排 round-robin；若正在 closing check，則取消 closing check 並恢復 active。
- `complete` 進入 closing check；只要出現 `continue` 就回 active；其餘有效 participant 都完成最後確認後才 completed。
- 每位 agent 各自維護 `budgetChars`／`usedChars`／`maxCalls`／`usedCalls`；另用 `globalMaxDispatches` 作 unattended hard stop。
- 合法開始的 output 不因事後超額被截斷；字元只在 Discord delivery confirmed 後計入。
- 帶正文的 `continue`／`complete` 先進入 delivery-pending fence，只有 confirmed delivery 才套用 discussion semantics；outcome unknown 維持 fence，確認未送達則 suspend。無正文的 `abstain` 驗證後直接推進狀態，不建立 delivery 或 canonical message。
- `!stop` 使當前 dispatch invalidated；process restart 將 active／closing-check 轉為 suspended。

目前根目錄 `conversation_policy.py` 仍是 Codex migration baseline，尚未接線，也不是 frozen shared contract。

## 遷移順序

1. 以已提交且完成 Phase 1.5 驗證的 Codex 程式建立乾淨基線。
2. 為 shared conversation policy 補上 frozen contract state machine 與回歸測試。（shared module 已完成；production wiring 尚未開始）
3. 完成獨立 review 後，才將 Codex Discord 入口改接 shared policy，並確認既有行為不退步。
4. 對 Antigravity 未提交工作樹做獨立測試與敏感資料檢查，再遷入 adapter。
5. 兩個 adapter 均通過共用 contract tests 後，才處理舊 fork 的退場或薄化。

## 不在初始快照中的功能

- Discord production deployment。
- Private Lane 或 stateless session。
- binding create／rebind control plane。
- runtime DB 選型。
- Claude Code adapter。
