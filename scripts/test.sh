#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
require_docker

docker compose $COMPOSE_BASE run --rm --no-deps \
  -v "$REPO_ROOT/tests:/app/tests" \
  -v "$REPO_ROOT/src:/app/src" \
  -v "$REPO_ROOT/migrations:/app/migrations" \
  -v "$REPO_ROOT/prompts:/app/prompts" \
  -v "$REPO_ROOT/dev.py:/app/dev.py:ro" \
  -v "$REPO_ROOT/pyproject.toml:/app/pyproject.toml" \
  --entrypoint sh api -c \
  'uv sync --frozen -q && PYTHONPATH=/app:/app/src uv run pytest -q "$@" && echo && uv run ruff check src tests migrations' \
  -- "$@"
