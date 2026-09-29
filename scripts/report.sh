#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
require_docker
ensure_env

mkdir -p "$REPO_ROOT/tmp/runs"

docker compose $COMPOSE_DEV run --rm \
  -v "$REPO_ROOT/tmp:/app/tmp" \
  --entrypoint python api -m wsignal.report_cli "$@"
