# Workspace Dependencies

Status: Current
Owner area: build
Last reviewed: 2026-07-11

## Overview

Inventory of direct dependencies for `aka-no-claw` and its sibling distributions. All sibling packages are consumed via local editable installation (PYTHONPATH / editable mode) and are not resolvable from PyPI.

## Siblings (Direct Python Imports)

| Distribution | Import Packages | Repository | Usage | Classification | Version | Consumers |
|---|---|---|---|---|---|---|
| telegram-core | `telegram_core` | https://github.com/jojojen/telegram_core.git | Direct | Runtime | 0.1.0 | aka-no-claw, price-monitor-bot |
| telegram-nl | `telegram_nl` | https://github.com/jojojen/telegram_nl.git | Direct | Runtime | 0.1.0 | aka-no-claw, price-monitor-bot |
| price-monitor-bot | `price_monitor_bot`, `market_monitor`, `tcg_tracker` | https://github.com/jojojen/price_monitor_bot.git | Direct | Runtime | 0.1.0 | aka-no-claw |
| sns-monitor-bot | `sns_monitor` | https://github.com/jojojen/sns_monitor_bot.git | Direct | Runtime | 0.1.0 | aka-no-claw |

## aka-no-claw (this distribution)

| Distribution | Import Package | Version | Classification |
|---|---|---|---|
| aka-no-claw | `openclaw_adapter`, `assistant_runtime` | 0.1.0 | Runtime |

## Non-Python Consumers

| Consumer | Transport | Purpose |
|---|---|---|
| reputation_snapshot | HTTP (not a Python import) | Seller reputation proofs for research command |
| aka_no_claw_web | SSE + JSON (command bridge HTTP API) | Dashboard frontend for command bridge |

## Notes

- **Sibling dirty-state and HEAD tracking**: D1.3 validator (separate slice) is responsible for reporting mismatches between manifest revisions and local worktree HEADs.
- **Dependencies are declared** in `pyproject.toml` for reproducibility; local resolution happens via editable mode during development and CI `PYTHONPATH` / custom `site-packages` during deployment.
- **reputation_snapshot is HTTP-only**: does not appear in the Python packages manifest because it is consumed over HTTP, not as a Python import.
