# Verification Matrix

Status: Current
Owner area: verification

Last reviewed: 2026-07-17

## Verification By Change Type

| Change type | Minimum verification | Broader verification |
|---|---|---|
| Docs only | Review changed Markdown, check relative links, run a lightweight grep for missing referenced files | n/a |
| README / agent docs | Check links to new docs and referenced commands | `python -m openclaw_adapter list-tools` |
| Tool registry / CLI | `python -m openclaw_adapter list-tools` | Targeted pytest for changed command handlers |
| Telegram command wiring | Targeted command tests if available; inspect ack/background behavior | Bot polling smoke with configured test bot |
| Price lookup | Targeted price/TCG tests | Live lookup smoke if source access is safe |
| Liquidity scoring | Targeted scoring tests and docs check | Dashboard smoke and sample command output |
| `/research` | `tests/test_research_command.py`, related Telegram tests | Live `/research` smoke on one Mercari URL if safe |
| `/new` dynamic tools | `tests/test_dynamic_tools.py` or targeted dynamic tool tests | `python -m openclaw_adapter.dynamic_tools selftest` |
| SNS monitor adapter | `tests/test_sns_*`, command registry tests | `sns-monitor-service` dry run with local DB/log inspection |
| `/snsbuzz` | SNS buzz digest and integration tests | Controlled Telegram smoke for a known keyword |
| Reputation snapshot adapter | Reputation adapter tests | Local `reputation-agent` plus `/snapshot` smoke with non-sensitive URL |
| Opportunity agent | Opportunity pipeline/store/scoring tests | Local agent tick with test DB or controlled candidate |
| Dashboard | Dashboard unit tests if available | `serve-dashboard` local browser smoke |
| DB schema or runtime path | Init/migration tests for all readers/writers | `/restartall` or `POST /api/command/restartall` smoke and log check |
| Web session/run event spine | `tests/test_session_events.py`, `tests/test_session_event_journal.py`, `tests/test_session_projection.py`, `tests/test_command_bridge_event_contract.py` | Restart bridge; replay `/api/command/events` after a completed async run and verify one final message |
| Web generated-tool approval | `tests/test_approval_store.py`, `tests/test_dynamic_tool_approval.py`, `tests/test_command_bridge_approval_http.py` plus Web approval-card tests | Enable staged config; restart; prove approve-once, reject, expiry, hash mismatch, reconnect recovery, replay idempotency, privacy-safe events, and destructive second confirmation |
| Web prompt queue / interjection | `tests/test_prompt_queue_store.py`, `tests/test_prompt_queue_drain.py`, `tests/test_command_bridge_server_http.py`, `tests/test_goal_loop.py`, Web `PromptQueueStrip`/`InputBar` tests | Enable staged config; supported restart; queue/reorder/edit/reload/cancel two prompts, prove one FIFO drain, then prove a goal-loop interjection is accepted only at a declared boundary |
| Launchd/service wiring | Targeted service tests and path-resolution tests | `/restartall` smoke plus log inspection on this machine; run the device setup script only for first-setup/cold-start verification |

## Web Event Spine Live Proof (2026-07-17)

- Supported `/restartall` recreated the bridge and Telegram workers; bridge
  listener and Telegram polling/443 connection were healthy.
- A retained journal with more than three 500-event pages replayed without
  gaps. Negotiated NDJSON emitted only the new run's nine durable events
  (`seq` 1613–1621), alongside the legacy `start`/`done` frames.
- A background research job completed after its submit connection closed;
  reopen polling recovered six progress items, while cursor recovery returned
  four durable progress checkpoints, one assistant message, and one terminal
  event (`seq` 1622–1630). Repeated poll/replay did not advance cursor 1630.
- A separate live cancellation remained interrupted and produced one
  `run.cancelled`; it did not regress to completed.

## Web Generated-Tool Approval Live Proof (2026-07-17)

- With `OPENCLAW_WEB_APPROVALS_ENABLED=true` after `/restartall`, a controlled
  generated tool paused before its first write and rendered an approval card.
- Approve-once executed exactly once. Replaying the same decision token was
  idempotent and left the output timestamp unchanged.
- Explicit reject, expiry, and artifact-hash mismatch produced no side effect.
- Reload recovered the single unresolved approval card; its controls remained
  usable, then became disabled after resolution.
- A destructive fixture required `再按一次確認`; the first approval click made
  no HTTP decision and created no file.
- Request events contained bounded hashes/effects/scopes but no generated source
  or raw arguments. Temporary proof fixtures were deleted and audit events kept.

## Web Prompt Queue Live Proof (2026-07-17)

- After the supported `/restartall`, bridge, Telegram, TLS Web frontend, and
  `GET /api/command/queue` were healthy with the queue feature enabled.
- Two queued prompts persisted in the per-session atomic snapshot, survived
  reconnect inspection, accepted an edit, and reordered by server positions
  rather than timestamps. Exactly one prompt entered `draining`; the second
  began only after the first terminal event. Both `run.accepted` events carried
  their matching `source_prompt_id` values.
- A queued prompt cancelled before claim disappeared from the public snapshot
  and created no run.
- A live goal run accepted an interjection only while its run ID was active.
  The local planner timed out before its next declared boundary; the queued
  interjection was therefore safely demoted to `next_turn`, started once with a
  new source ID, and completed independently. It was never injected into the
  terminated goal run.

## Reporting Verification

Every final summary or PR should state:

- What was run.
- What was not run.
- Why skipped items were skipped.
- Any remaining risk.

## Common Commands

```bash
python -m openclaw_adapter list-tools
python -m pytest
python -m pytest tests/test_research_command.py
python -m pytest tests/test_telegram_bot.py
python -m pytest tests/test_sns_integration.py
python -m pytest tests/test_sns_monitor_service.py
```

Use the repo-local `.venv` when present:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m openclaw_adapter list-tools
```

## CI Lanes (Workstream C, issue #72)

Standard lanes per repo, source of truth is each repo's own
`.github/workflows/tests.yml` — this table just points at them and gives the
equivalent local command.

| Repo | Lane | CI trigger | Local equivalent |
|---|---|---|---|
| `aka_no_claw` | Incremental static checks (blocking changed-file Ruff) | every push/PR | `python scripts/check_incremental_static.py --base <base-sha> --head HEAD` |
| `aka_no_claw` | Fast PR (syntax + build + non-blocking full Ruff report) | every push/PR | `python -m compileall -q src tests && python -m build` |
| `aka_no_claw` | Full offline suite | every push/PR | see below (needs 4 sibling repos on `PYTHONPATH`) |
| `aka_no_claw_web` | Frontend (test + typecheck + build) | every push/PR | `cd frontend && npm ci && npm test && npm run build` |

Reproducing `aka_no_claw`'s full-offline lane locally (siblings as true
directory siblings, matching local multi-repo dev layout — CI instead nests
them under `_deps/` since `actions/checkout` can't place a `path` outside
`$GITHUB_WORKSPACE`):

```bash
cd aka_no_claw
PYTHONPATH=.:src:../price_monitor_bot/src:../telegram_nl/src:../telegram_core/src:../sns_monitor_bot/src \
  .venv/bin/python -m pytest -q -rs
```

Ruff baseline (non-blocking in CI for now — see C4 note in `pyproject.toml`'s
`[tool.ruff]` section):

```bash
.venv/bin/ruff check src tests
```

The blocking C4 gate is intentionally incremental: it runs Ruff only on
changed `src/` and `tests/` Python files, so the legacy repo-wide backlog does
not block unrelated work. The GitHub required-check candidates for `main` are
`Incremental static checks`, `Fast PR (syntax + package build)`, `Full offline
suite`, and `docs-health`; configure them only after each has a green,
deterministic run on the candidate revision.

## Verification Boundaries

- Docs-only changes do not need full runtime tests.
- Shared settings, DB paths, and service wiring deserve broader verification.
- Cross-repo runtime changes require tests or smoke checks in each affected repo.
- Networked tests and live Telegram smoke tests should be reported explicitly because they depend on local credentials and current service availability.
