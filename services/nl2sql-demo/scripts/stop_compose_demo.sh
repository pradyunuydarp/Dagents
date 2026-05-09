#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_ENV="${COMPOSE_ENV:-$ROOT_DIR/env/.env.compose}"

if ! docker info >/dev/null 2>&1; then
  printf 'Docker daemon is not reachable; nothing can be stopped from this script.\n' >&2
  exit 1
fi

(cd "$ROOT_DIR" && docker compose --env-file "$COMPOSE_ENV" down)
