# Verification Matrix

Status: Current
Owner area: verification

Last reviewed: 2026-06-25

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
| Launchd/service wiring | Targeted service tests and path-resolution tests | `/restartall` smoke plus log inspection on this machine; run the device setup script only for first-setup/cold-start verification |

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

## Verification Boundaries

- Docs-only changes do not need full runtime tests.
- Shared settings, DB paths, and service wiring deserve broader verification.
- Cross-repo runtime changes require tests or smoke checks in each affected repo.
- Networked tests and live Telegram smoke tests should be reported explicitly because they depend on local credentials and current service availability.
