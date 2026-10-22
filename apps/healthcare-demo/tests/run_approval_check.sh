#!/usr/bin/env bash
# Check the release approval in a browser: the pilot panel's "Approve release".
#
# The demo's own release gates decide whatever the records say, so a run may
# never enable "Approve release". This script starts the API with one release
# gate a trained candidate passes (tests/lenient_gates_server.py, which only
# tests start), starts the Vite dev server in front of it, and runs
# frontend/approve.mjs. Both servers listen on free loopback ports. The agents
# run inside the API process, as on the published demo.
#
#   apps/healthcare-demo/tests/run_approval_check.sh
#
# No Docker and no database are needed: each hospital's records are in memory.
# The check always runs on in-memory stores, because the release it approves
# passed only the test gate. So the script clears HEALTHCARE_DEMO_DATABASE_URL
# and every HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL for the API it starts, and
# the server refuses to start on any database other than a test's scratch
# database.
#
# The planner comes from DAGENTSC_BIN, else the dune build in the Dagents
# checkout, else dagentsc on PATH. The script never builds it.
#
# Exits with approve.mjs's status: 0 passed, 1 failed, 2 skipped (no browser
# or no playwright-core). Exit 2 is not a pass, so CI fails on it.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${APP_ROOT}/../.." && pwd)}"
FRONTEND="${APP_ROOT}/frontend"

if [[ ! -d "${DAGENTS_HOME}/dagents/common" ]]; then
  echo "Could not find the Dagents framework. Set DAGENTS_HOME to a Dagents checkout." >&2
  exit 1
fi

PLANNER="${DAGENTSC_BIN:-${DAGENTS_HOME}/bindings/ocaml/_build/default/bin/dagentsc.exe}"
if [[ -x "${PLANNER}" ]]; then
  export DAGENTSC_BIN="${PLANNER}"
elif ! command -v dagentsc >/dev/null 2>&1; then
  echo "The dagentsc planner was not found. Set DAGENTSC_BIN, or build it:" >&2
  echo "  cd ${DAGENTS_HOME}/bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe" >&2
  echo "Without it the Ethical Guard denies every request and no pilot reaches its gates." >&2
  exit 1
fi

PYTHON="${PYTHON:-${DAGENTS_HOME}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"
for tool in node npm curl; do
  command -v "${tool}" >/dev/null 2>&1 || { echo "${tool} is required." >&2; exit 1; }
done
if [[ ! -x "${FRONTEND}/node_modules/.bin/vite" ]]; then
  echo "Installing the frontend's dependencies (first run only)"
  (cd "${FRONTEND}" && npm ci --no-audit --no-fund)
fi

LOG_DIR="$(mktemp -d)"
API_PID=""
UI_PID=""

# Each server runs in a process group of its own, so cleanup stops everything
# it starts: vite runs esbuild, and the API runs the planner. A background job
# of this script is not a group leader, so `exec setsid` keeps its PID.
SETSID=""
command -v setsid >/dev/null 2>&1 && SETSID="setsid"

# Stop a process group, then any child left outside it.
stop_group() {
  local pid="$1" child
  [[ -z "${pid}" ]] && return 0
  kill -TERM -- "-${pid}" 2>/dev/null || true
  for child in $(pgrep -P "${pid}" 2>/dev/null || true); do kill -TERM "${child}" 2>/dev/null || true; done
  kill -TERM "${pid}" 2>/dev/null || true
  for _ in $(seq 1 40); do
    kill -0 "${pid}" 2>/dev/null || break
    sleep 0.25
  done
  kill -KILL -- "-${pid}" 2>/dev/null || true
  kill -KILL "${pid}" 2>/dev/null || true
  wait "${pid}" 2>/dev/null || true
}

cleanup() {
  trap - EXIT INT TERM
  stop_group "${UI_PID}"
  stop_group "${API_PID}"
  rm -rf "${LOG_DIR}"
}
trap cleanup EXIT INT TERM

# Wait until a URL answers, while the process that serves it is still running.
wait_for() {
  local url="$1" pid="$2" name="$3" log="$4" seconds="${5:-120}"
  for ((i = 0; i < seconds * 2; i++)); do
    if curl --noproxy '*' -sf -o /dev/null "${url}"; then return 0; fi
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "${name} stopped before it answered. Last log lines:" >&2
      tail -20 "${log}" >&2
      return 1
    fi
    sleep 0.5
  done
  echo "${name} did not answer within ${seconds} seconds. Last log lines:" >&2
  tail -20 "${log}" >&2
  return 1
}

free_port() {
  "${PYTHON}" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

echo "Starting the API with a release gate a trained candidate passes"
(
  cd "${APP_ROOT}/backend"
  # In-memory stores only, whatever this shell names: a release approved under
  # the test gate must never reach a database the demo uses. The empty value
  # also stops the app's env files from supplying one, because they never
  # replace a variable that is already set.
  for name in $(compgen -e); do
    if [[ "${name}" =~ ^HEALTHCARE_DEMO_SITE_.+_DATABASE_URL$ ]]; then unset "${name}"; fi
  done
  export HEALTHCARE_DEMO_DATABASE_URL=""
  # The agents run in process and nothing can add a hospital, whatever this
  # machine's environment says.
  HEALTHCARE_DEMO_AGENTS=in-process HEALTHCARE_DEMO_DEPLOYER=off NO_PROXY="127.0.0.1,localhost" \
    PYTHONPATH="${DAGENTS_HOME}:${APP_ROOT}/backend" DAGENTS_HOME="${DAGENTS_HOME}" \
    exec ${SETSID} "${PYTHON}" "${HERE}/lenient_gates_server.py" --port-file "${LOG_DIR}/api.port"
) >"${LOG_DIR}/api.log" 2>&1 &
API_PID=$!
for _ in $(seq 1 240); do
  [[ -s "${LOG_DIR}/api.port" ]] && break
  if ! kill -0 "${API_PID}" 2>/dev/null; then
    echo "The API stopped before it chose a port. Last log lines:" >&2
    tail -20 "${LOG_DIR}/api.log" >&2
    exit 1
  fi
  sleep 0.5
done
[[ -s "${LOG_DIR}/api.port" ]] || { echo "The API did not choose a port." >&2; exit 1; }
API_PORT="$(cat "${LOG_DIR}/api.port")"
wait_for "http://127.0.0.1:${API_PORT}/api/v1/health" "${API_PID}" "The API" "${LOG_DIR}/api.log"

echo "Starting the UI"
UI_PORT=""
for _ in 1 2 3; do
  port="$(free_port)"
  (
    cd "${FRONTEND}"
    HEALTHCARE_DEMO_API_URL="http://127.0.0.1:${API_PORT}" \
      exec ${SETSID} node_modules/.bin/vite --host 127.0.0.1 --port "${port}" --strictPort
  ) >"${LOG_DIR}/ui.log" 2>&1 &
  UI_PID=$!
  if wait_for "http://127.0.0.1:${port}/" "${UI_PID}" "The UI" "${LOG_DIR}/ui.log" 60 2>/dev/null; then
    UI_PORT="${port}"
    break
  fi
  # Another process took the port between choosing it and binding it.
  stop_group "${UI_PID}"
  UI_PID=""
done
if [[ -z "${UI_PORT}" ]]; then
  echo "The UI did not start. Last log lines:" >&2
  tail -20 "${LOG_DIR}/ui.log" >&2
  exit 1
fi

echo "Running the approval check"
set +e
(cd "${FRONTEND}" && node approve.mjs "http://127.0.0.1:${UI_PORT}/")
STATUS=$?
set -e
if [[ "${STATUS}" == 2 ]]; then
  echo "The approval check skipped: no browser or no playwright-core. A skip is not a pass." >&2
elif [[ "${STATUS}" != 0 ]]; then
  echo "The approval check failed. Last lines of the API log:" >&2
  tail -30 "${LOG_DIR}/api.log" >&2
fi
exit "${STATUS}"
