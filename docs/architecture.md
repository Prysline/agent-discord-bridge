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

## Shared orchestration core

`agent_bridge/orchestrator.py` 提供目前 memory-only 的 shared-core wiring target：

- `CanonicalLog` 產生 core-owned `eventId` 與 room-local contiguous `seq`；Discord message／channel ID 只屬 transport metadata，不能取代 canonical identity。
- 已授權、非 control-command 的 human event 先進 canonical log；`!discuss`／`!stop` 只套用 core control semantics，不重複成普通 message。human-turn 依 mention 順序 sequential dispatch，agent output 不會反向觸發 AI。
- bounded discussion 直接使用 `ConversationPolicy`，不另寫 round-robin、quota 或 closing-check state machine；discussion 開始時 snapshot room-specific existing bindings。
- 每個 room／agent／binding generation 維護 `lastCanonicalSyncedSeq`、`nativeKnownSeqs` 與 pending context fence。Request 以 dispatch 當下 high watermark 建立 immutable event-delta snapshot。
- `contextCommit=committed` 才推進 contiguous coverage；`not_committed` 不推進；`unknown` 與 invalid response 保留 uncertainty fence，不 replay。
- `continue`／`complete` delivery confirmed 後，先 append 完整 agent canonical event，再推進 shared policy；`unknown` 不建立 ghost event並阻止同 room 下一個 AI，`not_delivered` fail closed。`abstain` 不發 Discord、不建立 canonical output。
- `!stop` 先 invalidates policy token，再依 adapter capability best-effort cancel；尚未 attempt 的 late output 不再送出，已 attempt 且 outcome unknown 的 delivery 則保留 reconciliation fence。之後確認 delivered 只補 canonical history 與字元 accounting，不恢復 discussion；確認未送達不補送。restart 將 active discussion suspended，且遺失的 pending context／delivery 保留 uncertainty，不宣稱可恢復。

`agent_bridge/contracts.py` 是 wiring 使用的 shared request／result validator；`codex_adapter/contracts.py` 保留 Codex result helpers，但重用同一份 shared AgentRequest validation，避免 schema 漂移。

這些元件尚未持久化。Root Discord entry 已有 opt-in human-turn 與 bounded-discussion driver；driver 只反覆呼叫 shared core 選出的合法 turn，並讓 `!stop` 在 in-flight request 期間進入既有 stop／reconciliation lifecycle。Participant、quota 與 exact existing binding 由本機設定提供，root 不另建 scheduler。Peer ingestion、durable restart 與完整 production cutover仍未接線；bounded discussion 也尚未完成真人 Discord E2E。

## Shared conversation policy

`agent_bridge/conversation_policy.py` 是 core-owned deterministic state machine：

- `!discuss` 以 mention 順序固定 participants 與 round-robin；錯誤 participant 使整次 start 失敗。
- Peer Discord output 只成為 context，不觸發下一位 agent。
- 普通 human intervention 不重排 round-robin；若正在 closing check，則取消 closing check 並恢復 active。
- `complete` 進入 closing check；只要出現 `continue` 就回 active；其餘有效 participant 都完成最後確認後才 completed。
- 每位 agent 各自維護 `budgetChars`／`usedChars`／`maxCalls`／`usedCalls`；adapter 透過 execution lifecycle observer 在 `turn/start` confirmed 或 ambiguous 時才增加 `usedCalls`。`globalMaxDispatches` 是 core dispatch reservation 的 unattended hard stop，與實際 model-call accounting 分開。
- 合法開始的 output 不因事後超額被截斷；字元只在 Discord delivery confirmed 後計入。
- 帶正文的 `continue`／`complete` 先進入 delivery-pending fence，只有 confirmed delivery 才套用 discussion semantics；outcome unknown 維持 fence，確認未送達則 suspend。無正文的 `abstain` 驗證後直接推進狀態，不建立 delivery 或 canonical message。
- `!stop` 使當前 dispatch invalidated；process restart 將 active／closing-check 轉為 suspended。

目前根目錄 `conversation_policy.py` 與 `bot.py` 仍是 Codex migration baseline，也不是 frozen shared contract。舊 runtime 仍會讀取 Discord recent history、使用 peer mention chaining，並在 thread 遺失時自動建立新 thread；shared path 不使用這些語意。

## 遷移順序

1. 以已提交且完成 Phase 1.5 驗證的 Codex 程式建立乾淨基線。
2. 為 shared conversation policy 補上 frozen contract state machine 與回歸測試。（已完成）
3. 建立 canonical log、event-delta cursor、shared contracts 與 orchestrator wiring target。（已完成 memory-only core）
4. 以本機 existing-binding bootstrap 提供 exact logical/native mapping，將 Codex Discord human-turn 與 bounded-discussion root 入口接至 shared core；完整 create／rebind operations 仍留待後續 control-plane slice。（本機 wiring 已完成；bounded discussion Manual E2E pending）
5. 以既有 probe evidence 建立 Antigravity exact binding、authenticated Sidecar transport 與 persistent adapter，並在 composition layer 依 participant 選擇 runtime。（已完成本機實作與 automated fake-transport integration；真人 cross-adapter E2E pending）
6. 兩個 adapter 均通過共用 contract tests 後，才處理舊 fork 的退場或薄化。（contract regression 已接入 full suite；舊 fork 尚未退場）

## Antigravity adapter

`antigravity_adapter/` 不讀 Discord，也不持有 discussion state。Resolver 只接受 exact `bindingId + generation`，把 native `conversationId` 留在 adapter-local mapping；既有 inactive generation 只要仍有明確 mapping 便可服務 discussion snapshot。Transport 每次呼叫都重新讀取 gitignored rendezvous，且只接受 `127.0.0.1`，以 ephemeral bearer token 與 instance identity 存取 Sidecar。

Sidecar `/send` 接受後才回報 confirmed invocation；送出結果不明則保守回報 ambiguous invocation 並以可得 request identity read back。唯一 positive result 才把 context 判為 committed；無法相關時維持 unknown，不 replay。正式模型輸出目前一律映射為 `continue`，不自行發明 `complete`／`abstain` parser。平台沒有已證明的強 cancellation，因此 capability 是 `canCancelInFlight=false`；core invalidation 仍保證 late output 不會被送往 Discord或推進 discussion。

目前 cross-adapter automated coverage 使用 fake Sidecar transport；尚未驗證真實 Antigravity Sidecar、真實 Codex binding 與 Discord 同場往返，因此不得稱為 production-ready 或 cross-adapter E2E verified。

## 不在初始快照中的功能

- Discord production deployment。
- Private Lane 或 stateless session。
- binding create／rebind control plane。
- runtime DB 選型。
- Claude Code adapter。
