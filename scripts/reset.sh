#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
require_docker

echo "this drops the database volume. ctrl-c within 3s to abort."
sleep 3
docker compose $COMPOSE_DEV down -v
exec "$REPO_ROOT/scripts/up.sh"
