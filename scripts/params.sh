#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
require_docker
ensure_env

mounts=(
  -v "$REPO_ROOT/src:/app/src:ro"
  -v "$REPO_ROOT/prompts:/app/prompts:ro"
)
if [ -d "$REPO_ROOT/docs/reference" ]; then
  mounts+=(-v "$REPO_ROOT/docs/reference:/app/docs/reference:ro")
fi

docker compose $COMPOSE_BASE run --rm \
  "${mounts[@]}" \
  --entrypoint python api -m wsignal.params_cli "$@"
