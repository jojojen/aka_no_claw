#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_WORKSPACE="${SOURCE_WORKSPACE:-/source}"
SOURCE_AKA="${SOURCE_WORKSPACE}/aka_no_claw"
WORKSPACE_DIR="${WORKSPACE_DIR:-/tmp/mac-mini-smoke-workspace}"
FAKE_BIN="${FAKE_BIN:-/tmp/mac-mini-fake-bin}"
REAL_PYTHON="$(command -v python || command -v python3)"
LOG_DIR="${WORKSPACE_DIR}/logs"

log() {
  printf '[mac-mini-docker-smoke] %s\n' "$*"
}

fail() {
  printf '[mac-mini-docker-smoke] ERROR: %s\n' "$*" >&2
  exit 1
}

assert_file_contains() {
  local file_path="$1"
  local expected="$2"
  if ! grep -Fq "${expected}" "${file_path}"; then
    fail "Expected ${file_path} to contain: ${expected}"
  fi
}

write_fake_commands() {
  rm -rf "${FAKE_BIN}"
  mkdir -p "${FAKE_BIN}"

  cat > "${FAKE_BIN}/brew" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

printf 'brew %s\n' "$*" >> "${MAC_MINI_SMOKE_LOG_DIR}/brew.log"
case "${1:-}" in
  --prefix)
    if [[ "${2:-}" == "tesseract" ]]; then
      printf '/opt/homebrew/opt/tesseract\n'
    else
      printf '/opt/homebrew\n'
    fi
    ;;
  list)
    exit 1
    ;;
esac
exit 0
EOF

  cat > "${FAKE_BIN}/tesseract" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF

  cat > "${FAKE_BIN}/curl" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

printf 'curl %s\n' "$*" >> "${MAC_MINI_SMOKE_LOG_DIR}/curl.log"
url="${@: -1}"
if [[ "${url}" == */api/tags ]]; then
  [[ -f "${MAC_MINI_SMOKE_LOG_DIR}/ollama-ready" ]]
  exit
fi
exit 0
EOF

  cat > "${FAKE_BIN}/ollama" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail
printf 'ollama %s\n' "$*" >> "${MAC_MINI_SMOKE_LOG_DIR}/ollama.log"
case "${1:-}" in
  serve)
    touch "${MAC_MINI_SMOKE_LOG_DIR}/ollama-ready"
    while true; do sleep 60; done
    ;;
  pull)
    exit 0
    ;;
esac
exit 0
EOF

  cat > "${FAKE_BIN}/venv-python" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

printf 'venv-python %s\n' "$*" >> "${MAC_MINI_SMOKE_LOG_DIR}/python.log"

if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "playwright" ]]; then
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "openclaw_adapter" ]]; then
  touch "${MAC_MINI_SMOKE_LOG_DIR}/openclaw-started"
  while true; do sleep 60; done
fi
if [[ "${1:-}" == "-" ]]; then
  exit 0
fi

for arg in "$@"; do
  case "${arg}" in
    scripts/init_db.py|*/scripts/init_db.py)
      mkdir -p instance
      touch instance/app.db
      exit 0
      ;;
    scripts/generate_keys.py|*/scripts/generate_keys.py)
      mkdir -p keys
      touch keys/ed25519_private_key.pem
      exit 0
      ;;
    app.py)
      touch "${MAC_MINI_SMOKE_LOG_DIR}/reputation-started"
      while true; do sleep 60; done
      ;;
  esac
done

exec "${REAL_PYTHON}" "$@"
EOF

  cat > "${FAKE_BIN}/python3" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

printf 'python3 %s\n' "$*" >> "${MAC_MINI_SMOKE_LOG_DIR}/python.log"

if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  venv_dir="${3:?missing venv path}"
  mkdir -p "${venv_dir}/bin"
  cp "${FAKE_BIN}/venv-python" "${venv_dir}/bin/python"
  chmod +x "${venv_dir}/bin/python"
  exit 0
fi

exec "${REAL_PYTHON}" "$@"
EOF

  chmod +x "${FAKE_BIN}"/*
}

stage_workspace() {
  rm -rf "${WORKSPACE_DIR}"
  mkdir -p \
    "${WORKSPACE_DIR}/aka_no_claw" \
    "${WORKSPACE_DIR}/price_monitor_bot" \
    "${WORKSPACE_DIR}/reputation_snapshot/scripts" \
    "${LOG_DIR}"

  cp "${SOURCE_AKA}/start-mac-mini-stack.command" "${WORKSPACE_DIR}/aka_no_claw/start-mac-mini-stack.command"
  cp "${SOURCE_AKA}/stop-mac-mini-stack.command" "${WORKSPACE_DIR}/aka_no_claw/stop-mac-mini-stack.command"
  cp "${SOURCE_AKA}/.env.example" "${WORKSPACE_DIR}/aka_no_claw/.env.example"
  chmod +x "${WORKSPACE_DIR}/aka_no_claw/start-mac-mini-stack.command" "${WORKSPACE_DIR}/aka_no_claw/stop-mac-mini-stack.command"

  cat > "${WORKSPACE_DIR}/aka_no_claw/.env" <<'EOF'
OPENCLAW_TELEGRAM_BOT_TOKEN=fake-token
OPENCLAW_TELEGRAM_CHAT_ID=123456
OPENCLAW_TESSERACT_PATH=C:\Program Files\Tesseract-OCR\tesseract.exe
OPENCLAW_TESSDATA_DIR=C:\AI_Related\openclaw\tessdata
OPENCLAW_LOCAL_TEXT_BACKEND=
OPENCLAW_LOCAL_TEXT_MODEL=
OPENCLAW_LOCAL_TEXT_ENDPOINT=http://127.0.0.1:11434
OPENCLAW_LOCAL_VISION_BACKEND=ollama
OPENCLAW_LOCAL_VISION_MODEL=qwen2.5vl:7b
OPENCLAW_LOCAL_VISION_ENDPOINT=http://127.0.0.1:11434
REPUTATION_AGENT_SERVER_URL=http://127.0.0.1:5000
REPUTATION_AGENT_POLL_SECS=1
EOF

  cat > "${WORKSPACE_DIR}/aka_no_claw/requirements-dev.txt" <<'EOF'
-r requirements.txt
pytest==9.0.3
EOF
  cat > "${WORKSPACE_DIR}/aka_no_claw/requirements.txt" <<'EOF'
-e ../price_monitor_bot
-e .
EOF
  cat > "${WORKSPACE_DIR}/reputation_snapshot/requirements.txt" <<'EOF'
Flask==3.1.0
playwright==1.52.0
PyNaCl==1.5.0
EOF

  mkdir -p "${WORKSPACE_DIR}/aka_no_claw/.venv/Scripts" "${WORKSPACE_DIR}/reputation_snapshot/.venv/Scripts"
  touch "${WORKSPACE_DIR}/aka_no_claw/.venv/Scripts/python.exe"
  touch "${WORKSPACE_DIR}/reputation_snapshot/.venv/Scripts/python.exe"
}

run_smoke() {
  export FAKE_BIN
  export REAL_PYTHON
  export MAC_MINI_SMOKE_LOG_DIR="${LOG_DIR}"
  export PATH="${FAKE_BIN}:${PATH}"

  log "Running shell syntax checks."
  bash -n "${WORKSPACE_DIR}/aka_no_claw/start-mac-mini-stack.command"
  bash -n "${WORKSPACE_DIR}/aka_no_claw/stop-mac-mini-stack.command"

  log "Running mocked Mac mini stack start with Ollama setup enabled."
  (
    cd "${WORKSPACE_DIR}/aka_no_claw"
    MACOS_DOCKER_SIMULATE=1 \
      AUTO_INSTALL_SYSTEM_DEPS=1 \
      CLEAN_COPIED_RUNTIME=1 \
      SETUP_OLLAMA=1 \
      SETUP_OLLAMA_VISION=0 \
      START_NOTIFY=1 \
      ./start-mac-mini-stack.command
  ) | tee "${LOG_DIR}/start.log"

  [[ -f "${WORKSPACE_DIR}/aka_no_claw/run/mac-mini-stack.pid" ]] || fail "PID file was not created."
  [[ ! -d "${WORKSPACE_DIR}/aka_no_claw/.venv/Scripts" ]] || fail "Copied Windows aka_no_claw venv was not removed."
  [[ ! -d "${WORKSPACE_DIR}/reputation_snapshot/.venv/Scripts" ]] || fail "Copied Windows reputation_snapshot venv was not removed."

  assert_file_contains "${LOG_DIR}/start.log" "Environment:"
  assert_file_contains "${LOG_DIR}/start.log" "Ollama text model default: qwen3:4b"
  assert_file_contains "${LOG_DIR}/start.log" "Installing macOS runtime dependencies with Homebrew"
  assert_file_contains "${LOG_DIR}/start.log" "Ensuring Ollama model is available: qwen3:4b"
  assert_file_contains "${LOG_DIR}/start.log" "Generated REPUTATION_AGENT_ADMIN_TOKEN"
  assert_file_contains "${WORKSPACE_DIR}/aka_no_claw/.env" "REPUTATION_AGENT_ADMIN_TOKEN="
  assert_file_contains "${WORKSPACE_DIR}/reputation_snapshot/.env" "ADMIN_TOKEN="
  assert_file_contains "${WORKSPACE_DIR}/reputation_snapshot/.env" "APP_HOST=127.0.0.1"
  assert_file_contains "${WORKSPACE_DIR}/reputation_snapshot/.env" "APP_PORT=5000"
  assert_file_contains "${LOG_DIR}/brew.log" "brew install python@3.12"
  assert_file_contains "${LOG_DIR}/brew.log" "brew install tesseract"
  assert_file_contains "${LOG_DIR}/ollama.log" "ollama pull qwen3:4b"

  log "Running mocked stack stop."
  (
    cd "${WORKSPACE_DIR}/aka_no_claw"
    ./stop-mac-mini-stack.command
  ) | tee "${LOG_DIR}/stop.log"

  [[ ! -f "${WORKSPACE_DIR}/aka_no_claw/run/mac-mini-stack.pid" ]] || fail "PID file was not removed by stop script."
  assert_file_contains "${LOG_DIR}/stop.log" "Stopped."
}

main() {
  [[ -f "${SOURCE_AKA}/start-mac-mini-stack.command" ]] || fail "Source aka_no_claw not mounted at ${SOURCE_AKA}"
  write_fake_commands
  stage_workspace
  run_smoke
  log "Mac mini Docker smoke test passed."
}

main "$@"
