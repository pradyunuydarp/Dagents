#!/usr/bin/env bash
# Run the NL2SQL demo locally, with no Docker: the framework services, the
# backend and the UI.
#
#   services/nl2sql-demo/scripts/run_local_demo.sh          # start everything and wait
#   services/nl2sql-demo/scripts/run_local_demo.sh --check  # build the UI, run the browser check, exit
#
# It starts core-service, pipeline-service, model-service, the LMA and the GMA,
# then the NL2SQL backend, which calls all five for each question, then the UI.
# model-service needs torch. Without it a normal run goes on and the page shows
# model-service as unavailable; --check fails.
#
# A normal run uses the addresses in env/.env.shared (the *_PUBLIC_URL
# variables) or the same variables from the environment, and serves the UI with
# the Vite dev server on FRONTEND_PORT (default 5173).
#
# --check picks free loopback ports itself, builds the UI and serves the bundle
# with `vite preview`, which sends /api to the backend. Then it runs
# frontend/smoke.mjs against it and gives the smoke test pipeline-service's
# process id, so its last check can stop that service. It exits 1 when the
# smoke test fails, and 0 when it passes or skips because no browser is
# available ("smoke test skipped" in the output).
#
# The planner comes from DAGENTSC_BIN when it is set. Otherwise the script
# builds it with `opam exec -- dune build`. PYTHON_BIN chooses the Python that
# runs the services (default: .venv/bin/python, then python3). Ctrl-C stops
# every process the script started.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
APP_DIR="$ROOT_DIR/services/nl2sql-demo"
BACKEND_DIR="$APP_DIR/backend"
FRONTEND_DIR="$APP_DIR/frontend"
SHARED_ENV="$ROOT_DIR/env/.env.shared"

MODE="run"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check) MODE="check"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/.venv/bin/python}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="${PYTHON_BIN_FALLBACK:-python3}"
fi

# Every address below is on this machine, so no request may go through a proxy.
export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"

section() {
  printf '\n== %s ==\n' "$1"
}

LOG_DIR="$(mktemp -d)"
PIDS=()
FRONTEND_PID=""

# Kill a process and everything it started. `npm run dev` and `npx vite
# preview` start a shell that starts node, and signalling the outer process
# alone leaves the server holding its port.
kill_tree() {
  local pid="$1" signal="${2:-TERM}" child
  [[ -z "$pid" ]] && return 0
  for child in $(pgrep -P "$pid" 2>/dev/null || true); do
    kill_tree "$child" "$signal"
  done
  kill "-$signal" "$pid" 2>/dev/null || true
}

cleanup() {
  trap - EXIT INT TERM
  local pid waited=0
  # The ${…+…} form keeps `set -u` quiet in bash 3.2 (macOS) while PIDS is empty.
  local all=("$FRONTEND_PID" ${PIDS[@]+"${PIDS[@]}"})
  for pid in "${all[@]}"; do kill_tree "$pid" TERM; done
  while (( waited < 40 )); do
    local alive=""
    for pid in "${all[@]}"; do
      if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then alive="yes"; fi
    done
    [[ -z "$alive" ]] && break
    sleep 0.25
    waited=$((waited + 1))
  done
  for pid in "${all[@]}"; do kill_tree "$pid" KILL; done
  wait 2>/dev/null || true
  rm -rf "$LOG_DIR"
}
trap cleanup EXIT INT TERM

log_tail() {
  printf -- '--- last lines of %s ---\n' "$(basename "$1")" >&2
  tail -20 "$1" >&2 || true
}

# Wait until a URL answers, or the process behind it exits, or 90 seconds pass.
# Importing torch makes model-service the slowest to start.
wait_for() {
  local url="$1" label="$2" pid="$3" log="$4"
  for _ in $(seq 1 180); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
      printf '%s is ready: %s\n' "$label" "$url"
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then break; fi
    sleep 0.5
  done
  printf '%s did not become ready at %s\n' "$label" "$url" >&2
  log_tail "$log"
  return 1
}

# A variable from the environment, or else from env/.env.shared.
shared() {
  local value="${!1:-}"
  if [[ -z "$value" && -f "$SHARED_ENV" ]]; then
    value="$(sed -n "s/^$1=//p" "$SHARED_ENV" | tail -n 1)"
  fi
  printf '%s' "$value"
}

# The port of an http://host:port address.
port_of() {
  local rest="${1#*://}"
  rest="${rest%%/*}"
  printf '%s' "${rest##*:}"
}

# Free loopback ports, held together while they are chosen so they differ.
free_ports() {
  "$PYTHON_BIN" - "$1" <<'PY'
import socket
import sys

held = []
for _ in range(int(sys.argv[1])):
    bound = socket.socket()
    bound.bind(("127.0.0.1", 0))
    held.append(bound)
print(" ".join(str(bound.getsockname()[1]) for bound in held))
PY
}

# --- the planner and the tools ---------------------------------------------

if [[ -n "${DAGENTSC_BIN:-}" ]]; then
  if ! command -v "$DAGENTSC_BIN" >/dev/null 2>&1; then
    echo "DAGENTSC_BIN is set to $DAGENTSC_BIN, which is not an executable file." >&2
    exit 1
  fi
else
  section "Build the OCaml planner"
  (cd "$ROOT_DIR/bindings/ocaml" && opam exec -- dune build ./bin/dagentsc.exe)
  DAGENTSC_BIN="$ROOT_DIR/bindings/ocaml/_build/default/bin/dagentsc.exe"
fi
export DAGENTSC_BIN

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to serve the frontend." >&2
  exit 1
fi

section "Install frontend dependencies if needed"
# --check also needs playwright-core, which an older install may lack.
if [ ! -d "$FRONTEND_DIR/node_modules" ] || { [[ "$MODE" == "check" ]] && [ ! -d "$FRONTEND_DIR/node_modules/playwright-core" ]; }; then
  (cd "$FRONTEND_DIR" && npm ci --no-audit --no-fund)
fi

# --- ports -------------------------------------------------------------------

SERVICE_VARS=(CORE_SERVICE_PUBLIC_URL PIPELINE_SERVICE_PUBLIC_URL MODEL_SERVICE_PUBLIC_URL LMA_PUBLIC_URL GMA_PUBLIC_URL)
if [[ "$MODE" == "check" ]]; then
  read -r -a CHOSEN <<<"$(free_ports 7)"
  for index in "${!SERVICE_VARS[@]}"; do
    export "${SERVICE_VARS[$index]}=http://127.0.0.1:${CHOSEN[$index]}"
  done
  BACKEND_PORT="${CHOSEN[5]}"
  FRONTEND_PORT="${CHOSEN[6]}"
  BACKEND_HOST="127.0.0.1"
  # An empty folder, so the check runs the heuristic adapter whatever model
  # files this machine has.
  mkdir -p "$LOG_DIR/models"
  export NL2SQL_MODELS_DIR="$LOG_DIR/models"
  export NL2SQL_REQUEST_TIMEOUT_SECONDS="${NL2SQL_REQUEST_TIMEOUT_SECONDS:-30}"
else
  for variable in "${SERVICE_VARS[@]}"; do
    value="$(shared "$variable")"
    if [[ -z "$value" ]]; then
      echo "$variable is not set in the environment or in env/.env.shared." >&2
      exit 1
    fi
    export "$variable=$value"
  done
  BACKEND_PORT="${BACKEND_PORT:-$(port_of "$(shared NL2SQL_DEMO_PUBLIC_URL)")}"
  FRONTEND_PORT="${FRONTEND_PORT:-5173}"
  BACKEND_HOST="0.0.0.0"
  export NL2SQL_MODELS_DIR="${NL2SQL_MODELS_DIR:-$ROOT_DIR/models}"
fi

# --- the framework services and the backend ----------------------------------

PIPELINE_PID=""
MISSING=()

start_service() {
  local name="$1" module="$2" path="$3" url="$4" health="$5"
  PYTHONPATH="$path" "$PYTHON_BIN" -m uvicorn "$module" \
    --host 127.0.0.1 --port "$(port_of "$url")" --log-level warning >"$LOG_DIR/$name.log" 2>&1 &
  local pid=$!
  PIDS+=("$pid")
  [[ "$name" == "pipeline-service" ]] && PIPELINE_PID="$pid"
  if ! wait_for "$url$health" "$name" "$pid" "$LOG_DIR/$name.log"; then
    if [[ "$MODE" == "check" ]]; then
      echo "The check needs every framework service, and $name did not start." >&2
      exit 1
    fi
    MISSING+=("$name")
  fi
}

section "Start the framework services"
start_service core-service app.main:app "$ROOT_DIR:$ROOT_DIR/services/core-service" "$CORE_SERVICE_PUBLIC_URL" /api/v1/health
start_service pipeline-service app.main:app "$ROOT_DIR:$ROOT_DIR/services/pipeline-service" "$PIPELINE_SERVICE_PUBLIC_URL" /api/v1/health
start_service model-service app.main:app "$ROOT_DIR:$ROOT_DIR/services/model-service" "$MODEL_SERVICE_PUBLIC_URL" /api/v1/health
start_service lma dagents.lma.main:app "$ROOT_DIR" "$LMA_PUBLIC_URL" /health
start_service gma dagents.gma.main:app "$ROOT_DIR" "$GMA_PUBLIC_URL" /health
if (( ${#MISSING[@]} > 0 )); then
  printf 'Not running: %s. The page shows each one as unavailable.\n' "${MISSING[*]}" >&2
fi

section "Start the NL2SQL backend"
PYTHONPATH="$BACKEND_DIR:$ROOT_DIR" \
NL2SQL_API_PORT="$BACKEND_PORT" \
"$PYTHON_BIN" -m uvicorn app.main:app \
  --app-dir "$BACKEND_DIR" \
  --host "$BACKEND_HOST" \
  --port "$BACKEND_PORT" >"$LOG_DIR/backend.log" 2>&1 &
BACKEND_PID=$!
PIDS+=("$BACKEND_PID")
wait_for "http://127.0.0.1:$BACKEND_PORT/api/v1/health" "backend" "$BACKEND_PID" "$LOG_DIR/backend.log"

# --- the browser check --------------------------------------------------------

if [[ "$MODE" == "check" ]]; then
  section "Build the NL2SQL frontend"
  # A live build: relative /api paths, sent to the backend by `vite preview`.
  (cd "$FRONTEND_DIR" && env -u VITE_DAGENTS_STATIC -u VITE_NL2SQL_API_BASE -u DEMO_BASE_PATH npm run build) \
    >"$LOG_DIR/build.log" 2>&1 || { log_tail "$LOG_DIR/build.log"; exit 1; }

  section "Serve the bundle"
  (
    cd "$FRONTEND_DIR"
    NL2SQL_DEMO_API_URL="http://127.0.0.1:$BACKEND_PORT" exec npx vite preview \
      --host 127.0.0.1 --port "$FRONTEND_PORT" --strictPort
  ) >"$LOG_DIR/frontend.log" 2>&1 &
  FRONTEND_PID=$!
  wait_for "http://127.0.0.1:$FRONTEND_PORT/" "frontend" "$FRONTEND_PID" "$LOG_DIR/frontend.log"

  section "Run the frontend smoke test"
  set +e
  (cd "$FRONTEND_DIR" && node smoke.mjs "http://127.0.0.1:$FRONTEND_PORT/" --pipeline-service-pid "$PIPELINE_PID")
  SMOKE_STATUS=$?
  set -e
  case "$SMOKE_STATUS" in
    0) ;;
    2) echo "(smoke test skipped: no browser available)" ;;
    *)
      echo "The frontend smoke test failed." >&2
      log_tail "$LOG_DIR/backend.log"
      exit "$SMOKE_STATUS"
      ;;
  esac
  exit 0
fi

# --- the UI ------------------------------------------------------------------

section "Start the NL2SQL frontend"
(cd "$FRONTEND_DIR" && VITE_NL2SQL_API_BASE="http://127.0.0.1:$BACKEND_PORT" exec npm run dev -- --host 0.0.0.0 --port "$FRONTEND_PORT") \
  >"$LOG_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!
wait_for "http://127.0.0.1:$FRONTEND_PORT" "frontend" "$FRONTEND_PID" "$LOG_DIR/frontend.log"

section "Demo is running"
printf 'Open: http://127.0.0.1:%s\n' "$FRONTEND_PORT"
printf 'Backend API: http://127.0.0.1:%s/api/v1\n' "$BACKEND_PORT"
printf 'Logs: %s\n' "$LOG_DIR"
printf 'Press Ctrl-C to stop every process.\n'
wait
