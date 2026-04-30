#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPUTATION_DIR="${WORKSPACE_DIR}/reputation_snapshot"
AKA_VENV="${SCRIPT_DIR}/.venv"
PID_FILE="${SCRIPT_DIR}/run/rpi5-stack.pid"

stop_orphaned_stack_processes() {
  found=0
  while read -r pid command_line; do
    [[ -z "${pid}" || -z "${command_line}" ]] && continue
    if [[ "${command_line}" == *"${REPUTATION_DIR}/.venv/bin/python"* && "${command_line}" == *"app.py"* ]]; then
      found=1
      printf '[rpi5-stack] Stopping orphaned reputation_snapshot server PID %s\n' "${pid}"
      kill "${pid}" >/dev/null 2>&1 || true
    elif [[ "${command_line}" == *"${AKA_VENV}/bin/python"* && "${command_line}" == *"openclaw_adapter"* ]]; then
      found=1
      printf '[rpi5-stack] Stopping orphaned OpenClaw process PID %s\n' "${pid}"
      kill "${pid}" >/dev/null 2>&1 || true
    fi
  done < <(ps -eo pid=,command= 2>/dev/null || true)
  if [[ "${found}" == "1" ]]; then
    sleep 1
  fi
}

if [[ ! -f "${PID_FILE}" ]]; then
  printf '[rpi5-stack] No PID file found at %s\n' "${PID_FILE}"
  stop_orphaned_stack_processes
  exit 0
fi

while read -r pid; do
  pid="${pid//$'\r'/}"
  [[ -z "${pid}" ]] && continue
  command_line="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
  if [[ -z "${command_line}" ]]; then
    printf '[rpi5-stack] Skipping stale PID %s\n' "${pid}"
    continue
  fi
  if [[ "${command_line}" != *"${SCRIPT_DIR}"* && "${command_line}" != *"${REPUTATION_DIR}"* && "${command_line}" != *"openclaw_adapter"* && "${command_line}" != *"ollama serve"* ]]; then
    printf '[rpi5-stack] Skipping stale or unrelated PID %s\n' "${pid}"
    continue
  fi
  if kill -0 "${pid}" >/dev/null 2>&1; then
    printf '[rpi5-stack] Stopping PID %s\n' "${pid}"
    kill "${pid}" >/dev/null 2>&1 || true
  fi
done < "${PID_FILE}"

rm -f "${PID_FILE}"
stop_orphaned_stack_processes
printf '[rpi5-stack] Stopped.\n'
