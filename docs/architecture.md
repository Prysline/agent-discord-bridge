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
       - Antigravity CDP
       - future adapters
  -> Discord delivery
```

共用層負責 deterministic routing、allowlist、回合與字元限制、停止條件及安全診斷。Runtime adapter 只負責把既有 request 交給對應 agent，並回報可驗證的結果；不得自行選擇 Discord 頻道、改寫共同配額、冒用其他 Bot 或擴張權限。

## 目前衝突

兩份來源 `conversation_policy.py` 不能直接互換：

- Codex 版採明確提及／回覆與 `PeerTurnLimiter`。
- Antigravity 版採 `human-turn`／`bounded-discussion` 狀態機，並包含每位 Agent 回合上限與合計字元上限。

合併時應先以測試固定共同產品契約，再讓兩個入口使用同一模組；不能把不同狀態模型平均拼接，也不能因其中一份較完整就靜默改變另一份既有行為。

## 遷移順序

1. 以已提交且完成 Phase 1.5 驗證的 Codex 程式建立乾淨基線。
2. 為 shared conversation policy 補上共同契約與回歸測試。
3. 將 Codex Discord 入口改接 shared policy，確認既有行為不退步。
4. 對 Antigravity 未提交工作樹做獨立測試與敏感資料檢查，再遷入 adapter。
5. 兩個 adapter 均通過共用 contract tests 後，才處理舊 fork 的退場或薄化。

## 不在初始快照中的功能

- Discord production deployment。
- Private Lane 或 stateless session。
- binding create／rebind control plane。
- runtime DB 選型。
- 完整 `complete`／`abstain` discussion lifecycle。
- Claude Code adapter。
