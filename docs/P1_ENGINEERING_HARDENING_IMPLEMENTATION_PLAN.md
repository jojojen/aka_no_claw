# P1 Engineering Hardening Implementation Plan

Last reviewed: 2026-07-11
Status: Planned
Owner area: agent-maintenance

## Read This First

This is the canonical implementation plan for the P1 engineering-hardening
program tracked by GitHub issue
[`aka_no_claw#80`](https://github.com/jojojen/aka_no_claw/issues/80).

Use this document as the handoff trail for implementation. The GitHub issues
define ownership, scope, and completion checklists; this file owns the detailed
execution order, file-level changes, verification gates, rollback strategy, and
cross-repository dependencies.

This plan describes intended work, not shipped behavior. Current runtime truth
remains in `SYSTEM_MANIFEST.yaml`, `docs/SYSTEM_MAP.md`, and
`docs/CURRENT_STATE.md`.

## 1. Program Outcome

The workspace should reach a state where:

1. every default test command is deterministic, offline, bounded, and exits
   without leaked project workers;
2. every repository has a required CI lane for its deterministic contract;
3. an `aka_no_claw` revision identifies the exact compatible sibling revisions;
4. cross-repository DB, Python, HTTP, SSE, Telegram, and proof contracts are
   owned and versioned;
5. the largest orchestration modules become compatibility facades over cohesive,
   independently testable services;
6. user-visible behavior, failure reporting, provenance, rate-limit discipline,
   and service restart behavior remain correct throughout the refactors.

## 2. Explicit Non-Goals

- No monorepo conversion.
- No mandatory third-party live tests on every pull request.
- No replacement of NordVPN Meshnet.
- No token rotation for values that are not real secrets.
- No feature redesign disguised as refactoring.
- No prompt-only replacement of deterministic parsing, validation, pricing,
  deduplication, or safety policy.
- No large open-world keyword/entity tables.
- No destructive synchronization of dirty sibling worktrees.
- No big-bang rewrite of a runtime entrypoint.

## 3. Baseline Evidence

Observed on 2026-07-11:

| Repository | Baseline | Engineering signal |
|---|---|---|
| `aka_no_claw` | 2496 passed, 7 skipped, 1 failed | Failure came from a current unindexed local document; suite runtime was about 3m19s. |
| `sns_monitor_bot` | 187 passed, 1 failed | A fixed 2026-05-23 fixture expired relative to the rolling 30-day window. |
| `price_monitor_bot` | 564 passed, 8 skipped | Pytest returned green but stale-heartbeat warnings continued after completion. |
| `reputation_snapshot` | first 9 deterministic tests passed, then live capture stalled | Plain pytest enters Mercari/Playwright tests because live mode defaults on. |
| `telegram_core` | 67 passed | Fast deterministic baseline. |
| `telegram_nl` | 61 passed | Fast deterministic baseline. |
| `aka_no_claw_web` | 189 passed | Toolchain emitted Vite/esbuild and localStorage warnings. |

Do not turn these counts into permanent expectations. CI should report current
counts, resolved revisions, skipped markers, and elapsed time, but acceptance is
behavioral rather than a hard-coded number.

## 4. Dependency And Delivery Graph

```text
T1 sns deterministic clock ─────────┐
T2 price worker shutdown ───────────┼──> C1 required offline CI
T3 reputation test lanes ───────────┘

D1 workspace revision manifest ─────┬──> C2 producer/consumer CI
D2 versioned boundary contracts ────┘

D2 + characterization tests ──> R1 command bridge extraction
                              ├─> R2 Telegram adapter extraction
                              ├─> R3 research pipeline extraction
                              └─> R4 dynamic-tool pipeline extraction
```

Safe parallel work:

- T1, T2, and T3 are in different repositories and may proceed in parallel.
- D1 may begin while test baselines are fixed, but CI must not make a broken
  baseline required.
- R1 and R2 may proceed in parallel only after their shared Telegram/Web
  contracts are characterized.
- R3 and R4 are separate implementation streams even though one issue tracks
  the oversized pipeline program.

Unsafe parallel work:

- two PRs moving the same facade at once;
- contract-version work and consumer migration without an agreed compatibility
  interval;
- a scoring/behavior change mixed into code-motion PRs;
- CI resolving sibling default-branch HEAD while a revision-lock format is being
  changed independently.

## 5. Workstream T — Deterministic Test Baselines

### T1. SNS rolling-window tests

Owner repository: `sns_monitor_bot`

Tracking issue:
[`sns_monitor_bot#1`](https://github.com/jojojen/sns_monitor_bot/issues/1)

#### Root cause

`src/sns_monitor/interest_profile.py` computes a rolling cutoff from the real
UTC clock. `tests/test_interest_profile.py` inserts absolute timestamps from
2026-05-23. The test inevitably changes result when the calendar advances.

#### PR T1.1 — inject the clock

Files expected to change:

- `src/sns_monitor/interest_profile.py`
- `tests/test_interest_profile.py`

Implementation:

1. Add an optional keyword-only `now: datetime | None` at the smallest useful
   boundary, preferably `_query_feedback_aggregates`.
2. Thread it through `build_user_interest_profile` for tests and deterministic
   callers.
3. Default to timezone-aware UTC now for production.
4. Decide and test the policy for a naive supplied datetime. Prefer explicit
   rejection over silently assuming local time.
5. Derive fixture timestamps relative to a fixed reference instant.

Required cases:

- 29 days old: included;
- exactly 30 days old: included because SQL uses `>=`;
- 30 days and 1 second old: excluded;
- another chat: excluded;
- `up`, `down`, and `bought`: counted independently;
- future timestamp: behavior explicitly documented and tested;
- missing DB/table: remains an unavailable/degraded source, not an exception.

#### PR T1.2 — exercise the real schema builder

The current reduced test fixture manually creates the table. Add one test using
the current production DB initialization/migration path so fixture schema drift
cannot hide a broken column contract.

Verification:

```bash
.venv/bin/python -m pytest -q tests/test_interest_profile.py
.venv/bin/python -m pytest -q
```

Rollback: optional clock injection is removable without data migration.

Exit gate: full suite remains green regardless of execution date/timezone.

### T2. Price-monitor poll lifecycle

Owner repository: `price_monitor_bot`, with possible shared fix in
`telegram_core`.

Tracking issue:
[`price_monitor_bot#3`](https://github.com/jojojen/price_monitor_bot/issues/3)

#### Investigation before editing

1. Give every project-owned thread/timer a stable name.
2. Capture the thread list immediately after the first stale warning.
3. Bisect test files or use focused fixture execution to identify the first
   owner that starts the survivor.
4. Trace construction, start, cancellation event, rescheduling, join, and
   watchdog shutdown.
5. Determine whether the leak is in `price_monitor_bot` adaptation or the
   `telegram_core` primitive.

Do not fix this by suppressing the warning, making threads daemon-only, or
adding a sleep.

#### PR T2.1 — diagnostics and lifecycle contract

Define one lifecycle contract for project-owned workers:

- `start()` is idempotent or fails clearly on duplicate use;
- `stop()` is non-blocking and sets cancellation;
- `join(timeout)` waits and reports timeout;
- stop prevents future timer rescheduling;
- exceptions still transition to a terminal state;
- shutdown does not require network success;
- watchdog stops before it can report an intentionally stopped worker as wedged.

Log worker identity, lifecycle state, and last-heartbeat age. Never log tokens or
chat identifiers.

#### PR T2.2 — fixtures and leak assertion

1. Centralize worker creation in `yield` fixtures.
2. Put `stop()` and `join()` in `finally`.
3. Add a session-end assertion scoped only to known project worker names.
4. Capture logs and fail if orderly shutdown emits a stale-heartbeat warning.

Required cases:

- start/stop;
- stop before start;
- double stop;
- poll exception;
- slow/blocked transport;
- watchdog and worker shutdown ordering;
- repeated construction in the same process;
- pytest/interpreter exit.

Verification:

```bash
.venv/bin/python -m pytest -q tests/<lifecycle-test-files>
.venv/bin/python -m pytest -q
```

If `telegram_core` changes, run its complete suite and the `aka_no_claw`
Telegram contract slice.

Exit gate: pytest exits promptly with no project workers and no false wedged
warnings; genuine runtime stale detection remains enabled.

### T3. Reputation offline/live lanes

Owner repository: `reputation_snapshot`

Tracking issue:
[`reputation_snapshot#1`](https://github.com/jojojen/reputation_snapshot/issues/1)

#### PR T3.1 — markers and safe default

1. Add project pytest configuration.
2. Register `live_capture`.
3. Mark `tests/test_live_capture.py` explicitly.
4. Change `RUN_LIVE_CAPTURE_TESTS` default to off.
5. Make plain pytest run no Mercari request.
6. Document the explicit live command.

Recommended commands:

```bash
.venv/bin/python -m pytest -q -m "not live_capture"
RUN_LIVE_CAPTURE_TESTS=1 .venv/bin/python -m pytest -q -m live_capture
```

#### PR T3.2 — bound live work

- deduplicate repeated default URLs;
- cap profile and item cases;
- set per-navigation and overall deadlines;
- reuse a browser session only where isolation remains correct;
- close page, context, browser, driver, and temporary artifacts in `finally`;
- fail fast on 429/bot-interstitial state;
- stop the remaining host batch after rate limiting;
- classify source drift separately from local runtime failure.

#### PR T3.3 — fixture-backed parser/proof coverage

Sanitize and store representative HTML/visible-text fixtures where permitted.
Cover display name, review totals, seller/buyer roles, proof creation, signature
verification, expiry, and revocation without network access.

Exit gate: default tests are offline and deterministic; the manual/scheduled
live lane is bounded, explicit, and still proves current source compatibility.

## 6. Workstream D1 — Reproducible Workspace Revisions

Owner repository: `aka_no_claw`

Tracking issue: [`#73`](https://github.com/jojojen/aka_no_claw/issues/73)

### D1.1 Inventory direct dependencies

Produce a generated/verified table containing:

- distribution name;
- import packages;
- owning repository;
- direct or transitive usage;
- optional/runtime/dev classification;
- current local HEAD and dirty state;
- current package version;
- consumers.

Correct the `aka_no_claw` package identity in `pyproject.toml`. Declare direct
imports such as `telegram-nl` and `sns-monitor-bot` directly rather than relying
on incidental editable installation.

### D1.2 Compatibility manifest

Add `config/workspace-lock.toml` or an equivalent machine-readable file.

Minimum schema:

```toml
schema_version = 1
generated_at = "<UTC timestamp>"

[packages.telegram-core]
repository = "https://github.com/jojojen/telegram_core.git"
revision = "<full immutable sha>"
distribution = "telegram-core"
contract_version = 1
```

Rules:

- only full immutable commit SHAs;
- no local absolute paths;
- one source of truth for CI and deployment resolution;
- local editable mode may differ, but the mismatch must be visible;
- updates include reason, affected consumers, and verification performed.

### D1.3 Validator and local diagnostics

Add a read-only validator that reports:

- missing sibling checkout;
- expected/actual SHA;
- dirty worktree;
- package metadata mismatch;
- incompatible contract version;
- duplicate distribution/import ownership.

It must never reset, checkout, stash, clean, or overwrite sibling work.

### D1.4 Separate install modes

- local workspace development: relative editable siblings;
- CI/deploy: revisions from the compatibility manifest;
- clean wheel smoke: build and install artifacts into a new environment.

Keep the old local flow during migration. Switch CI first, then deployment after
one controlled revision-bump rehearsal.

### D1.5 Upgrade and rollback rehearsal

1. Advance one low-risk sibling revision.
2. Run producer and consumer contract tests.
3. Record resolved SHAs.
4. Roll the manifest back to the previous revision.
5. Prove the old workspace still installs and starts.

Exit gate: an `aka_no_claw` commit identifies a reproducible compatible sibling
set while developers can still use dirty editable worktrees intentionally.

## 7. Workstream D2 — Versioned Cross-Repo Contracts

Owner repository: contract owner for implementation; `aka_no_claw` owns the
workspace inventory and integration verification.

Tracking issue: [`#77`](https://github.com/jojojen/aka_no_claw/issues/77)

### D2.1 Contract inventory

Inventory at least:

| Contract | Producer | Consumers |
|---|---|---|
| monitor DB read model | `price_monitor_bot` | `aka_no_claw`, SNS interest profile |
| SNS DB/inbox | `sns_monitor_bot` | `aka_no_claw` |
| opportunity DB/inbox | `aka_no_claw` opportunity agent | Telegram/dashboard/SNS readers |
| knowledge DB/inbox | `aka_no_claw` knowledge service | research/dynamic tools/digests |
| reputation HTTP/proof | `reputation_snapshot` | `aka_no_claw` |
| Telegram registry/hooks | `telegram_core` + consumers | price bot and aka adapter |
| command bridge JSON/SSE | `aka_no_claw` | `aka_no_claw_web` |

For each, record version, transport, owner, compatibility policy, current
failure semantics, fixtures, and migration owner.

### D2.2 Required failure vocabulary

Every boundary must distinguish:

- `unavailable`: optional source absent/offline;
- `empty`: compatible source returned zero records;
- `stale`: compatible but outside freshness policy;
- `incompatible`: unsupported contract/schema version;
- `corrupt`: cannot parse or verify;
- `rate_limited`: upstream explicitly throttled;
- `rejected`: policy intentionally refused the operation.

No `incompatible` or `corrupt` condition may silently become `empty`.

### D2.3 SQLite contract strategy

1. Add/read a monotonic `schema_version` metadata record.
2. Validate before querying.
3. Prefer stable views/read-model tables for cross-repository reads.
4. Keep writes behind the owner service/inbox.
5. Provide an explicit compatibility result containing expected/actual version.
6. Add migration and backward-read tests.

Do not create a global shared ORM/model package.

### D2.4 HTTP, proof, JSON, and SSE

Version request/response envelopes and SSE event types. Define additive and
breaking changes. Store sanitized producer fixtures that consumers verify in
their own CI. Reputation proof compatibility must continue to verify old valid
proofs during the supported interval.

### D2.5 Telegram registry contract

- version generic hook expectations;
- test command and callback-prefix uniqueness;
- keep domain vocabulary out of `telegram_core`;
- preserve callback wire formats until an explicit migration;
- run both consumer suites for shared changes.

### D2.6 Migration sequence

For a breaking boundary:

1. producer accepts/emits old and new where practical;
2. consumer adds new-version support;
3. compatibility manifest advances;
4. live behavior is verified;
5. old support is removed only after the compatibility window;
6. rollback restores the previous manifest/version without data loss.

Exit gate: every cross-repo boundary has an owner/version and incompatibility is
visible before business logic interprets the data.

## 8. Workstream C — Multi-Repo CI

Owner repository: each repository owns its basic workflow; `aka_no_claw` owns
the workspace compatibility matrix.

Tracking issue: [`#72`](https://github.com/jojojen/aka_no_claw/issues/72)

### C1. Standard lanes

1. **Fast PR**: deterministic targeted tests, docs checks, package import/build.
2. **Full offline**: all non-live tests.
3. **Producer/consumer contract**: producer PR tested against pinned consumers.
4. **Frontend**: `npm ci`, tests, TypeScript check, production build.
5. **Live**: manual/scheduled, credential-gated, concurrency 1, bounded requests.

### C2. Per-repository bootstrap order

1. `telegram_core` and `telegram_nl`: easiest zero-dependency baselines.
2. `sns_monitor_bot`: after T1.
3. `reputation_snapshot`: after T3.
4. `price_monitor_bot`: after T2.
5. `aka_no_claw_web`.
6. `aka_no_claw`: after deterministic sibling lanes and D1 manifest validator.

Each Python workflow should:

- use Python 3.12;
- install declared dependencies;
- build a wheel/sdist;
- install the wheel in a clean environment;
- run the repository's offline suite;
- print resolved revisions and marker skips;
- cancel superseded PR jobs;
- avoid printing environment values;
- upload only useful failure artifacts.

### C3. Producer-to-consumer edges

```text
telegram_core -> price_monitor_bot, aka_no_claw
telegram_nl -> price_monitor_bot, aka_no_claw
price_monitor_bot -> aka_no_claw
sns_monitor_bot -> aka_no_claw
reputation_snapshot -> aka_no_claw
aka_no_claw command bridge -> aka_no_claw_web
```

Required lane uses pinned compatibility revisions. A separate non-required lane
may probe consumer default-branch HEAD for forward compatibility.

### C4. Static checks rollout

Introduce gradually:

- Ruff baseline or changed-files gate;
- Pyright/Mypy on boundary modules first;
- dependency metadata validation;
- docs health/drift;
- secret scanning;
- tracked runtime-artifact check.

Do not enable a repository-wide legacy lint backlog as a required gate in the
same PR that introduces the tool.

### C5. Branch protection readiness

Only mark a lane required after:

- it is deterministic for representative PRs;
- live sources are excluded;
- its ownership and failure triage are documented;
- its runtime is acceptable;
- reruns do not change outcome without code/environment change.

Exit gate: every repo has required offline CI and shared producers run consumer
contract tests using explicitly reported SHAs.

### Progress log

- 2026-07-11 — `aka_no_claw`'s own lanes only (C2 step 6's non-D1-dependent
  half): `fast-pr` job (byte-compile gate + `python -m build` sdist/wheel,
  no runtime deps needed) and `full-offline` job (full 2500+ test suite,
  siblings `price_monitor_bot`/`telegram_nl`/`telegram_core`/`sns_monitor_bot`
  checked out and put on `PYTHONPATH`, resolved SHAs printed per C2). Found
  and fixed two undeclared runtime dependencies while proving out the CI
  install list: `numpy` (used by `intent_cache.py`) and `sns-monitor-bot`
  (used by `opportunity_sns_discovery.py`) were missing from
  `pyproject.toml`'s `dependencies`. Still open: C3's pinned
  producer→consumer matrix (blocked on #73's SHA-manifest — there's no
  pinning mechanism to build it on top of yet), CI for the other 5 repos,
  C4 static checks, C5 branch protection.
- 2026-07-11 — first `full-offline` run went red (11 failures, all in
  `tests/test_research_command.py`). Root-caused: `_load_fixture()` hardcoded
  `Path(__file__).resolve().parents[2] / "price_monitor_bot"`, a path that
  only resolves in the local multi-repo dev layout (true sibling checkouts).
  Under CI's nested `actions/checkout` layout (siblings under `_deps/` inside
  this workspace, since `checkout` refuses a `path` outside
  `$GITHUB_WORKSPACE`), that path pointed outside the workspace entirely and
  `FileNotFoundError` cascaded into 11 assertion failures with unrelated-
  looking symptoms (missing title, empty entity list, URL-only search query).
  Not a sibling-repo version-drift issue — ruled that out first via SHA-
  matched fresh clones reproducing 100% pass locally. Fixed by trying both
  candidate paths (sibling-dev layout, then `_deps/` layout) before raising.
  Verified against a simulated CI directory layout locally (92/92 pass) in
  addition to the full local suite (2501 passed / 7 skipped, unchanged).
- 2026-07-11 — C1 items 1 and 4, plus C2 step 5:
  - `aka_no_claw_web` frontend lane added (`npm ci` / `npm test` / `tsc
    --noEmit && vite build`). First run failed for a real, pre-existing
    reason unrelated to the new workflow: `package-lock.json` was missing
    `esbuild@0.28.x` entries for every platform, so `npm ci` fails
    deterministically — reproduced in a clean Node 22 Docker container
    (matching the Actions runner) before touching anything, so this wasn't
    assumed to be CI-only flakiness. Fixed by regenerating the lock via
    `npm install` in that same container, then re-verified `npm ci` + test +
    build clean from the regenerated lock. Confirmed green on Actions.
  - Ruff added as a **non-blocking** baseline step in `aka_no_claw`'s
    fast-pr lane (`continue-on-error: true`) per C4's explicit instruction
    not to make an existing lint backlog a required gate in the same PR
    that introduces the tool — repo currently has 141 pre-existing findings,
    left as follow-up cleanup rather than fixed in bulk here.
  - Standard lanes/commands documented in `docs/VERIFICATION_MATRIX.md`
    under "CI Lanes (Workstream C, issue #72)", including the local
    reproduction command for each lane.
  - Still open: C3's pinned producer→consumer matrix (hard-blocked on #73's
    SHA-manifest — there is no pinning mechanism to build it on top of),
    CI for `telegram_core`/`telegram_nl`/`sns_monitor_bot`/`price_monitor_bot`/
    `reputation_snapshot` (their own per-repo workflows, not just being
    checked out as siblings for aka_no_claw's suite), C5 branch-protection
    rule changes (a GitHub repo-settings change affecting what blocks merges
    for everyone — deliberately not flipped without explicit sign-off).
- 2026-07-12 — C4 corrective follow-up: added a blocking `Incremental static
  checks` job. It computes the PR base / pushed-from SHA, invokes
  `scripts/check_incremental_static.py`, and runs Ruff only on changed Python
  files under `src/` and `tests/`; the historical whole-repo report remains
  non-blocking. C5 readiness now documents the exact candidate contexts and
  requires one green deterministic run of this new job before enabling them
  as GitHub required checks.

## 9. Workstream R1 — Command Bridge Decomposition

Tracking issue: [`#74`](https://github.com/jojojen/aka_no_claw/issues/74)

### Target responsibility map

```text
command_bridge.py             compatibility facade/composition
bridge/conversation.py        session, continuation, persistence, locking
bridge/providers.py           backend selection, pool rotation, metadata
bridge/planner.py             trusted plan generation and validation
bridge/executor.py            allowlisted tool execution and result ledger
bridge/goals.py               goal-loop orchestration and budgets
bridge/music.py               bounded music plan and resume
bridge/workflows.py           workflow author/edit/run integration
bridge/home_control.py        Bluetooth/IR/VPN capability adapters
bridge/responses.py           blocking/stream response synthesis
```

Names are provisional. Prefer fewer modules if responsibilities remain cohesive.

### R1.0 Characterization inventory

> 2026-07-12 — inventory shipped: [`R1_COMMAND_BRIDGE_INVENTORY.md`](R1_COMMAND_BRIDGE_INVENTORY.md)
> (public surface × routes × consumers, state/locks, threads/cancellation,
> providers, stores, response contracts, coverage gaps, risk notes).
> Remaining in R1.0: the four characterization-test gaps listed in its §8.

Before moving code, map:

- public `CommandBridge` methods;
- HTTP routes and frontend consumers;
- state fields and synchronization;
- threads/processes and cancellation;
- providers and fallback semantics;
- stores and persisted payloads;
- blocking, async, and streaming response contracts;
- current tests and untested branches.

Add contract tests for JSON and SSE event ordering, disconnect, retry/fallback,
session resume, concurrent conversations, and orphaned result handling.

### R1.1 Extract pure response/model helpers

Move parsing, formatting, and response construction with no IO. Preserve exact
fields and event ordering. This PR should be mechanical code motion.

### R1.2 Extract provider routing

Own provider selection, sticky provider, pool rotation, timeout/error classes,
and model metadata in one collaborator. Use a small protocol and deterministic
fakes. Preserve explicit unavailable/fallback reporting.

### R1.3 Separate planner and executor

> 2026-07-12 — R1.3a shipped (see #74 comment for hash): trusted planning
> extracted to `command_bridge_planner.py` (`ChatToolPlanner` + `PlannerDeps`
> protocol; prompt assembly, per-backend plan generation, strict-JSON
> validation). R1.3b is now extracted to `command_bridge_executor.py`
> (`ChatToolExecutor` + `ExecutorDeps`): allowlisted policy-map dispatch,
> bounded per-conversation tool ledger, streaming/orphan mechanics, and
> satisfaction-judge parsing/fallback. The bridge keeps same-name thin
> delegates and executor calls back through them, so existing instance
> monkeypatches remain compatible. Next: R1.4 conversation state.

Planner produces validated typed plans only. Executor maps an allowlisted plan
to registered tools. Satisfaction judgement and goal escalation are explicit
steps. Untrusted model text never selects an arbitrary function.

### R1.4 Extract conversation state

> 2026-07-12 — R1.4a shipped (see #74 comment for hash): the Web-console
> session snapshot and disconnected-stream orphan append moved to
> `command_bridge_conversation.py` (`ConversationSession`).  The collaborator
> serializes lazy store construction and append read-modify-write, while the
> bridge keeps `_sessions()` / injected-store seams compatible.  A concurrent
> orphan-result test proves all simultaneous completions persist.  Remaining:
> paused music and goal continuation state, expiry, and confirmation ownership.
> 2026-07-12 — R1.4b shipped: `ConversationState` now owns paused music,
> goal continuation, pending-confirmation, and completed-workflow maps plus
> their locks. Bridge attributes remain compatibility aliases until R1.6.

Move session memory, paused plans, continuation state, orphaned results, expiry,
and locking. Test concurrent sessions, disconnect races, and process restart.

### R1.5 Extract capabilities one at a time

> 2026-07-12 — R1.5a shipped: Web music command, queue, callback, and
> now-playing adapters moved to `command_bridge_music.py` (`MusicCapability`)
> behind same-name bridge delegates. R1.5b moved Web workflow command and
> editor callback routing to `command_bridge_workflow.py` (`WorkflowCapability`).
> R1.5c moved Bluetooth and IR Web command/callback routing to
> `command_bridge_home.py` (`HomeCapability`). R1.5 is complete; next R1.6
> removes transitional legacy implementations and leaves a thin facade.

Recommended order: music, workflow, then home control. Each service owns no HTTP
details and exposes narrow typed methods.

### R1.6 Reduce the facade

`CommandBridge` should wire collaborators and preserve compatibility exports.
Do not maintain two implementations. Remove temporary aliases only after all
backend/server/frontend consumers migrate.

Per-PR verification:

- targeted backend tests;
- full offline suite;
- Web tests and production build if payloads change;
- cancellation/resource-leak checks;
- local browser smoke;
- restart and real changed-behavior verification before push.

## 10. Workstream R2 — Telegram Adapter Decomposition

Tracking issue: [`#75`](https://github.com/jojojen/aka_no_claw/issues/75)

### R2.0 Ownership inventory

For every command and callback prefix, record owning repository/module,
registration site, handler, DB access, background behavior, formatter, tests,
and compatibility requirements.

Add uniqueness/precedence tests across:

```text
telegram_core
  -> price_monitor_bot.TelegramCommandProcessor
  -> openclaw_adapter.telegram_bot.TelegramCommandProcessor
```

### R2.1 Compatibility exports

Separate imports/re-exports first. Keep old import paths working while consumers
migrate. No behavior change.

### R2.2 Registry construction

Move aka-specific command and callback registration to a dedicated registry.
Test collisions, precedence, aliases, and help/list output.

### R2.3 Media ingestion

Extract voice/audio/photo/document handling. Preserve size/duration/type limits,
temporary-file cleanup, transcript-to-normal-dispatch behavior, and failure
messages.

### R2.4 Background jobs

Extract acknowledgement, progress, final result, error reporting, cancellation,
and shutdown. Ensure background work cannot outlive owned resources silently.

### R2.5 Narrow processor hooks

Reduce the aka processor to explicit consumer hooks. Generic transport/polling
stays in `telegram_core`; price-domain behavior stays in `price_monitor_bot`;
aka-specific orchestration stays in `openclaw_adapter` domain modules.

Required characterization:

- allowlist and unauthorized chat;
- slash vs NL routing;
- command/callback precedence;
- pending reply/capture modes;
- duplicate update handling;
- voice transcript dispatch;
- background ack/progress/final/error;
- inherited price commands;
- inbox DB ownership;
- shutdown/cancellation.

## 11. Workstream R3 — Research Pipeline

Tracking issue: [`#76`](https://github.com/jojojen/aka_no_claw/issues/76)

### Stage envelope

Every stage returns a typed result with stage/schema version, status, payload,
provenance, warnings, failure class, elapsed time, host request counts, and
cache/freshness metadata.

Stages:

1. request normalization;
2. target/entity resolution;
3. evidence acquisition;
4. condition/vision assessment;
5. comparable offers and fair value;
6. liquidity/demand;
7. seller/reputation;
8. appreciation/context;
9. report synthesis;
10. persistence/follow-up actions.

### Delivery PRs

1. Responsibility/dependency/budget inventory plus characterization fixtures.
2. Stage result envelope and one source-budget ledger.
3. Pure normalization/report models/synthesis.
4. Seller/reputation stage.
5. Condition/vision stage.
6. Market/fair-value stage.
7. Liquidity/demand and appreciation/context stages.
8. Explicit scheduler, dependencies, cancellation, and overall deadline.
9. Thin public facade and documentation.

Required cases include complete URL research, product-name research, unavailable
seller/vision sources, no comparable offers, 429, slow stage, partial evidence,
cancellation, compatible follow-up buttons, and provenance retention.

Do not change scoring or source weighting during extraction. Any semantic change
gets a separate issue/PR after the facade is stable.

Verification includes a genuinely fresh end-to-end research result shown to the
user, not only assertions.

## 12. Workstream R4 — Dynamic-Tool Pipeline

Tracking issue: [`#76`](https://github.com/jojojen/aka_no_claw/issues/76)

### Target responsibilities

```text
dynamic_tools/specification.py
dynamic_tools/knowledge_context.py
dynamic_tools/providers.py
dynamic_tools/safety.py
dynamic_tools/sandbox.py
dynamic_tools/repair.py
dynamic_tools/evaluation.py
dynamic_tools/catalog.py
dynamic_tools/service.py
```

### Delivery PRs

1. Threat/behavior/resource inventory and characterization tests.
2. Typed immutable specification and bounded RAG context.
3. Provider protocol with deterministic failure fakes.
4. Static capability/safety policy and machine-readable rejection reasons.
5. Sandbox, resource limits, and cleanup for every terminal state.
6. Bounded repair controller with repeated-attempt detection.
7. Generator-independent evaluation and discriminating tests.
8. Versioned catalog/artifact metadata and thin facade.

Required cases:

- safe tool succeeds;
- syntax failure repaired within budget;
- repeated ineffective repair stops;
- unsafe import/filesystem/process/network request rejected;
- executable but semantically wrong output rejected;
- provider unavailable/timeout/truncation;
- missing RAG evidence;
- verifier tampering attempt;
- cancellation and child-process cleanup;
- old catalog reload compatibility.

Generation, safety policy, execution, repair, and evaluation must remain
separate. A generator cannot edit or bypass its verifier. Successful execution
alone is never proof of correctness.

Verification must generate and show a genuinely fresh accepted artifact and a
representative rejection.

## 13. Standard PR Template For This Program

Every PR should state:

```text
Issue / phase:
Problem fixed:
Before responsibility:
After responsibility:
Public contract impact:
Schema/version impact:
Producer SHA(s):
Consumer SHA(s):
Tests run:
Live verification:
Rollback:
Remaining first unchecked phase:
```

Code-motion PRs explicitly say `No intended semantic change`. If observable
behavior changes, stop and split the semantic change unless required to preserve
correctness.

## 14. Standard Verification Rules

For any changed repository:

1. run targeted tests;
2. run the full deterministic/offline suite;
3. run producer/consumer contract tests for a shared boundary;
4. run frontend tests/build when Web contracts change;
5. run bounded live checks only where relevant;
6. inspect project-owned threads/processes/temp artifacts;
7. restart affected running services using the documented path;
8. verify the actual changed user-visible behavior;
9. only then prepare the push summary and await confirmation.

If any source is unavailable, report it explicitly. Do not replace it with a
semantically different source and call the result verified.

## 15. Documentation Updates During Delivery

This plan remains `Planned` until the program completes. Individual PRs update:

- `SYSTEM_MANIFEST.yaml` and `CURRENT_STATE.md` for status/ownership changes;
- `SYSTEM_MAP.md` for shipped architecture/flows;
- `TASK_ROUTING.md` for module ownership/path changes;
- `VERIFICATION_MATRIX.md` for new required commands;
- `DOCS_INDEX.md` and `DOC_AUDIT.md` for document lifecycle changes.

When all workstreams ship, fold stable contract and workflow rules into the
authoritative truth docs, mark this plan Historical, and move it to
`docs/archive/` instead of leaving a permanent second architecture truth.

## 16. Program Completion Checklist

- [ ] T1 SNS time-window tests are deterministic.
- [ ] T2 price-monitor tests exit without leaked workers/warnings.
- [ ] T3 reputation default tests are offline; live lane is explicit/bounded.
- [ ] D1 workspace revisions and direct dependencies are reproducible.
- [ ] D2 cross-repo boundaries have owners and versions.
- [ ] C every repository has required offline CI.
- [ ] C shared producers run pinned consumer contract tests.
- [ ] R1 command bridge is a compatibility facade.
- [ ] R2 Telegram adapter is thin aka-specific wiring.
- [ ] R3 research uses explicit evidence stages and budgets.
- [ ] R4 dynamic tools separate generation, safety, execution, repair, and evaluation.
- [ ] Fresh research and generated-tool outputs were reviewed.
- [ ] Runtime services were restarted and changed behavior verified.
- [ ] Canonical truth docs match the shipped system.
- [ ] This plan is archived after its stable rules are folded into canonical docs.
