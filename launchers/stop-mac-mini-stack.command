#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
AKA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPUTATION_DIR="${WORKSPACE_DIR}/reputation_snapshot"
WEB_DIR="${WORKSPACE_DIR}/aka_no_claw_web/frontend"

AKA_VENV="${AKA_DIR}/.venv"
RUN_DIR="${AKA_DIR}/run"
PID_FILE="${RUN_DIR}/mac-mini-stack.pid"
TMUX_SOCKET="${TMUX_SOCKET:-openclaw_codex}"
AIVIS_APP_PATH="${AIVIS_APP_PATH:-${HOME}/Applications/AivisSpeech.app}"
AIVIS_ENGINE_RUN="${AIVIS_APP_PATH}/Contents/Resources/AivisSpeech-Engine/run"

LAUNCHCTL_LABELS=(
  local.openclaw.ollama
  local.openclaw.aivis
  local.openclaw.reputation
  local.openclaw.telegram
  local.openclaw.sns_monitor
  local.openclaw.price_monitor
  local.openclaw.chat_web
  local.openclaw.opportunity
)

log() {
  printf '[mac-mini-stack] %s\n' "$*"
}

warn() {
  printf '[mac-mini-stack] WARN: %s\n' "$*" >&2
}

stop_launchctl_jobs() {
  if ! command -v launchctl >/dev/null 2>&1; then
    return
  fi

  local label
  for label in "${LAUNCHCTL_LABELS[@]}"; do
    if launchctl print "gui/$(id -u)/${label}" >/dev/null 2>&1; then
      log "Stopping launchctl job ${label}."
      launchctl remove "${label}" >/dev/null 2>&1 || warn "launchctl remove failed for ${label}"
    fi
  done
}

stop_tmux_services() {
  if ! command -v tmux >/dev/null 2>&1; then
    return
  fi
  if tmux -L "${TMUX_SOCKET}" has-session >/dev/null 2>&1; then
    log "Stopping tmux socket ${TMUX_SOCKET}."
    tmux -L "${TMUX_SOCKET}" kill-server >/dev/null 2>&1 || warn "tmux kill-server failed for ${TMUX_SOCKET}"
  fi
}

owned_by_stack() {
  local command_line="$1"

  [[ "${command_line}" == *"${AKA_DIR}"* ]] && return 0
  [[ "${command_line}" == *"${REPUTATION_DIR}"* ]] && return 0
  [[ "${command_line}" == *"${WEB_DIR}"* ]] && return 0
  [[ "${command_line}" == *"${AIVIS_ENGINE_RUN}"* ]] && return 0
  return 1
}

stop_pid_if_owned() {
  local pid="$1"
  local label="${2:-PID ${pid}}"
  local command_line

  command_line="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
  if [[ -z "${command_line}" ]]; then
    log "Skipping stale PID ${pid}."
    return
  fi
  if ! owned_by_stack "${command_line}" && [[ "${command_line}" != *"ollama serve"* ]]; then
    log "Skipping stale or unrelated PID ${pid}."
    return
  fi

  log "Stopping ${label}: ${pid}."
  kill "${pid}" >/dev/null 2>&1 || true
}

stop_pid_file_processes() {
  if [[ ! -f "${PID_FILE}" ]]; then
    log "No PID file found at ${PID_FILE}."
    return
  fi

  log "Stopping processes from ${PID_FILE}."
  local pid
  while read -r pid; do
    pid="${pid//$'\r'/}"
    [[ -z "${pid}" ]] && continue
    stop_pid_if_owned "${pid}"
  done < "${PID_FILE}"
  rm -f "${PID_FILE}"
}

stop_orphaned_stack_processes() {
  local found=0
  local pid command_line

  while read -r pid command_line; do
    [[ -z "${pid}" || -z "${command_line}" ]] && continue

    if [[ "${command_line}" == *"${REPUTATION_DIR}/.venv/bin/python"* && "${command_line}" == *"app.py"* ]]; then
      found=1
      stop_pid_if_owned "${pid}" "orphaned reputation_snapshot"
    elif [[ "${command_line}" == *"${AKA_VENV}/bin/python"* && "${command_line}" == *"openclaw_adapter"* ]]; then
      found=1
      stop_pid_if_owned "${pid}" "orphaned OpenClaw worker"
    elif [[ "${command_line}" == *"${WEB_DIR}"* && "${command_line}" == *"vite"* ]]; then
      found=1
      stop_pid_if_owned "${pid}" "orphaned Web frontend"
    elif [[ "${command_line}" == *"${AIVIS_ENGINE_RUN}"* ]]; then
      found=1
      stop_pid_if_owned "${pid}" "orphaned AivisSpeech engine"
    fi
  done < <(ps -eo pid=,command= 2>/dev/null || true)

  if [[ "${found}" == "1" ]]; then
    sleep 1
  fi
}

stop_pattern() {
  local label="$1"
  local pattern="$2"
  local pids pid

  pids="$(pgrep -f "${pattern}" 2>/dev/null | tr '\n' ' ' || true)"
  if [[ -z "${pids}" ]]; then
    return
  fi

  log "Stopping ${label}: ${pids}"
  for pid in ${pids}; do
    [[ "${pid}" == "$$" ]] && continue
    kill "${pid}" >/dev/null 2>&1 || true
  done
}

force_pattern() {
  local label="$1"
  local pattern="$2"
  local pids pid

  pids="$(pgrep -f "${pattern}" 2>/dev/null | tr '\n' ' ' || true)"
  if [[ -z "${pids}" ]]; then
    return
  fi

  log "Force stopping ${label}: ${pids}"
  for pid in ${pids}; do
    [[ "${pid}" == "$$" ]] && continue
    kill -9 "${pid}" >/dev/null 2>&1 || true
  done
}

stop_restartall_scope_processes() {
  # Keep this list aligned with service_restart.py's stop phase, but do not
  # perform the later kickstart/start phase.
  launchctl remove "local.openclaw.telegram" >/dev/null 2>&1 || true
  stop_pattern "telegram" "openclaw_adapter telegram-poll"
  stop_pattern "vite web" "aka_no_claw_web/frontend/node_modules/.bin/vite --host 0.0.0.0"
  stop_pattern "command bridge" "openclaw_adapter command-bridge --lan --port 8781|openclaw_adapter command-bridge --host 0.0.0.0 --port 8781 --lan"
  stop_pattern "chat web" "openclaw_adapter chat-web --host 0.0.0.0 --port 8780"
  stop_pattern "reputation snapshot" "reputation_snapshot/.venv/bin/python app.py"
  stop_pattern "scrape workers" "openclaw_adapter.scrape_worker"
  stop_pattern "playwright drivers" "related_to_claw/.*/playwright/driver/package/cli.js run-driver"
}

force_restartall_scope_processes() {
  force_pattern "telegram" "openclaw_adapter telegram-poll"
  force_pattern "vite web" "aka_no_claw_web/frontend/node_modules/.bin/vite --host 0.0.0.0"
  force_pattern "command bridge" "openclaw_adapter command-bridge --lan --port 8781|openclaw_adapter command-bridge --host 0.0.0.0 --port 8781 --lan"
  force_pattern "chat web" "openclaw_adapter chat-web --host 0.0.0.0 --port 8780"
  force_pattern "reputation snapshot" "reputation_snapshot/.venv/bin/python app.py"
  force_pattern "scrape workers" "openclaw_adapter.scrape_worker"
  force_pattern "playwright drivers" "related_to_claw/.*/playwright/driver/package/cli.js run-driver"
}

force_remaining_stack_processes() {
  local pid command_line

  while read -r pid command_line; do
    [[ -z "${pid}" || -z "${command_line}" ]] && continue

    if [[ "${command_line}" == *"${REPUTATION_DIR}/.venv/bin/python"* && "${command_line}" == *"app.py"* ]] \
      || [[ "${command_line}" == *"${AKA_VENV}/bin/python"* && "${command_line}" == *"openclaw_adapter"* ]] \
      || [[ "${command_line}" == *"${WEB_DIR}"* && "${command_line}" == *"vite"* ]] \
      || [[ "${command_line}" == *"${AIVIS_ENGINE_RUN}"* ]]; then
      log "Force stopping remaining stack PID ${pid}."
      kill -9 "${pid}" >/dev/null 2>&1 || true
    fi
  done < <(ps -eo pid=,command= 2>/dev/null || true)
}

print_remaining_listeners() {
  local ports=(5173 8781 8780 10101)
  local port

  if ! command -v lsof >/dev/null 2>&1; then
    return
  fi

  for port in "${ports[@]}"; do
    if lsof -nP -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
      warn "Port ${port} still has a listener:"
      lsof -nP -iTCP:"${port}" -sTCP:LISTEN >&2 || true
    fi
  done
}

main() {
  stop_launchctl_jobs
  stop_tmux_services
  stop_pid_file_processes
  stop_restartall_scope_processes
  stop_orphaned_stack_processes

  sleep 2
  force_restartall_scope_processes
  force_remaining_stack_processes
  print_remaining_listeners

  log "Stopped."
}

main "$@"
