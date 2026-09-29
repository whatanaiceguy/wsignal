#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
ensure_env
if [ $# -eq 0 ]; then set -- upgrade head; fi
docker compose $COMPOSE_BASE run --rm \
  -v "$REPO_ROOT/migrations:/app/migrations" \
  migrate alembic "$@"
