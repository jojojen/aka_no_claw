#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
AKA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PRICE_DIR="${WORKSPACE_DIR}/price_monitor_bot"
REPUTATION_DIR="${WORKSPACE_DIR}/reputation_snapshot"

PYTHON_BIN="${PYTHON_BIN:-python3}"
HOST="${REPUTATION_HOST:-127.0.0.1}"
PORT="${REPUTATION_PORT:-5000}"
AUTO_INSTALL_SYSTEM_DEPS="${AUTO_INSTALL_SYSTEM_DEPS:-1}"
INSTALL_SYSTEM_DEPS="${INSTALL_SYSTEM_DEPS:-${AUTO_INSTALL_SYSTEM_DEPS}}"
START_NOTIFY="${START_NOTIFY:-0}"
CLEAN_COPIED_RUNTIME="${CLEAN_COPIED_RUNTIME:-1}"
AUTO_DISABLE_UNAVAILABLE_LOCAL_AI="${AUTO_DISABLE_UNAVAILABLE_LOCAL_AI:-1}"
SETUP_OLLAMA="${SETUP_OLLAMA:-0}"
SETUP_OLLAMA_VISION="${SETUP_OLLAMA_VISION:-0}"
OLLAMA_DEFAULT_TEXT_MODEL="${OLLAMA_DEFAULT_TEXT_MODEL:-}"
MACOS_DOCKER_SIMULATE="${MACOS_DOCKER_SIMULATE:-0}"
MAC_REQUIRE_APPLE_SILICON="${MAC_REQUIRE_APPLE_SILICON:-0}"

AKA_VENV="${AKA_DIR}/.venv"
REPUTATION_VENV="${REPUTATION_DIR}/.venv"
RUN_DIR="${AKA_DIR}/run"
LOG_DIR="${AKA_DIR}/logs"
PID_FILE="${RUN_DIR}/mac-mini-stack.pid"
RUNTIME_ENV_FILE="${RUN_DIR}/mac-mini-stack.env"
LAUNCHCTL_OLLAMA_LABEL="local.openclaw.ollama"
LAUNCHCTL_REPUTATION_LABEL="local.openclaw.reputation"
LAUNCHCTL_TELEGRAM_LABEL="local.openclaw.telegram"
LAUNCHCTL_OPPORTUNITY_LABEL="local.openclaw.opportunity"
LAUNCHCTL_SNS_MONITOR_LABEL="local.openclaw.sns_monitor"
LAUNCHCTL_PRICE_MONITOR_LABEL="local.openclaw.price_monitor"
LAUNCHCTL_CHAT_WEB_LABEL="local.openclaw.chat_web"
LAUNCHCTL_AIVIS_LABEL="local.openclaw.aivis"

AIVIS_HOST="${AIVIS_HOST:-127.0.0.1}"
AIVIS_PORT="${AIVIS_PORT:-10101}"
AIVIS_APP_PATH="${AIVIS_APP_PATH:-${HOME}/Applications/AivisSpeech.app}"
AIVIS_ENGINE_RUN="${AIVIS_APP_PATH}/Contents/Resources/AivisSpeech-Engine/run"
AIVIS_READY_TIMEOUT_SECONDS="${AIVIS_READY_TIMEOUT_SECONDS:-60}"

# Bind on all interfaces so the user's phone can reach the page over the mesh
# VPN; access is still restricted to loopback + meshnet (100.64.0.0/10) by the
# chat-web app-layer guard, so LAN/public clients get 403.
CHAT_WEB_HOST="${CHAT_WEB_HOST:-0.0.0.0}"
CHAT_WEB_PORT="${CHAT_WEB_PORT:-8780}"

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

log() {
  printf '[mac-mini-stack] %s\n' "$*"
}

fail() {
  printf '[mac-mini-stack] ERROR: %s\n' "$*" >&2
  exit 1
}

require_dir() {
  local path="$1"
  local label="$2"
  if [[ ! -d "${path}" ]]; then
    fail "${label} not found at ${path}. Keep aka_no_claw, price_monitor_bot, and reputation_snapshot under the same parent directory."
  fi
}

have_command() {
  command -v "$1" >/dev/null 2>&1
}

is_darwin() {
  [[ "$(uname -s)" == "Darwin" ]]
}

is_macos_runtime() {
  is_darwin || [[ "${MACOS_DOCKER_SIMULATE}" == "1" ]]
}

use_launchctl_services() {
  is_darwin && [[ "${MACOS_DOCKER_SIMULATE}" != "1" ]] && have_command launchctl
}

sudo_if_needed() {
  if [[ "$(id -u)" == "0" ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

detect_os_pretty_name() {
  if is_darwin && have_command sw_vers; then
    printf 'macOS %s (%s)\n' "$(sw_vers -productVersion)" "$(sw_vers -buildVersion)"
    return
  fi
  if [[ "${MACOS_DOCKER_SIMULATE}" == "1" ]]; then
    printf 'macOS Docker simulation on %s\n' "$(uname -s)"
    return
  fi
  if [[ -r /etc/os-release ]]; then
    . /etc/os-release
    printf '%s\n' "${PRETTY_NAME:-${NAME:-unknown}}"
    return
  fi
  uname -s
}

detect_total_memory_mib() {
  if have_command sysctl; then
    local bytes
    bytes="$(sysctl -n hw.memsize 2>/dev/null || true)"
    if [[ "${bytes}" =~ ^[0-9]+$ ]]; then
      printf '%d\n' "$((bytes / 1024 / 1024))"
      return
    fi
  fi
  awk '/MemTotal:/ { printf "%d\n", int($2 / 1024); found = 1 } END { if (!found) print 0 }' /proc/meminfo 2>/dev/null || printf '0\n'
}

detect_machine_model() {
  if have_command sysctl; then
    sysctl -n hw.model 2>/dev/null && return
  fi
  printf 'unknown\n'
}

detect_cpu_brand() {
  if have_command sysctl; then
    sysctl -n machdep.cpu.brand_string 2>/dev/null && return
  fi
  printf 'unknown\n'
}

choose_ollama_default_text_model() {
  if [[ -n "${OLLAMA_DEFAULT_TEXT_MODEL}" ]]; then
    return
  fi
  OLLAMA_DEFAULT_TEXT_MODEL="qwen3:4b"
}

detect_runtime_environment() {
  local os_name
  local arch
  local model
  local cpu_brand
  local memory_mib
  local docker_hint=""

  os_name="$(detect_os_pretty_name)"
  arch="$(uname -m)"
  model="$(detect_machine_model)"
  cpu_brand="$(detect_cpu_brand)"
  memory_mib="$(detect_total_memory_mib)"
  choose_ollama_default_text_model

  if [[ -f /.dockerenv || "${MACOS_DOCKER_SIMULATE}" == "1" ]]; then
    docker_hint=" docker=1"
  fi

  log "Environment: os=${os_name} arch=${arch} memory=${memory_mib}MiB model=${model} cpu=${cpu_brand}${docker_hint}"
  log "Ollama text model default: ${OLLAMA_DEFAULT_TEXT_MODEL}"

  if ! is_macos_runtime; then
    fail "This launcher is for macOS. Set MACOS_DOCKER_SIMULATE=1 only inside the Docker compatibility tests."
  fi

  case "${arch}" in
    arm64|aarch64)
      log "Detected Apple Silicon-compatible architecture."
      ;;
    *)
      if [[ "${MAC_REQUIRE_APPLE_SILICON}" == "1" ]]; then
        fail "MAC_REQUIRE_APPLE_SILICON=1 but architecture is ${arch}."
      fi
      log "This does not look like Apple Silicon; continuing because MAC_REQUIRE_APPLE_SILICON=${MAC_REQUIRE_APPLE_SILICON}."
      ;;
  esac
}

remove_path() {
  local path="$1"
  if [[ -e "${path}" ]]; then
    log "Removing copied runtime artifact: ${path}"
    rm -rf "${path}"
  fi
}

venv_python_is_compatible() {
  local python_path="$1"
  [[ -x "${python_path}" ]] || return 1
  "${python_path}" - <<'PY' >/dev/null 2>&1
import sys

raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
}

cleanup_incompatible_venv() {
  local venv_dir="$1"
  local label="$2"
  if [[ ! -d "${venv_dir}" ]]; then
    return
  fi
  if venv_python_is_compatible "${venv_dir}/bin/python"; then
    return
  fi
  log "Removing incompatible ${label} virtualenv copied from another platform: ${venv_dir}"
  rm -rf "${venv_dir}"
}

cleanup_copied_runtime_artifacts() {
  if [[ "${CLEAN_COPIED_RUNTIME}" != "1" ]]; then
    log "Skipping copied-runtime cleanup because CLEAN_COPIED_RUNTIME=${CLEAN_COPIED_RUNTIME}."
    return
  fi

  cleanup_incompatible_venv "${AKA_DIR}/.venv" "aka_no_claw"
  cleanup_incompatible_venv "${REPUTATION_DIR}/.venv" "reputation_snapshot"
  cleanup_incompatible_venv "${PRICE_DIR}/.venv" "price_monitor_bot"

  remove_path "${RUN_DIR}/mac-mini-stack.pid"
  remove_path "${AKA_DIR}/.pytest_cache"
  remove_path "${REPUTATION_DIR}/.pytest_cache"
  remove_path "${PRICE_DIR}/.pytest_cache"
}

install_homebrew_if_needed() {
  if have_command brew; then
    return
  fi
  if [[ "${MACOS_DOCKER_SIMULATE}" == "1" ]]; then
    log "Homebrew is not available in Docker simulation; skipping system package installation."
    return 1
  fi
  if [[ "${AUTO_INSTALL_SYSTEM_DEPS}" != "1" ]]; then
    fail "Homebrew is required for automatic macOS dependency setup. Install Homebrew or run with AUTO_INSTALL_SYSTEM_DEPS=0 after preparing dependencies manually."
  fi
  if ! have_command curl; then
    fail "curl is required to install Homebrew."
  fi

  log "Installing Homebrew; the installer may ask for your password..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  if [[ -x /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  elif [[ -x /usr/local/bin/brew ]]; then
    eval "$(/usr/local/bin/brew shellenv)"
  fi
  have_command brew || fail "Homebrew installation finished but brew is still not on PATH."
}

install_system_packages() {
  if [[ "${AUTO_INSTALL_SYSTEM_DEPS}" != "1" ]]; then
    log "Skipping Homebrew package installation because AUTO_INSTALL_SYSTEM_DEPS=${AUTO_INSTALL_SYSTEM_DEPS}."
    return
  fi
  install_homebrew_if_needed || return

  log "Installing macOS runtime dependencies with Homebrew..."
  brew update
  brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
  brew list tesseract >/dev/null 2>&1 || brew install tesseract
}

python_is_compatible() {
  local candidate="$1"
  "${candidate}" - <<'PY' >/dev/null 2>&1
import sys

raise SystemExit(0 if sys.version_info >= (3, 12) else 1)
PY
}

ensure_python() {
  if have_command "${PYTHON_BIN}" && python_is_compatible "${PYTHON_BIN}"; then
    log "Using Python: $("${PYTHON_BIN}" -c 'import sys; print(sys.executable, sys.version.split()[0])')"
    return
  fi
  if have_command python3.12 && python_is_compatible python3.12; then
    PYTHON_BIN="python3.12"
    log "Using Python: $(python3.12 -c 'import sys; print(sys.executable, sys.version.split()[0])')"
    return
  fi
  if is_darwin && have_command brew; then
    brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
    for candidate in /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12; do
      if [[ -x "${candidate}" ]] && python_is_compatible "${candidate}"; then
        PYTHON_BIN="${candidate}"
        log "Using Python: $("${PYTHON_BIN}" -c 'import sys; print(sys.executable, sys.version.split()[0])')"
        return
      fi
    done
  fi
  fail "Could not find or prepare a Python 3.12+ interpreter."
}

port_is_available() {
  local host="$1"
  local port="$2"
  "${PYTHON_BIN}" - "${host}" "${port}" <<'PY' >/dev/null 2>&1
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.settimeout(0.5)
    if sock.connect_ex((host, port)) == 0:
        raise SystemExit(1)

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    try:
        sock.bind((host, port))
    except OSError:
        raise SystemExit(1)
PY
}

choose_reputation_port() {
  if port_is_available "${HOST}" "${PORT}"; then
    return
  fi

  if [[ -n "${REPUTATION_PORT:-}" ]]; then
    fail "REPUTATION_PORT=${PORT} is already in use on ${HOST}. Choose a free REPUTATION_PORT and retry."
  fi

  local candidate
  for candidate in 5055 5056 5057 8765; do
    if port_is_available "${HOST}" "${candidate}"; then
      log "Port ${PORT} is already in use on ${HOST}; using REPUTATION_PORT=${candidate} for this run."
      PORT="${candidate}"
      return
    fi
  done

  fail "No free local reputation_snapshot port found. Set REPUTATION_PORT to an available port and retry."
}

ensure_venv() {
  local venv_dir="$1"
  local project_name="$2"
  if [[ ! -x "${venv_dir}/bin/python" ]]; then
    log "Creating ${project_name} virtual environment..."
    "${PYTHON_BIN}" -m venv "${venv_dir}"
  fi
}

find_system_chromium() {
  local candidate
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "/Applications/Chromium.app/Contents/MacOS/Chromium" \
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"; do
    if [[ -x "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  for candidate in chromium chromium-browser google-chrome-stable; do
    if have_command "${candidate}"; then
      command -v "${candidate}"
      return 0
    fi
  done
  return 1
}

install_reputation_snapshot() {
  log "Installing reputation_snapshot dependencies..."
  ensure_venv "${REPUTATION_VENV}" "reputation_snapshot"
  "${REPUTATION_VENV}/bin/python" -m pip install --upgrade pip
  "${REPUTATION_VENV}/bin/python" -m pip install -r "${REPUTATION_DIR}/requirements.txt"
  if [[ "${INSTALL_SYSTEM_DEPS}" == "1" && "$(uname -s)" == "Linux" ]]; then
    "${REPUTATION_VENV}/bin/python" -m playwright install-deps chromium || log "Playwright install-deps failed; continuing with available system dependencies."
  fi
  if find_system_chromium >/dev/null; then
    log "Using system Chromium at $(find_system_chromium)."
  else
    "${REPUTATION_VENV}/bin/python" -m playwright install chromium
  fi
}

install_openclaw() {
  log "Installing price_monitor_bot and aka_no_claw dependencies..."
  ensure_venv "${AKA_VENV}" "aka_no_claw"
  "${AKA_VENV}/bin/python" -m pip install --upgrade pip
  (
    cd "${AKA_DIR}"
    "${AKA_VENV}/bin/python" -m pip install -r requirements-dev.txt
  )
  if find_system_chromium >/dev/null; then
    log "Using system Chromium at $(find_system_chromium)."
  else
    "${AKA_VENV}/bin/python" -m playwright install chromium
  fi
}

copy_env_if_missing() {
  local env_path="$1"
  local example_path="$2"
  local label="$3"
  if [[ ! -f "${env_path}" && -f "${example_path}" ]]; then
    cp "${example_path}" "${env_path}"
    chmod 600 "${env_path}" || true
    log "Created ${label} .env from .env.example."
  fi
}

get_env_value() {
  local file_path="$1"
  local key="$2"
  if [[ ! -f "${file_path}" ]]; then
    return 0
  fi
  awk -F= -v key="${key}" '
    $1 == key {
      sub(/^[^=]*=/, "")
      gsub(/\r$/, "")
      gsub(/^["'"'"']|["'"'"']$/, "")
      print
      exit
    }
  ' "${file_path}"
}

backup_file_once() {
  local file_path="$1"
  if [[ ! -f "${file_path}" ]]; then
    return
  fi
  local backup_path="${file_path}.mac-backup"
  if [[ ! -f "${backup_path}" ]]; then
    cp "${file_path}" "${backup_path}"
    log "Backed up ${file_path} to ${backup_path}."
  fi
}

set_env_value() {
  local file_path="$1"
  local key="$2"
  local value="$3"
  local tmp_path="${file_path}.tmp.$$"

  if [[ -f "${file_path}" ]] && grep -q -E "^${key}=" "${file_path}"; then
    awk -v key="${key}" -v value="${value}" '
      BEGIN { FS = OFS = "=" }
      $1 == key { print key "=" value; next }
      { print }
    ' "${file_path}" > "${tmp_path}"
    mv "${tmp_path}" "${file_path}"
  else
    if [[ -s "${file_path}" ]] && [[ "$(tail -c 1 "${file_path}")" != "" ]]; then
      printf '\n' >> "${file_path}"
    fi
    printf '%s=%s\n' "${key}" "${value}" >> "${file_path}"
  fi
}

have_tty() {
  [[ -r /dev/tty && -w /dev/tty ]]
}

generate_admin_token() {
  if have_command openssl; then
    openssl rand -hex 32
    return
  fi
  "${PYTHON_BIN}" - <<'PY'
import secrets

print(secrets.token_hex(32))
PY
}

ensure_prompted_env_value() {
  local file_path="$1"
  local key="$2"
  local prompt="$3"
  local secret="${4:-0}"
  local value

  value="$(get_env_value "${file_path}" "${key}")"
  if [[ -n "${value}" ]]; then
    return
  fi

  value="${!key:-}"
  if [[ -n "${value}" ]]; then
    set_env_value "${file_path}" "${key}" "${value}"
    log "Wrote ${key} to ${file_path} from the current shell environment."
    return
  fi

  if ! have_tty; then
    fail "${key} is empty in ${file_path}. Run this launcher interactively once, or export ${key}=... before starting."
  fi

  while [[ -z "${value}" ]]; do
    if [[ "${secret}" == "1" ]]; then
      printf '%s: ' "${prompt}" > /dev/tty
      IFS= read -r -s value < /dev/tty
      printf '\n' > /dev/tty
    else
      printf '%s: ' "${prompt}" > /dev/tty
      IFS= read -r value < /dev/tty
    fi
    if [[ -z "${value}" ]]; then
      printf 'Value is required.\n' > /dev/tty
    fi
  done

  set_env_value "${file_path}" "${key}" "${value}"
  log "Saved ${key} to ${file_path}."
}

ensure_default_env_value() {
  local file_path="$1"
  local key="$2"
  local value="$3"

  if [[ -z "$(get_env_value "${file_path}" "${key}")" ]]; then
    set_env_value "${file_path}" "${key}" "${value}"
    log "Set ${key}=${value} in ${file_path}."
  fi
}

ensure_admin_token_env_value() {
  local file_path="$1"
  local key="REPUTATION_AGENT_ADMIN_TOKEN"
  local value

  if [[ -n "$(get_env_value "${file_path}" "${key}")" ]]; then
    return
  fi

  value="${REPUTATION_AGENT_ADMIN_TOKEN:-}"
  if [[ -z "${value}" ]]; then
    value="$(generate_admin_token)"
    log "Generated ${key} for the local reputation_snapshot service."
  else
    log "Using ${key} from the current shell environment."
  fi

  set_env_value "${file_path}" "${key}" "${value}"
}

configure_aka_env() {
  local env_path="${AKA_DIR}/.env"

  copy_env_if_missing "${env_path}" "${AKA_DIR}/.env.example" "aka_no_claw"
  chmod 600 "${env_path}" || true

  log "Checking ${env_path} for required Mac mini runtime values..."
  ensure_prompted_env_value "${env_path}" "OPENCLAW_TELEGRAM_BOT_TOKEN" "Telegram bot token from @BotFather" "1"
  ensure_prompted_env_value "${env_path}" "OPENCLAW_TELEGRAM_CHAT_ID" "Telegram chat id to send and receive OpenClaw messages"
  ensure_admin_token_env_value "${env_path}"
  ensure_default_env_value "${env_path}" "OPENCLAW_LOCAL_TEXT_BACKEND" "ollama"
  ensure_default_env_value "${env_path}" "OPENCLAW_LOCAL_TEXT_ENDPOINT" "http://127.0.0.1:11434"
  ensure_default_env_value "${env_path}" "OPENCLAW_LOCAL_TEXT_MODEL" "${OLLAMA_DEFAULT_TEXT_MODEL}"
  ensure_default_env_value "${env_path}" "OPENCLAW_LOCAL_TEXT_TIMEOUT_SECONDS" "75"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_AGENT_ENABLED" "1"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_DB_PATH" "data/opportunities.sqlite3"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_INTERVAL_SECONDS" "900"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_LLM_TIMEOUT_SECONDS" "180"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_SNS_LOOKBACK_HOURS" "24"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_CANDIDATE_LIMIT" "4"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_LISTING_LIMIT" "5"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_CANDIDATE_CHECK_INTERVAL_SECONDS" "1800"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_MIN_HEAT_SCORE" "70"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_MAX_PRICE_RATIO" "0.85"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_MIN_PRICE_CONFIDENCE" "0.60"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_MIN_TOTAL_REVIEWS" "30"
  ensure_default_env_value "${env_path}" "OPENCLAW_OPPORTUNITY_MIN_POSITIVE_RATE" "97"
  set_env_value "${env_path}" "REPUTATION_AGENT_SERVER_URL" "http://${HOST}:${PORT}"
  ensure_default_env_value "${env_path}" "REPUTATION_AGENT_POLL_SECS" "5"
}

sync_reputation_env() {
  local admin_token="$1"
  local env_path="${REPUTATION_DIR}/.env"

  if [[ ! -f "${env_path}" ]]; then
    cat > "${env_path}" <<EOF
APP_HOST=${HOST}
APP_PORT=${PORT}
DB_PATH=instance/app.db
DEFAULT_EXPIRES_DAYS=30
PARSER_VERSION=mercari_parser_v0
ENV=local
ADMIN_TOKEN=${admin_token}
EOF
    chmod 600 "${env_path}" || true
    log "Created reputation_snapshot .env and synced ADMIN_TOKEN from aka_no_claw."
    return
  fi

  backup_file_once "${env_path}"
  set_env_value "${env_path}" "APP_HOST" "${HOST}"
  set_env_value "${env_path}" "APP_PORT" "${PORT}"
  set_env_value "${env_path}" "ADMIN_TOKEN" "${admin_token}"
  if [[ -z "$(get_env_value "${env_path}" "DB_PATH")" ]]; then
    set_env_value "${env_path}" "DB_PATH" "instance/app.db"
  fi
  if [[ -z "$(get_env_value "${env_path}" "DEFAULT_EXPIRES_DAYS")" ]]; then
    set_env_value "${env_path}" "DEFAULT_EXPIRES_DAYS" "30"
  fi
  if [[ -z "$(get_env_value "${env_path}" "PARSER_VERSION")" ]]; then
    set_env_value "${env_path}" "PARSER_VERSION" "mercari_parser_v0"
  fi
  if [[ -z "$(get_env_value "${env_path}" "ENV")" ]]; then
    set_env_value "${env_path}" "ENV" "local"
  fi
  log "Synced reputation_snapshot .env host/port/admin token for this Mac run."
}

is_windows_path() {
  local value="$1"
  [[ "${value}" =~ ^[A-Za-z]:[\\/].* || "${value}" == *"\\"* ]]
}

find_tessdata_dir() {
  local candidate
  if have_command brew; then
    candidate="$(brew --prefix tesseract 2>/dev/null || true)"
    if [[ -n "${candidate}" && -d "${candidate}/share/tessdata" ]]; then
      printf '%s\n' "${candidate}/share/tessdata"
      return 0
    fi
  fi
  for candidate in \
    /opt/homebrew/share/tessdata \
    /usr/local/share/tessdata \
    /usr/share/tesseract-ocr/5/tessdata \
    /usr/share/tessdata; do
    if [[ -d "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

ollama_endpoint_ready() {
  local endpoint="${1:-http://127.0.0.1:11434}"
  if have_command curl; then
    curl -fsS "${endpoint%/}/api/tags" >/dev/null 2>&1 && return 0
  fi
  url_available "${endpoint%/}/api/tags"
}

start_ollama_if_available() {
  if ! have_command ollama; then
    return 1
  fi
  if ollama_endpoint_ready "http://127.0.0.1:11434"; then
    return 0
  fi
  if use_launchctl_services; then
    if launchctl_job_exists "${LAUNCHCTL_OLLAMA_LABEL}"; then
      launchctl remove "${LAUNCHCTL_OLLAMA_LABEL}" >/dev/null 2>&1 || true
    fi
    launchctl submit -l "${LAUNCHCTL_OLLAMA_LABEL}" \
      -o "${LOG_DIR}/ollama.log" \
      -e "${LOG_DIR}/ollama.log" \
      -- /bin/bash -lc 'exec ollama serve'
    local pid
    pid="$(launchctl_job_pid "${LAUNCHCTL_OLLAMA_LABEL}")"
    [[ -n "${pid}" ]] && echo "${pid}" >> "${PID_FILE}"
  else
    nohup ollama serve >> "${LOG_DIR}/ollama.log" 2>&1 &
    echo $! >> "${PID_FILE}"
  fi
  for _ in $(seq 1 20); do
    if ollama_endpoint_ready "http://127.0.0.1:11434"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

aivis_endpoint_ready() {
  curl -fsS -m 3 "http://${AIVIS_HOST}:${AIVIS_PORT}/version" >/dev/null 2>&1
}

start_aivis_engine() {
  # AivisSpeech is the preferred /quiz vocab-card TTS; the bot falls back to
  # macOS `say` (Kyoko) only when this engine is unreachable. Keep it supervised
  # so cards always get the AivisSpeech voice. TTS is non-critical: never fail the
  # whole stack here, just warn and let the Kyoko fallback cover the gap.
  if aivis_endpoint_ready; then
    log "AivisSpeech already serving on ${AIVIS_HOST}:${AIVIS_PORT}."
    return 0
  fi
  if [[ ! -x "${AIVIS_ENGINE_RUN}" ]]; then
    log "AivisSpeech engine binary not found at ${AIVIS_ENGINE_RUN}; skipping (run launchers/install-aivis-speech.command to install). /quiz audio will use macOS Kyoko."
    return 0
  fi
  if ! have_command curl; then
    log "curl not available to probe AivisSpeech; skipping engine supervision."
    return 0
  fi

  log "Starting AivisSpeech engine on ${AIVIS_HOST}:${AIVIS_PORT}..."
  if use_launchctl_services; then
    if launchctl_job_exists "${LAUNCHCTL_AIVIS_LABEL}"; then
      launchctl remove "${LAUNCHCTL_AIVIS_LABEL}" >/dev/null 2>&1 || true
    fi
    launchctl submit -l "${LAUNCHCTL_AIVIS_LABEL}" \
      -o "${LOG_DIR}/aivis_speech.log" \
      -e "${LOG_DIR}/aivis_speech.log" \
      -- "${AIVIS_ENGINE_RUN}" --host "${AIVIS_HOST}" --port "${AIVIS_PORT}" --output_log_utf8
    local pid
    pid="$(launchctl_job_pid "${LAUNCHCTL_AIVIS_LABEL}")"
    [[ -n "${pid}" ]] && echo "${pid}" >> "${PID_FILE}"
  else
    nohup "${AIVIS_ENGINE_RUN}" --host "${AIVIS_HOST}" --port "${AIVIS_PORT}" --output_log_utf8 \
      >> "${LOG_DIR}/aivis_speech.log" 2>&1 &
    echo $! >> "${PID_FILE}"
  fi

  local waited=0
  while (( waited < AIVIS_READY_TIMEOUT_SECONDS )); do
    if aivis_endpoint_ready; then
      log "AivisSpeech is ready on ${AIVIS_HOST}:${AIVIS_PORT}."
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  log "AivisSpeech did not become ready within ${AIVIS_READY_TIMEOUT_SECONDS}s; /quiz audio will fall back to macOS Kyoko until it comes up. See ${LOG_DIR}/aivis_speech.log"
  return 0
}

model_list_from_env() {
  local raw="$1"
  local fallback="$2"
  if [[ -z "${raw}" ]]; then
    raw="${fallback}"
  fi
  printf '%s\n' "${raw}" | tr ',' '\n' | awk '{ gsub(/^[ \t]+|[ \t]+$/, ""); if ($0 != "") print }'
}

configured_ollama_required() {
  local text_backend
  local vision_backend
  text_backend="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_TEXT_BACKEND")"
  vision_backend="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_VISION_BACKEND")"
  [[ "${text_backend}" == "ollama" || "${vision_backend}" == "ollama" ]]
}

ensure_ollama_installed_if_needed() {
  if have_command ollama; then
    return
  fi
  if [[ "${MACOS_DOCKER_SIMULATE}" == "1" ]]; then
    fail "Ollama is required by the configured local AI backends, but it is unavailable in Docker simulation."
  fi
  if [[ "${AUTO_INSTALL_SYSTEM_DEPS}" != "1" ]]; then
    fail "Ollama is required by the configured local AI backends. Re-run with AUTO_INSTALL_SYSTEM_DEPS=1 or install Ollama manually."
  fi
  install_homebrew_if_needed
  log "Installing Ollama because .env enables local AI backends..."
  brew install ollama
}

ensure_configured_ollama_models() {
  local text_backend
  local vision_backend
  local text_model
  local vision_models

  text_backend="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_TEXT_BACKEND")"
  vision_backend="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_VISION_BACKEND")"
  text_model="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_TEXT_MODEL")"
  vision_models="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_VISION_MODEL")"

  while read -r model; do
    [[ -z "${model}" ]] && continue
    log "Ensuring Ollama model is available: ${model}"
    ollama pull "${model}"
  done < <(
    {
      if [[ "${text_backend}" == "ollama" ]]; then
        model_list_from_env "${text_model}" "${OLLAMA_DEFAULT_TEXT_MODEL}"
      fi
      if [[ "${vision_backend}" == "ollama" ]]; then
        model_list_from_env "${vision_models}" ""
      fi
    } | awk '!seen[$0]++'
  )
}

ensure_configured_ollama_runtime() {
  if ! configured_ollama_required; then
    return
  fi
  ensure_ollama_installed_if_needed
  if ! start_ollama_if_available; then
    fail "Ollama is configured for local AI, but the server did not become reachable on http://127.0.0.1:11434"
  fi
  ensure_configured_ollama_models
}

setup_ollama_if_requested() {
  if [[ "${SETUP_OLLAMA}" != "1" ]]; then
    return
  fi

  if ! have_command ollama; then
    if [[ "${MACOS_DOCKER_SIMULATE}" == "1" ]]; then
      fail "SETUP_OLLAMA=1 but ollama is unavailable in Docker simulation."
    fi
    install_homebrew_if_needed
    log "Installing Ollama for local natural-language support..."
    brew install ollama
  fi

  if ! start_ollama_if_available; then
    fail "Ollama is installed but did not become reachable on http://127.0.0.1:11434"
  fi

  local text_model
  local vision_models
  text_model="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_TEXT_MODEL")"
  if [[ "${SETUP_OLLAMA_VISION}" == "1" ]]; then
    vision_models="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_VISION_MODEL")"
  else
    vision_models=""
    log "Skipping Ollama vision model pulls. Set SETUP_OLLAMA_VISION=1 to opt in."
  fi

  while read -r model; do
    [[ -z "${model}" ]] && continue
    log "Ensuring Ollama model is available: ${model}"
    ollama pull "${model}"
  done < <(
    {
      model_list_from_env "${text_model}" "${OLLAMA_DEFAULT_TEXT_MODEL}"
      model_list_from_env "${vision_models}" ""
    } | awk '!seen[$0]++'
  )
}

url_available() {
  local url="$1"
  "${AKA_VENV}/bin/python" - "${url}" <<'PY' >/dev/null 2>&1
import sys
import urllib.request

try:
    with urllib.request.urlopen(sys.argv[1], timeout=2):
        raise SystemExit(0)
except Exception:
    raise SystemExit(1)
PY
}

prepare_openclaw_runtime_env() {
  local tesseract_path
  local tessdata_dir
  local local_vision_backend
  local local_vision_endpoint
  local local_vision_model
  local local_text_backend
  local local_text_endpoint
  local local_text_model

  tesseract_path="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_TESSERACT_PATH")"
  tessdata_dir="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_TESSDATA_DIR")"
  local_vision_backend="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_VISION_BACKEND")"
  local_vision_endpoint="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_VISION_ENDPOINT")"
  local_vision_model="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_VISION_MODEL")"
  local_text_backend="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_TEXT_BACKEND")"
  local_text_endpoint="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_TEXT_ENDPOINT")"
  local_text_model="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_TEXT_MODEL")"

  if [[ -n "${local_vision_backend}" ]]; then
    export OPENCLAW_LOCAL_VISION_BACKEND="${local_vision_backend}"
  fi
  if [[ -n "${local_vision_endpoint}" ]]; then
    export OPENCLAW_LOCAL_VISION_ENDPOINT="${local_vision_endpoint}"
  fi
  if [[ -n "${local_vision_model}" ]]; then
    export OPENCLAW_LOCAL_VISION_MODEL="${local_vision_model}"
  fi
  if [[ -n "${local_text_backend}" ]]; then
    export OPENCLAW_LOCAL_TEXT_BACKEND="${local_text_backend}"
  fi
  if [[ -n "${local_text_endpoint}" ]]; then
    export OPENCLAW_LOCAL_TEXT_ENDPOINT="${local_text_endpoint}"
  fi
  if [[ -n "${local_text_model}" ]]; then
    export OPENCLAW_LOCAL_TEXT_MODEL="${local_text_model}"
  fi

  if [[ "${SETUP_OLLAMA}" == "1" ]]; then
    if [[ -z "${local_text_backend}" ]]; then
      export OPENCLAW_LOCAL_TEXT_BACKEND="ollama"
      export OPENCLAW_LOCAL_TEXT_MODEL="${OPENCLAW_LOCAL_TEXT_MODEL:-${OLLAMA_DEFAULT_TEXT_MODEL}}"
      log "Enabled OPENCLAW_LOCAL_TEXT_BACKEND=ollama for natural-language routing."
    fi
    if [[ "${SETUP_OLLAMA_VISION}" == "1" && -z "${local_vision_backend}" && -n "$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_LOCAL_VISION_MODEL")" ]]; then
      export OPENCLAW_LOCAL_VISION_BACKEND="ollama"
      log "Enabled OPENCLAW_LOCAL_VISION_BACKEND=ollama because vision models are configured."
    fi
  fi

  if [[ -z "${tesseract_path}" || "${tesseract_path}" == "auto" ]] || is_windows_path "${tesseract_path}"; then
    export OPENCLAW_TESSERACT_PATH="$(command -v tesseract || true)"
    log "Using Mac runtime Tesseract path: ${OPENCLAW_TESSERACT_PATH:-disabled}"
  fi

  if [[ -z "${tessdata_dir}" || "${tessdata_dir}" == "auto" ]] || is_windows_path "${tessdata_dir}"; then
    export OPENCLAW_TESSDATA_DIR="$(find_tessdata_dir || true)"
    log "Using Mac runtime tessdata path: ${OPENCLAW_TESSDATA_DIR:-default}"
  fi

  if [[ "${AUTO_DISABLE_UNAVAILABLE_LOCAL_AI}" == "1" ]]; then
    if [[ "${local_vision_backend}" == "ollama" ]]; then
      local_vision_endpoint="${local_vision_endpoint:-http://127.0.0.1:11434}"
      if ! ollama_endpoint_ready "${local_vision_endpoint}"; then
        export OPENCLAW_LOCAL_VISION_BACKEND=""
        export OPENCLAW_LOCAL_VISION_MODEL=""
        log "Disabled OPENCLAW_LOCAL_VISION_BACKEND=ollama for this Mac run because Ollama is not available."
      fi
    fi
    if [[ "${local_text_backend}" == "ollama" ]]; then
      local_text_endpoint="${local_text_endpoint:-http://127.0.0.1:11434}"
      if ! ollama_endpoint_ready "${local_text_endpoint}"; then
        export OPENCLAW_LOCAL_TEXT_BACKEND=""
        export OPENCLAW_LOCAL_TEXT_MODEL=""
        log "Disabled OPENCLAW_LOCAL_TEXT_BACKEND=ollama for this Mac run because Ollama is not available."
      fi
    fi
  fi
}

validate_env() {
  configure_aka_env

  local token
  local chat_id
  local admin_token
  token="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_TELEGRAM_BOT_TOKEN")"
  chat_id="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_TELEGRAM_CHAT_ID")"
  admin_token="$(get_env_value "${AKA_DIR}/.env" "REPUTATION_AGENT_ADMIN_TOKEN")"

  if [[ -z "${token}" ]]; then
    fail "OPENCLAW_TELEGRAM_BOT_TOKEN is empty in ${AKA_DIR}/.env"
  fi
  if [[ -z "${chat_id}" ]]; then
    fail "OPENCLAW_TELEGRAM_CHAT_ID is empty in ${AKA_DIR}/.env"
  fi
  if [[ -z "${admin_token}" ]]; then
    fail "REPUTATION_AGENT_ADMIN_TOKEN is empty in ${AKA_DIR}/.env"
  fi

  sync_reputation_env "${admin_token}"
}

init_reputation_runtime() {
  log "Preparing reputation_snapshot database and keys..."
  (
    cd "${REPUTATION_DIR}"
    "${REPUTATION_VENV}/bin/python" scripts/init_db.py
    if [[ ! -f "keys/ed25519_private_key.pem" ]]; then
      "${REPUTATION_VENV}/bin/python" scripts/generate_keys.py
    fi
  )
}

launchctl_job_exists() {
  local label="$1"
  launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1
}

launchctl_job_pid() {
  local label="$1"
  launchctl print "gui/$(id -u)/${label}" 2>/dev/null | awk -F= '/pid =/ { gsub(/^[ \t]+|[ \t]+$/, "", $2); print $2; exit }'
}

stop_launchctl_jobs() {
  if ! use_launchctl_services; then
    return
  fi
  local label
  for label in "${LAUNCHCTL_OLLAMA_LABEL}" "${LAUNCHCTL_AIVIS_LABEL}" "${LAUNCHCTL_REPUTATION_LABEL}" "${LAUNCHCTL_TELEGRAM_LABEL}" "${LAUNCHCTL_SNS_MONITOR_LABEL}" "${LAUNCHCTL_PRICE_MONITOR_LABEL}" "${LAUNCHCTL_OPPORTUNITY_LABEL}" "${LAUNCHCTL_CHAT_WEB_LABEL}"; do
    if launchctl_job_exists "${label}"; then
      log "Stopping launchctl job ${label}."
      launchctl remove "${label}" >/dev/null 2>&1 || true
    fi
  done
}

append_runtime_export() {
  local key="$1"
  local value="${2:-}"
  printf 'export %s=' "${key}" >> "${RUNTIME_ENV_FILE}"
  printf '%q\n' "${value}" >> "${RUNTIME_ENV_FILE}"
}

reset_runtime_env_file() {
  : > "${RUNTIME_ENV_FILE}"
  chmod 600 "${RUNTIME_ENV_FILE}" || true
}

stop_orphaned_stack_processes() {
  local found=0
  while read -r pid command_line; do
    [[ -z "${pid}" || -z "${command_line}" ]] && continue
    if [[ "${command_line}" == *"${REPUTATION_DIR}/.venv/bin/python"* && "${command_line}" == *"app.py"* ]]; then
      found=1
      log "Stopping orphaned reputation_snapshot server PID ${pid}."
      kill "${pid}" >/dev/null 2>&1 || true
    elif [[ "${command_line}" == *"${AKA_VENV}/bin/python"* && "${command_line}" == *"openclaw_adapter"* ]]; then
      found=1
      log "Stopping orphaned OpenClaw process PID ${pid}."
      kill "${pid}" >/dev/null 2>&1 || true
    fi
  done < <(ps -eo pid=,command= 2>/dev/null || true)
  if [[ "${found}" == "1" ]]; then
    sleep 1
  fi
}

stop_existing_stack() {
  stop_launchctl_jobs
  if [[ ! -f "${PID_FILE}" ]]; then
    stop_orphaned_stack_processes
    return
  fi
  log "Stopping previous stack from ${PID_FILE}..."
  while read -r pid; do
    pid="${pid//$'\r'/}"
    [[ -z "${pid}" ]] && continue
    local command_line=""
    command_line="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    if [[ -z "${command_line}" ]]; then
      log "Skipping stale PID ${pid}."
      continue
    fi
    if [[ "${command_line}" != *"${AKA_DIR}"* && "${command_line}" != *"${REPUTATION_DIR}"* && "${command_line}" != *"openclaw_adapter"* && "${command_line}" != *"ollama serve"* ]]; then
      log "Skipping stale or unrelated PID ${pid}."
      continue
    fi
    if kill -0 "${pid}" >/dev/null 2>&1; then
      kill "${pid}" >/dev/null 2>&1 || true
    fi
  done < "${PID_FILE}"
  rm -f "${PID_FILE}"
  stop_orphaned_stack_processes
}

start_reputation_server() {
  log "Starting reputation_snapshot server on ${HOST}:${PORT}..."
  local admin_token
  local chromium_path
  admin_token="$(get_env_value "${AKA_DIR}/.env" "REPUTATION_AGENT_ADMIN_TOKEN")"
  chromium_path="$(find_system_chromium || true)"

  if use_launchctl_services; then
    reset_runtime_env_file
    append_runtime_export "APP_HOST" "${HOST}"
    append_runtime_export "APP_PORT" "${PORT}"
    append_runtime_export "ADMIN_TOKEN" "${admin_token}"
    if [[ -n "${chromium_path}" ]]; then
      append_runtime_export "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH" "${chromium_path}"
    fi
    launchctl submit -l "${LAUNCHCTL_REPUTATION_LABEL}" \
      -o "${LOG_DIR}/reputation_snapshot.log" \
      -e "${LOG_DIR}/reputation_snapshot.log" \
      -- /bin/bash -lc "source '${RUNTIME_ENV_FILE}'; cd '${REPUTATION_DIR}'; exec '${REPUTATION_VENV}/bin/python' app.py"
    local pid
    pid="$(launchctl_job_pid "${LAUNCHCTL_REPUTATION_LABEL}")"
    [[ -n "${pid}" ]] && echo "${pid}" >> "${PID_FILE}"
    return
  fi

  (
    cd "${REPUTATION_DIR}"
    export APP_HOST="${HOST}"
    export APP_PORT="${PORT}"
    export ADMIN_TOKEN="${admin_token}"
    if [[ -n "${chromium_path}" ]]; then
      export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH="${chromium_path}"
    fi
    nohup "${REPUTATION_VENV}/bin/python" app.py >> "${LOG_DIR}/reputation_snapshot.log" 2>&1 &
    echo $! >> "${PID_FILE}"
  )
}

wait_for_reputation_server() {
  log "Waiting for reputation_snapshot to answer..."
  local url="http://${HOST}:${PORT}/admin?token=$(get_env_value "${AKA_DIR}/.env" "REPUTATION_AGENT_ADMIN_TOKEN")"
  for _ in $(seq 1 30); do
    if "${AKA_VENV}/bin/python" - "${url}" <<'PY'
import sys
import urllib.request

url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=2) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
    then
      log "reputation_snapshot is ready."
      return
    fi
    sleep 1
  done
  fail "reputation_snapshot did not become ready. Check ${LOG_DIR}/reputation_snapshot.log"
}

start_openclaw_telegram() {
  log "Starting OpenClaw Telegram bot without dashboards..."
  local admin_token
  local chromium_path
  admin_token="$(get_env_value "${AKA_DIR}/.env" "REPUTATION_AGENT_ADMIN_TOKEN")"
  chromium_path="$(find_system_chromium || true)"
  local args=(telegram-poll --with-reputation-agent --no-dashboard)
  if [[ "${START_NOTIFY}" == "1" ]]; then
    args+=(--notify-startup)
  fi

  # Keep Telegram as a shell-started process on macOS. `launchctl submit` runs it
  # as a background daemon and cannot reliably access local-network devices such
  # as BroadLink RM4 Mini, while the same process launched from the user shell can.
  (
    cd "${AKA_DIR}"
    export REPUTATION_AGENT_SERVER_URL="http://${HOST}:${PORT}"
    export REPUTATION_AGENT_ADMIN_TOKEN="${admin_token}"
    prepare_openclaw_runtime_env
    if [[ -n "${chromium_path}" ]]; then
      export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH="${chromium_path}"
    fi
    nohup "${AKA_VENV}/bin/python" -m openclaw_adapter "${args[@]}" >> "${LOG_DIR}/openclaw_telegram.log" 2>&1 &
    echo $! >> "${PID_FILE}"
  )
}

opportunity_agent_enabled() {
  local enabled
  enabled="$(get_env_value "${AKA_DIR}/.env" "OPENCLAW_OPPORTUNITY_AGENT_ENABLED")"
  enabled="$(printf '%s' "${enabled:-0}" | tr '[:upper:]' '[:lower:]')"
  [[ "${enabled}" != "0" && "${enabled}" != "false" && "${enabled}" != "no" && "${enabled}" != "off" ]]
}

start_sns_monitor_service() {
  log "Starting OpenClaw SNS monitor service..."
  if use_launchctl_services; then
    launchctl submit -l "${LAUNCHCTL_SNS_MONITOR_LABEL}" \
      -o "${LOG_DIR}/openclaw_sns_monitor.log" \
      -e "${LOG_DIR}/openclaw_sns_monitor.log" \
      -- /bin/bash -lc "source '${RUNTIME_ENV_FILE}'; cd '${AKA_DIR}'; export PYTHONPATH='.:src'; exec '${AKA_VENV}/bin/python' -m openclaw_adapter sns-monitor-service"
    local pid
    pid="$(launchctl_job_pid "${LAUNCHCTL_SNS_MONITOR_LABEL}")"
    [[ -n "${pid}" ]] && echo "${pid}" >> "${PID_FILE}"
    return
  fi

  (
    cd "${AKA_DIR}"
    nohup "${AKA_VENV}/bin/python" -m openclaw_adapter sns-monitor-service >> "${LOG_DIR}/openclaw_sns_monitor.log" 2>&1 &
    echo $! >> "${PID_FILE}"
  )
}

start_price_monitor_service() {
  log "Starting OpenClaw price monitor service..."
  if use_launchctl_services; then
    launchctl submit -l "${LAUNCHCTL_PRICE_MONITOR_LABEL}" \
      -o "${LOG_DIR}/openclaw_price_monitor.log" \
      -e "${LOG_DIR}/openclaw_price_monitor.log" \
      -- /bin/bash -lc "source '${RUNTIME_ENV_FILE}'; cd '${AKA_DIR}'; export PYTHONPATH='.:src'; exec '${AKA_VENV}/bin/python' -m openclaw_adapter price-monitor-service"
    local pid
    pid="$(launchctl_job_pid "${LAUNCHCTL_PRICE_MONITOR_LABEL}")"
    [[ -n "${pid}" ]] && echo "${pid}" >> "${PID_FILE}"
    return
  fi

  (
    cd "${AKA_DIR}"
    nohup "${AKA_VENV}/bin/python" -m openclaw_adapter price-monitor-service >> "${LOG_DIR}/openclaw_price_monitor.log" 2>&1 &
    echo $! >> "${PID_FILE}"
  )
}

start_chat_web_service() {
  log "Starting OpenClaw local web chat (http://${CHAT_WEB_HOST}:${CHAT_WEB_PORT}/chat)..."
  if use_launchctl_services; then
    launchctl submit -l "${LAUNCHCTL_CHAT_WEB_LABEL}" \
      -o "${LOG_DIR}/openclaw_chat_web.log" \
      -e "${LOG_DIR}/openclaw_chat_web.log" \
      -- /bin/bash -lc "source '${RUNTIME_ENV_FILE}'; cd '${AKA_DIR}'; export PYTHONPATH='.:src'; exec '${AKA_VENV}/bin/python' -m openclaw_adapter chat-web --host '${CHAT_WEB_HOST}' --port '${CHAT_WEB_PORT}'"
    local pid
    pid="$(launchctl_job_pid "${LAUNCHCTL_CHAT_WEB_LABEL}")"
    [[ -n "${pid}" ]] && echo "${pid}" >> "${PID_FILE}"
    return
  fi

  (
    cd "${AKA_DIR}"
    nohup "${AKA_VENV}/bin/python" -m openclaw_adapter chat-web --host "${CHAT_WEB_HOST}" --port "${CHAT_WEB_PORT}" >> "${LOG_DIR}/openclaw_chat_web.log" 2>&1 &
    echo $! >> "${PID_FILE}"
  )
}

start_opportunity_agent() {
  if ! opportunity_agent_enabled; then
    log "Skipping opportunity agent because OPENCLAW_OPPORTUNITY_AGENT_ENABLED=0."
    return
  fi

  log "Starting OpenClaw opportunity agent..."
  if use_launchctl_services; then
    launchctl submit -l "${LAUNCHCTL_OPPORTUNITY_LABEL}" \
      -o "${LOG_DIR}/opportunity_agent.log" \
      -e "${LOG_DIR}/opportunity_agent.log" \
      -- /bin/bash -lc "source '${RUNTIME_ENV_FILE}'; cd '${AKA_DIR}'; export PYTHONPATH='.:src'; exec '${AKA_VENV}/bin/python' -m openclaw_adapter opportunity-agent"
    local pid
    pid="$(launchctl_job_pid "${LAUNCHCTL_OPPORTUNITY_LABEL}")"
    [[ -n "${pid}" ]] && echo "${pid}" >> "${PID_FILE}"
    return
  fi

  (
    cd "${AKA_DIR}"
    nohup "${AKA_VENV}/bin/python" -m openclaw_adapter opportunity-agent >> "${LOG_DIR}/opportunity_agent.log" 2>&1 &
    echo $! >> "${PID_FILE}"
  )
}

main() {
  detect_runtime_environment
  require_dir "${PRICE_DIR}" "price_monitor_bot"
  require_dir "${REPUTATION_DIR}" "reputation_snapshot"
  stop_existing_stack
  cleanup_copied_runtime_artifacts
  install_system_packages
  ensure_python
  choose_reputation_port
  validate_env
  install_reputation_snapshot
  install_openclaw
  init_reputation_runtime
  ensure_configured_ollama_runtime
  setup_ollama_if_requested
  start_aivis_engine
  start_reputation_server
  wait_for_reputation_server
  start_openclaw_telegram
  start_sns_monitor_service
  start_price_monitor_service
  start_chat_web_service
  start_opportunity_agent

  log "Started."
  log "PID file: ${PID_FILE}"
  log "Logs:"
  log "  ${LOG_DIR}/reputation_snapshot.log"
  log "  ${LOG_DIR}/openclaw_telegram.log"
  log "  ${LOG_DIR}/openclaw_sns_monitor.log"
  log "  ${LOG_DIR}/openclaw_price_monitor.log"
  log "  ${LOG_DIR}/openclaw_chat_web.log"
  log "  ${LOG_DIR}/opportunity_agent.log"
  log "  ${LOG_DIR}/aivis_speech.log"
  if use_launchctl_services; then
    log "macOS Terminal may show '[Process completed]' after this; the services keep running in the background."
  fi
}

main "$@"
