#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"
IMAGE="${IMAGE:-aka-no-claw:mac-mini-realistic}"
PLATFORM="${DOCKER_PLATFORM:-}"

build_args=(build --file "${REPO_ROOT}/docker/mac-mini-realistic/Dockerfile" --tag "${IMAGE}")
if [[ -n "${PLATFORM}" ]]; then
  build_args+=(--platform "${PLATFORM}")
fi
build_args+=("${REPO_ROOT}")
docker "${build_args[@]}"

run_args=(run --rm)
if [[ -n "${PLATFORM}" ]]; then
  run_args+=(--platform "${PLATFORM}")
fi
run_args+=(--volume "${WORKSPACE_ROOT}:/source:ro" "${IMAGE}")
docker "${run_args[@]}"
