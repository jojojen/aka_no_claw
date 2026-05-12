# Raspberry Pi 5 Quick Run

Put these three folders under the same parent directory on the Raspberry Pi:

```text
ai_work_space/
  aka_no_claw/
  price_monitor_bot/
  reputation_snapshot/
```

On the first run, the script creates `aka_no_claw/.env` from `.env.example` if needed and asks for the required Telegram values:

```dotenv
OPENCLAW_TELEGRAM_BOT_TOKEN=...
OPENCLAW_TELEGRAM_CHAT_ID=...
REPUTATION_AGENT_SERVER_URL=http://127.0.0.1:5000
REPUTATION_AGENT_ADMIN_TOKEN=...
```

`REPUTATION_AGENT_ADMIN_TOKEN` is generated automatically if it is empty, then reused as
`reputation_snapshot`'s `ADMIN_TOKEN`. The startup script will also create
`reputation_snapshot/.env` if it is missing.

For non-interactive setup, prefill the Telegram values from the shell:

```bash
OPENCLAW_TELEGRAM_BOT_TOKEN='...' OPENCLAW_TELEGRAM_CHAT_ID='...' ./start-rpi5-stack.sh
```

## First Run

```bash
cd ~/ai_work_space/aka_no_claw
chmod +x start-rpi5-stack.sh stop-rpi5-stack.sh
./start-rpi5-stack.sh
```

The script will:

- detect OS, CPU architecture, board model, Docker/test mode, and total memory
- install Raspberry Pi OS / Debian packages with `apt-get`
- install or build Python 3.12 if the system Python is older
- remove copied Windows virtual environments that would interfere on Linux
- ignore stale or unrelated copied PID files safely
- create and fill `aka_no_claw/.env`, prompting once for Telegram token and chat id
- create Python virtual environments
- install Python packages for all three folders
- install Playwright browser support or use system Chromium
- initialize the `reputation_snapshot` database and keys
- sync `reputation_snapshot/.env` host, port, and `ADMIN_TOKEN` from `aka_no_claw/.env`
- replace copied Windows Tesseract paths with Pi runtime paths
- disable unavailable local Ollama backends for the current run, unless you opt into Ollama setup
- start `reputation_snapshot`
- start OpenClaw Telegram polling without opening dashboards

If `reputation_snapshot/.env` already exists, the script keeps a one-time backup at
`reputation_snapshot/.env.rpi-backup` before syncing the Pi runtime values.

If you already prepared system packages yourself and do not want the script to use `sudo apt-get`:

```bash
AUTO_INSTALL_SYSTEM_DEPS=0 ./start-rpi5-stack.sh
```

To require a real Raspberry Pi 5 board instead of allowing Docker or another Linux host:

```bash
RPI5_REQUIRE_PI=1 ./start-rpi5-stack.sh
```

If Python must be built locally, the default source version is `3.12.10`; override it with `PYTHON_VERSION=3.12.x` if needed.

To skip copied-runtime cleanup:

```bash
CLEAN_COPIED_RUNTIME=0 ./start-rpi5-stack.sh
```

## Local Natural Language / Ollama

Your Windows `.env` may contain machine-local settings such as:

```dotenv
OPENCLAW_TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
OPENCLAW_TESSDATA_DIR=C:\AI_Related\codex_work_space\.openclaw_ocr\tessdata
OPENCLAW_LOCAL_VISION_BACKEND=ollama
```

The Pi startup script does not edit `aka_no_claw/.env`, but at runtime it replaces Windows Tesseract paths with Linux paths. If Ollama is configured but not installed/reachable, it disables that local backend for the current run so the bot still starts.

To install and use Ollama on an 8GB Pi for natural-language routing:

```bash
SETUP_OLLAMA=1 ./start-rpi5-stack.sh
```

The script defaults to `gemma3:1b` for text routing because it is much safer on 8GB RAM than larger vision models.

```dotenv
OPENCLAW_LOCAL_TEXT_BACKEND=ollama
OPENCLAW_LOCAL_TEXT_ENDPOINT=http://127.0.0.1:11434
OPENCLAW_LOCAL_TEXT_MODEL=gemma3:1b
```

By default, `SETUP_OLLAMA=1` only ensures the text model. It will not pull vision models on an 8GB Pi. If you still want image fallback through Ollama, set `OPENCLAW_LOCAL_VISION_MODEL` and explicitly opt in:

```bash
SETUP_OLLAMA=1 SETUP_OLLAMA_VISION=1 ./start-rpi5-stack.sh
```

Expect larger vision models such as `qwen2.5vl:7b` to be much heavier than text routing on 8GB RAM.

To send a Telegram startup notification:

```bash
START_NOTIFY=1 ./start-rpi5-stack.sh
```

## Docker Verification Before Pi

From Windows PowerShell:

```powershell
cd C:\AI_Related\ai_work_space\aka_no_claw
.\scripts\run-rpi5-docker-test.ps1
```

From bash:

```bash
cd ~/ai_work_space/aka_no_claw
./scripts/run-rpi5-docker-test.sh
```

The Docker test uses a Debian Python 3.12 container and a mocked Pi workspace. It runs the real
`start-rpi5-stack.sh` and `stop-rpi5-stack.sh` end to end while stubbing `apt-get`, `pip`,
Playwright, Chromium, Ollama, and long-running services. This verifies:

- shell syntax for the start and stop scripts
- environment detection output
- copied Windows virtualenv cleanup
- system dependency installation path
- `.env` validation and `reputation_snapshot/.env` synchronization
- safe Ollama auto-install path with `SETUP_OLLAMA=1`
- default pull of the Pi-friendly `gemma3:1b` text model
- vision model pull is skipped unless `SETUP_OLLAMA_VISION=1`
- reputation server and Telegram process orchestration
- stop script cleanup, including the Ollama process started by the stack

To ask Docker to run the smoke test as an ARM64 container when your Docker installation supports emulation:

```powershell
.\scripts\run-rpi5-docker-test.ps1 -Platform linux/arm64/v8
```

or:

```bash
DOCKER_PLATFORM=linux/arm64/v8 ./scripts/run-rpi5-docker-test.sh
```

## More Realistic Docker Test

The smoke test above is fast and safe because it stubs package installs and long-running services.
For a closer pre-Pi check, run the realistic Docker test:

```powershell
cd C:\AI_Related\ai_work_space\aka_no_claw
.\scripts\run-rpi5-realistic-docker-test.ps1
```

For ARM64 emulation:

```powershell
.\scripts\run-rpi5-realistic-docker-test.ps1 -Platform linux/arm64/v8
```

Bash equivalents:

```bash
./scripts/run-rpi5-realistic-docker-test.sh
DOCKER_PLATFORM=linux/arm64/v8 ./scripts/run-rpi5-realistic-docker-test.sh
```

The realistic test copies `aka_no_claw`, `price_monitor_bot`, and `reputation_snapshot` into a
temporary container workspace, deliberately excluding your real `.env`. It writes a sanitized test
`.env` with a fake Telegram token and then runs the real `start-rpi5-stack.sh`.

This test performs real operations for:

- Debian `apt-get` package installation
- Python virtual environment creation
- `pip install` for `reputation_snapshot`, `price_monitor_bot`, and `aka_no_claw`
- system Chromium / Playwright runtime dependency path
- `reputation_snapshot` database initialization
- Ed25519 key generation
- `reputation_snapshot` server startup and `/admin` readiness
- OpenClaw CLI import and tool registry loading
- stack stop and PID cleanup

It still avoids using your Telegram bot token and does not install or pull Ollama models by default.
Use the real Raspberry Pi for the final Telegram/Ollama check.

If you explicitly want the realistic Docker test to also try Ollama installation and the default
`gemma3:1b` text model pull, opt in:

```powershell
$env:REALISTIC_SETUP_OLLAMA='1'
.\scripts\run-rpi5-realistic-docker-test.ps1 -Platform linux/arm64/v8
```

or:

```bash
REALISTIC_SETUP_OLLAMA=1 DOCKER_PLATFORM=linux/arm64/v8 ./scripts/run-rpi5-realistic-docker-test.sh
```

This is much slower and still may not represent Pi thermal, memory, or accelerator behavior.

## Stop

```bash
./stop-rpi5-stack.sh
```

## Logs

```bash
tail -f logs/reputation_snapshot.log
tail -f logs/openclaw_telegram.log
```

The stack starts `reputation_snapshot` and OpenClaw Telegram polling without opening any dashboard browser windows. `price_monitor_bot` is installed into OpenClaw's virtual environment as the reusable market-monitoring package.

## Troubleshooting

If `logs/reputation_snapshot.log` shows:

```text
Address already in use
Port 5000 is in use by another program.
```

an older `reputation_snapshot` process is still holding the local API port. Update `aka_no_claw`,
then run the stop script once before starting again:

```bash
./stop-rpi5-stack.sh
./start-rpi5-stack.sh
```

The updated stop/start scripts also look for orphaned OpenClaw and `reputation_snapshot` processes
from the same copied workspace when the PID file is missing or stale.

If `logs/openclaw_telegram.log` or `logs/openclaw.log` shows:

```text
BrowserType.launch: Executable doesn't exist
Looks like Playwright was just installed or updated.
```

update both `aka_no_claw` and `price_monitor_bot`, then restart. The Mercari watcher now uses the
system Chromium path prepared by `start-rpi5-stack.sh` instead of requiring Playwright to download a
separate bundled browser:

```bash
./stop-rpi5-stack.sh
./start-rpi5-stack.sh
```

As a temporary workaround on an older copy, this also works but is slower and uses more disk:

```bash
./.venv/bin/python -m playwright install chromium
```

If `logs/openclaw_telegram.log` shows:

```text
AttributeError: 'NoneType' object has no attribute 'strip'
```

near `build_local_vision_clients`, the Telegram process crashed while local Ollama vision fallback
was disabled but an old `OPENCLAW_LOCAL_VISION_MODEL` value was still present. Update both
`aka_no_claw` and `price_monitor_bot`, then restart:

```bash
./stop-rpi5-stack.sh
./start-rpi5-stack.sh
```

The `node ... write EPIPE` message that may appear after this is usually a follow-on Playwright
pipe error after the Python process has already crashed, not the root cause.
