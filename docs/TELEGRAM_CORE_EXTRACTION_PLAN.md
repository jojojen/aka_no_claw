# Telegram Core Extraction Plan — shared `telegram_core` package

Last reviewed: 2026-07-02
Status: Planned
Owner area: telegram

Implementation + acceptance plan for extracting the Telegram infrastructure
layer out of `price_monitor_bot` into a new shared package **`telegram_core`**,
so that BOTH `price_monitor_bot` and `aka_no_claw` depend on it. Today the
dependency is inverted: `aka_no_claw`（the actual live bot）掛在
`price_monitor_bot` 的 Telegram 骨架上，還要靠 monkey-patch 才接得上。

Companion / cross-links:
[TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md](TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md)
(the NL-routing sibling of this infra split; `telegram_nl` is also the
packaging precedent), [SYSTEM_MAP.md](SYSTEM_MAP.md), [TASK_ROUTING.md](TASK_ROUTING.md).

## 0. Problem statement

`aka_no_claw` is the only live Telegram bot (tmux session `telegram` runs
`openclaw_adapter telegram-poll`). Yet the transport, the dispatcher contract,
and the polling loop all live in `price_monitor_bot`, which itself never runs a
poller (it only re-exports `run_telegram_polling` in
`price_monitor_bot/__init__.py:29` — no script or `__main__` uses it). The
result: 13 of aka's modules import `price_monitor_bot`, and the poller can only
be customised by **globally monkey-patching a class attribute on the foreign
module** before calling it.

想要的終態：

```text
        telegram_nl  (already shared: NL intent router)
             ▲
             │
        telegram_core  (NEW: transport + dispatcher contract + polling + list_view)
         ▲         ▲
         │         │
 price_monitor_bot   aka_no_claw (openclaw_adapter)
 (price/TCG domain:   (bot composition, OpenClaw commands,
  lookup, watch,       music/quiz/voice/sns/opportunity…)
  photo pipeline,          │
  renderers)               └────────► price_monitor_bot（只剩 price 領域內容，
                                       不再是 Telegram 基礎設施的房東）
```

`telegram_core` 對 `price_monitor_bot`／`openclaw_adapter` **零 import**（用
零依賴 pyproject + grep gate 強制），杜絕循環。

## 1. Current state — coupling inventory (verified against code, 2026-07-02)

### 1.1 The monkey-patch (worst signal)

- `openclaw_adapter/telegram_bot.py:17` — `import price_monitor_bot.bot as _price_bot_module`
- `telegram_bot.py:319` — `class TelegramCommandProcessor(_BaseTelegramCommandProcessor)` subclass
- `telegram_bot.py:1912-1916` — **`_price_bot_module.TelegramCommandProcessor = lambda **kwargs: TelegramCommandProcessor(settings=…, workflow_editor=…, **kwargs)`** then calls `_base_run_telegram_polling(...)`

Root cause: `price_monitor_bot/bot.py:2895` — `run_telegram_polling` instantiates
the **module-level** `TelegramCommandProcessor` itself, with no injection point.
aka has no other way to get its subclass (help text, YouTube like-song plan,
zh-translate handler, workflow editor text capture) into the loop.

### 1.2 Full import map (aka_no_claw → price_monitor_bot)

| aka module | Imports | Nature |
|---|---|---|
| `telegram_bot.py:22-53` | 30+ symbols from `price_monitor_bot.bot` (client, plan types, renderers, `run_telegram_polling`, processor) | **infrastructure + domain mixed** |
| `telegram_bot.py:1962,1996,2155` | `TelegramBotClient` (RAG digest, home schedule, quiz scheduler senders) | infrastructure |
| `opportunity_agent.py:23` `quiz_command.py:57` `voice_command.py:29` | `TelegramBotClient` | infrastructure |
| `telegram_bot.py:104` `sns_commands.py:19` `opportunity_command.py:12` `knowledge_command.py:89,123` `music_favorites.py:23` `command_bridge.py:2165` | `price_monitor_bot.list_view` (`ListRow`, `build_list_view`, mode consts) | infrastructure (generic UI primitive) |
| `telegram_bot.py:173` | `TelegramTextReplyPlan` | infrastructure (contract) |
| `commands.py:5` `formatters.py:3` | price commands / formatters re-export wrappers | **price domain — legitimate direction**, but wrapper indirection is dead weight |
| `telegram_bot.py:54` `research_command.py:2935` | `watch_monitor` | price domain — legitimate |
| `opportunity_agent.py:24` | `commands.lookup_card` | price domain — legitimate |

**No other consumers**: `sns_monitor_bot`, `reputation_snapshot`,
`aka_no_claw_web` import `price_monitor_bot` **nowhere** (grep-verified). The
migration only has two clients: aka + price itself.

### 1.3 `price_monitor_bot/bot.py` is a 4,392-line mixed bag

Generic infrastructure and price/TCG domain logic interleave in one file; the
processor class alone is ~1,500 lines. Symbol-level disposition in §3.

### 1.4 Packaging precedent: `telegram_nl`

`telegram_nl` already proves the pattern: sibling repo, zero-dependency
pyproject (`name = "telegram-nl"`), editable-installed into consumers
(aka `.venv` has `price-monitor-bot` and `telegram-nl` both editable from
sibling dirs; `price-monitor-bot` declares `telegram-nl` as a dependency).
`telegram_core` follows the identical pattern.

> Considered alternative — a second top-level package inside the
> price_monitor_bot repo (like `market_monitor`/`tcg_tracker`). Rejected: the
> point is that Telegram infra must not live in the price repo's blast radius;
> a shared repo also lets `price_monitor_bot` pin/upgrade independently.

## 2. Target architecture

New sibling repo `~/ai_work_space/related_to_claw/telegram_core`:

```text
telegram_core/
  pyproject.toml            # name="telegram-core", dependencies = []  ← 零依賴是硬規則
  src/telegram_core/
    __init__.py             # re-export public API
    transport.py            # TelegramBotClient, TelegramFileAttachment, multipart
    contracts.py            # RegisteredCommand, TelegramTextReplyPlan,
                            # TelegramTextIntentOption, PendingTelegramTextClarification
    processor.py            # CoreCommandProcessor（generic registries + allowlist）
    polling.py              # run_telegram_polling, PollHeartbeat, watchdog, drain
    list_view.py            # verbatim move
    logging_utils.py        # mask_identifier, trim_for_log
  tests/                    # moved/duplicated coverage for the above
```

After the plan completes:

- `price_monitor_bot.bot` keeps ONLY price/TCG domain: query dataclasses,
  photo-intent pipeline, watch/set-price parsing, renderers, price command
  sets, and a `PriceCommandProcessor(CoreCommandProcessor)` subclass. It
  depends on `telegram-core`.
- `openclaw_adapter.telegram_bot` builds its processor **by composition /
  explicit factory injection** — the monkey-patch is gone. Infra imports point
  at `telegram_core`; imports of `price_monitor_bot` remain only for genuine
  price features (lookup, trend, watch, photo renderers), which the aka bot
  really does expose to the user.
- `run_telegram_polling` lives in `telegram_core.polling`, takes a
  processor (or factory) parameter, and routes domain-specific callbacks via
  the SAME registry mechanism the pluggable commands already use — no
  hardcoded `cond:`/`snsbulk:` branches in core.

## 3. Symbol disposition table (`price_monitor_bot/bot.py` → where)

| Symbol (bot.py line) | Disposition | Phase |
|---|---|---|
| `TelegramBotClient` (:604) | → `telegram_core.transport` — pure urllib Telegram API transport | P1 |
| `TelegramFileAttachment` (:294), `_encode_multipart_body` (:4338) | → `transport` | P1 |
| `send_telegram_test_message` (:3756) | → `transport` | P1 |
| `list_view.py` (whole file, 105 lines) | → `telegram_core.list_view` verbatim | P1 |
| `logging_utils.py` (`mask_identifier`, `trim_for_log`, 21 lines) | → `telegram_core.logging_utils` | P1 |
| `RegisteredCommand` (:308), `TelegramTextReplyPlan` (:328) | → `telegram_core.contracts` | P1 |
| `TelegramTextIntentOption` (:403), `PendingTelegramTextClarification` (:411) | → `contracts`（generic NL-clarify state） | P1 |
| `TelegramCommandProcessor` (:769) — allowlist fail-closed, command/callback/view/deleter registries, pending-text-clarification state, `/start /help /ping /status /tools` built-ins, unknown-text fallthrough, `_extract_command_name/_remainder` (:4368/:4378) | → split: generic half becomes `telegram_core.processor.CoreCommandProcessor`; price half becomes `PriceCommandProcessor(CoreCommandProcessor)` staying in bot.py | P2 |
| `run_telegram_polling` (:2843), `PollHeartbeat` (:2682), `_heartbeat_beacon` (:2716), `_is_conflict_error` (:2749), `_drain_pending_updates` (:2755), `start_poll_watchdog` (:2794) | → `telegram_core.polling`（含 fail-closed 空 allowlist 啟動守衛） | P3 |
| `handle_telegram_message` (:3540), `handle_telegram_callback_query` (:3182), `_send_text_reply_plan` (:3701), `_guess_current_page` (:3527), `_list_view_renderer` (:3153), `_list_item_deleter` (:3168) | → `polling`; generic envelope + `pg:`/`del:`/`close:` list-view routes + registry dispatch. Domain branches extracted as **registered callback handlers** (see next rows) | P3 |
| `_handle_condition_callback` (:2983) — watch condition picker | stays `price_monitor_bot`, re-registered as a `cond:` callback handler | P3 |
| `_handle_sns_bulk_update_callback` (:3060), `PendingTelegramSnsBulkUpdate` (:440) | **moves to aka `sns_commands.py`** as a registered `snsbulk:` handler — SNS 是 aka 領域，本來就放錯房子（cross-link NL refactor doc） | P3 |
| `_handle_photo_message` (:3800) + photo clarification pipeline (:355-401, :461-604) + `PhotoLookupReply` (:372) | stays `price_monitor_bot` (TCG-flavoured); polling loop exposes a `photo_message_handler` hook | P3 |
| Query dataclasses `TelegramLookupQuery/PhotoQuery/ReputationQuery/ResearchQuery/ReputationDelivery` (:264-301), `PendingTelegramPriceFeedback` (:426) | stay `price_monitor_bot` | — |
| `parse_watch_command` (:2305), `parse_set_price_command` (:2345), `parse_lookup_command` (:2361), `parse_reputation_snapshot_command` (:2409), board/lookup/photo renderers (:2416-2662), `build_processing_ack` (:2662), price command sets (:78-83) | stay `price_monitor_bot` | — |
| `commands.py`, `formatters.py`, `watch_monitor.py` | stay `price_monitor_bot`（price 領域） | — |
| `natural_language.py`（29-line shim → `telegram_nl`） | stays; precedent for the P1 compat shims | — |

## 4. Implementation phases

單一斷點（★ CHECKPOINT）放在 P2 之後、P3（動到 live polling 流程，風險最高）
之前。每個 phase 都必須讓 **兩個 repo 的測試套件全綠** 並且 live bot 可用後
才算完成；rollback 一律是 `git revert` 該 phase 的 commit（P0-P2 都保留舊
import 路徑，revert 不牽連他人）。

### Phase 0 — kill the monkey-patch in place（不新增套件，先拆最痛的耦合）

Changes:

1. `price_monitor_bot/bot.py` — `run_telegram_polling(..., processor_factory:
   Callable[..., TelegramCommandProcessor] | None = None)`；body 改用
   `(processor_factory or TelegramCommandProcessor)(...)`（:2895）。
2. `openclaw_adapter/telegram_bot.py:1912` — 刪除
   `_price_bot_module.TelegramCommandProcessor = …`，改為
   `processor_factory=lambda **kw: TelegramCommandProcessor(settings=settings,
   workflow_editor=_wf_editor, **kw)` 傳入 `_base_run_telegram_polling`。
   同時刪除 `telegram_bot.py:17` 的 `import price_monitor_bot.bot as
   _price_bot_module`。

Tests (stage gate):

- New in price repo: `run_telegram_polling` uses the injected factory（fake
  client + factory 記錄呼叫、收到與原本相同的 kwargs；polling loop 以
  KeyboardInterrupt 快速退出）。
- New in aka: 建構 poller 佈線後 assert `price_monitor_bot.bot.
  TelegramCommandProcessor` **is** 原 class（未被改掉）。
- Grep gate（加入驗收清單，之後每 phase 重跑）:
  `grep -rn "_price_bot_module" src/` → 0 hits。
- Both suites green.

Acceptance:

1. Monkey-patch 及其 module import 完全消失。
2. aka 的 processor 客製（help text、YouTube like-song、workflow editor 文字
   捕捉）行為不變 — 由既有 aka 測試 + live smoke（§5）證明。

Estimated diff: ~40 lines. 風險最低、收益最大，獨立可回收（就算後面 phase
全部不做，這步也值得）。

**P0 單獨一輪上線（已與主上確認 2026-07-02）**：P0 自成一輪 commit → §A 摘要
→ push → 重啟龍蝦 → live smoke 全過，之後才開 P1。

### Phase 1 — bootstrap `telegram_core`, move pure leaves

Changes:

1. Create sibling repo `telegram_core`（layout §2；pyproject `name =
   "telegram-core"`, `dependencies = []`, `requires-python = ">=3.12"`，比照
   `telegram_nl/pyproject.toml`）。
2. **Verbatim moves**（`git diff --no-index` 驗證逐字一致）:
   `list_view.py`、`logging_utils.py`、`TelegramBotClient` +
   `TelegramFileAttachment` + `_encode_multipart_body` +
   `send_telegram_test_message` → `transport.py`、`RegisteredCommand` +
   `TelegramTextReplyPlan` + `TelegramTextIntentOption` +
   `PendingTelegramTextClarification` → `contracts.py`。
3. `price_monitor_bot` 端保留 **compat shims**（比照它自己的
   `natural_language.py` shim 前例）：`bot.py`／`list_view.py`／
   `logging_utils.py` 以 `from telegram_core.… import *`-style 具名 re-export
   維持所有既有 import 路徑可用 → price repo 3,748 行的
   `tests/test_telegram_bot.py` **不改一行也要全綠**。
4. Packaging: `pip install -e ../telegram_core`（aka `.venv`；price 測試若用
   相同 venv 即涵蓋，若有獨立 venv 也要裝）；兩個 pyproject 的 dependencies
   加 `"telegram-core"`。
5. aka 端把 §1.2 表中所有 **infrastructure** import 改指向 `telegram_core`
   （`list_view` 6 個模組、`TelegramBotClient` 6 處、`TelegramTextReplyPlan`）。
   price-domain import 不動。

Tests (stage gate):

- telegram_core 新 tests：transport 的 payload 組裝（send_message 截斷 4096、
  reply_markup 傳遞、multipart 編碼）、`build_list_view` 分頁／edit-mode／
  callback_data 格式（從 price repo test_telegram_bot.py 搬對應案例）。
- price repo suite 全綠（**未修改**，證明 shims 完整）。
- aka suite 全綠。
- Import-direction gate：`grep -rn "price_monitor_bot\|openclaw_adapter"
  telegram_core/src/` → 0 hits。

Acceptance:

1. `telegram_core` 可獨立 `pytest` 全綠、零依賴安裝。
2. aka 內除 price-domain 之外不再 import `price_monitor_bot.list_view` /
   `TelegramBotClient` / `TelegramTextReplyPlan`（grep 證明）。
3. Live smoke（§5）通過 — 特別是任一 list view（`/snslist`）翻頁＋刪除鈕。

### Phase 2 — split the processor

Changes:

1. `telegram_core/processor.py` 新增 `CoreCommandProcessor`：allowlist
   fail-closed、四個 registry、pending-text-clarification 狀態機、
   `/start /help /ping /status /tools` 與 unknown-text fallthrough、
   `build_reply_plan` 的 **generic 骨架**（command 解析 → 內建 → registry →
   NL clarify → unknown-text hook），以及可覆寫的 `_help_text()` 等 hook。
2. `price_monitor_bot.bot.TelegramCommandProcessor` 改為繼承
   `CoreCommandProcessor`，只保留 price/sns built-ins（`PRICE_LOOKUP/TREND/
   PHOTO_SCAN/REPUTATION/WATCH…` 分支）、photo/price-feedback/sns-bulk pending
   狀態與其 renderers。既名 `TelegramCommandProcessor` 不改名（少動 3,748 行
   測試；改名留給 P4 評估）。
3. aka 的 subclass（`telegram_bot.py:319`）父類不變（仍繼承 price 的
   processor — aka bot 真的有 price 功能，這是合法的內容依賴）。

Tests (stage gate):

- telegram_core 新 tests：`CoreCommandProcessor` 單測 — 空 allowlist 拒答、
  未知指令 fallthrough、registry 分派、pending-text-clarification 過期。
- **Characterization 先行**：搬移前先在 price repo 為 `build_reply_plan` 的
  分支順序補齊特徵測試（built-in 優先序、`/help` 覆寫、unknown text）——
  搬移後必須不改測試而全綠。
- Both suites + telegram_core suite green；grep gates 重跑。

Acceptance:

1. 職責分界清楚：`CoreCommandProcessor` 內 **零** price/sns 字彙（no
   `lookup`/`watch`/`sns` identifiers — reviewed by grep + eyeball）。
2. 分支優先順序與現狀完全一致（characterization 測試證明）。
3. Live smoke 通過。

### ★ CHECKPOINT — 主上驗收（唯一斷點）

Deliverables to sign off before P3:

1. P0-P2 diff 摘要 + 兩 repo 測試報告 + telegram_core 測試報告。
2. Live smoke checklist（§5）逐項結果 — 用「重啟龍蝦」重啟後在真 Telegram 驗。
3. P3 的 hook 介面草案（`photo_message_handler`、callback route 註冊表、
   `processor` 參數簽名）— 動 live polling 前先簽核介面。
4. 決定 `snsbulk:` handler 搬到 aka 的落點（`sns_commands.py`）與
   `TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md` 的分工邊界。

不通過則停在 P2：此時 monkey-patch 已消失、純基礎層已共享，系統穩定可長住。

### Phase 3 — move polling / message / callback flow

Changes:

1. `telegram_core/polling.py`：`run_telegram_polling`（收 `processor`
   實例或 factory；保留空-allowlist 啟動守衛、drain、heartbeat、watchdog、
   409 backoff）、`handle_telegram_message`（text envelope → processor；photo
   → `photo_message_handler` hook；無 hook 則忽略 photo）、
   `handle_telegram_callback_query`（`pg:`/`del:`/`close:` list-view 內建 +
   callback registry 分派；`cond:`/`snsbulk:` 等域分支全部改走 registry）。
2. `price_monitor_bot`：`cond:` handler、photo pipeline 以 hook/registry 形式
   註冊；`bot.py` 的 `run_telegram_polling` 變成 thin wrapper（組 price 預設
   renderers 後轉呼叫 core）— 舊簽名維持，price 測試不改。
3. aka：`telegram_bot.py` 改呼叫 `telegram_core.polling.run_telegram_polling`
   （直接傳 processor 實例，factory 中繼站功成身退）；
   `_handle_sns_bulk_update_callback` + `PendingTelegramSnsBulkUpdate` 搬進
   `openclaw_adapter/sns_commands.py` 並註冊 `snsbulk:` handler。

Tests (stage gate):

- telegram_core：polling loop 單測（fake client：update 分流、offset 前進、
  callback 分派、409 backoff、heartbeat touch、drain）— 從 price repo 搬
  對應案例改造。
- price repo：`cond:` handler 經 registry 觸發的特徵測試。
- aka：`snsbulk:` 流程搬家後的既有測試遷移 + 全綠。
- Grep gates + both suites + core suite。

Acceptance:

1. `telegram_core.polling` 內零域分支（無 `cond:`/`snsbulk:`/photo 字彙，
   photo 只剩 hook 名）。
2. live 驗證（§5 全項）：翻頁、刪除、watch condition picker、SNS bulk 確認、
   photo scan、NL 澄清按鈕全部走新分派路徑後行為不變。
3. 409 防護不退化：重啟仍走「重啟龍蝦」，啟動後 lsof 驗證單一 poller。

### Phase 4 — cleanup

Changes:

1. aka：刪 `openclaw_adapter/commands.py`、`openclaw_adapter/formatters.py`
   wrapper（呼叫端直接 import `price_monitor_bot.commands/formatters`；先
   grep 內部使用點逐一改）。
2. price repo：`tests/test_telegram_bot.py` 按歸屬拆遷（core 部分 →
   telegram_core/tests；price 部分留下），然後**評估**移除 P1 shims —— 只在
   兩 repo 已無使用者時移除；有殘留就保留 shims 並登記到期日。
3. Docs truth 更新（§7）+ 本文件 Status 改 Current（或 fold 進 SYSTEM_MAP 後
   archive）。

Acceptance:

1. `grep -rn "from price_monitor_bot" aka/src` 只剩 price 領域 import
   （commands/formatters/watch_monitor/bot 的 price renderers）。
2. 兩 repo + telegram_core 三套測試全綠；docs checkers 全 PASS。

## 5. Test & verification strategy

### Deterministic（每 phase 必跑）

```text
aka_no_claw:      .venv/bin/python -m pytest -q          # 既有 2,189+ 項
price_monitor_bot: 其 repo root 以其 venv 跑 pytest -q    # 含 3,748 行 bot 測試
telegram_core:     其 repo root pytest -q                 # P1 起
grep gates:        (a) aka src 無 _price_bot_module
                   (b) telegram_core src 無 price_monitor_bot/openclaw_adapter
                   (c) phase 各自的 import-方向斷言
docs checkers:     scripts/check_docs_*（動到 docs 的 commit）
```

已知既存紅燈：aka `tests/test_telegram_bot.py` 兩個 `/status` 文字測試在
main 上已失敗（與本計劃無關）；驗收以「不新增失敗」為準，並另案修復。

### Live smoke checklist（P0 起每次重啟後；CHECKPOINT 與 P3 完整跑）

重啟只用「重啟龍蝦」(`/restartall`)；**嚴禁**手動 kill+nohup（409 storm，見
CLAUDE.md）。**嚴禁**動到 8781 的手動 command-bridge；橋接改動一律先在 8799
臨時埠驗證。重啟後先驗：

```text
tmux -L openclaw_codex list-panes -a -F "#{session_name} pid=#{pane_pid}"
lsof -nP -p <telegram-pid> | grep ESTABLISHED   # 149.154.x.x:443 恰一條
```

然後真 Telegram 逐項：

| # | 動作 | 驗證面 |
|---|---|---|
| 1 | `/ping`、`/help`、`/status` | core built-ins、aka help 覆寫 |
| 2 | `/trend`、`/lookup <卡名>` | price built-ins 經新分派 |
| 3 | `/snslist` → 翻頁 → ✏️ 編輯 → ❌ 刪除（取消）→ ✖️ 關閉 | list_view + `pg:/del:/close:` callback |
| 4 | `/watch <url>` → condition picker 按鈕 | `cond:` callback（P3 後走 registry） |
| 5 | 貼一張卡照 + 無 caption | photo hook 路徑 |
| 6 | 自然語言一句（模糊 → 澄清按鈕 → 點選） | pending-text-clarification |
| 7 | `/music playbest`、`/quiz` 一題 | aka registry commands + TelegramBotClient 發送 |
| 8 | Web chat 打一句話（8781 bridge） | 橋接不受影響 |

### 推送

跨 repo commit 用 `multi-repo-push.py`；push 前依 §A 協議列 repo／檔案／主旨
待「推」或「ok」。`telegram_core` 新 repo 的建立與遠端掛載也在 §A 摘要中列明。

## 6. Risks & mitigations

| 風險 | 影響 | 緩解 |
|---|---|---|
| 動 live polling（P3）壞掉唯一的 bot | 全家功能停擺 | P3 前有 CHECKPOINT 簽核介面；每步 live smoke；rollback = revert + 重啟龍蝦 |
| 409 storm（重啟不當） | poller 假活、`/new` 全死 | 只用 `/restartall`；lsof 驗單一 ESTABLISHED |
| price repo 3,748 行測試被搬移弄紅 | 大量 churn | P1/P2 用 compat shims + 不改名策略，測試零修改全綠為硬性 gate；拆遷延到 P4 |
| `telegram_core` 偷渡反向 import 形成循環 | 架構倒退 | 零依賴 pyproject + grep gate（進 CI docs-health 同款 workflow 可後補） |
| editable install 順序／stale egg-info | ImportError 假象 | phase 開頭固定 `pip install -e` 三包並 `pip list` 驗證 |
| `snsbulk:` 搬家撞上 NL ownership refactor | 重工 | CHECKPOINT 議程第 4 項先劃界；該 doc 交叉連結本計劃 |
| 兩 repo 版本錯位（aka 新 / price 舊） | 匯入錯誤 | 同一輪 multi-repo push；shims 保證舊路徑過渡期可用 |

## 7. Docs truth updates（本計劃執行時同步）

- `SYSTEM_MAP.md`：架構圖加入 `telegram_core`，修正依賴方向敘述。
- `TASK_ROUTING.md`：「Telegram 基礎（transport/polling/list view）改哪裡」
  → `telegram_core`；price 領域不變。
- `CURRENT_STATE.md`：telegram 子系統列出三包關係。
- `DOCS_INDEX.md` / `DOC_AUDIT.md`：本文件已登記（Planned, telegram）；每
  phase 完成後更新進度註記，全部落地後改 Current 或 fold+archive。
- `TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md`：補交叉連結（infra 搬遷 vs NL
  routing 搬遷的分工）。
- 動 docs 的 push 前跑 [DOC_DRIFT_CHECKLIST.md](DOC_DRIFT_CHECKLIST.md)。

## 8. Out of scope

- `telegram_nl` 內部的 fallback shrink（另案，見其 doc）。
- price 領域邏輯搬動（lookup/watch/photo 留在 price_monitor_bot）。
- Web chat / command-bridge 架構（只驗證不受影響）。
- webhook 模式、async client 等新功能 — 本計劃是純搬遷，**零行為變更**。
