#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"
IMAGE="${IMAGE:-aka-no-claw:rpi5-smoke}"

build_args=(build --file "${REPO_ROOT}/docker/rpi5-smoke/Dockerfile" --tag "${IMAGE}")
if [[ -n "${DOCKER_PLATFORM:-}" ]]; then
  build_args+=(--platform "${DOCKER_PLATFORM}")
fi
build_args+=("${REPO_ROOT}")

docker "${build_args[@]}"

docker_args=(run --rm)
if [[ -n "${DOCKER_PLATFORM:-}" ]]; then
  docker_args+=(--platform "${DOCKER_PLATFORM}")
fi
docker_args+=(--volume "${WORKSPACE_ROOT}:/source:ro" "${IMAGE}")

docker "${docker_args[@]}"
