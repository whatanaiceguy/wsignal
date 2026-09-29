# syntax=docker/dockerfile:1

# The web app is built here and served by the api, so the whole service is one port.
FROM node:24-slim AS web
WORKDIR /web
RUN corepack enable && corepack prepare pnpm@10.30.0 --activate
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies resolve from the lock alone, so this layer is cached until the
# lock changes. Source edits below do not reinstall anything.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY migrations/ ./migrations/
COPY alembic.ini ./

# Prompts are files, loaded and hashed at runtime, so they are part of the
# image rather than of the source tree only. Without this the container starts
# fine and fails on the first run with a missing file, while a local checkout
# works - the worst version of this bug.
COPY prompts/ ./prompts/

RUN uv sync --frozen --no-dev

COPY --from=web /web/dist ./web/dist

EXPOSE 8000
CMD ["uvicorn", "wsignal.interface.app:app", "--host", "0.0.0.0", "--port", "8000"]
