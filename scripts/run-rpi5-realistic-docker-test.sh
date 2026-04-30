#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="$(cd "${REPO_ROOT}/.." && pwd)"
IMAGE="${IMAGE:-aka-no-claw:rpi5-realistic}"

build_args=(build --file "${REPO_ROOT}/docker/rpi5-realistic/Dockerfile" --tag "${IMAGE}")
if [[ -n "${DOCKER_PLATFORM:-}" ]]; then
  build_args+=(--platform "${DOCKER_PLATFORM}")
fi
build_args+=("${REPO_ROOT}")

docker "${build_args[@]}"

docker_args=(run --rm)
if [[ -n "${DOCKER_PLATFORM:-}" ]]; then
  docker_args+=(--platform "${DOCKER_PLATFORM}")
fi
docker_args+=(
  --volume "${WORKSPACE_ROOT}:/source:ro"
  --env "REALISTIC_SETUP_OLLAMA=${REALISTIC_SETUP_OLLAMA:-0}"
  --env "REALISTIC_SETUP_OLLAMA_VISION=${REALISTIC_SETUP_OLLAMA_VISION:-0}"
  "${IMAGE}"
)

docker "${docker_args[@]}"
