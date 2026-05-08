#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
APP_DIR="$ROOT_DIR/services/nl2sql-demo"
BACKEND_DIR="$APP_DIR/backend"
FRONTEND_DIR="$APP_DIR/frontend"
DAGENTSC_BIN="${DAGENTSC_BIN:-$ROOT_DIR/bindings/ocaml/_build/default/bin/dagentsc.exe}"
BACKEND_PORT="${BACKEND_PORT:-8070}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="${PYTHON_BIN_FALLBACK:-python3}"
fi

cleanup() {
  if [ -n "${BACKEND_PID:-}" ]; then kill "$BACKEND_PID" 2>/dev/null || true; fi
  if [ -n "${FRONTEND_PID:-}" ]; then kill "$FRONTEND_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

section() {
  printf '\n== %s ==\n' "$1"
}

wait_for() {
  local url="$1"
  local label="$2"
  for _ in $(seq 1 45); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      printf '%s is ready: %s\n' "$label" "$url"
      return 0
    fi
    sleep 1
  done
  printf '%s did not become ready: %s\n' "$label" "$url" >&2
  return 1
}

section "Build OCaml functional planner"
(cd "$ROOT_DIR/bindings/ocaml" && opam exec -- dune build ./bin/dagentsc.exe)

section "Install frontend dependencies if needed"
if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
  (cd "$FRONTEND_DIR" && npm install)
fi

section "Start NL2SQL backend"
PYTHONPATH="$BACKEND_DIR:$ROOT_DIR" \
DAGENTSC_BIN="$DAGENTSC_BIN" \
NL2SQL_MODELS_DIR="${NL2SQL_MODELS_DIR:-$ROOT_DIR/models}" \
NL2SQL_API_PORT="$BACKEND_PORT" \
"$PYTHON_BIN" -m uvicorn app.main:app \
  --app-dir "$BACKEND_DIR" \
  --host 0.0.0.0 \
  --port "$BACKEND_PORT" &
BACKEND_PID=$!
wait_for "http://127.0.0.1:$BACKEND_PORT/api/v1/health" "backend"

section "Start NL2SQL frontend"
(cd "$FRONTEND_DIR" && VITE_NL2SQL_API_BASE="http://127.0.0.1:$BACKEND_PORT" npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT") &
FRONTEND_PID=$!
wait_for "http://127.0.0.1:$FRONTEND_PORT" "frontend"

section "Demo is running"
printf 'Open: http://127.0.0.1:%s\n' "$FRONTEND_PORT"
printf 'Backend API: http://127.0.0.1:%s/api/v1\n' "$BACKEND_PORT"
printf 'Press Ctrl-C to stop both processes.\n'
wait
