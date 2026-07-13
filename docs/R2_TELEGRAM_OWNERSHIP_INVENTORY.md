# R2.0 — Telegram Command & Callback Ownership Inventory

Last reviewed: 2026-07-13
Status: Current — R2.0 deliverable; update registration-site line numbers as
R2.1–R2.5 move code.
Owner area: telegram

Workstream R2 (issue [#75](https://github.com/jojojen/aka_no_claw/issues/75)),
stage R2.0 of `docs/P1_ENGINEERING_HARDENING_IMPLEMENTATION_PLAN.md` §10.
Records, for every command and callback prefix: owning repository/module,
registration site, handler, DB access, background behavior, formatter, tests,
and compatibility requirements. The uniqueness/precedence rules below are
pinned by `tests/test_registry_precedence.py`.

R2.1 (done): pure re-exports (names telegram_bot.py imported from
price_monitor_bot/telegram_core but never used) moved to
`openclaw_adapter/telegram_compat.py`; telegram_bot re-imports them so legacy
paths keep working. Surface pinned by `tests/test_telegram_compat.py`.

R2.2 slice 1 (done): /research Telegram glue (reply cache/views `rs`,
notifier factory, seller-snapshot lookups, appreciation enricher, yuyutei
resolver, `default_web_research_renderer`) moved to
`openclaw_adapter/research_telegram.py`; token guards to `telegram_env.py`;
local-text-model selector + not-configured messages to `llm_pool_settings.py`.
telegram_bot re-imports everything, so registration sites and legacy import
paths are unchanged.

R2.2 slice 2 (done): photo pipeline glue (composite `build_photo_renderer`,
`default_photo_renderer`, `default_photo_intent_analyzer` + fixed photo menu,
the `imgtr` 顯示原文 callback + its OCR-original cache, plus `default_lookup_renderer`
/ `default_board_loader`) moved to `openclaw_adapter/photo_render.py`.
telegram_bot re-imports them; test monkeypatches retarget the owning module.

R2.2 slice 3 (done): reputation-snapshot delivery (`default_reputation_renderer`,
playwright PDF/preview `render_reputation_snapshot_artifacts`, chromium launch
resolution, `format_reputation_snapshot_result` / `_delivery_text`) moved to
`openclaw_adapter/reputation_render.py`. The `TelegramFileAttachment` /
`TelegramReputation{Query,Delivery}` legacy re-exports now route through
`telegram_compat`. telegram_bot re-imports everything; renderer test
monkeypatches retarget `reputation_render`.

R2.2 slice 4 (done): local-text-model glue (`default_web_fetch_renderer` for
`/fetch`, `_call_local_text_model`, `build_translate_handler` for
`/translateja` `/translatezh`) moved to `openclaw_adapter/local_text.py`.
telegram_bot re-imports the three names; `_select_text_generation_model` stays
re-exported from telegram_bot (still consumed by `toolset`). chat_web keeps
importing `build_translate_handler` via the telegram_bot re-export; the
`_call_local_text_model` translate test retargets `local_text`.

R2.3 (done): media ingestion. The voice/audio download + validation (file_id /
file_size / duration / mime-type limits) + local-whisper transcription logic
moved out of the `TelegramCommandProcessor.handle_audio_message` body into
`openclaw_adapter/media_ingest.py::transcribe_telegram_audio`; the intake-ack
string is now `media_ingest.AUDIO_INTAKE_ACK_TEXT`. The processor's
`handle_audio_message` / `build_audio_intake_ack_text` are now thin delegating
hooks — same (transcript, error) tuple contract, same validation order and
error strings. (Photo ingestion already lives in `photo_render` from R2.2
slice 2; there is no document/file ingestion path.) Existing processor-level
audio tests pass unchanged since they exercise the hooks, not internals.

Layer chain:

```text
telegram_core (generic transport/polling/dispatch, zero domain vocabulary)
  -> price_monitor_bot.TelegramCommandProcessor (price/marketplace domain)
    -> openclaw_adapter.telegram_bot.TelegramCommandProcessor (aka wiring)
```

## 1. Dispatch precedence (the contract every later stage must preserve)

### 1.1 Text messages (`telegram_core/polling.py::handle_telegram_message`)

1. Allowlist gate (`is_allowed_chat`) — unauthorized chats are logged+dropped.
2. Builtin commands `/start /help /ping /status /tools` — dispatched **before**
   the command registry. `_validate_registries`
   (`telegram_core/processor.py:146`) raises at construction if a registration
   collides with a builtin or doesn't start with `/`, so shadowing is
   impossible by construction.
3. Command registry (`RegisteredCommand` rows; `background=True` rows get an
   ack message then run in a worker thread).
4. Pending reply/capture modes (ForceReply flows, editor capture), then
   `unknown_text_handler` (NL routing).

### 1.2 Callback queries (`telegram_core/polling.py::handle_telegram_callback_query`)

`callback_data` is split on the **first** `:` → `(prefix, payload)`. Order:

1. Allowlist gate.
2. `processor.handle_callback_query_async(...)` hook — checked **first**.
   Override chain (each falls through via `super()`):
   - aka: `goal` (threads a goal-bridge execution, edits message when done)
   - price: `wprc`, `fbprc` (send a brand-new ForceReply message — the
     edit-only registry contract can't express this)
   - core: default returns False.
   A registry entry under `goal`/`wprc`/`fbprc` is dead code.
3. `_callback_registry` (the merged prefix→handler dict). A registered prefix
   **wins over the core builtins below**, so registering `pg`/`del`/`close`/
   `popt`/`topt`/`noop` silently breaks generic list views / clarifications.
4. Core builtins:
   - `pg:<list>:<page>:<mode>` — repaginate / read↔edit toggle
   - `del:<list>:<id>` — delete one row, re-render page
   - `close:<list>` — clear keyboard
   - `popt:<N>` / `topt:<N>` — pick photo/text clarification option
   - `noop` — label buttons
   `<list>` resolves through `view_handlers` / `item_deleter_handlers` (the
   "list-kind" namespace, separate from the callback-prefix namespace).

### 1.3 Registry construction & merge precedence

- aka `_build_registries` (`openclaw_adapter/telegram_bot.py:1547`) returns
  `(command_handlers, callback_handlers, view_handlers, item_deleter_handlers)`.
- `run_telegram_polling` (`telegram_bot.py:2454`) then merges, via plain
  `dict.update` (**later wins, silently**):
  - `CatalogPlanner.callback_handlers()` → `cataloguse catalognew catalogno`
  - `WorkflowEditor.callback_handlers()` → `wfe` (+ re-registers `/workflow`)
- price `TelegramCommandProcessor.__init__` (`price_monitor_bot/bot.py:529`)
  supplies defaults and merges `{**defaults, **(external or {})}` — **external
  (aka) kwargs win on key collision**:
  - callbacks: `cond wedit wmkt wback fbpos`
  - views: `wl` · deleters: `wl`
- aka processor `__init__` (`telegram_bot.py:350`) does
  `setdefault("goal", self._handle_goal_callback)` — injected kwargs would win,
  and the async hook intercepts `goal` anyway (see 1.2).

## 2. telegram_core (repo `telegram_core`)

| Surface | Registration site | Notes |
|---|---|---|
| `/start /help /ping /status /tools` | `processor.py` `BUILTIN_COMMANDS` + polling dispatch | help/status text composed from subclass overrides (`_help_text_extra` etc.) |
| callback builtins `pg del close popt topt noop` | `polling.py::handle_telegram_callback_query` | payload shapes frozen (see 1.2); buttons built by `list_view.py` |
| list-view rendering / delete buttons | `list_view.py` | `callback_data` ≤64 bytes (Telegram cap) — row ids must stay short |
| duplicate-update guard, allowlist, heartbeat, watchdog, 409 drain | `polling.py` | generic; no domain vocabulary (Phase-3 invariant) |

DB access: none. Tests: `telegram_core` repo suite + consumers' characterization.
Compatibility: `_validate_registries` error contract; `prefix:payload` split on
first `:`; registry-beats-builtin callback precedence.

## 3. price_monitor_bot (`src/price_monitor_bot/bot.py`)

| Surface | Kind | Handler / site | DB | Notes |
|---|---|---|---|---|
| `cond wedit wmkt wback` | callback defaults | `_cond_callback` etc., `__init__` :529 | MonitorDatabase (`watch_db`) | watch condition/edit/market flows |
| `fbpos` | callback default | `_fbpos_callback` | watch_db + feedback service | positive price feedback |
| `wprc fbprc` | async hook | `handle_callback_query_async` :762 | watch_db, feedback service | ForceReply flows; bypass registry |
| `wl` | view + deleter default | `render_watchlist_view` / `delete_marketplace_watch_by_id` | watch_db | list-kind namespace |
| photo pipeline, pending photo/text clarifications, price feedback capture | message hooks | same class | watch_db | `popt`/`topt` payloads land here |
| price NL routing, intent fast path | `unknown_text_handler` wiring | same class | — | aka passes its own router on top |

Commands: price domain **does not register commands itself** for aka — aka's
`_build_registries` registers price-facing commands (`/price /lookup /watch
/trend /snapshot …`) as thin lambdas over `_BaseTelegramCommandProcessor`
methods (`_handle_lookup`, `_handle_watch`, `render_watchlist_view`, …), using
a base-processor instance built inside `_build_registries`.
Tests: `price_monitor_bot` suite + aka `tests/test_command_registry.py`
(`test_price_monitor_base_command_sets_are_registered`).
Compatibility: kwargs-beat-defaults merge; `wl` list-kind reserved; `wprc`/
`fbprc` `[prefix:id]` ForceReply text markers parsed by reply handlers.

## 4. openclaw_adapter (`src/openclaw_adapter/telegram_bot.py`)

### 4.1 Commands — all registered as data in `_build_registries` (:1678–2041)

All rows are `RegisteredCommand` with `**command_metadata(name)` (usage/router
hints come from `workflow_command.py`'s `_COMMAND_METADATA` — the single source
of truth). "bg" = `background=True` with ack. Handler builders live in the
named module; the registry site is the only wiring point.

| Command (aliases) | Handler builder / module | DB / state | bg | Tests |
|---|---|---|---|---|
| `/quiz`, `/quizlikesong` | `build_quiz_handler` (quiz) | quiz sqlite | bg | test_quiz_command_label, quiz suites |
| `/voice` | `build_voice_handler` | settings-backed voice params | sync | test_command_registry |
| `/generateaudio /saynow` | audio builders | aivis/say | bg | test_saynow_command |
| `/translateja /ja /jp /translatezh /zh` | translate builders | local LLM | bg | telegram/translate tests |
| `/new` | `dynamic_tool_runner.run` | codegen knowledge DB | bg | test_command_registry (disabled path) |
| `/workflow` (conditional, needs runner; re-registered in `run_telegram_polling` with editor) | `build_workflow_handler` (workflow_command) | WorkflowStore JSON | bg | test_workflow_command |
| `/schedulehome` | `build_schedulehome_handler` (late registration :2028, closes over finished registry) | home-schedule store | sync | test_home_schedule |
| `/backupclaw /backup /clawrecover /recoverclaw` | backup/recover builders | filesystem | bg | test_backup_command |
| `/restartall` | `service_restart.trigger_restart_all` | tmux/launchd | sync | manual (production restart) |
| `/stats /scorecard` | `build_scorecard_handler` | quiz sqlite | sync | scorecard tests |
| `/knowledge /kb` | knowledge builders | knowledge sqlite (via inbox when provided) | sync | test_knowledge_command |
| `/source` | `build_source_handler` | source registry | sync | test_source_command, test_source_registry |
| `/lookup /price` | base `_handle_lookup` | monitor sqlite | sync | test_telegram_bot |
| `/trend /trending /hot /heat /liquidity` | base `_handle_liquidity` | monitor sqlite | sync | test_telegram_bot |
| `/snapshot /proof /repcheck /reputation` | base `_handle_reputation_snapshot` | playwright artifacts | bg | reputation tests |
| `/scan /image /photo` | help text (photo caption flow does real work) | — | sync | test_command_registry |
| `/search /web` | base `_handle_web_research` (round-robin engine pool) | — | bg | search tests |
| `/fetch /read` | base `_handle_web_fetch` | — | bg | fetch tests |
| `/music /musicqueue /musiclistall /musiclistbest /musicnowbest /musicmute /musiclouder /musiclower` | music builders (music_command, music_favorites) | FavoritesStore | mixed | test_music_command |
| `/bluetooth /ir` | `build_bluetooth_handler` / `build_ir_handler` | home-control | sync | test_bluetooth_command, test_ir_command |
| `/visionlook` | vision builder | local vision model | bg | vision tests |
| `/research /resaerch` | `build_research_handler` (research pipeline; R3 scope) | knowledge sqlite, caches | bg | test_research_command |
| `/fix` | `build_fix_handler` + `FixPendingApplyCache` | pending-apply cache | bg | test_fix_command |
| `/vpn` | `build_vpn_handler` + `VpnConfigStore` (+`VpnRotationScheduler` when `start_schedulers`) | vpn_rotation.json | sync | test_vpn_command |
| `/watch /watchlist /watches /unwatch /stopwatch /setprice /updatewatch` | base watch methods | monitor sqlite (watch_inbox when provided) | sync | test_telegram_bot |
| `/snsadd /sns_add /snslist /sns_list /snsdelete /sns_delete /snsbuzz /sns_buzz /snsclearfilter` | sns builders | sns sqlite (sns_inbox when provided) | mixed | sns tests |
| `/hunt /opportunity` | `build_hunt_handler` | opportunity sqlite (opportunity_inbox) | sync | hunt tests |

### 4.2 Callback prefixes

| Prefix | Registered at | Handler | Notes |
|---|---|---|---|
| `quiz voice music bt ir` | `_build_registries` :2099 | domain builders | |
| `ragkeep ragdel` | :2099 | `_build_rag_callback_handler` adapters | knowledge_inbox writes |
| `snsdel snsaddok snsfb` | :2099 | sns builders | sns_inbox writes |
| `oppfb` | :2099 | `build_hunt_callback_handler` | opportunity_inbox |
| `rs` | :2099 | research reply cache (`_ResearchReplyCache`, TTL/64-byte token) | |
| `fix` | :2099 | `FixPendingApplyCache` | |
| `imgtr` | :2099 | `_ImageTranslateOriginalCache` (token → OCR原文) | 64-byte cap workaround |
| `wf` | :2099 | `_workflow_list_adapter` (run/schedule/delete/rename/renameid re-dispatch through command registry) | |
| `sh` | :2099 | schedulehome builder | |
| `cataloguse catalognew catalogno` | `run_telegram_polling` :2504 merge | `CatalogPlanner` | silent `update()` |
| `wfe` | :2520 merge | `WorkflowEditor._handle_callback` (sub-actions inside payload) | silent `update()` |
| `goal` | processor `__init__` `setdefault` + async hook | `_handle_goal_callback` / async override | async hook intercepts first |

### 4.3 View / deleter list-kinds (namespace of `pg`/`del`/`close` payloads)

| Kind | View | Deleter | Owner |
|---|---|---|---|
| `km` `kc` | knowledge market/coding | ✓ | aka knowledge |
| `sl` | snslist | ✓ | aka sns |
| `hl` | huntlist | ✓ | aka opportunity |
| `mb` (`MUSIC_BEST_LIST_KIND`) | music best | ✓ | aka music |
| `wl` | watchlist | ✓ | price_monitor_bot default |

### 4.4 Media & background (R2.3/R2.4 scope, listed for ownership only)

- `handle_audio_message` (:385) — voice/audio download → STT → normal dispatch;
  temp-file cleanup; aka-owned.
- Photo captions route through price photo pipeline + aka caption recognizers
  (`imgtr` embedding recognizer; `/scan` caption).
- Background helpers `_start_backup_scheduler`, `_start_title_corpus_rebuilder`,
  `_start_rag_daily_digest`, `_start_home_schedule_scheduler`,
  `_start_watch_monitor` (:2599–2953) — aka-owned; started in
  `run_telegram_polling`; `VpnRotationScheduler` started inside
  `_build_registries` when `start_schedulers=True` (poller only — the bridge
  also builds registries and must not double-rotate).

## 5. Compatibility requirements (must survive R2.1–R2.5 unchanged)

1. `callback_data` wire formats are frozen: `prefix:payload`, first-`:` split,
   64-byte Telegram cap; `pg`/`del`/`close` payload shapes; `[wprc:id]` /
   `[fbprc:id]` ForceReply markers.
2. Registered commands must never collide with `BUILTIN_COMMANDS` (construction
   raises) and must keep `command_metadata` as the single metadata source.
3. Callback prefix namespace: one flat namespace across aka registry, runtime
   merges, price defaults, async-hook prefixes and core builtins — collisions
   are either construction errors (`:` / empty) or **silent** shadowing
   (everything else). `tests/test_registry_precedence.py` pins disjointness.
4. List-kind namespace (`view_handlers`/`item_deleter_handlers` keys) is
   separate from the prefix namespace; `wl` is reserved by price_monitor_bot.
5. External-kwargs-beat-defaults merge in the price layer; later-wins
   `dict.update` in `run_telegram_polling` (any future merge must stay
   collision-free — tested).
6. `_build_registries` stays data-only: adding a command never edits
   price_monitor_bot/bot.py or telegram_core.
