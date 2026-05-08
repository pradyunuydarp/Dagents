#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_ENV="${COMPOSE_ENV:-$ROOT_DIR/env/.env.compose}"

(cd "$ROOT_DIR" && docker compose --env-file "$COMPOSE_ENV" down)
