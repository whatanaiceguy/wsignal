#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
require_docker
ensure_env

docker compose $COMPOSE_BASE run --rm \
  -v "$REPO_ROOT/src:/app/src:ro" \
  --entrypoint python api -m wsignal.retier_cli "$@"
