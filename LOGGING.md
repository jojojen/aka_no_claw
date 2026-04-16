# Logging Guide

## Overview

OpenClaw now writes detailed runtime logs for the main assistant entrypoints and the TCG lookup pipeline.

Default log file:

```text
logs/openclaw.log
```

The `logs/` directory is ignored by git.

## Environment Variables

Configure logging in `.env`:

```dotenv
LOG_LEVEL=INFO
LOG_FILE_PATH=logs/openclaw.log
LOG_RAW_RESULT_LIMIT=20
```

Meaning:

- `LOG_LEVEL`
  - console verbosity
  - examples: `INFO`, `DEBUG`, `WARNING`
- `LOG_FILE_PATH`
  - file path for the persistent log file
- `LOG_RAW_RESULT_LIMIT`
  - max number of raw / processed offers included in a single detailed log event

The file logger always keeps `DEBUG` detail so the trace stays useful even when console noise is reduced.

## What Gets Logged

### CLI / Assistant Entrypoint

- startup environment
- selected database path
- selected log file path
- received CLI command and arguments

### Telegram

- received messages
- parsed commands
- masked chat id
- lookup arguments
- liquidity-board requests
- reply send events
- startup notification

Telegram bot token is never logged.

### Dashboard

- dashboard server startup
- HTTP lookup requests
- lookup arguments
- lookup success / failure

### TCG Lookup Pipeline

- requested card spec
- participating source clients
- search terms per source
- outgoing HTTP GET targets
- raw parsed candidates from each source
- score assigned to each candidate
- matched offers after filtering
- final processed offers
- fair value / notes

## Typical Flow

For one lookup, the log should let you reconstruct this chain:

1. which command was received
2. which card spec was built
3. which sources were queried
4. which search strings were used
5. which raw candidate rows were parsed
6. which scores were assigned
7. which offers survived filtering
8. what final fair value / notes were returned

## Example Checks

PowerShell:

```powershell
Get-Content .\logs\openclaw.log -Tail 100
```

Search only Telegram lines:

```powershell
Get-Content .\logs\openclaw.log | Select-String "Telegram"
```

Search only one card:

```powershell
Get-Content .\logs\openclaw.log | Select-String "メガシビルドン"
```
