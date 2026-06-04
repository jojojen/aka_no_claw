#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AKA_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

AIVIS_VERSION="${AIVIS_VERSION:-1.1.0-preview.4}"
AIVIS_PORT="${AIVIS_PORT:-10101}"
AIVIS_READY_TIMEOUT_SECONDS="${AIVIS_READY_TIMEOUT_SECONDS:-3600}"
AIVIS_FORCE_REINSTALL="${AIVIS_FORCE_REINSTALL:-0}"

AIVIS_DOWNLOAD_DIR="${HOME}/Downloads/aivis_install"
AIVIS_ARCHIVE_NAME="AivisSpeech-macOS-arm64-${AIVIS_VERSION}.zip"
AIVIS_ARCHIVE_PATH="${AIVIS_DOWNLOAD_DIR}/${AIVIS_ARCHIVE_NAME}"
AIVIS_RELEASE_URL="https://github.com/Aivis-Project/AivisSpeech/releases/download/${AIVIS_VERSION}/${AIVIS_ARCHIVE_NAME}"
AIVIS_UNPACK_DIR="${HOME}/Applications/aivis_unpack"
AIVIS_APP_PATH="${HOME}/Applications/AivisSpeech.app"
AIVIS_APP_SOURCE="${AIVIS_UNPACK_DIR}/AivisSpeech/AivisSpeech.app"
AIVIS_MODEL_DIR="${HOME}/Library/Application Support/AivisSpeech-Engine/Models"
AIVIS_LOG_DIR="${HOME}/Library/Application Support/AivisSpeech-Engine/Logs"

log() {
  printf '[install-aivis] %s\n' "$*"
}

fail() {
  printf '[install-aivis] ERROR: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "Required command not found: $1"
}

require_macos_arm64() {
  [[ "$(uname -s)" == "Darwin" ]] || fail "This installer is for macOS."
  [[ "$(uname -m)" == "arm64" ]] || fail "This installer is for Apple Silicon (arm64)."
}

is_ready() {
  curl -fsS "http://127.0.0.1:${AIVIS_PORT}/version" >/dev/null 2>&1 &&
  curl -fsS "http://127.0.0.1:${AIVIS_PORT}/speakers" >/dev/null 2>&1
}

download_archive() {
  mkdir -p "${AIVIS_DOWNLOAD_DIR}"
  if [[ -s "${AIVIS_ARCHIVE_PATH}" ]]; then
    log "Reusing existing archive: ${AIVIS_ARCHIVE_PATH}"
    return
  fi
  log "Downloading ${AIVIS_ARCHIVE_NAME}"
  curl -L -C - --fail --progress-bar -o "${AIVIS_ARCHIVE_PATH}" "${AIVIS_RELEASE_URL}"
}

install_app_bundle() {
  mkdir -p "${HOME}/Applications"
  rm -rf "${AIVIS_UNPACK_DIR}"
  mkdir -p "${AIVIS_UNPACK_DIR}"
  log "Unpacking ${AIVIS_ARCHIVE_NAME}"
  unzip -q "${AIVIS_ARCHIVE_PATH}" -d "${AIVIS_UNPACK_DIR}"
  [[ -d "${AIVIS_APP_SOURCE}" ]] || fail "Unpacked app bundle not found: ${AIVIS_APP_SOURCE}"
  rm -rf "${AIVIS_APP_PATH}"
  log "Installing app bundle to ${AIVIS_APP_PATH}"
  ditto "${AIVIS_APP_SOURCE}" "${AIVIS_APP_PATH}"
  xattr -dr com.apple.quarantine "${AIVIS_APP_PATH}" >/dev/null 2>&1 || true
}

cleanup_stale_models() {
  mkdir -p "${AIVIS_MODEL_DIR}" "${AIVIS_LOG_DIR}"
  if [[ -f "${AIVIS_MODEL_DIR}/default.aivmx" ]]; then
    log "Removing stale model alias ${AIVIS_MODEL_DIR}/default.aivmx"
    rm -f "${AIVIS_MODEL_DIR}/default.aivmx"
  fi
}

restart_app() {
  pkill -f 'AivisSpeech.app/Contents/MacOS/AivisSpeech' >/dev/null 2>&1 || true
  sleep 2
  log "Launching ${AIVIS_APP_PATH}"
  open "${AIVIS_APP_PATH}"
}

wait_until_ready() {
  local waited=0
  while (( waited < AIVIS_READY_TIMEOUT_SECONDS )); do
    if is_ready; then
      log "AivisSpeech is ready on port ${AIVIS_PORT}"
      curl -fsS "http://127.0.0.1:${AIVIS_PORT}/version"
      printf '\n'
      return 0
    fi
    sleep 5
    waited=$((waited + 5))
    if (( waited % 60 == 0 )); then
      local size
      size="$(du -sh "${HOME}/Library/Application Support/AivisSpeech-Engine" 2>/dev/null | awk '{print $1}')"
      log "Still waiting (${waited}s/${AIVIS_READY_TIMEOUT_SECONDS}s). Cache size=${size:-0}"
    fi
  done
  log "Recent Aivis logs:"
  tail -80 "${HOME}/Library/Logs/AivisSpeech/"*.log 2>/dev/null || true
  tail -80 "${AIVIS_LOG_DIR}/AivisSpeech-Engine.log" 2>/dev/null || true
  fail "AivisSpeech did not become ready within ${AIVIS_READY_TIMEOUT_SECONDS} seconds."
}

main() {
  require_macos_arm64
  require_command curl
  require_command unzip
  require_command ditto
  require_command open

  if [[ "${AIVIS_FORCE_REINSTALL}" != "1" ]] && is_ready; then
    log "AivisSpeech is already ready; skipping reinstall."
    curl -fsS "http://127.0.0.1:${AIVIS_PORT}/version"
    printf '\n'
    exit 0
  fi

  download_archive
  install_app_bundle
  cleanup_stale_models
  restart_app
  wait_until_ready

  log "Verify with:"
  log "  curl -fsS http://127.0.0.1:${AIVIS_PORT}/version"
  log "  curl -fsS http://127.0.0.1:${AIVIS_PORT}/speakers | head"
  log "OpenClaw will prefer AivisSpeech automatically once these checks pass."
}

main "$@"
