#!/usr/bin/env bash
# Browser check of two people approving at once: frontend/approve.mjs.
#
#   tests/run_approval_check.sh
#
# Starts the API (tests/approval_server.py) and the UI (the Vite dev server)
# on free loopback ports that the system picks, runs approve.mjs against them,
# and stops both. The API runs the agents in its own process and keeps every
# hospital's records in memory. Its release gates let every candidate through,
# because the check needs two candidates a person may approve; the planner
# still makes each decision. The check changes the model in use, so it never
# runs against a demo someone else started.
#
# Needs the planner (DAGENTSC_BIN, or the dune build) and a Chromium
# (CHROMIUM_PATH, or a preinstalled one). Exits 0 when the check passes, 1
# when it fails, and 2 when no browser is available. Exit 2 is a skip, not a
# pass.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${APP_ROOT}/../.." && pwd)}"

if [[ ! -d "${DAGENTS_HOME}/dagents/common" ]]; then
  echo "Could not find the Dagents framework. Set DAGENTS_HOME to your Dagents checkout." >&2
  exit 1
fi

PLANNER="${DAGENTSC_BIN:-${DAGENTS_HOME}/bindings/ocaml/_build/default/bin/dagentsc.exe}"
if [[ ! -x "${PLANNER}" ]] && ! command -v dagentsc >/dev/null 2>&1; then
  echo "The dagentsc planner is not built. Without it the Guard denies every request." >&2
  echo "  cd ${DAGENTS_HOME}/bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe" >&2
  exit 1
fi
[[ -x "${PLANNER}" ]] && export DAGENTSC_BIN="${PLANNER}"

PYTHON="${PYTHON:-${DAGENTS_HOME}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to serve the frontend." >&2
  exit 1
fi
if [[ ! -d "${APP_ROOT}/frontend/node_modules" ]]; then
  echo "Installing frontend dependencies (first run only)"
  (cd "${APP_ROOT}/frontend" && npm install --no-audit --no-fund)
fi

# Two loopback ports that nothing listens on, chosen by the system. Both
# sockets are open together, so the two ports differ.
read -r API_PORT UI_PORT < <("${PYTHON}" -c 'import socket
sockets = [socket.socket() for _ in range(2)]
for s in sockets:
    s.bind(("127.0.0.1", 0))
print(*(s.getsockname()[1] for s in sockets))')

BACKEND_PID=""
FRONTEND_PID=""
LOG_DIR="$(mktemp -d)"

# Kill a process and everything it started: `npm run dev` starts a shell,
# which starts the node process that holds the port.
kill_tree() {
  local pid="$1" signal="${2:-TERM}" child
  [[ -z "${pid}" ]] && return 0
  for child in $(pgrep -P "${pid}" 2>/dev/null || true); do
    kill_tree "${child}" "${signal}"
  done
  kill "-${signal}" "${pid}" 2>/dev/null || true
}

cleanup() {
  trap - EXIT INT TERM
  kill_tree "${FRONTEND_PID}" TERM
  kill_tree "${BACKEND_PID}" TERM
  sleep 1
  kill_tree "${FRONTEND_PID}" KILL
  kill_tree "${BACKEND_PID}" KILL
  wait "${FRONTEND_PID}" 2>/dev/null || true
  wait "${BACKEND_PID}" 2>/dev/null || true
  rm -rf "${LOG_DIR}"
}
trap cleanup EXIT INT TERM

wait_for() {
  local url="$1" name="$2" log="$3" pid="$4"
  for ((i = 0; i < 90; i++)); do
    if curl -sf -o /dev/null "${url}"; then return 0; fi
    if ! kill -0 "${pid}" 2>/dev/null; then break; fi
    sleep 1
  done
  echo "${name} did not come up at ${url}. Last log lines:" >&2
  tail -20 "${log}" >&2
  return 1
}

echo "Starting the API on :${API_PORT}, with gates that let every candidate through"
(
  cd "${APP_ROOT}"
  PYTHONPATH="${DAGENTS_HOME}:${APP_ROOT}/backend" exec "${PYTHON}" -m tests.approval_server --port "${API_PORT}"
) >"${LOG_DIR}/backend.log" 2>&1 &
BACKEND_PID=$!
wait_for "http://127.0.0.1:${API_PORT}/api/v1/health" "The API" "${LOG_DIR}/backend.log" "${BACKEND_PID}"

echo "Starting the UI on :${UI_PORT}"
(
  cd "${APP_ROOT}/frontend"
  HEALTHCARE_DEMO_API_URL="http://127.0.0.1:${API_PORT}" exec npm run dev -- --host 127.0.0.1 --port "${UI_PORT}" --strictPort
) >"${LOG_DIR}/frontend.log" 2>&1 &
FRONTEND_PID=$!
wait_for "http://127.0.0.1:${UI_PORT}/" "The UI" "${LOG_DIR}/frontend.log" "${FRONTEND_PID}"

set +e
(cd "${APP_ROOT}/frontend" && node approve.mjs "http://127.0.0.1:${UI_PORT}/")
STATUS=$?
set -e
case "${STATUS}" in
  0) ;;
  2) echo "(approval check skipped: no browser available)" ;;
  *)
    echo "The approval check failed. Last API log lines:" >&2
    tail -20 "${LOG_DIR}/backend.log" >&2
    ;;
esac
exit "${STATUS}"
