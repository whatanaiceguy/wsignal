#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
docker compose $COMPOSE_DEV down "$@"
