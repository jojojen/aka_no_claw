# 龍蝦架構重構 TODO

> **接手前必讀：** 整體架構說明見下方 §Background。每項任務獨立可做，建議順序 1 → 2 → 3 → 3.5 → 4。

---

## Background — 目前架構 vs 目標架構

### 目前（問題）

已有獨立 launchd service：`local.openclaw.{telegram, reputation, opportunity, ollama, aivis}`。
問題是 `local.openclaw.telegram` 這一個 process 還塞著：
- Telegram polling + 指令 dispatch
- SNS 背景監控（RSS 輪詢、classifier、EntityResearcher、通知推播）
- 價格背景監控（watch_monitor 警示、card image crawler）
- 各 handler 的業務邏輯有部分散在 price_monitor_bot

另一個**既存違規**：`data/opportunities.sqlite3` 目前被 opportunity service 和
telegram process（/hunt 的寫入操作）**兩個 process 寫**——Task 3.5 一併修正。

### 目標（telegram 拆出兩個新 service）

```
local.openclaw.telegram      ← 只做 Telegram polling + 指令 dispatch
local.openclaw.sns_monitor   ← SNS 背景監控 + 推播（no polling）【新增】
local.openclaw.price_monitor ← 價格背景監控 + 推播（no polling）【新增】
（reputation / opportunity / ollama / aivis 既有 service 維持不動）
```

```
price_monitor_bot/          ← 純基礎設施（TelegramBotClient + dispatcher）
  bot.py
    TelegramBotClient       ← HTTP wrapper
    TelegramCommandProcessor ← 指令分派（registry extension point）
    run_telegram_polling    ← poll loop（接受 command_handlers / callback_handlers）

aka_no_claw/                ← 業務邏輯主 repo + telegram service entry
  telegram_bot.py
    TelegramCommandProcessor（subclass）
    _build_registries()     ← 把所有指令/callback 以資料注入 base dispatcher
    run_telegram_polling()  ← wrapper，啟動 telegram service（polling only）

sns_monitor/                ← aka_no_claw sub-package，SNS 監控邏輯
  service.py                ← 獨立 entry，啟動 sns_monitor launchd service
```

依賴方向（**單向**，不可反轉）：
```
aka_no_claw → price_monitor_bot
sns_monitor ← aka_no_claw（sub-package）
```

Registry 運作方式：
- `aka_no_claw/_build_registries()` 回傳 `command_handlers` + `callback_handlers`
- base dispatcher 先查 registry，查不到才走內建 price/hunt 分支
- 新增指令只需改 `_build_registries()`，**不需動 price_monitor_bot**

---

## §並發與 DB 擁有權（拆 process 前必讀，已定案）

拆成三個 process 後，最大的技術陷阱是**多個 process 共用一個 SQLite 檔**。
這已討論定案，所有 Task 的 DB 設計都必須遵守本節，不要回退成「共用同一檔 + busy_timeout」。

### 為什麼「獨立 table」不行

SQLite 的鎖是**檔案（database）層級**，不是 table 層級：

- Rollback journal 模式：任一寫入交易鎖住整個 `.sqlite3` 檔。
- WAL 模式（目前用的）：放寬到「多讀者 + 單寫者並行」，但那個**單寫者限制仍是全檔案層級**——
  同一檔案同時只能有一個 writer。
- 所以「telegram process 寫 A table、sns_monitor 寫 B table，但在同一個檔」→
  兩個寫入**仍在檔案層級互相序列化**，會撞 `database is locked`。**獨立 table 沒有任何隔離效果。**

### 定案：single-writer-per-file + inbox 介面（don't share DB, share interface）

**核心規則：每個 `.sqlite3` 檔只能有一個 process 會「寫」它。**

| DB 檔（實際路徑，settings 為準） | 唯一 writer | 其他 process |
|---|---|---|
| `data/sns.sqlite3`（SNS 追蹤目標、buzz；`sns_db_path`） | sns_monitor service | telegram 唯讀 |
| `data/monitor.sqlite3`（價格追蹤、警示；`monitor_db_path`） | price_monitor service | telegram 唯讀；sns_monitor 的 KnowledgePrewarmer 唯讀 |
| `data/knowledge.sqlite3`（知識庫/RAG；`knowledge_db_path`） | **sns_monitor service**（見下方說明） | telegram 唯讀；telegram 寫入走 knowledge_inbox |
| `data/opportunities.sqlite3`（/hunt；`opportunity_db_path`） | **opportunity service（既有）** | telegram 唯讀；telegram 寫入走 opportunity_inbox |
| `data/quiz.sqlite3` | telegram process | 背景 service 不碰 |
| `data/*_inbox.sqlite3`（sns / watch / knowledge / opportunity） | 請求 producer（目前都是 telegram） | owner service 高頻 poll（見下） |

**⚠️ 檔名一律以 settings 欄位為準（`sns_db_path` / `monitor_db_path` / …），沿用既有檔案。**
不要發明新檔名（例如 `sns_monitor.sqlite3`）建出空檔、把既有資料晾在一邊。

**讀**：telegram 的查詢（`/snslist` `/snsbuzz` `/watch` 列表、`/hunt status`、knowledge grounding）
以 `mode=ro` + WAL 唯讀開 owner 的主檔。WAL 下「owner 寫 + telegram 讀」並行不互卡、零延遲。
小坑：唯讀開 WAL 檔需要 `-shm`/`-wal` 可寫（同一使用者下沒問題）；若撞到
`unable to open database file`，先確認 owner service 已跑過、檔案已是 WAL mode，不要亂繞。

**寫**：telegram 的**一切寫入——指令和 callback 按鈕都算**——永不直接寫 owner 的主檔，
一律寫一筆 request 到對應 inbox。涵蓋範圍：

- `sns_inbox` ← `/snsadd` `/snsdelete` ＋ callback `snsdel` `snsaddok` `snsfb`
- `watch_inbox` ← `/watch add` 等寫入指令 ＋ callback `del` `close`
- `knowledge_inbox` ← `/knowledge` 寫入操作、dynamic_tools/RAG 寫入、callback `ragkeep` `ragdel`
- `opportunity_inbox` ← `/hunt del`、pin/unpin、別名修改 ＋ callback `oppfb` `cond` `bulk`

owner service poll 撈 inbox → 寫自己的主檔 → 標記 done →（需要時）`TelegramBotClient` HTTP 推播。

**inbox poll 頻率（重要，別做錯）**：撈 inbox 必須是**獨立的高頻輕量迴圈（每 2~5 秒）**，
不可綁在 agent 的主掃描週期上（opportunity agent 是 15 分鐘一輪——按鈕等 15 分鐘才生效
是不可接受的）。讀一個小 inbox 檔幾乎零成本。

**按鈕 UX（樂觀回應）**：callback 寫進 inbox 後**立刻**回 toast（「✅ 已記錄」），重繪畫面用
樂觀更新（直接移除/標記該項），不重查主檔——owner 還沒套用，重查會看到舊狀態。

**inbox 的多 writer 例外**：若未來有多個 producer（telegram + dashboard）寫同一個 inbox，
inbox 是**唯一**容許多 writer 的檔——寫入是極小、極稀疏的 INSERT，用 WAL +
`PRAGMA busy_timeout=5000` + retry 包住即可。**主檔鐵則單一 writer，無例外。**

這樣**所有主檔都只有一個 writer**，跨 process 寫入競爭從架構上消滅，不是靠鎖硬撐。

### knowledge / opportunities 擁有權（已拍板）

- **`data/knowledge.sqlite3` owner = sns_monitor。** 理由：寫入大戶在 SNS 管線
  （`sns_tools.py` 的 classifier 會 `mark_referenced` / `append_observation`、
  `EntityResearcher` 持續寫研究結果）。telegram 端寫入（/knowledge、RAG、ragkeep/ragdel）
  量少 → 走 knowledge_inbox；讀取（grounding、/kb 視圖）照常唯讀。
  `KnowledgePrewarmer` 唯讀 `data/monitor.sqlite3`（跨 service 讀，允許）。
- **`data/opportunities.sqlite3` owner = opportunity service（既有）。** 此檔**今天就是
  雙 writer**（opportunity service ＋ telegram 的 /hunt 寫入），屬既存違規；Task 3.5 搬
  /hunt 時一併把 telegram 端寫入改走 opportunity_inbox，opportunity service 加 2~5 秒
  inbox poll 迴圈。

### inbox table 形狀（最小實作，不要上 MQ/socket）

```sql
-- sns_inbox.sqlite3，唯一 writer = telegram process
CREATE TABLE IF NOT EXISTS sns_requests (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id    TEXT NOT NULL,
  action     TEXT NOT NULL,          -- 'add' | 'delete' | 'feedback' | 'pin' | 'close' | …依 domain 擴充
  payload    TEXT NOT NULL,          -- 帳號/keyword/JSON
  status     TEXT NOT NULL DEFAULT 'pending',  -- pending | done | error
  created_at TEXT NOT NULL,
  processed_at TEXT
);
```

- telegram：`/snsadd @foo` → INSERT 一筆 pending → 立刻回 ack「收到，正在加入追蹤…」
- sns_monitor：每輪 poll `WHERE status='pending'` → 寫主檔 → UPDATE status='done' → 推播結果

### Schema migration 擁有權

**每個檔的 schema migration 只由它的 writer owner 在啟動時負責**，零跨 process race：

- 例：`data/sns.sqlite3` 由 sns_monitor 建表；`sns_inbox` 由 telegram（producer）建表。
- telegram 唯讀開背景主檔前，假設 schema 已存在；若檔不存在或缺表 → 提示「監控 service 未啟動」，
  不要自己建表（避免兩邊 schema 定義分歧）。

### 這同時修掉的兩個先前疑慮

- **Option B 矛盾**（SNS 邏輯說要在 sns_monitor，但指令其實跑在 telegram process）：
  釐清為「**寫入邏輯歸 owner service**（透過 inbox 觸發），telegram 只做**唯讀查詢 + 丟寫入請求**」。
  指令 handler 本身很薄，不含 SNS 業務寫入邏輯。
- **migration ownership 未定義**：見上，每檔由其 writer owner 負責。

---

## §服務獨立性原則（設計不變量，Codex 必須維持）

切割目標是「**runtime 獨立**」：三個 service 各自跑、各自 log、各自重啟、互不拖累。
以下是必須維持的不變量，未來新增任何功能（含 dashboard）都要遵守。

### 1. 依賴方向：telegram → 背景 service（單向，不可反轉）

- telegram 的 SNS／price 查詢指令**唯讀依賴**背景 service 的 DB 檔（`/snslist` 讀
  `data/sns.sqlite3`）。這是**正確且刻意**的：telegram 的 SNS 功能本來就該依賴 sns_monitor。
- **反向絕對禁止**：sns_monitor / price_monitor **不可** import 或依賴 telegram process 的任何
  狀態。背景 service 要推 Telegram → 用 `TelegramBotClient` 走 HTTP `sendMessage`
  （`sendMessage` 與 `getUpdates` 不衝突 → telegram polling 掛了，背景推播照常運作）。

### 2. inbox 是「通用寫入入口」，不是 telegram 專用

任何**非 owner** 想寫背景 service 的主檔，一律走該檔的 inbox（維持 single-writer-per-file）。

- telegram 的 `/snsadd` → 寫 `sns_inbox.sqlite3`。
- 未來 sns_monitor **dashboard** 的「新增追蹤」按鈕，若 dashboard 是獨立 web process →
  也寫 `sns_inbox.sqlite3`，不可直接寫主檔。
- 若 dashboard 跟 sns_monitor 同一個 process（只是它開的 web thread）→ 可直接寫（仍是單一 writer）。

→ 結論：sns_monitor 的輸入管道可無限擴充（telegram 指令、dashboard、未來其他來源），
都走 inbox，永不破壞單一 writer。

### 3. Dashboard 情境（已確認可行）

sns_monitor 日後可加獨立 dashboard：
- **看監控**（唯讀）→ `mode=ro` 開 `data/sns.sqlite3`，不需 telegram / price 在跑。
- **寫入** → 依 §2 走 inbox 或與 owner 同 process。
- dashboard **只碰自己 service 的檔**，不可去讀 telegram / price 的 DB。

### 4. 共用 code / .env 是刻意的 monorepo，不要過早拆 repo

- 三個 service 共用同一份原始碼（`TelegramBotClient`、`assistant_runtime`）與同一份 `.env`
  （唯讀共用設定，無寫競爭）。這是**良性的 monorepo 共用**，非缺點。
- 「runtime 獨立」已達成；「原始碼／repo 獨立」是**更下一步**，只有當某個 service 要部署到
  另一台機器、或要成為完全獨立 pip 套件時才需要。**目前不做**——過早拆 repo 只增維護成本。
- 若未來真要拆：把 `TelegramBotClient` 抽成共用 pip 套件、各 service 各自管自己的 settings/.env。

---

## Task 1 — /knowledge 邏輯 + DB 搬到 aka_no_claw

### 現狀（問題）

`price_monitor_bot/bot.py` 仍有 `/knowledge` 的 sub-view 路由邏輯：

```python
# bot.py ~1248（仍在 build_reply_plan 裡）
if sub == "market":
    text, reply_markup, _ = self.render_knowledge_market_view()
    return TelegramTextReplyPlan(...)
if sub == "coding":
    text, reply_markup, _ = self.render_knowledge_coding_view()
    return TelegramTextReplyPlan(...)
return TelegramTextReplyPlan(reply=self._handle_knowledge(remainder, str(chat_id)))
```

`render_knowledge_market_view` / `render_knowledge_coding_view` 也定義在 bot.py，
但資料來自 `KnowledgeDatabase`（aka_no_claw 的 DB）。

### 目標

`price_monitor_bot` 完全不含 knowledge domain 邏輯。`/knowledge` 透過 registry 轉發。

### 做法

1. **aka_no_claw `knowledge_command.py`**
   - `build_knowledge_handler(settings)` 返回的 handler 自己解析 `remainder`，
     處理 `market` / `coding` sub-view（直接在 handler 內 return `(text, markup)` tuple）
   - 把 `render_knowledge_market_view` / `render_knowledge_coding_view` 的邏輯搬進來
     （或直接 inline 進 handler，視複雜度決定）
   - **注意：** Task 3 之後 `data/knowledge.sqlite3` 擁有權歸 sns_monitor（見 §並發），
     telegram 端的 knowledge 寫入要改走 knowledge_inbox。Task 1 階段仍是單一 process，
     可先直接寫，但請把「寫入操作」集中在少數函式，方便 Task 3 抽換。

2. **aka_no_claw `telegram_bot.py` `_build_registries()`**
   - 加一條：`"/knowledge": RegisteredCommand(build_knowledge_handler(settings))`
   - 別名 `"/kb"` 也要加

3. **price_monitor_bot `bot.py`**
   - 移除 `KNOWLEDGE_COMMANDS` 常數
   - 移除 `build_reply_plan` 裡的 `/knowledge` 分支（含 sub-view if/elif）
   - 移除 `_handle_knowledge` method
   - 移除 constructor 的 `knowledge_handler` param + `self._knowledge_handler` + `knowledge_db_path`
   - 移除 `render_knowledge_market_view` / `render_knowledge_coding_view` methods
   - 移除 `run_telegram_polling` 的 `knowledge_handler` / `knowledge_db_path` params + 傳遞

4. **測試**
   - aka_no_claw：新增 `/kb market`、`/kb coding`、`/kb <query>` 的 handler 單元測試
   - price_monitor_bot：確認 `/knowledge` 不再有內建分支；registry test 加 `"/knowledge"` key
   - 兩套跑全綠 `price_monitor_bot/.venv/bin/python -m pytest -q`
     + `aka_no_claw/.venv/bin/python -m pytest -q`

5. **重啟驗證**
   - `launchctl kickstart -k gui/$(id -u)/local.openclaw.telegram`
   - lsof 確認 ESTABLISHED；測 `/kb market`、`/kb coding`、`/knowledge 皮卡丘`

---

## Task 2 — /sns 邏輯 + DB 解耦出 price_monitor_bot

### 現狀（問題）

`price_monitor_bot/bot.py` 有大量 SNS domain 邏輯：

- `SNS_ADD_COMMANDS / SNS_LIST_COMMANDS / SNS_DELETE_COMMANDS / SNS_BUZZ_COMMANDS`
- `_handle_sns_add / _handle_sns_delete / _handle_sns_list / _handle_sns_buzz` methods
- callback 分支：`snsdel / snsaddok / snsfb` 硬寫在 `handle_telegram_callback_query`
- constructor 持有 `SnsDatabase`（`self._sns_db`）、`self._sns_buzz_fn`

### 目標

SNS 所有指令邏輯和 DB 操作都在 `sns_monitor`（或獨立的 sns service）；
`price_monitor_bot` 不含任何 SNS domain 邏輯，完全透過 registry 轉發。

### 目標架構

```
_build_registries() 加入：
  command_handlers["/snsadd"]    = RegisteredCommand(build_sns_add_handler(settings), ...)
  command_handlers["/snsdelete"] = RegisteredCommand(build_sns_delete_handler(settings), ...)
  command_handlers["/snslist"]   = RegisteredCommand(build_sns_list_handler(settings), ...)
  command_handlers["/snsbuzz"]   = RegisteredCommand(build_sns_buzz_handler(settings), ...)

  callback_handlers["snsdel"]   = build_snsdel_callback_handler(settings)
  callback_handlers["snsaddok"] = build_snsaddok_callback_handler(settings)
  callback_handlers["snsfb"]    = build_snsfb_callback_handler(settings)
```

`sns_monitor` 側新增這些 `build_sns_*_handler(settings)` builder。

### 做法

1. **sns_monitor（aka_no_claw sub-package）**
   - 新增 `telegram_commands.py`（或類似）
   - 實作 `build_sns_add_handler` / `build_sns_delete_handler` /
     `build_sns_list_handler` / `build_sns_buzz_handler`
     — 把現有 `bot.py._handle_sns_*` 邏輯搬進來，handler 簽名 `(remainder, chat_id) -> str`
   - 實作 `build_snsdel_callback_handler` / `build_snsaddok_callback_handler` /
     `build_snsfb_callback_handler`
     — 把現有 callback 分支邏輯搬進來，簽名 `(payload, original_text, chat_id) -> (toast, text, markup)`

2. **aka_no_claw `telegram_bot.py` `_build_registries()`**
   - import 並加入上述 handlers

3. **price_monitor_bot `bot.py`**
   - 移除 `SNS_*_COMMANDS` 常數
   - 移除 `build_reply_plan` 裡全部 `/sns*` 分支
   - 移除 `_handle_sns_*` methods（共 4 個）
   - 移除 `handle_telegram_callback_query` 裡 `snsdel / snsaddok / snsfb` callback 分支
   - 移除 constructor 的 `sns_db` + `sns_buzz_fn` params + `self._sns_db` + `self._sns_buzz_fn`
   - 移除 `run_telegram_polling` 的 `sns_db` / `sns_buzz_fn` params

4. **測試 + 重啟驗證**（同 Task 1 流程）

### SNS polling 架構決定（選項 B）

**決定：同 bot token，SNS 背景監控拆成獨立 launchd service。**

- Telegram /sns* 指令仍透過主 bot 的 registry 路由 → log 仍在主 bot（`logs/openclaw_telegram.log`）
- SNS 背景監控（RSS 輪詢、keyword 追蹤、推播通知）拆成獨立 service：
  `local.openclaw.sns_monitor`，有自己的 log 檔（`logs/openclaw_sns_monitor.log`）
- 不需新 bot token；使用者體驗不變（同一個 bot）
- 可獨立 `launchctl kickstart -k gui/$(id -u)/local.openclaw.sns_monitor` 重啟監控，
  不影響主 polling

---

## Task 3 — SNS 背景監控拆成獨立 launchd service

> **前提：** Task 2 完成

**目標：** `local.openclaw.sns_monitor` 獨立 process，只跑背景監控（不含 Telegram polling）。

### 做法

1. **sns_monitor entry point**
   - 在 `aka_no_claw` 新增 `src/sns_monitor/__main__.py`（或
     `src/openclaw_adapter/sns_monitor_service.py`）
   - 只啟動背景監控執行緒（RSS 輪詢、keyword scheduler、推播通知發送）
   - 推播用 `TelegramBotClient`（直接 HTTP，不用 polling）
   - 有自己的 logging 設定，輸出到 `logs/openclaw_sns_monitor.log`

2. **aka_no_claw 主 process 移除 SNS 背景執行緒**
   - `run_telegram_polling()` 裡的 `_start_sns_monitor()` 呼叫移除
   - **DB 擁有權依 §並發與 DB 擁有權（single-writer-per-file + inbox）：**
     - `data/sns.sqlite3` 唯一 writer = sns_monitor service（沿用既有檔，勿建新檔名）
     - `data/knowledge.sqlite3` 擁有權**隨 SNS classifier 一起移轉給 sns_monitor**
       （EntityResearcher / `mark_referenced` / `append_observation` 都在它的管線裡）；
       telegram 端的 knowledge 寫入（/knowledge、RAG、ragkeep/ragdel）改走 `knowledge_inbox`
     - telegram 的 `/snslist` `/snsbuzz` 唯讀（`mode=ro` + WAL）開主檔查詢
     - telegram 的寫入——`/snsadd` `/snsdelete` **與 callback `snsdel`/`snsaddok`/`snsfb`**——
       **不直接寫主檔**，改寫 `sns_inbox.sqlite3`；sns_monitor 以 2~5 秒迴圈 poll inbox →
       寫主檔 → 推播；callback 用樂觀 toast／重繪（見 §並發）
   - **不要**讓兩個 process 寫同一個 `.sqlite3`（獨立 table 也不行，鎖是檔案層級）

3. **launchd plist**
   - 新增 `local.openclaw.sns_monitor.plist`，KeepAlive，`logs/openclaw_sns_monitor.log`

4. **驗證**
   - `launchctl kickstart -k gui/$(id -u)/local.openclaw.sns_monitor`
   - `tail -f logs/openclaw_sns_monitor.log` 看到 RSS 輪詢 log
   - 主 bot log 不再有 SNS 背景執行緒雜訊
   - /snsadd / /snslist 指令仍可用（透過 registry）

---

## Task 3.5 — /hunt 邏輯搬到 aka_no_claw（含修既存雙 writer）

### 現狀（問題）

`price_monitor_bot/bot.py` 有 /hunt 的指令路由 + opportunity-specific callbacks：

- `HUNT_COMMANDS = {"/hunt", "/opportunity"}` + `build_reply_plan` 裡的分支
- `_handle_hunt()` method（轉發給 `opportunity_status_renderer` / `opportunity_list_provider` 等）
- constructor kwargs：`opportunity_status_renderer`, `opportunity_target_remover`,
  `opportunity_list_provider`, `opportunity_alias_updater`, `opportunity_target_pinner`,
  `opportunity_target_unpinner`（共 6 個）
- callback 分支：`oppfb`, `cond`, `bulk`（hunt 專屬）

但 opportunity DB + 所有 agent 邏輯（`dismiss_opportunity_target`,
`format_opportunity_status`, `list_opportunity_targets` 等）已全在 aka_no_claw。

**既存違規（本 task 一併修）：** `data/opportunities.sqlite3` 現在就被兩個 process 寫——
opportunity service（既有獨立 launchd service）寫掃描結果，telegram process 的 /hunt
寫入操作（`dismiss_opportunity_target` / `pin_opportunity_target` /
`record_opportunity_feedback`(oppfb) / alias）也直接寫同一檔。違反 §並發鐵則。

### 目標

`/hunt` 透過 registry 轉發；price_monitor_bot 不含任何 opportunity domain 邏輯。

**注意：** `pg` / `del` / `close` / `popt` / `topt` / `noop` 這些通用 Telegram UI callback
也一併搬到 aka_no_claw（它們是純 Telegram UI，不是 price domain）。
`pg`/`del` 目前呼叫 `render_watchlist_view`（price_monitor_bot），
搬移時需將 watchlist list renderer 以 callback 形式注入或抽成獨立 builder，
讓 aka_no_claw 的 handler 能呼叫到。

### 做法

1. **aka_no_claw `opportunity_agent.py`（或新增 `opportunity_command.py`）**
   - 新增 `build_hunt_handler(settings)` → handler 自己處理 `/hunt list`、
     `/hunt status`、空動作等子命令分派，返回 `str` 或 `(text, markup)` tuple
   - 新增 `build_hunt_callback_handler(settings)` → 處理 `oppfb` / `cond` / `bulk`
     （簽名：`(payload, original_text, chat_id) -> (toast, new_text, markup)`）

2. **aka_no_claw `_build_registries()`**
   ```python
   "/hunt":        RegisteredCommand(build_hunt_handler(settings)),
   "/opportunity": RegisteredCommand(build_hunt_handler(settings)),
   ```
   ```python
   "oppfb": build_hunt_callback_handler(settings),  # oppfb prefix
   "cond":  build_hunt_callback_handler(settings),  # cond prefix
   "bulk":  build_hunt_callback_handler(settings),  # bulk prefix
   ```
   （或用一個 handler 內部 switch prefix，視實作決定）

3. **telegram 端寫入改走 `opportunity_inbox`（修既存雙 writer）**
   - `build_hunt_handler` / `build_hunt_callback_handler` 內的寫入操作
     （dismiss、pin/unpin、alias、oppfb 回饋、cond、bulk）改寫 `opportunity_inbox.sqlite3`，
     不再直接寫 `data/opportunities.sqlite3`
   - opportunity service 加一個**獨立的 2~5 秒 inbox poll 迴圈**
     （不可綁在 15 分鐘的主掃描週期上——按鈕等 15 分鐘才生效不可接受）
   - callback 樂觀回應：toast 立即回「已記錄」，重繪用樂觀更新、不重查主檔
   - 讀取（`/hunt status` `/hunt list`）改 `mode=ro` 唯讀開 `data/opportunities.sqlite3`

4. **price_monitor_bot `bot.py`**
   - 移除 `HUNT_COMMANDS` 常數
   - 移除 `build_reply_plan` 裡 `/hunt` 分支
   - 移除 `_handle_hunt()` method
   - 移除 6 個 opportunity kwargs + `self._opportunity_*` 屬性
   - 移除 `handle_telegram_callback_query` 裡 `oppfb` / `cond` / `bulk` 分支
   - 移除 `run_telegram_polling` 的對應 params
   - 移除 `pg` / `del` / `close` / `popt` / `topt` / `noop` 分支
     （純 Telegram UI，隨 hunt callback 一起搬到 aka_no_claw；watchlist renderer 需抽成 builder 注入）

5. **測試 + 重啟驗證**（同前；額外重啟 `launchctl kickstart -k gui/$(id -u)/local.openclaw.opportunity`
   並實測按 oppfb 按鈕 → 數秒內 inbox 標 done、主檔有寫入）

---

## Task 4 — price_monitor 背景監控拆成獨立 launchd service

> **前提：** Task 1 + Task 2 完成（price_monitor_bot 已是純基礎設施）

**目標：** `local.openclaw.price_monitor` 獨立 process，只跑背景監控（不含 Telegram polling）。

### 目前跑在 telegram process 裡、需要搬出來的背景工作

| 目前程式位置 | 做什麼 |
|---|---|
| `aka_no_claw/telegram_bot.py: _start_watch_monitor()` | 價格警示監控，觸發時用 TelegramBotClient 推播 |
| `aka_no_claw/telegram_bot.py: _start_card_image_crawler()` | 定期爬 snkrdunk 熱門商品圖片、預熱 hash DB |
| `price_monitor_bot/watch_monitor.py: ensure_monitor()` | 背景 watchdog + 輪詢 |

### 做法

1. **price_monitor_bot（或 aka_no_claw）新增 service entry**
   - 新增 `src/price_monitor_bot/monitor_service.py`（或 aka_no_claw 的 entry）
   - 只啟動背景監控執行緒：watch_monitor + card image crawler
   - 推播用 `TelegramBotClient`（直接 HTTP，不用 polling）
   - 有自己的 logging，輸出到 `logs/openclaw_price_monitor.log`

2. **aka_no_claw `run_telegram_polling()` 移除背景監控呼叫**
   - 移除 `_start_watch_monitor()`
   - 移除 `_start_card_image_crawler()`
   - `telegram` service 只剩 polling + 指令 dispatch + 各 scheduler（quiz daily、rag daily、backup）

3. **launchd plist**
   - 新增 `local.openclaw.price_monitor.plist`，KeepAlive
   - log：`logs/openclaw_price_monitor.log`

4. **DB 擁有權（依 §並發與 DB 擁有權）**
   - `data/monitor.sqlite3` 唯一 writer = price_monitor service（它 write-heavy；沿用既有檔）
   - telegram 的 watchlist 查詢唯讀（`mode=ro` + WAL）開該檔；
     sns_monitor 的 KnowledgePrewarmer 也唯讀此檔（跨 service 讀，允許）
   - telegram 的寫入——`/watch add` `/watch del` 等指令**與 callback `del`/`close`**——
     → 寫 `watch_inbox.sqlite3`；price_monitor 以 2~5 秒迴圈 poll inbox → 寫主檔
   - **不要**兩個 process 寫同一個 `.sqlite3`（鎖是檔案層級，獨立 table 無效）

5. **驗證**
   - `launchctl kickstart -k gui/$(id -u)/local.openclaw.price_monitor`
   - `tail -f logs/openclaw_price_monitor.log` 看到 watch_monitor + crawler log
   - 主 bot log 不再有背景監控雜訊
   - watchlist 價格警示推播仍正常送達

---

## §資安加固（待辦，可隨架構 task 一起或獨立處理）

> 已完成（零代碼）：`.env` 改 600 權限、docs 去識別、`*.sqlite3-journal` 移出 git 追蹤。
> 以下四項需改 code，已掃描確認。

### SEC-1 — 啟動防呆：allowlist 空白不可 fail-open（低成本、高價值）

**問題：** `bot.py:883` `is_allowed_chat` 在 `_allowed_chat_ids` 為空時直接回 `True`（fail-open）。
若 `.env` 漏設 `OPENCLAW_TELEGRAM_CHAT_IDS`，**全世界都能對 bot 下 `/new` 動態執行代碼**。

**修法（price_monitor_bot `bot.py`）：**
```python
# run_telegram_polling 開頭，驗證 allowed_chat_ids 非空
if not allowed_chat_ids:
    raise RuntimeError("OPENCLAW_TELEGRAM_CHAT_IDS 未設定，拒絕啟動（防止 fail-open）")
```
一行，不影響任何正常操作。

---

### SEC-2 — Log redaction：防止 bot token 隨例外進入 log 檔（低成本）

**問題：** `TelegramBotClient._base_url` 含 token（`https://api.telegram.org/bot<TOKEN>/…`）。
HTTPError traceback 若把完整 URL 印進 log，token 就躺在 `logs/openclaw_telegram.log` 與備份裡。
Log 檔本身只有本機可讀，但備份在外接 SSD（如未加密則風險升高）。

**修法（price_monitor_bot `bot.py`）：**
```python
import logging, re

class _TokenRedactFilter(logging.Filter):
    _PAT = re.compile(r"bot[0-9]{8,12}:[A-Za-z0-9_-]{30,}")
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = self._PAT.sub("bot<REDACTED>", record.msg)
        record.args = tuple(
            self._PAT.sub("bot<REDACTED>", a) if isinstance(a, str) else a
            for a in (record.args or ())
        )
        return True
```
在 `configure_logging`（`assistant_runtime/logging_utils.py`）把這個 filter 加到 root logger 即可；
約 15 行。

---

### SEC-3 — `/new` pip allowlist：防止 LLM 自選惡意套件（中成本）

**問題：** `dynamic_tools.py` 的 `# requires:` 機制讓 LLM 自行指定套件名自動 `pip install`。
觸發面：prompt injection（你貼一篇文章，文章裡夾帶 `# requires: reqests`（typo）→ 裝到惡意套件）。
`_is_safe_pkg` 只驗名稱格式，不驗套件身份。

**修法（`dynamic_tools.py`）：**
```python
_APPROVED_PACKAGES = frozenset({
    "yfinance", "requests", "httpx", "beautifulsoup4", "bs4",
    "pandas", "numpy", "lxml", "html5lib", "tabulate",
    "python-dateutil", "pytz", "tqdm", "pillow",
    # 可隨需要擴充
})

def _is_approved_pkg(name: str) -> bool:
    base = re.split(r"[><=!\[]", name)[0].lower().strip()
    return base in _APPROVED_PACKAGES
```
在 `_pip_install` 裡，不在清單的套件先送 Telegram 訊息請你確認，等你回「ok」再裝。
觸發面已是白名單 chat（=你自己），緊急程度中等。

---

### SEC-4 — `/new` 沙箱：防止生成代碼讀寫你的 home 目錄（中成本）

**問題：** `/new` 生成的代碼以你的 user 身分跑，`_clean_env` 只遮蔽 `$HOME`，但代碼可以
硬編 `/Users/jen/.ssh/id_rsa` 直接讀取。觸發面：prompt injection。
白名單 chat（=你自己）大幅降低實際風險，但仍是最大的潛在傷面。

**修法（`dynamic_tools.py` `_execute` 方法）：**
用 macOS `sandbox-exec` 包住 subprocess，用最小 profile：
```python
_SANDBOX_PROFILE = """
(version 1)
(allow default)
(deny file-write* (subpath "/Users"))
(allow file-write* (subpath "{tool_dir}"))
(deny network-outbound (remote ip "169.254.169.254"))  ; block cloud metadata
"""

# 在 _execute 裡：
cmd = ["sandbox-exec", "-p", _SANDBOX_PROFILE.format(tool_dir=str(tool_dir)),
       str(python), str(tool_path), ...]
```
`sandbox-exec` 是 macOS 內建，無需安裝。拒絕寫 `/Users` 以外只允許 tool dir，
network 維持允許（/new 的工具本來就會抓資料）。

---

## 注意事項

- **單向依賴不可反轉：** `aka_no_claw → price_monitor_bot`，不能反過來 import
- **pytest 跑法：** `.venv/bin/python -m pytest -q`（不加 PYTHONPATH 前綴，不用 heredoc）
- **重啟：** `launchctl kickstart -k gui/$(id -u)/local.openclaw.telegram`，
  驗證：`lsof -nP -p <pid> | grep 149.154.*:443.*ESTABLISHED`
- **push 前：** 跑兩套全測試綠，Rule A 摘要等確認後才 push
