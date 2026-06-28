#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_WORKSPACE="${SOURCE_WORKSPACE:-/source}"
WORKSPACE_DIR="${WORKSPACE_DIR:-/tmp/mac-mini-realistic-workspace}"
AKA_DIR="${WORKSPACE_DIR}/aka_no_claw"
REPUTATION_DIR="${WORKSPACE_DIR}/reputation_snapshot"
LOG_DIR="${WORKSPACE_DIR}/realistic-test-logs"

log() {
  printf '[mac-mini-realistic-test] %s\n' "$*"
}

fail() {
  printf '[mac-mini-realistic-test] ERROR: %s\n' "$*" >&2
  exit 1
}

copy_repo() {
  local repo_name="$1"
  [[ -d "${SOURCE_WORKSPACE}/${repo_name}" ]] || fail "${repo_name} is missing from ${SOURCE_WORKSPACE}"
  tar \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='venv' \
    --exclude='.pytest_cache' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='logs' \
    --exclude='run' \
    --exclude='reports' \
    --exclude='data/*.sqlite3' \
    --exclude='data/*.db' \
    -C "${SOURCE_WORKSPACE}" \
    -cf - "${repo_name}" | tar -C "${WORKSPACE_DIR}" -xf -
}

stage_workspace() {
  rm -rf "${WORKSPACE_DIR}"
  mkdir -p "${WORKSPACE_DIR}" "${LOG_DIR}"
  copy_repo "aka_no_claw"
  copy_repo "price_monitor_bot"
  copy_repo "reputation_snapshot"

  chmod +x "${AKA_DIR}/launchers/start-mac-mini-stack.command"

  cat > "${AKA_DIR}/.env" <<'EOF'
MONITOR_ENV=development
LOG_LEVEL=INFO
LOG_FILE_PATH=logs/openclaw.log
MONITOR_DB_PATH=data/monitor.sqlite3
YUYUTEI_USER_AGENT=OpenClawPriceMonitor/0.1 (+https://local-dev)
OPENCLAW_TELEGRAM_BOT_TOKEN=0000000000:realistic-docker-fake-token
OPENCLAW_TELEGRAM_CHAT_ID=123456789
OPENCLAW_TESSERACT_PATH=auto
OPENCLAW_TESSDATA_DIR=auto
OPENCLAW_LOCAL_VISION_BACKEND=
OPENCLAW_LOCAL_VISION_ENDPOINT=http://127.0.0.1:11434
OPENCLAW_LOCAL_VISION_MODEL=qwen2.5vl:7b
OPENCLAW_LOCAL_TEXT_BACKEND=
OPENCLAW_LOCAL_TEXT_ENDPOINT=http://127.0.0.1:11434
OPENCLAW_LOCAL_TEXT_MODEL=
OPENCLAW_CA_BUNDLE_PATH=
OPENCLAW_TLS_INSECURE_SKIP_VERIFY=0
REPUTATION_AGENT_SERVER_URL=http://127.0.0.1:5000
REPUTATION_AGENT_ADMIN_TOKEN=
REPUTATION_AGENT_POLL_SECS=1
EOF
  chmod 600 "${AKA_DIR}/.env"
}

wait_for_openclaw_log() {
  local log_path="${AKA_DIR}/logs/openclaw_telegram.log"
  for _ in $(seq 1 20); do
    [[ -s "${log_path}" ]] && return 0
    sleep 1
  done
  return 1
}

run_realistic_stack() {
  log "Starting Mac mini compatibility stack test with real pip installs and real service startup."
  (
    cd "${AKA_DIR}"
    MACOS_DOCKER_SIMULATE=1 \
      AUTO_INSTALL_SYSTEM_DEPS=0 \
      CLEAN_COPIED_RUNTIME=1 \
      SETUP_OLLAMA=0 \
      START_NOTIFY=0 \
      ./launchers/start-mac-mini-stack.command
  ) | tee "${LOG_DIR}/start.log"

  [[ -x "${AKA_DIR}/.venv/bin/python" ]] || fail "aka_no_claw virtualenv was not created."
  [[ -x "${REPUTATION_DIR}/.venv/bin/python" ]] || fail "reputation_snapshot virtualenv was not created."
  [[ -f "${REPUTATION_DIR}/instance/app.db" ]] || fail "reputation_snapshot database was not initialized."
  [[ -f "${REPUTATION_DIR}/keys/ed25519_private_key.pem" ]] || fail "reputation_snapshot key was not generated."
  grep -Fq "REPUTATION_AGENT_ADMIN_TOKEN=" "${AKA_DIR}/.env" || fail "aka_no_claw .env was not filled."
  grep -Fq "ADMIN_TOKEN=" "${REPUTATION_DIR}/.env" || fail "reputation_snapshot .env was not synced."

  log "Checking reputation_snapshot admin endpoint."
  AKA_ENV_PATH="${AKA_DIR}/.env" "${AKA_DIR}/.venv/bin/python" - <<'PY'
import os
from pathlib import Path
from urllib.request import urlopen

env = Path(os.environ["AKA_ENV_PATH"]).read_text(encoding="utf-8")
token = ""
for line in env.splitlines():
    if line.startswith("REPUTATION_AGENT_ADMIN_TOKEN="):
        token = line.split("=", 1)[1].strip()
        break
if not token:
    raise SystemExit("missing generated token")
with urlopen(f"http://127.0.0.1:5000/admin?token={token}", timeout=10) as response:
    if response.status != 200:
        raise SystemExit(f"unexpected admin status {response.status}")
PY

  log "Checking OpenClaw CLI imports and tool registry."
  (
    cd "${AKA_DIR}"
    "${AKA_DIR}/.venv/bin/python" -m openclaw_adapter list-tools >/tmp/openclaw-tools.txt
  )
  grep -Fq "tcg.lookup-card" /tmp/openclaw-tools.txt || fail "OpenClaw tool registry did not load expected tools."

  wait_for_openclaw_log || log "OpenClaw Telegram log did not appear before stop; fake token may have exited early."

  log "Realistic setup smoke complete. Live restarts are covered by /restartall tests."
}

main() {
  stage_workspace
  run_realistic_stack
  log "Realistic Mac mini Docker compatibility test passed."
}

main "$@"
