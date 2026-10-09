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

Shared orchestration core 已驗證 `continue`／`complete`／`abstain`／`await-human` lifecycle，並讓 Codex persistent adapter 重用 shared request validator；本機 bootstrap 以 exact `bindingId + generation` 載入既有 Codex thread。Bounded discussion 會要求 runtime 回傳單一結構化 JSON 結果，Codex 與 Antigravity adapter 均會 fail closed 地解析 status，再由 shared core 執行 closing-check 或進入等待人類狀態。`await-human` 的說明送達 Discord 後會停止排程；下一則通過既有 access policy 的人類訊息加入 canonical context 並恢復討論，`!stop` 仍可終止。跨 adapter 的自動整合測試已覆蓋兩方確認完成。Opt-in human-turn root path，以及 Codex + Antigravity bounded discussion（含 human intervention 與 in-flight `!stop`）均已完成一次真人 Discord E2E，但新增的結構化完成與等待人類判斷尚未另做真人 E2E。Durable restart 與完整 production rollout仍未完成。

## 安裝與首次啟動

以下以 Windows PowerShell 與單一 Codex Agent 為推薦的最小路徑。完成這條路徑後，
再視需要加入多 Agent 或 Antigravity。不要把真實 Token、Discord numeric ID、persona
或 native session ID 提交到 Git。

### 1. 建立 Discord Bot

1. 在 [Discord Developer Portal](https://discord.com/developers/applications) 建立 Application，並在 **Bot** 頁建立 Bot。
2. 在 Bot 頁啟用 **Message Content Intent**，複製 Bot Token；Token 等同密碼，不要貼到 issue、Discord 或 commit。
3. 從 **Installation**／OAuth2 安裝 Bot 到自己的伺服器。只授予 **View Channel**、**Send Messages**、**Read Message History** 與 **Add Reactions**；不需要 Administrator。
4. 在 Discord 開啟 Developer Mode，複製：自己的 User ID、Bot User ID，以及要使用的 Channel ID。不要把身分組 ID 當成 Bot User ID。

Discord 介面若有變動，請參考官方的 [Building your first Discord Bot](https://docs.discord.com/developers/quick-start/getting-started)。

### 2. 安裝 Python dependencies

需要 Python 3.11+、Git，以及已安裝並登入的 Codex CLI。Codex 安裝與登入方式請以
[Codex CLI 官方文件](https://learn.chatgpt.com/docs/codex/cli)為準。

```powershell
git clone https://github.com/Prysline/agent-discord-bridge.git
Set-Location agent-discord-bridge
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
codex --version
```

最後一行必須能顯示版本；Bot 會自行啟動 `codex app-server`，不需要另外開一個
app-server process。

### 3. 建立首次設定

#### 推薦：使用首次設定精靈

目前首次設定精靈只支援單一 Discord Bot＋第一個 Codex Agent。若第一個 Agent 使用
Antigravity，請改用下方手動設定及本節末的 Sidecar 說明。

不需要先建立 `.env` 或 `config.json`。在 repo 根目錄啟動只監聽本機的設定頁：

```powershell
python -m agent_bridge.setup_ui
```

開啟終端機顯示的 <http://127.0.0.1:8766>，依序填入 Discord 入口、第一個
Codex Agent 與 Codex runtime。第一個 Agent 的 `agentId` 是後續 Binding 使用的穩定
身分，例如 `agent-one`；`agent-a` 也只是文件範例，不是固定值。

首次設定精靈的三個區塊與畫面欄位如下：

| 畫面區塊 | 必填欄位 | 用途 |
| --- | --- | --- |
| Discord 入口 | Bot Token、Bot User ID、你的 User ID、你的名稱、Channel ID、頻道名稱 | 建立入口 Bot、第一位授權使用者與第一個授權頻道；兩個名稱都只是本機辨識標記。 |
| 第一個 Agent | Agent ID、Discord Alias、顯示名稱、Sender ID、討論字元額度、討論呼叫上限 | Agent ID 是不可隨意更換的 Binding 身分；Alias 是共用 Bot 的選擇名稱；Sender ID 是本機 routing key，不是 Discord numeric ID。 |
| Codex Runtime | Model、工作目錄、Persona | Model 必須是目前 Codex 帳號可用的名稱；工作目錄可使用預設 `shared_workspace`。 |

第一次建立時，這個 Agent 的 Sender 會使用「Discord 入口」所填的同一個 Bot Token
與 Bot User ID。之後若要增加獨立 Discord Bot 帳號，請在啟動後的管理前台新增
Sender，不要把第二組 Token 填進首次設定精靈。

「建立首次設定」成功後會產生 `.env`、`config.json`、
`agent-management.local.json`、空的 `bindings.local.json` 與 `persona.md`。精靈不會
覆寫任何已存在的目標檔；已有本機設定時請使用管理前台或下面的手動方式。

設定完成後按 `Ctrl+C` 關閉精靈，直接跳到「4. 啟動 Bot」。

#### 替代：手動建立本機設定檔

```powershell
Copy-Item .env.example .env
Copy-Item config.example.json config.json
Copy-Item persona.example.md persona.md
```

第一次使用推薦建立空的 current-schema binding 檔，再由啟動後的管理前台建立
Binding；不要直接手填 thread ID：

```powershell
python -c 'from pathlib import Path; Path("bindings.local.json").write_text("{\n  \"bindingLineages\": [],\n  \"activeBindings\": [],\n  \"codexBindings\": [],\n  \"antigravityBindings\": []\n}\n", encoding="utf-8")'
```

`bindings.example.json` 是既有 Codex + Antigravity binding 的格式範例，不是可以不改
就直接啟動的預設檔。

#### 手動決定第一個 Agent 並填寫 Codex-only 設定

在 `.env` 至少填寫：

```dotenv
DISCORD_TOKEN=從 Discord Developer Portal 取得的 Bot Token
BOT_USER_ID=Bot User ID
ROOT_BOT_LABEL=Discord Bridge
LEGACY_AGENT_DISPLAY_NAME=第一個 Agent 的顯示名稱
AGENT_ADMIN_PORT=8766
SHARED_CORE_ENABLED=true
SHARED_AGENT_ID=agent-a
CODEX_PATH=codex
CODEX_MODEL=你的 Codex 帳號可用模型
CODEX_CWD=shared_workspace
SHARED_BINDINGS_PATH=bindings.local.json
```

`SHARED_AGENT_ID` 必須和 `config.json` participant 的 `agentId` 完全相同。
`CODEX_CWD` 是 Agent 的隔離工作目錄；相對路徑會以 repo 根目錄解析。
`ROOT_BOT_LABEL` 只用於終端機與視窗標題，不代表任何 Agent；
`LEGACY_AGENT_DISPLAY_NAME` 只供未啟用 shared core 時的 legacy prompt 使用。
舊版 `.env` 的 `BOT_DISPLAY_NAME` 仍可作 legacy 名稱 fallback，但新設定不應再使用。
`AGENT_ADMIN_PORT` 是本機管理前台的 loopback port。
`CODEX_MODEL` 只是在 Agent 尚未指定「建立模型」時的 fallback；Bot 啟動後的 Agent
管理前台會讀取目前 Codex runtime 回報的 model list。為 Agent 選擇的模型只用於
之後建立的新 Thread，不會改寫既有 Thread。若 fallback 已不在可用清單，建立操作會
在送出 `thread/start` 前拒絕，請先在 Agent 管理前台選擇可用模型並重新啟動 Bot。
注意：runtime 的 model list 是公告清單，不保證目前登入的 ChatGPT 帳號方案能實際
執行每個模型；真正的帳號相容性會在第一個 turn 由 Codex 驗證。若收到模型不支援
提示，請改選帳號可用的模型，並建立新的 Thread；既有 Thread 不會被換模型。

接著把 `config.json` 改成下面的最小版本，將三個全大寫 placeholder 換成真實
Discord numeric ID。JSON 內的 ID 必須保留為字串：

```json
{
  "dmPolicy": "allowlist",
  "allowFrom": ["HUMAN_DISCORD_USER_ID"],
  "conversation": {
    "contextMessages": 12,
    "maxPeerTurns": 2
  },
  "sharedDiscussion": {
    "globalMaxDispatches": 10,
    "participants": [
      {
        "agentId": "agent-a",
        "agentAlias": "agent-a",
        "adapter": "codex",
        "mentionId": "BOT_USER_ID",
        "displayName": "Agent A",
        "budgetChars": 2000,
        "maxCalls": 5
      }
    ]
  },
  "channels": {
    "CHANNEL_ID": {
      "name": "private-room",
      "requireMention": true,
      "allowFrom": ["HUMAN_DISCORD_USER_ID"],
      "allowBotMention": false,
      "allowBotFrom": []
    }
  }
}
```

欄位用途如下：

- `allowFrom`：可觸發 Bot 的人類 Discord User ID。
- `channels` 的 key：允許使用的 Channel ID。
- `channels.*.allowFrom`：該頻道允許的人類 User ID。
- `sharedDiscussion.participants`：第一次先只保留一個 `adapter: "codex"` participant。
- participant 的 `agentId`：例如 `agent-a`，並與 `SHARED_AGENT_ID` 相同。
- participant 的 `agentAlias`：共用 Bot 選擇 Agent 時使用的小寫名稱；省略時預設等於 `agentId`。
- participant 的 `mentionId`：單一 ingress Bot 架構下填同一個 Bot User ID。
- `budgetChars`／`maxCalls`：只限制 bounded discussion，不限制一般 human-turn。

第一次啟動可以先不建立 `agent-management.local.json`。此時程式會以 `.env` 的
`DISCORD_TOKEN`／`BOT_USER_ID` 和 `config.json` participants 建立相容的
`legacy-default` Sender。之後可從管理前台儲存成正式的本機管理設定。

### 4. 啟動 Bot

```powershell
.\.venv\Scripts\Activate.ps1
python bot.py
```

啟動成功後，終端機會顯示 Discord Bot 上線、Codex app-server 初始化，以及管理前台
網址。預設管理前台是 <http://127.0.0.1:8766>。這個內嵌前台必須和 Bot 同時運行，
才能執行 binding 操作。

終端機會把唯一的 `Discord Root Listener` 與每個啟用的 `Discord Sender` 分開列出。
Root Listener 負責接收訊息；Sender 負責用指定 Discord 帳號送出 Agent 回覆。看到多個
Sender 不代表有多個訊息入口。

### 5. 建立第一個頻道 Binding

建議直接使用管理前台的「為授權頻道建立 Binding」，不必先在 Discord 發訊息。
輸入已加入 allowlist 的 Channel ID、選擇已啟用的 Agent，然後：

- Codex：可輸入既有 Thread ID 後「驗證並綁定」，或建立新 Thread。
- Antigravity：輸入既有 Conversation ID；Bridge 不建立新的 Antigravity Conversation。

這個操作只建立 Discord 頻道與 AI 聊天窗的關聯，不會建立假的 human event、呼叫
模型或傳送 Discord 訊息。成功後，在 Discord 傳送的第一則正常訊息才會進入
canonical history 並觸發 Agent。

若先在 Discord mention 尚未 Binding 的 Agent，仍會進入「待完成的聊天窗綁定」
fallback：Bridge 保留那則訊息、不呼叫模型，待 Binding 成功後才接續處理。

請確認目前開啟的是 `python bot.py` 啟動後顯示的內嵌管理前台，而不是
`python -m agent_bridge.admin_ui` 的獨立設定前台。獨立前台沒有連接 Bot runtime，
不能建立 onboarding pending 或執行 Binding 操作。

fallback 的操作步驟如下：

1. 在 allowlist 內的 Discord 頻道真正 mention Bot 並傳送第一則訊息。
2. 訊息必須符合該頻道的 mention 規則；共用 Bot 有多個啟用 Agent 時，還要使用
   `@Bot alias: 訊息` 明確指定 Agent。
3. Bot 應回覆「還沒有設定聊天窗」，並保留這則訊息，不呼叫模型。
4. 開啟 Bot 終端機顯示的管理前台網址，在「待完成的頻道綁定」找到該 Agent。
5. 如果該區仍顯示「目前沒有待處理項目」，先檢查訊息是否通過 allowlist、是否真的
   mention Bot、Alias 是否正確，以及終端機是否正在執行目前 checkout 的 `bot.py`。
6. Codex Agent 可選擇「建立新聊天窗」，或輸入既有 Codex Thread ID 後選擇「驗證並綁定」。Antigravity Agent 請輸入既有 Conversation ID；目前不支援從 Bridge 建立新的 Antigravity Conversation。Conversation 可以位於任何 Antigravity Project，但 transcript 必須實際位於目前 Sidecar `--transcript-root` 涵蓋的目錄樹中；重開 Antigravity 不會自動改寫這個路徑。
7. persistence 與 runtime publication 成功後，Bridge 會用原本保留的第一則訊息開始回覆；不必重新傳送。

若操作失敗，前台會保留 pending。Codex create outcome 不明時不要連續重按；先到
Codex 檢查是否已建立 thread，再用「驗證並綁定」納管。

### 6. 使用啟動後的管理前台

`python bot.py` 運行期間，開啟 <http://127.0.0.1:8766>。目前畫面由上到下分成：

| 畫面標題 | 可以做什麼 | 何時生效 |
| --- | --- | --- |
| Agent 管理 | 新增 Agent，設定顯示名稱、Alias、adapter、Codex 建立模型、入口 Bot、Sender、啟用狀態及 bounded discussion 額度。入口 Bot 決定 mention／reply 路由，Sender 決定由哪個 Bot 帳號發出回覆；既有 Agent ID 為唯讀。 | 和 Discord Senders 一起按「驗證並儲存 Agent／Sender」；重新啟動 Bot 後生效。 |
| Discord Senders | 新增 Discord 發言帳號，設定 Sender ID、標籤、Bot User ID、Token 與啟用狀態。Token 已設定且 Sender ID 未變時可留白保留。 | 和 Agents 一起按「驗證並儲存」；重新啟動 Bot 後生效。 |
| Discord 授權使用者 | 新增／移除具本機名稱的使用者，並設定 DM 規則。名稱只作辨識，權限仍依 numeric User ID。 | 和授權頻道一起按「儲存使用者／頻道設定」；重新啟動 Bot 後生效。 |
| Discord 授權頻道 | 新增／移除頻道、選擇允許使用者及是否需要 mention。新增頻道不會同時建立 Agent 或 AI 聊天窗。 | 和授權使用者一起按「儲存使用者／頻道設定」；重新啟動 Bot 後生效。 |
| 為授權頻道建立 Binding | 直接輸入已授權 Channel ID，為 Agent 綁定既有 native 聊天窗；Codex 也可建立新 Thread。此操作不會製造訊息或呼叫模型。 | 成功後立即更新 persistence 與目前 runtime，不需重啟。 |
| 待完成的頻道綁定 | 連結已管理聊天窗、驗證並綁定既有 Codex Thread／Antigravity Conversation、建立新的 Codex Thread，或取消 pending。Antigravity 不支援從 Bridge 建立新 Conversation。 | 成功後立即更新 persistence 與目前 runtime，不需重啟。 |
| Binding 管理 | 單獨搬移 Agent、把同頻道全部 active Agent 一起搬移，或解除頻道綁定。 | 成功後立即生效，不會刪除 Discord 頻道或 native AI 對話。 |

解除頻道綁定會保留 logical lineage 與 native mapping，讓同一聊天窗之後可以重新綁定；
它不會刪除 Codex Thread 或 Antigravity Conversation。若重複建立後再解除，這些 detached
Binding 會繼續出現在可重用清單。刪除／retire native session 是不同的破壞性操作，目前
管理前台不會自動執行。

這裡的 **control plane** 是 Bridge 用來驗證、納管或建立 AI 原生聊天窗的操作層，
不是模型回覆本身。Codex 可驗證既有 Thread 並建立新 Thread；Antigravity 目前只提供
不送出訊息的既有 Conversation 驗證與綁定，不提供建立新 Conversation。

建立 onboarding pending 時，一般訊息一次只能指定一個入口 Bot。若一個 Bot 對應
多個 Agent，使用 `@Bot agent-alias: 訊息`；若要同時邀請多個 Agent 討論，使用
`!discuss @BotA @BotB -- 討論目標`，不要在一般訊息同時 mention 多個 Bot。系統、
Binding 與安全錯誤由 Root Listener 發送並以 `[SYSTEM]` 開頭；Agent 模型正文則由
該 Agent 設定的 Sender 發送。

「Discord 授權使用者／頻道」和「Agent／Sender」是兩套獨立儲存操作；修改其中一區
不會自動儲存另一區。群組搬移可從已授權頻道的下拉選單選擇目標；單獨搬移目前會
要求輸入目標 Channel ID，該 ID 仍必須先存在於 Discord 授權頻道中。

若用 `python -m agent_bridge.admin_ui` 單獨開啟前台，只能編輯並儲存設定；pending
onboarding 與 Binding 操作必須使用 `python bot.py` 提供的內嵌前台。

### 7. 停止與再次啟動

在執行 `python bot.py` 的終端機按 `Ctrl+C`。再次啟動仍使用：

```powershell
.\.venv\Scripts\Activate.ps1
python bot.py
```

不要用工作管理員或掃描後終止所有 Python process。若程式未正常停止，先確認 PID
及命令列確實屬於本專案再處理。

### 選用：Antigravity existing-conversation adapter

目前只支援已明確存在的 Antigravity conversation；管理前台可在 pending onboarding
輸入既有 Conversation ID，透過 Sidecar 只讀驗證後建立或重用 Binding，但不提供
建立新 Antigravity Conversation。請先完成 Codex-only 啟動，再依官方
[Antigravity Sidecars 文件](https://antigravity.google/docs/sidecars)安裝 Sidecar：

1. 在 `~/.gemini/config/sidecars/<sidecarId>/sidecar.json` 建立 Sidecar manifest。
2. 不要原樣使用相對 import。將 `command` 設為本專案 `.venv` 的 Python 絕對路徑，並在 `args` 以絕對路徑執行 `antigravity_adapter/sidecar/worker.py`。
3. 把 `--rendezvous` 設為 gitignored runtime file 的絕對路徑，並讓 `.env` 的 `ANTIGRAVITY_RENDEZVOUS_PATH` 指向同一檔案。
4. 把 `--transcript-root` 設為包含目標 conversation subtree 的 Antigravity brain root；不要猜測或分享這個私人路徑。
5. 在 `~/.gemini/config/config.json` 的 `sidecars.<sidecarId>.enabled` 設為 `true`，完整重啟 Antigravity。
6. 在內嵌管理前台的「為授權頻道建立 Binding」輸入已授權 Channel ID、選擇 Antigravity Agent，再輸入既有 Conversation ID 並按「驗證並綁定」。Bridge 會只讀驗證 Conversation，並在 persistence 成功後建立 logical lineage、room association 與 native mapping。若先從 Discord 發訊息，也可沿用 pending onboarding fallback。

一般 onboarding 不需要手動編輯 `bindings.local.json`。只有進階復原或診斷時才應直接檢查該檔案；其中 logical lineage、room association 與 `conversationId` mapping 的 `bindingId + generation` 必須完全一致。更新到含 existing-conversation 驗證端點的版本後，請完整重啟 Antigravity Sidecar 與 Bot，再使用管理前台。

Sidecar manifest 可由 `antigravity_adapter/sidecar/sidecar.example.json` 開始修改。
Antigravity 意外退出時，應先確認 Sidecar worker、listener 與 rendezvous 都只有一份，
再恢復跨 adapter 操作。

### 設定檔角色

| 檔案 | 用途 | 是否可提交真實內容 |
| --- | --- | --- |
| `.env` | Discord Token、Bot ID、runtime 路徑與開關 | 否 |
| `config.json` | allowlist、頻道與 participant policy | 否 |
| `persona.md` | 本機 Agent 身分與回覆邊界 | 否 |
| `bindings.local.json` | logical lineage、room association 與 native mapping | 否 |
| `agent-management.local.json` | Agent、Alias 與 Discord Sender | 否 |

`bindings.local.json` 保存 stable logical lineage、目前 room association 與 adapter-local native mapping；shared state 不持有 Codex thread ID／Antigravity conversation ID。設定矛盾會 fail closed。

Antigravity Sidecar 的追蹤範例位於 `antigravity_adapter/sidecar/sidecar.example.json`；安裝時須把 rendezvous 與 transcript root placeholders 換成本機設定。transcript root 必須明確指向包含各 conversation subtree 的 Antigravity brain root；worker 不猜 home、使用者名稱或私人路徑。`agentapi send-message` 負責 dispatch，`/result` 以 dispatch 前 byte offset、file identity、唯一 request marker 與 bounded JSONL delta 恢復 final；Stop hook 可用相同 extractor 提前完成，但只是 optional fast path。worker 只監聽 `127.0.0.1`，每次啟動輪替 bearer token 與 instance identity，並從 Antigravity 注入的 PATH discovery `agentapi`；不支援的 wrapper 會拒絕啟動，不會退回 CDP 或猜測安裝路徑。Windows runtime 另以 opaque named mutex、exclusive loopback socket 與經 creation-time驗證的 parent process handle保護單一 instance；parent watcher以 owned cancellation event安全退出，parent結束時 worker自行 shutdown。Rendezvous以 Windows atomic no-replace primitive發布；既有 live、stale 或無法判定 ownership 的 artifact一律 fail closed，worker不會覆寫它，也不會掃描或終止未知 process。

設定 `SHARED_CORE_ENABLED=true` 與明確的 `SHARED_AGENT_ID` 後，root bot 的 authorized human-turn 使用 shared canonical event-delta；缺少 binding 時保留第一則訊息並進入 onboarding，不呼叫模型，也不 fallback legacy execution。`config.json` 的 participant 以 `adapter: codex|antigravity` 選擇 composition；Antigravity 另需把 `ANTIGRAVITY_RENDEZVOUS_PATH` 指向 Sidecar 每次啟動 atomic 發布的 gitignored runtime file。`!stop` 仍只走 shared invalidation；Antigravity 目前準確宣告 `canCancelInFlight=false`，late result 不能越過 core token。cross-adapter 真人 E2E 已完成；onboarding control plane 尚未做真人 E2E，chunking 與 durable persistence尚未完成。

## 操作與驗證

### 本機 Agent 管理前台

`python bot.py` 會同時啟動內嵌管理前台。若只想在 Bot 啟動前預先編輯 Agent／Sender
設定，可以選擇複製 `agent-management.example.json` 為 gitignored 的
`agent-management.local.json`，再單獨啟動：

```powershell
python -m agent_bridge.admin_ui
```

獨立啟動的前台只適合編輯設定；需要操作 pending onboarding 或 Binding 時，請使用
`python bot.py` 啟動的內嵌前台。前台只監聽 `127.0.0.1:8766`，可編輯 Agent、adapter、討論額度與
Discord Sender。既有 Agent ID 為唯讀。當前 Bot 內嵌前台可處理待完成 onboarding：
重新連結已管理的 detached Binding、驗證並綁定既有 Codex thread，或在明確確認後
建立 Codex thread；Antigravity 可只讀驗證並綁定既有 Conversation，但不支援建立新
Conversation。它也可搬移或
解除 room association，但不提供 rebind、retire 或 generation mutation。Token 只會顯示「已設定」；只有 Sender ID
未變且 Token 已設定時，留白才會保留原值，新增或更名的 Sender 必須輸入 Token。
儲存採完整驗證後的單檔 atomic replace，並於重新啟動 Bot 後生效。可用
`127.0.0.1` 或 `localhost` 開啟；關閉前台請回到終端機按 `Ctrl+C`。

「Discord 授權使用者／頻道」可新增具本機辨識名稱的授權使用者與頻道，並選擇每個頻道
允許哪些使用者。安全判斷永遠使用 Discord numeric ID；名稱只作顯示，不會取代
identity。存取設定會保留既有 peer Bot policy，驗證後 atomic 寫回 `config.json`，
重新啟動 Bot 後生效。

Agent 的「啟用」是正式參與開關。內部 `available` 欄位保留給 runtime 狀態，
不由管理前台手動編輯。有 Binding 的 Agent 只能停用，不能從管理設定移除。
Sender 仍被 Agent 引用時也不能移除。移除只更新管理清單，不會修改 Binding；
移除 Sender 也不會影響既有 AI 對話。

Codex Agent 的「建立模型」來自啟動中的 Codex runtime；設定只影響日後由 Bridge
建立的新 Thread。`persona.md` 的共用 persona 也只在建立 Codex Thread 時送入一次，
不會每輪重送。Antigravity 既有 Conversation 的模型與人格由它所屬的 Project／Agent
設定決定，Bridge 不會把 Codex persona 注入 Antigravity，也不會在這個欄位覆寫模型。

Agent 的「入口 Bot」是接收 human-turn 的 Discord 身分；「Sender」是實際送出該
Agent 回覆的 Discord 身分，兩者不必相同。入口 Bot 與 Sender 都必須指向啟用的
Discord Sender 設定。專用入口 Bot 只對應一個 Agent 時，mention 或回覆該 Bot 會
直接選中 Agent；多個 Agent 共用同一入口 Bot 時才需要 Alias。一般 human-turn 若
同時 mention 多個不同 Bot 會 fail closed；多 Agent 討論請改用 `!discuss`。目前只有
Root Listener 接收 DM，Send-only Sender 不會成為額外 DM listener。

`Agent Alias` 是人在共用 ingress Bot 後選擇 Agent 的穩定名稱，只能使用小寫
英文字母開頭，後接小寫字母、數字、`_` 或 `-`，最多 32 字元；啟用中的 Alias
必須唯一。同一個入口 Bot 只有一個啟用 Agent 時可直接使用 `@Bot 訊息`。同一個 ingress Bot
有多個啟用 Agent 時，使用 `@Bot planner: 訊息`；bounded discussion 則使用
`!discuss planner coder -- 討論目標`。缺少或無法辨識 Alias 時會拒絕執行，不會
猜測或呼叫模型。Selector 只負責 ingress routing，不會寫進 canonical human
content；Discord 回覆前綴仍使用顯示名稱。回覆 Bot 訊息時可省略再次 mention；
共用 Bot 仍須以 `planner: 訊息` 指定 Agent。

「討論字元額度」是單場 bounded discussion 中該 Agent 已確認送達 Discord 的
完整 Unicode 字元數；某次合法回覆可超過剩餘額度，但會阻止下一次排程。「討論
呼叫上限」是該 Agent 在同一場討論可啟動的模型呼叫數。兩者都不套用於一般
human-turn。Binding 欄位分別顯示穩定 logical `bindingId`、目前 generation，及
該版本是否存在可用的既有 AI 對話 mapping。搬移與解除綁定都保留 logical lineage
與 native session；建立／綁定只有 persistence 成功後才發布到 runtime。Codex 建立
結果不明時不會自動重試；若 native thread 已建立但本機保存失敗，前台會要求先到
Codex 檢查，再以「綁定既有聊天窗」納管。

Binding 管理同時提供單一 Agent 搬移與「搬移全部 Agent」。群組搬移只允許選擇已
授權頻道；系統會先確認來源全部 idle、目標沒有衝突且每個 native mapping 均有效，
再以一次 persistence save 與一次 runtime batch mutation 完成。任一檢查失敗時整組
不搬移，也不改變 native session。

若沒有 `agent-management.local.json`，既有 `config.json` participants 與
`DISCORD_TOKEN` / `BOT_USER_ID` 會組成相容的 `legacy-default` sender。多個啟用
Agent 共用同一 Sender 時，Discord 顯示會自動加上 `<displayName>: `；專屬
Sender 不強制署名。Sender 不存在、未登入或無法存取頻道時一律 fail closed，
不會改用其他 Bot 冒名發送。

啟動 log 會分開顯示唯一的 `Discord Root Listener` 與每個啟用的 Discord
Sender。Root Listener 負責接收訊息；其他 Sender 使用各自帳號發言，不會變成額外的
訊息入口。log 只輸出 Sender label、Sender ID 與 Discord 帳號，不會輸出 Token。

```powershell
python -m py_compile bot.py conversation_policy.py agent_bridge\conversation_policy.py
python -m unittest discover -s tests -v
git diff --check
```

自動測試不會登入 Discord，也不會使用真實 Token。Codex human-turn root path 與 Codex + Antigravity bounded discussion（含 human intervention 與 in-flight `!stop`）均已完成一次真人 Manual E2E。這些結果不代表已部署、CI verified、durable crash recovery 或完整 production rollout。

## 授權與來源

本專案以 MIT License 發布。遷移來源與保留的署名請見 [`SOURCE.md`](SOURCE.md)。
