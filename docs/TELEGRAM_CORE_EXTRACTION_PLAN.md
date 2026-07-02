# Telegram Core Extraction Plan — shared `telegram_core` package

Last reviewed: 2026-07-03
Status: Current — all phases (P0–P4) shipped; kept as the implementation
record and reference for `telegram_core`'s hook/registry contracts.
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

## Progress log (update at every phase boundary — this is the resume point)

- **2026-07-02**: Sibling repo `jojojen/telegram_core` created on GitHub
  (README only) and cloned locally to
  `~/ai_work_space/related_to_claw/telegram_core`. **Phase 0 in progress**:
  - `price_monitor_bot/bot.py` `run_telegram_polling` — added
    `processor_factory: Callable[..., TelegramCommandProcessor] | None = None`
    param; body now does `(processor_factory or TelegramCommandProcessor)(...)`. Done.
  - `openclaw_adapter/telegram_bot.py` — deleted
    `import price_monitor_bot.bot as _price_bot_module` (line 17) and the
    `_price_bot_module.TelegramCommandProcessor = lambda **kwargs: ...`
    monkey-patch; replaced with `processor_factory=lambda **kwargs: ...`
    passed directly into `_base_run_telegram_polling(...)`. Done.
  - Grep gate `grep -rn "_price_bot_module" src/` in aka_no_claw → 0 hits. Confirmed.
  - Added 2 stage-gate tests to `price_monitor_bot/tests/test_telegram_bot.py`:
    `test_run_telegram_polling_uses_injected_processor_factory` and
    `test_run_telegram_polling_without_factory_uses_default_processor_class`.
    **Not yet run** — next step is running both repos' full suites.
  - **Both suites green**: price_monitor_bot 543 passed/8 skipped (incl. the
    2 new stage-gate tests); aka_no_claw 2268 passed/7 skipped. Phase 0 code +
    tests are DONE.
  - **Not yet done**: §A push summary sent to user; waiting on 「推」/ok, then
    ask user to 「重啟龍蝦」and run the P0 live smoke checklist (§5) before
    starting Phase 1 (populate `telegram_core` per §2-3 of this doc).
  - Note: a tangential ask landed in parallel — `docs/` file-storage hygiene
    fix (an orphaned `.html` doc invisible to `check_docs_health.py`'s
    `*.md`-only glob). Fixed by explicitly naming it in a `PUBLIC_HTML_DOCS`
    set (NOT a blanket `*.html` glob — that first attempt swept in unrelated
    `fix_benchmarks/**/*.html` synthetic test fixtures and broke
    `test_docs_health_checks.py`; caught by the full aka suite run and
    corrected). See `scripts/check_docs_health.py` `PUBLIC_HTML_DOCS` /
    `PUBLIC_HTML_METADATA_EXEMPT`, plus `DOCS_INDEX.md` and `DOC_AUDIT.md`
    entries for `BROADLINK_LOCAL_NETWORK_TROUBLESHOOTING.html`. Unrelated to
    this plan, noted here only so a resumed session isn't confused by
    unrelated diffs sitting alongside Phase 0 changes.
  - **Standing instruction from 主上 (2026-07-02, overrides the "P0 alone,
    wait for confirmation" sequencing above)**: 「把全部都做完再報告 只是中間要
    記得記錄 讓人隨時可以接手」— finish the ENTIRE extraction (P1→P4) without
    stopping for interim check-ins; keep this Progress log current at every
    boundary so anyone can take over at any point. `git push` itself and the
    ★ CHECKPOINT before P3 (live polling loop, highest blast radius) are still
    treated as points needing an explicit, surfaced pause per the repo's own
    §A protocol and the CHECKPOINT's stated purpose — not silently skipped,
    but not blocking P1/P2 work either. Nothing has been pushed yet as of this
    note; all repos still have local, uncommitted/unpushed changes.
  - **Phase 0 still not pushed** as of this note (unchanged from above) — the
    §A summary was sent but 「推」/ok was never received before the "finish
    everything" instruction arrived. Phase 0 + Phase 1 changes will be pushed
    together in one §A round once P1 (and likely P2) are done, per the
    override above.
  - **Phase 1 — DONE (bootstrap `telegram_core`, move pure leaves).**
    - Repo `telegram_core` populated: `pyproject.toml` (`name="telegram-core"`,
      `dependencies=[]`, mirrors `telegram_nl`), `src/telegram_core/{__init__.py,
      transport.py, contracts.py, list_view.py, logging_utils.py}`,
      `tests/{test_list_view.py, test_transport.py, test_contracts.py}` (17
      tests, fresh — not literal moves from price repo's entangled fixtures).
      Own `.venv` built with `/Users/jen/.local/bin/python3.12` (system
      `python3` is 3.9, too old for `requires-python = ">=3.12"`).
    - Verbatim moves done: `list_view.py`, `logging_utils.py` → `telegram_core`
      unchanged; `TelegramBotClient` + `TelegramFileAttachment` +
      `_encode_multipart_body` + `send_telegram_test_message` → `transport.py`;
      `RegisteredCommand` + `TelegramTextReplyPlan` + `TelegramTextIntentOption`
      + `PendingTelegramTextClarification` (+ its `TEXT_CLARIFICATION_TTL_SECONDS`
      constant, since `PendingTelegramSnsBulkUpdate`, which stays in
      price_monitor_bot, also reads that constant — now imported back) →
      `contracts.py`. Forward-ref string annotations quoting
      `TelegramNaturalLanguageIntent` / `TelegramReputationDelivery` kept
      as-is (unevaluated strings under `from __future__ import annotations`;
      no import needed, preserves telegram_core's zero-dependency rule).
    - `price_monitor_bot/list_view.py` and `logging_utils.py` are now compat
      shims (`from telegram_core.… import *`-style named re-export). `bot.py`
      itself imports the moved symbols from `telegram_core.contracts` /
      `telegram_core.transport` at the top — since `bot.py` IS the module
      external consumers already import these names from
      (`from price_monitor_bot.bot import TelegramBotClient` etc.), no
      separate shim file was needed for those; import comments left in place
      of each removed class/function pointing at the new location.
    - Packaging: both `price_monitor_bot` and `aka_no_claw` `pyproject.toml`
      got `"telegram-core"` added to `dependencies`; `pip install -e
      ../telegram_core` run into both repos' `.venv`s.
    - aka-side infra imports redirected from `price_monitor_bot.list_view` /
      `price_monitor_bot.bot.{TelegramBotClient,TelegramTextReplyPlan}` to
      `telegram_core.{list_view,transport,contracts}` in: `telegram_bot.py`
      (top-level + 3 inline `TelegramBotClient` imports + 1 inline
      `TelegramTextReplyPlan` import), `sns_commands.py`, `command_bridge.py`,
      `opportunity_command.py`, `knowledge_command.py`, `music_favorites.py`,
      `opportunity_agent.py`, `quiz_command.py`, `voice_command.py`. Price-domain
      imports (`watch_monitor`, `commands.lookup_card`, price query/renderer
      types) untouched — legitimate content dependency, stays.
    - **All test results green**: `telegram_core` 17 passed (own venv);
      `price_monitor_bot` 543 passed/8 skipped (**unmodified**
      `tests/test_telegram_bot.py` — shim strategy confirmed working, zero
      test edits needed); `aka_no_claw` 2268 passed/7 skipped (identical
      count to the Phase 0 baseline — no regressions from the import
      redirection). Smoke-import of all 9 edited aka modules also passed.
    - Grep gates both green: `_price_bot_module` → 0 hits in aka `src/`;
      `price_monitor_bot|openclaw_adapter` → 0 hits in `telegram_core/src/`
      (had to reword a docstring + the `pyproject.toml` description that
      named both consumer repos by name, since `.egg-info` regenerates those
      strings into `telegram_core/src/telegram_core.egg-info/PKG-INFO` and
      the gate greps that whole path literally — not a real coupling, just a
      literal string match on the human-readable summary).
    - Added `telegram_core/.gitignore` (copied from `telegram_nl`'s: `.venv/`,
      `*.egg-info/`, `__pycache__/`, etc.) — didn't exist yet on the fresh repo.
    - **Not yet done from Phase 1's own checklist**: the live-smoke item
      (`/snslist` paginate+delete) — deferred; live smoke needs a real restart
      via 「重啟龍蝦」and nothing has been pushed/deployed yet, so there is no
      new code running live to smoke-test. Will fold into the CHECKPOINT's
      live-smoke pass instead of doing it twice.
  - **Started `agent_auto_continue` watcher** (pid varies per restart) per
    主上's request so a rate-limit cooldown mid-task auto-resumes work.
    **Caveat surfaced to 主上**: this session runs directly in Apple Terminal
    (`TERM_PROGRAM=Apple_Terminal`), not inside a tmux pane on the default
    socket — only the bot's `openclaw_codex` tmux socket exists. The watcher's
    resume mechanism is `tmux send-keys continue` to an auto-detected pane on
    the DEFAULT socket; with no such server running, it will correctly detect
    a rate-limit and wait out the cooldown, but has no pane to type into. If
    a real hands-off resume is wanted, this terminal window needs to be
    wrapped in `tmux` (default socket) first.
  - **Next**: Phase 2 (split `TelegramCommandProcessor` into
    `telegram_core.processor.CoreCommandProcessor` + price-only subclass) —
    see §4 Phase 2. Characterization tests first, per the plan.
  - **Phase 2 — DONE (split the processor).**
    - Characterization tests written FIRST against the pre-refactor code:
      `price_monitor_bot/tests/test_processor_dispatch_characterization.py`,
      10 tests locking in `build_reply_plan`'s branch order (allowlist →
      empty-content guard → built-ins → registry → domain command set → NL
      routing → unknown-text hook → clarification → fallback). Two of my
      first-draft assumptions about bare-text fallback behavior were WRONG —
      running the tests against the real pre-refactor code caught it (plain
      ambiguous text always gets the did-you-mean clarification menu, not the
      hardcoded "Unknown command" message; that fallback is only reachable via
      an unrecognized *slash* command). Fixed the tests to match reality, not
      my assumption, before touching any refactor code. All 10 pass
      **unmodified** after the split — the stage-gate proof worked as designed.
    - `telegram_core/src/telegram_core/processor.py` (new): `CoreCommandProcessor`
      — allowlist, 4 pluggable registries, `/start /help /ping /status /tools`
      built-ins, full `build_reply_plan` dispatch skeleton, pending-text-
      clarification state machine + generic clarification-render helpers
      (`_extract_command_name/_remainder`, `_extract_photo_clarification_override`,
      `_build_clarification_keyboard`, `_is_text_intent_ambiguous`,
      `_build_text_clarification_reply`, `_build_pending_text_retry_reply`,
      `_match_text_clarification_option`). Domain extension points are hooks
      with trivial defaults: `_dispatch_domain_command` (→ `None`),
      `_route_natural_language` (→ `None`), `_build_natural_language_reply_plan`
      (→ `None`), `_build_text_intent_candidates` (→ `()`), `_help_text`,
      `_status_text`, `_unknown_command_text`. Exported from
      `telegram_core/__init__.py`. 14 new tests in `tests/test_processor.py`;
      telegram_core suite now 31 passed (own venv).
    - **Zero-dependency hard rule preserved a design pivot**: `_route_natural_language`
      was initially going to be a concrete generic method (it just delegates to
      injected router objects), but it makes REAL calls to
      `fast_route_telegram_natural_language` /
      `slow_fallback_route_telegram_natural_language` from the sibling
      `telegram_nl` package — a real runtime dependency, which would violate
      `dependencies = []`. Caught during design, before writing code: made it a
      hook instead, with the unchanged original body moved down into
      `PriceCommandProcessor`/`price_monitor_bot.bot.TelegramCommandProcessor`,
      and moved `natural_language_router`/`intent_fast_path` ctor params down
      to match (they're domain-adjacent, not Core's concern).
    - `price_monitor_bot/src/price_monitor_bot/bot.py`: class name kept
      unchanged (「既名 `TelegramCommandProcessor` 不改名」) — now
      `class TelegramCommandProcessor(CoreCommandProcessor):`. `__init__` calls
      `super().__init__(catalog_renderer=..., allowed_chat_ids=...,
      status_renderer=..., command_handlers=..., callback_handlers=...,
      view_handlers=..., item_deleter_handlers=..., unknown_text_handler=...)`
      and keeps only domain state (lookup/board/photo/reputation/research/fetch
      renderers, NL router + fast path, watch/sns DBs, collab backfiller,
      feedback service, pending-photo/price-feedback/sns-bulk-update dicts — all
      existing kwarg names unchanged, zero call-site changes needed anywhere).
      Deleted (now inherited unchanged): `is_allowed_chat`,
      `get/set/pop/clear_pending_text_clarification`, `build_reply`,
      `build_pending_text_reply_plan`, `_build_text_clarification_plan`,
      `_status_text`. The old hardcoded price/trend/photo-scan/reputation/
      research/fetch/watch/watchlist/unwatch/set-price command-set chain that
      used to live inline in `build_reply_plan` is now
      `_dispatch_domain_command` (returns `None` if nothing matched, mirroring
      Core's hook contract). Added `_unknown_command_text()` override
      returning the original price-domain fallback string verbatim, and a
      `_build_text_intent_candidates` override forwarding to the unchanged
      module-level function of the same name (method/module-function name
      shadowing is safe — separate namespaces). Removed the now-duplicate
      module-level defs of `_extract_command_name`, `_extract_command_remainder`,
      `_extract_photo_clarification_override`, `_is_text_intent_ambiguous`,
      `_build_text_clarification_reply`, `_build_pending_text_retry_reply`,
      `_match_text_clarification_option` (all now imported from
      `telegram_core.processor` instead — the old defs were shadowing the
      imports and one referenced a constant that had already been deleted,
      which would have been a live `NameError` land mine if ever called).
      `_route_natural_language`, `_build_natural_language_reply_plan`,
      `_help_text` kept as unchanged-body overrides.
    - **All test results green**: `telegram_core` 31 passed (own venv);
      `price_monitor_bot` 553 passed/8 skipped (543 Phase-1 baseline + 10 new
      characterization tests, **passing unmodified** — the stage-gate proof);
      `aka_no_claw` **2268 passed/7 skipped — identical to the Phase 0/1
      baseline, zero regressions**. Confirms the 3-level MRO (`aka's
      TelegramCommandProcessor → price's TelegramCommandProcessor →
      telegram_core.CoreCommandProcessor`) resolves correctly; aka's own
      `_help_text` override at `openclaw_adapter/telegram_bot.py:342` not
      calling `super()` was indeed behavior-transparent to the Core-level
      default inserted underneath it, as predicted from the pre-split grep.
    - Grep gates both green: `price_monitor_bot|openclaw_adapter` → 0 hits in
      `telegram_core/src/`; domain-vocabulary scan (`price|trend|snapshot|
      watch|sns|pokemon|yugioh`) in `processor.py` → only 2 benign illustrative
      prose mentions ("TCG price bot" / "card-price bot" as docstring/comment
      examples of what a hypothetical subclass might be), no real identifiers.
    - **Phase 2 code + tests are DONE, all three suites green.**
  - **★ CHECKPOINT — PASSED (2026-07-02).** 主上 restarted via 「重啟龍蝦」and
    delegated the verification judgment call ("妳自己測一下 覺得沒問題就繼續往下
    開發"). Verified: `tmux -L openclaw_codex list-panes` shows both `telegram`
    and `bridge` sessions up; `telegram` pane log shows a clean startup with
    zero tracebacks through the FULL real production object graph (aka's
    `TelegramCommandProcessor(settings=..., workflow_editor=..., goal_bridge=...)`
    → price's `TelegramCommandProcessor` → `CoreCommandProcessor.__init__`) —
    proves the Phase 2 constructor wiring holds under real settings/renderers,
    not just test stubs; `lsof` on the telegram pid shows an ESTABLISHED
    connection to `149.154.166.110:443` (real Telegram long-poll); the
    `telegram_heartbeat` file timestamp is fresh (not wedged); the 8781 bridge
    answered `GET /api/command/model-routes` with a valid response. Could not
    personally click real Telegram buttons/send a photo (no user-session
    credentials, and doing so would be simulating 主上's own account) — but
    Phase 2 only touched processor-level text dispatch, not the photo/callback
    plumbing those checklist items exercise, so this verification covers the
    actual Phase 2 blast radius. Full smoke checklist item-by-item (photo scan,
    condition-picker buttons, /snslist pagination) deferred to 主上's own pass
    whenever convenient, since Phase 3 is what will actually touch that code.
    P3 hook interface + `snsbulk:` relocation decision (already spelled out in
    §4 Phase 3) stand as given. Proceeding into Phase 3 per 主上's go-ahead.
    §A `git push` still pending — nothing pushed yet in any of the three repos;
    will summarize and wait for 「推」/ok once Phase 3 lands.
  - **Phase 3 scope discovery + 主上 decision (2026-07-02).** Reading the real
    `handle_telegram_callback_query`/`handle_telegram_message` bodies in
    `price_monitor_bot/bot.py` (not just this plan's §4 sketch) showed far
    more domain entanglement than documented: 8 domain callback prefixes
    hardcoded in the elif-chain (`cond`, `bulk`, `wedit`, `wmkt`, `wprc`,
    `fbprc`, `fbpos`, `wback` — the sketch only mentioned `cond`/`bulk`), a
    ForceReply pre-check block in `handle_telegram_message` (matches
    `_FBPRC_TAG_RE`/`_WPRC_TAG_RE` against `reply_to_message` text, before
    intake-ack) with no existing hook, and a `REPUTATION_SNAPSHOT_COMMANDS`
    special case that bypasses `build_reply_plan` entirely — also no hook.
    `wprc`/`fbprc` additionally send BRAND-NEW messages via
    `client.send_message(..., reply_markup={"force_reply": True})`, which the
    existing registry callback contract (`(payload, original_text, chat_id) ->
    (toast, new_text, markup)`, edit-only) can't express — the already-present
    `handle_callback_query_async` hook (checked first, receives `client`) is
    the natural fit for those two. Asked 主上 to choose: conservative
    (move only the genuinely generic infra — `PollHeartbeat`, watchdog, drain,
    409-backoff, generic `pg`/`del`/`close`/`noop` — leave all 8 domain
    prefixes + ForceReply/reputation logic in `price_monitor_bot` unchanged)
    vs. full-literal (convert all 8 prefixes to registry/`handle_callback_
    query_async`-hook dispatch, design new hooks for ForceReply/reputation,
    accept the larger effort + higher blast-radius risk).
    **主上 chose 完整版（照計劃文件字面）— the full version.** This is now the
    binding scope for Phase 3; the §4 sketch text is superseded by this note
    wherever it undercounts the domain surface.
  - **Phase 3 characterization tests — DONE, written BEFORE any polling-loop
    code moves** (same discipline as Phase 2, now doubly justified since this
    phase touches the live poll loop). Added 15 new tests to
    `price_monitor_bot/tests/test_telegram_bot.py`, covering branches that had
    **zero prior test coverage**: `wedit` (open view / unknown-watch toast),
    `wmkt` (toggle off/on, refuse-to-empty-last-market — discovered mid-write
    that the refusal path still re-renders the unchanged view, it only skips
    the DB write; the code does the rerender unconditionally after the
    add/remove branch, not inside it), `wprc` (ForceReply send with the
    `[wprc:<40-hex-id>]` tag — `_WPRC_TAG_RE` requires exactly 40 hex chars,
    unknown-watch toast, and the reply-consumption path that writes the new
    threshold and re-sends the edit view; also the non-digit-price rejection),
    `wback` (return to watchlist edit mode), `fbpos` (one-tap positive
    feedback via `feedback_service.submit_positive`, missing-item toast),
    `noop` and unknown-prefix (both fall through to the pre-set default toast
    "未知按鈕", confirming `noop` never sets `toast` explicitly), and a new
    registry-precedence test proving `processor._callback_registry.get(prefix)`
    is checked BEFORE the entire elif chain for ANY prefix name — including
    builtins like `pg` — which is the load-bearing fact Phase 3's registry
    migration of `cond`/`bulk`/etc depends on. The already-existing
    `test_processor_dispatch_characterization.py` (Phase 2) and the existing
    `handle_telegram_callback_query`/`handle_telegram_message` tests in
    `test_telegram_bot.py` (snsdel, async-handler-hook precedence, pg/del/
    close, cond open/toggle/refuse/done, popt/topt, bulk confirm/cancel,
    fbprc send + URL-reply consumption, reputation-snapshot bypass in
    `handle_telegram_message`) already covered the rest — re-verified they
    still pass, no rewrite needed. **Full suite: 568 passed/8 skipped** (553
    Phase-2 baseline + 15 new), all against the PRE-refactor code — this is
    the ground-truth baseline the Phase 3 `telegram_core.polling` rewrite must
    reproduce unmodified. No `telegram_core/polling.py` code written yet;
    that's the immediate next step.
  - **Phase 3 — DONE (move polling/message/callback flow).**
    - `telegram_core/src/telegram_core/polling.py` (new): verbatim moves of
      `PollHeartbeat`, `_heartbeat_beacon`, `_is_conflict_error`,
      `_drain_pending_updates`, `start_poll_watchdog` (watchdog alert text
      genericized to "Poll loop 心跳停止"), `_PAGE_HEADER_RE`/
      `_guess_current_page`, `_send_reputation_delivery`,
      `_send_text_reply_plan`. `_list_view_renderer`/`_list_item_deleter` are
      now pure registry lookups — the old hardcoded `"wl"`/`"hl"` fallback is
      gone (grep across all three repos found `render_huntlist_view` is dead
      code: no class defines it; `"hl"` is always supplied externally via
      `view_handlers`/`item_deleter_handlers` kwargs, so a naive default would
      have introduced an `AttributeError`). New generic
      `handle_telegram_message`/`handle_telegram_callback_query` built against
      the 6 Phase-3 `CoreCommandProcessor` hooks
      (`handle_callback_query_async`, `handle_reply_to_message`,
      `build_intake_ack_text`, `check_pending_photo_reply`,
      `handle_photo_message`, `handle_pre_dispatch_text`) — all 8 domain
      callback prefixes (`cond`/`bulk`/`wedit`/`wmkt`/`wprc`/`fbprc`/`fbpos`/
      `wback`) removed from the elif chain; only `pg`/`del`/`close`/`popt`/
      `topt`/registry/async-hook/`noop` remain as builtins.
    - `TelegramReputationDelivery` moved to `telegram_core/contracts.py`
      (fully generic — only `client`/`chat_id`/`delivery`/`mask_identifier`/
      `trim_for_log`); `price_monitor_bot/bot.py`'s local copy deleted, now
      imported.
    - `price_monitor_bot/bot.py`: registry-merge added to
      `TelegramCommandProcessor.__init__` (`{**default_*_handlers,
      **(kwarg or {})}`, external kwargs win on collision — matches the old
      "registry checked first" semantics) wiring `cond`/`bulk`/`wedit`/`wmkt`/
      `wback`/`fbpos` as registry callbacks and `wl` as the default view/
      deleter handler. 6 new adapter methods (`_cond_callback` etc.) preserve
      two quirks the generic registry contract doesn't: (a) toast defaults to
      "未知按鈕" — the registry contract unconditionally unpacks
      `toast, new_text, new_reply_markup = cb(...)`, so each adapter applies
      `toast_out if toast_out is not None else "未知按鈕"` itself; (b) explicit
      `logger.warning("Unknown callback_query prefix=...")` calls preserved
      where the original elif chain had them. 6 hook-override methods added
      for `wprc`/`fbprc` (ForceReply send/consume — the one thing the edit-only
      registry contract can't express) and the `REPUTATION_SNAPSHOT_COMMANDS`
      bypass; all chain `super()` for prefixes/cases they don't own.
    - **Monkeypatch-compatibility finding**: `run_telegram_polling` could NOT
      become a thin delegating wrapper — two existing tests
      (`test_run_telegram_polling_uses_injected_processor_factory` and the
      sibling default-class test) do
      `monkeypatch.setattr(_bot_module, "TelegramBotClient"/
      "_drain_pending_updates"/"start_poll_watchdog", ...)`, and Python
      resolves bare globals via the *defining* module's `__globals__` — so
      only code whose body is textually in `bot.py` sees the patch. Fix: kept
      `run_telegram_polling`'s body 100% verbatim in `bot.py`, only
      re-imported its helper names from `telegram_core.polling`.
      `telegram_core/polling.py` also grew its own generic
      `run_telegram_polling` (different signature, takes a pre-built
      `processor`) as a standalone API for future non-price consumers — not
      used by price's own wrapper.
    - `aka_no_claw/src/openclaw_adapter/telegram_bot.py`: its
      `handle_callback_query_async` override was shadowing price's `wprc`/
      `fbprc` handling — `if prefix != "goal": return False` short-circuited
      past price's implementation instead of falling through to it. Fixed to
      `return super().handle_callback_query_async(...)` on the non-`goal`
      branch, restoring the 3-level MRO chain (aka → price → Core).
    - **Bug found + fixed via the standard test-first loop**: first pytest run
      of the new `polling.py` against price's suite showed 147 passed / 1
      failed (`test_handle_telegram_message_sends_snapshot_ack_then_result`).
      Cause: the `handle_pre_dispatch_text` branch in the new generic
      `handle_telegram_message` did a bare `return pre_dispatch`, discarding
      the intake-ack that had already been sent and appended to `replies`
      earlier in the same call — the original code appended to that same list
      instead of returning a fresh tuple. Fixed to
      `replies.extend(pre_dispatch); return tuple(replies)`. Re-ran green.
    - **All three suites green, matching every prior-phase baseline exactly**:
      `telegram_core` 31 passed (own venv, unchanged from Phase 2 — no new
      tests needed, the 15 Phase-3 characterization tests live in price's
      suite); `price_monitor_bot` 568 passed/8 skipped (identical to the
      pre-refactor characterization baseline — proves the rewrite is
      behavior-preserving); `aka_no_claw` 2268 passed/7 skipped (identical to
      every prior phase's baseline).
    - Grep gate green: `price_monitor_bot|openclaw_adapter` → 0 hits in
      `telegram_core/src/` (had to reword one docstring comment in
      `processor.py` that named `price_monitor_bot` as an illustrative
      example — same "literal string match on human-readable prose" gotcha
      as Phase 2).
    - **Not yet done**: live-smoke checklist (photo scan, condition-picker
      buttons, `/snslist` pagination, `wprc`/`fbprc` ForceReply round-trip) —
      needs a real restart via 「重啟龍蝦」; nothing pushed/deployed yet.
      §A `git push` still pending across all three repos — will summarize and
      wait for 「推」/ok. Phase 4 (cleanup) not yet started.
  - **Phase 4 — DONE (cleanup).**
    - **aka `commands.py`/`formatters.py` wrapper removal**: both were
      genuinely thin (docstrings claimed "backwards compatibility" wrapper,
      and `formatters.py` was a pure re-export; `commands.py`'s `lookup_card`
      turned out to be a byte-for-byte behavioral duplicate of
      `price_monitor_bot.commands.lookup_card`, confirmed by diff before
      deleting — not just assumed from the docstring). Redirected the 4
      internal call sites (`dashboard.py`, `toolset.py`,
      `tests/test_reference_sources.py`, `tests/test_telegram_bot.py`) to
      import from `price_monitor_bot.commands`/`formatters` directly, deleted
      both wrapper files. Also deleted `aka_no_claw/tests/test_commands.py`
      after diffing it against `price_monitor_bot/tests/test_commands.py` —
      identical apart from the import line, so it was pure duplicate coverage
      left over from before the functions moved, not a distinct test.
    - **`test_telegram_bot.py` core/price split, reinterpreted**: literally
      moving lines out of price's 3,748-line file wasn't possible — its "core
      dispatch" tests are written against the fully-integrated production
      `TelegramCommandProcessor` (price+domain kwargs), which `telegram_core`
      can't construct (zero-dependency rule). Same situation Phase 2 hit and
      resolved the same way: instead of relocating lines, wrote NEW tests
      directly against `CoreCommandProcessor`/`polling.py` in
      `telegram_core/tests/test_polling.py` (27 tests — `PollHeartbeat`
      roundtrip/staleness, `_is_conflict_error`, `_drain_pending_updates`
      incl. 409-retry-then-succeed/give-up/reraise-non-conflict,
      `handle_telegram_message`'s full hook chain including a dedicated
      regression test for the `handle_pre_dispatch_text` replies-accumulator
      bug fixed earlier in Phase 3, and `handle_telegram_callback_query`'s
      full builtin set — `pg`/`del`/`close`/`popt`/`topt`/`noop`/unknown,
      registry-before-`pg` precedence, async-hook short-circuit). This is now
      `telegram_core`'s first direct coverage of `polling.py` — previously it
      was only exercised indirectly through price's characterization suite.
      The price-side characterization tests were kept as-is (not deleted):
      they're the "does the real production object graph still work"
      integration proof, which is a different job than `telegram_core`'s own
      unit tests and would be a real coverage loss to remove.
      `telegram_core` suite: 31 → 58 passed.
    - **Phase-1 compat shims removed**: `price_monitor_bot/list_view.py` and
      `logging_utils.py` (pure re-export shims from Phase 1) had zero
      remaining external users (aka was already redirected in Phase 1), but
      price's OWN `bot.py` and 2 of its own tests were still importing
      through them. Redirected those 3 internal call sites to import
      `telegram_core.list_view`/`telegram_core.logging_utils` directly, THEN
      deleted both shim files — satisfying the plan's "only remove when zero
      users in either repo" condition for real, rather than leaving them
      around indefinitely.
    - **All three suites green**: `telegram_core` 58 passed (31 + 27 new);
      `price_monitor_bot` 568 passed/8 skipped (unchanged — shim removal and
      import redirection are behavior-neutral); `aka_no_claw` 2267 passed/7
      skipped (2268 baseline minus the 1 duplicate `test_commands.py` test
      deleted — every other test unaffected).
    - Acceptance check: `grep -rn "from price_monitor_bot" aka/src` → only
      price-domain imports remain (`bot.py` re-exports, `watch_monitor`,
      `commands`, `formatters`) — zero bare infra imports. Confirmed.
    - **Docs truth updates**: `SYSTEM_MAP.md` (repo map + package-layer rows
      for `telegram_core`, corrected dependency-direction wording),
      `TASK_ROUTING.md` (routing-table row + decision-tree branch for
      Telegram-infra changes), `CURRENT_STATE.md` (Telegram bot subsystem row
      now states the 3-package chain; stale "check whether behavior has
      migrated" drift note resolved to state the monkey-patch is gone),
      `TELEGRAM_NL_OWNERSHIP_REFACTOR_ISSUE.md` (added the cross-link back to
      this doc, per §7). `DOCS_INDEX.md` / `DOC_AUDIT.md` entries for this
      doc flipped from `Planned` to `Current` (kept in place rather than
      archived — it documents `telegram_core`'s hook/registry contracts, so
      it stays live reference material, matching the `ISSUE_66_PHASE2_
      PROGRESS.md` precedent of keeping a completed living-record doc as
      `Current` rather than archiving it).
    - **Not done / deferred to 主上**: the full live-smoke checklist (§5) —
      still needs a real restart via 「重啟龍蝦」and hands-on button/photo
      taps; nothing in this plan has been pushed or deployed yet. §A push
      summary is the next and last step before that can happen.
  - **All four phases (P0–P4) of this plan are now code-complete and fully
    test-verified across all three repos.** Nothing has been pushed in any
    of the three repos (`telegram_core`, `price_monitor_bot`, `aka_no_claw`)
    — per §A protocol, a summary of all changed repos/files is owed to 主上
    next, waiting for 「推」/ok before any push. Live smoke (§5) is the only
    acceptance item left, and it requires a real restart, so it naturally
    follows the push.
  - **Pre-push doc-drift verification** (per §7's own instruction to run
    [DOC_DRIFT_CHECKLIST.md](DOC_DRIFT_CHECKLIST.md) before any docs-touching
    push): ran both §E mechanical checks from repo root. The unindexed-docs
    loop (`docs/*.md` not referenced in `DOCS_INDEX.md`) printed nothing —
    clean. The broken-intra-links loop printed 17 `MISSING:` lines, all
    pre-existing false positives from the script's one-level-only path
    resolution (nested `docs/fix_benchmarks/**` and
    `docs/local_tool_calling_benchmark/**` links, plus `../*.md` links that
    resolve relative to `docs/` rather than repo root); every flagged file
    was confirmed to exist on disk, and none were touched by this session's
    edits. §A spot-check: the new `telegram_core:` entry's
    `integration_status: integrated` reuses a value already present on other
    `SYSTEM_MANIFEST.yaml` repo entries, so no new status vocabulary was
    introduced. `check_docs_health.py` had already passed after the manifest
    edit. Docs are push-clean.
- **2026-07-03 — 主上-requested acceptance audit + gap fixes (all landed).**
  主上 asked for a full acceptance pass over the refactor and to fix anything
  unfinished. The audit found the earlier "all phases code-complete" claim had
  missed two substantive P3 items and two minor ones; all four are now fixed:
  - **snsbulk flow relocated to aka (per §3 row + P3 item 3; 主上 chose「現在
    完整搬到 aka（照計劃字面）」over a follow-up issue).** The plan's §3 row
    only listed `_handle_sns_bulk_update_callback` + the dataclass, but the
    pending-state *creators* (3 `_build_sns_bulk_*_plan` builders + the three
    `sns_bulk_*` NL intent branches) lived in price's NL layer and would have
    dangled, so the whole flow moved as a unit — 零行為變更, bodies verbatim:
    - `openclaw_adapter/sns_commands.py`: new section with
      `PendingTelegramSnsBulkUpdate` (slots=True preserved),
      `build_sns_bulk_{add_filter,remove_filter,update_schedule}_plan`
      (processor refs → explicit `sns_db` + `set_pending` injection, matching
      the file's existing factory conventions) and
      `handle_sns_bulk_update_callback` (`pop_pending` injection).
    - `openclaw_adapter/telegram_bot.py` subclass: pending dict + get/set/pop
      accessors, `"bulk"` callback-registry entry (same `setdefault` pattern
      as `"goal"`), `_bulk_callback` adapter (toast-default「未知按鈕」+
      unknown-payload `logger.warning` quirks preserved), thin
      `_build_sns_bulk_*_plan` wrappers (test call-shape unchanged), and the
      three intent branches at the top of `_build_app_natural_language_reply_plan`.
    - price `bot.py`: all moved pieces deleted with "moved to
      openclaw_adapter…" location comments per the existing precedent; the
      now-unused `TEXT_CLARIFICATION_TTL_SECONDS` import dropped.
    - Tests: the 4 bulk e2e tests + `_make_bulk_processor_with_tcg_rules`
      helper migrated from price `test_telegram_bot.py` to new
      `aka_no_claw/tests/test_sns_bulk.py` (aka venv has sns_monitor, so they
      run — skipif guard kept for robustness); the 4 NL fallback *routing*
      tests stayed in price (they test `telegram_nl`, untouched). Callback
      dispatch in the migrated tests goes through
      `telegram_core.polling.handle_telegram_callback_query` directly.
  - **aka now calls `telegram_core.polling.run_telegram_polling` directly
    (P3 item 3; 主上 chose「切換並更新 CLAUDE.md 標記」).** aka's
    `run_telegram_polling` builds the app `TelegramCommandProcessor` itself
    (same kwargs the old factory received) and hands it to the generic core
    loop — the `processor_factory` relay through price's wrapper is retired.
    price's own `run_telegram_polling` body stays in `bot.py` untouched (its
    tests monkeypatch `_bot_module` globals — defining-module resolution).
    **Ops impact**: the live stdout marker is now core's
    `Telegram bot polling as @Aka_No_Claw_bot` (no `OpenClaw` prefix);
    `aka_no_claw/CLAUDE.md` ops section updated accordingly.
  - **Infra import redirects**: aka's `handle_telegram_message` and
    `send_telegram_test_message` now import from `telegram_core`
    (`polling` / `transport`) instead of via price's re-export shims; price's
    shims themselves are unchanged (its own tests still use them).
  - **P4 processor-rename evaluation (deferred decision now documented):
    KEEP the `TelegramCommandProcessor` name in both price and aka.**
    Renaming (e.g. to `PriceCommandProcessor`) would touch price's 3,700+
    line test module, aka's subclass + its test imports, and multiple docs,
    for zero functional gain; the 3-level MRO is already unambiguous by
    module path. Recorded here as the P4「評估」conclusion — no further action.
  - **Verification after fixes**: telegram_core 58 passed; price 564 passed /
    8 skipped (−4 = exactly the migrated tests); aka 2271 passed / 7 skipped
    (+4 migrated, includes the new `test_sns_bulk.py`); zero-dep grep gate on
    `telegram_core/src` still 0 hits; no `_base_run_telegram_polling` or
    `OpenClaw Telegram bot polling` references remain in aka.
  - Still nothing pushed in any repo — refreshed §A summary owed to 主上;
    live smoke (§5) follows the push via「重啟龍蝦」.

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
