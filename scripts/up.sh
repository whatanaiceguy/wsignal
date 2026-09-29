#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
require_docker
ensure_env

docker compose $COMPOSE_DEV up --build -d "$@"

echo
echo "waiting for the api to answer..."
port="$(grep -E '^API_PORT=' .env | cut -d= -f2)"
port="${port:-8000}"
for _ in $(seq 1 60); do
  if curl -fsS -m 2 "http://localhost:${port}/health" >/dev/null 2>&1; then
    curl -s "http://localhost:${port}/health"; echo
    echo
    echo "api   http://localhost:${port}"
    echo "docs  http://localhost:${port}/docs"
    exit 0
  fi
  sleep 1
done

echo "api did not answer within 60s. last logs:" >&2
docker compose $COMPOSE_DEV logs --tail 40 api >&2
exit 1
