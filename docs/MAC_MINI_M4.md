# Mac mini M4 Quick Run

This launcher mirrors the Raspberry Pi stack launcher, but targets a Mac mini M4 / Apple Silicon setup.

Keep these three folders under the same parent directory:

```text
ai_work_space/
  aka_no_claw/
  price_monitor_bot/
  reputation_snapshot/
```

## One-Button Start

On the Mac, double-click:

```text
aka_no_claw/start-mac-mini-stack.command
```

The first run may ask macOS Terminal for permission to run the script. If Finder says the file is not executable, run this once from Terminal:

```bash
chmod +x start-mac-mini-stack.command stop-mac-mini-stack.command
```

The startup script will:

- detect macOS, CPU architecture, model, and memory
- install Homebrew if needed, then install Python 3.12 and Tesseract
- create `aka_no_claw/.env` from `.env.example` if missing
- prompt once for Telegram bot token and chat id
- generate `REPUTATION_AGENT_ADMIN_TOKEN` if it is empty
- sync that token into `reputation_snapshot/.env` as `ADMIN_TOKEN`
- create Python virtual environments for `aka_no_claw` and `reputation_snapshot`
- install `price_monitor_bot`, OpenClaw, and `reputation_snapshot` dependencies
- use a system Chrome/Chromium if present, otherwise install Playwright Chromium
- initialize the `reputation_snapshot` database and signing keys
- start `reputation_snapshot`
- start OpenClaw Telegram polling with the reputation agent enabled and dashboard disabled

To stop everything, double-click:

```text
aka_no_claw/stop-mac-mini-stack.command
```

Logs are written to:

```text
aka_no_claw/logs/reputation_snapshot.log
aka_no_claw/logs/openclaw_telegram.log
```

## Non-Interactive Start

You can prefill required Telegram values from Terminal:

```bash
OPENCLAW_TELEGRAM_BOT_TOKEN='...' OPENCLAW_TELEGRAM_CHAT_ID='...' ./start-mac-mini-stack.command
```

To send a Telegram startup notification:

```bash
START_NOTIFY=1 ./start-mac-mini-stack.command
```

To skip Homebrew package setup after you have prepared dependencies yourself:

```bash
AUTO_INSTALL_SYSTEM_DEPS=0 ./start-mac-mini-stack.command
```

The reputation snapshot service defaults to port `5000`. If that port is already
in use on macOS, the launcher automatically falls back to a nearby local port
such as `5055`. To force a specific port:

```bash
REPUTATION_PORT=5055 ./start-mac-mini-stack.command
```

To require Apple Silicon:

```bash
MAC_REQUIRE_APPLE_SILICON=1 ./start-mac-mini-stack.command
```

## Optional Ollama

For a 16GB Mac mini M4, `.env.example` and `SETUP_OLLAMA=1` default to a balanced
local text router that leaves room for Telegram, `reputation_snapshot`, and Chromium:

```dotenv
OPENCLAW_LOCAL_TEXT_BACKEND=ollama
OPENCLAW_LOCAL_TEXT_MODEL=qwen3:4b
OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS=75
```

To install/start Ollama and pull the text model:

```bash
SETUP_OLLAMA=1 ./start-mac-mini-stack.command
```

Vision models are still opt-in:

```bash
SETUP_OLLAMA=1 SETUP_OLLAMA_VISION=1 ./start-mac-mini-stack.command
```

The 16GB vision preset is:

```dotenv
OPENCLAW_LOCAL_VISION_BACKEND=ollama
OPENCLAW_LOCAL_VISION_MODEL=gemma3:4b
OPENCLAW_LOCAL_VISION_TIMEOUT_SECONDS=180
```

For harder image cases, use one of these manual upgrades after confirming memory headroom:

```dotenv
OPENCLAW_LOCAL_TEXT_MODEL=qwen3:8b
OPENCLAW_LOCAL_VISION_MODEL=gemma3:4b
```

or:

```dotenv
OPENCLAW_LOCAL_TEXT_MODEL=qwen3:4b
OPENCLAW_LOCAL_VISION_MODEL=gemma3:12b
```

After confirming Ollama 0.7.0+ and enough free memory, you can also try:

```dotenv
OPENCLAW_LOCAL_VISION_MODEL=qwen2.5vl:7b,gemma3:12b
```

## Docker Verification

Docker cannot run macOS itself, so the test suite uses two compatibility levels:

- Smoke test: stubs Homebrew, Python venvs, Playwright, Ollama, and long-running services while exercising the real Mac launcher logic.
- Realistic test: runs real Python virtualenv creation, real `pip install`, real `reputation_snapshot` startup, real OpenClaw CLI import, and real PID cleanup inside a Debian container with `MACOS_DOCKER_SIMULATE=1`.

From Windows PowerShell:

```powershell
cd C:\AI_Related\ai_work_space\aka_no_claw
.\scripts\run-mac-mini-docker-test.ps1
.\scripts\run-mac-mini-realistic-docker-test.ps1
```

To ask Docker to use an ARM64 Linux container:

```powershell
.\scripts\run-mac-mini-docker-test.ps1 -Platform linux/arm64/v8
.\scripts\run-mac-mini-realistic-docker-test.ps1 -Platform linux/arm64/v8
```

Bash equivalents:

```bash
./scripts/run-mac-mini-docker-test.sh
./scripts/run-mac-mini-realistic-docker-test.sh
DOCKER_PLATFORM=linux/arm64/v8 ./scripts/run-mac-mini-realistic-docker-test.sh
```

The final real check should still be done on the Mac mini, because Docker cannot simulate macOS Terminal permissions, Homebrew's real host integration, or Apple Silicon memory pressure exactly.
