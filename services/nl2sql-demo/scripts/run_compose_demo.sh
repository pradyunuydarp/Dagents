#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_ENV="${COMPOSE_ENV:-$ROOT_DIR/env/.env.compose}"
if [ -f "$COMPOSE_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$COMPOSE_ENV"
  set +a
fi
FRONTEND_PORT="${NL2SQL_DEMO_FRONTEND_HOST_PORT:-5173}"
BACKEND_PORT="${NL2SQL_DEMO_BACKEND_HOST_PORT:-8070}"
export NL2SQL_INSTALL_OPTIONAL_MODELS="${NL2SQL_INSTALL_OPTIONAL_MODELS:-false}"

section() {
  printf '\n== %s ==\n' "$1"
}

require_docker() {
  if ! docker info >/dev/null 2>&1; then
    printf 'Docker daemon is not reachable. Start Docker Desktop and rerun this script.\n' >&2
    exit 1
  fi
}

wait_for() {
  local url="$1"
  local label="$2"
  for _ in $(seq 1 90); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      printf '%s is ready: %s\n' "$label" "$url"
      return 0
    fi
    sleep 2
  done
  printf '%s did not become ready: %s\n' "$label" "$url" >&2
  return 1
}

require_docker

section "Build OCaml functional planner"
(cd "$ROOT_DIR/bindings/ocaml" && opam exec -- dune build ./bin/dagentsc.exe)

section "Start Dagents stack and NL2SQL app"
printf 'NL2SQL optional model dependencies: %s\n' "$NL2SQL_INSTALL_OPTIONAL_MODELS"
(cd "$ROOT_DIR" && docker compose --env-file "$COMPOSE_ENV" up -d --build \
  gma lma model-service pipeline-service core-service nl2sql-demo-backend nl2sql-demo-frontend)

wait_for "http://127.0.0.1:$BACKEND_PORT/api/v1/health" "nl2sql backend"
wait_for "http://127.0.0.1:$FRONTEND_PORT" "nl2sql frontend"

section "Demo is running"
printf 'Open: http://127.0.0.1:%s\n' "$FRONTEND_PORT"
printf 'Probe command: %s/services/nl2sql-demo/scripts/probe_api.sh\n' "$ROOT_DIR"
printf 'Stop command: docker compose --env-file %s down\n' "$COMPOSE_ENV"
