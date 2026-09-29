#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
require_docker
ensure_env

docker compose $COMPOSE_BASE run --rm --no-deps \
  -v "$REPO_ROOT/tmp:/app/tmp" \
  -v "$REPO_ROOT/src:/app/src:ro" \
  --entrypoint python api "/app/tmp/$1" "${@:2}"
