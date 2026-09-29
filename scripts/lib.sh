set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

COMPOSE_BASE="-f docker-compose.yml"
COMPOSE_DEV="$COMPOSE_BASE -f docker-compose.dev.yml"

ensure_env() {
  if [ ! -f .env ]; then
    cp .env.example .env
    echo "created .env from .env.example"
  fi
}

require_docker() {
  if ! docker info >/dev/null 2>&1; then
    echo "docker is not reachable." >&2
    echo "  inside WSL, start it with:  sudo service docker start" >&2
    exit 1
  fi
}
