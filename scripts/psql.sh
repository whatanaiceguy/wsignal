#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
ensure_env
user="$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2)"; user="${user:-wsignal}"
db="$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2)"; db="${db:-wsignal}"
docker compose $COMPOSE_DEV exec db psql -U "$user" -d "$db" "$@"
