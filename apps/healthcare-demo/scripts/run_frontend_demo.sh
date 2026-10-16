#!/usr/bin/env bash
# Run the healthcare demo end to end: the API, the UI, and a short tour.
#
# One command, no Docker. Starts the API with scripts/run_local_demo.sh, starts
# the Vite dev server in front of it, waits until both answer, and prints what
# to try on each page. Without HEALTHCARE_DEMO_DATABASE_URL the API keeps each
# hospital's records in memory, loaded from the committed International Stroke
# Trial extract, so no database is needed.
#
#   scripts/run_frontend_demo.sh                        # start it and print the tour
#   scripts/run_frontend_demo.sh --verify               # run the smoke test first, then start
#   scripts/run_frontend_demo.sh --check                # run the smoke test and exit
#   scripts/run_frontend_demo.sh --screenshots <dir>    # run the smoke test, save the README
#                                                       # screenshots to <dir>, and exit
#   scripts/run_frontend_demo.sh --agents network       # run the GMA and each hospital's LMA
#                                                       # as their own processes (needs
#                                                       # HEALTHCARE_DEMO_DATABASE_URL)
#
# --agents combines with the other options. The ports come from the environment
# or from env/.env.healthcare-demo (HEALTHCARE_DEMO_API_PORT,
# HEALTHCARE_DEMO_UI_PORT and, for network mode, the agents' ports). Ctrl-C
# stops every server the script started.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${APP_ROOT}/../.." && pwd)}"
ENV_FILE="${APP_ROOT}/env/.env.healthcare-demo"

MODE="run"
SHOT_DIR=""
AGENTS="in-process"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --verify) MODE="verify"; shift ;;
    --check) MODE="check"; shift ;;
    --screenshots)
      MODE="check"
      SHOT_DIR="${2:-}"
      if [[ -z "${SHOT_DIR}" ]]; then
        echo "--screenshots needs a directory" >&2
        exit 2
      fi
      mkdir -p "${SHOT_DIR}"
      SHOT_DIR="$(cd "${SHOT_DIR}" && pwd)"
      shift 2
      ;;
    --agents)
      AGENTS="${2:-}"
      shift 2 || { echo "--agents needs in-process or network" >&2; exit 2; }
      ;;
    --agents=*) AGENTS="${1#--agents=}"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
case "${AGENTS}" in
  in-process|network) ;;
  *) echo "--agents must be in-process or network, not '${AGENTS}'" >&2; exit 2 ;;
esac

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim() { printf '\033[2m%s\033[0m\n' "$*"; }

# --- preflight -------------------------------------------------------------

if [[ ! -d "${DAGENTS_HOME}/dagents/common" ]]; then
  echo "Could not find the Dagents framework." >&2
  echo "Set DAGENTS_HOME to your Dagents checkout, for example:" >&2
  echo "  DAGENTS_HOME=~/src/Dagents $0" >&2
  exit 1
fi

PLANNER="${DAGENTSC_BIN:-${DAGENTS_HOME}/bindings/ocaml/_build/default/bin/dagentsc.exe}"
if [[ ! -x "${PLANNER}" ]] && ! command -v dagentsc >/dev/null 2>&1; then
  echo "The dagentsc planner is not built." >&2
  echo "  cd ${DAGENTS_HOME}/bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe" >&2
  echo >&2
  echo "Without it the Ethical Guard denies every request. That is the correct" >&2
  echo "behaviour, but then every panel in the UI shows a denial." >&2
  exit 1
fi
[[ -x "${PLANNER}" ]] && export DAGENTSC_BIN="${PLANNER}"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required to serve the frontend." >&2
  exit 1
fi

PYTHON="${PYTHON:-${DAGENTS_HOME}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"

# A port from the environment, or else from env/.env.healthcare-demo.
port() {
  local value="${!1:-}"
  if [[ -z "${value}" && -f "${ENV_FILE}" ]]; then
    value="$(sed -n "s/^$1=//p" "${ENV_FILE}" | tail -n 1)"
  fi
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "$1 must be a port number. Set it in the environment or in env/.env.healthcare-demo." >&2
    return 1
  fi
  printf '%s' "${value}"
}

API_PORT="$(port HEALTHCARE_DEMO_API_PORT)"
UI_PORT="$(port HEALTHCARE_DEMO_UI_PORT)"
export HEALTHCARE_DEMO_API_PORT="${API_PORT}"

if [[ "${AGENTS}" == "network" && -z "${HEALTHCARE_DEMO_DATABASE_URL:-}" ]]; then
  echo "--agents network needs HEALTHCARE_DEMO_DATABASE_URL." >&2
  echo "Agents in separate processes cannot read another process's in-memory store." >&2
  exit 1
fi

if [[ ! -d "${APP_ROOT}/frontend/node_modules" ]]; then
  bold "Installing frontend dependencies (first run only)"
  (cd "${APP_ROOT}/frontend" && npm install --no-audit --no-fund)
fi

# --- process management ----------------------------------------------------

BACKEND_PID=""
FRONTEND_PID=""
LOG_DIR="$(mktemp -d)"

# Kill a process and everything it started.
#
# Killing the PID alone is not enough: `npm run dev` starts `sh -c vite`, which
# starts a node process, and signalling the outer shell leaves the dev server
# holding the port. The next run would then fail to bind.
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
  local waited=0
  while (( waited < 20 )); do
    if ! kill -0 "${FRONTEND_PID}" 2>/dev/null && ! kill -0 "${BACKEND_PID}" 2>/dev/null; then
      break
    fi
    sleep 0.25
    waited=$((waited + 1))
  done
  kill_tree "${FRONTEND_PID}" KILL
  kill_tree "${BACKEND_PID}" KILL
  wait "${FRONTEND_PID}" 2>/dev/null || true
  wait "${BACKEND_PID}" 2>/dev/null || true
  rm -rf "${LOG_DIR}"
}
trap cleanup EXIT INT TERM

wait_for() {
  local url="$1" name="$2" log="$3" tries=60
  for ((i = 0; i < tries; i++)); do
    if curl -sf -o /dev/null "${url}"; then return 0; fi
    sleep 1
  done
  echo "${name} did not come up at ${url}. Last log lines:" >&2
  tail -20 "${log}" >&2
  return 1
}

if [[ "${AGENTS}" == "network" ]]; then
  bold "Starting the GMA, one LMA per hospital, and the API on :${API_PORT}"
else
  bold "Starting the API on :${API_PORT}"
fi
(
  cd "${APP_ROOT}/backend"
  HEALTHCARE_DEMO_API_HOST=127.0.0.1 DAGENTS_HOME="${DAGENTS_HOME}" PYTHON="${PYTHON}" exec \
    "${HERE}/run_local_demo.sh" --agents "${AGENTS}"
) >"${LOG_DIR}/backend.log" 2>&1 &
BACKEND_PID=$!
wait_for "http://127.0.0.1:${API_PORT}/api/v1/health" "The API" "${LOG_DIR}/backend.log"

bold "Starting the UI on :${UI_PORT}"
(
  cd "${APP_ROOT}/frontend"
  HEALTHCARE_DEMO_API_URL="http://127.0.0.1:${API_PORT}" npm run dev -- --port "${UI_PORT}" --strictPort
) >"${LOG_DIR}/frontend.log" 2>&1 &
FRONTEND_PID=$!
wait_for "http://127.0.0.1:${UI_PORT}/" "The UI" "${LOG_DIR}/frontend.log"

# Where the records are and where the agents run, as the API reports it.
STATUS="$(curl -sf "http://127.0.0.1:${API_PORT}/api/v1/framework/status" || true)"
STORAGE="$(printf '%s' "${STATUS}" | "${PYTHON}" -c 'import json, sys
source = json.load(sys.stdin).get("cohort_source")
print({"in-memory": "in memory, one store per hospital", "postgres": "in Postgres, one schema per hospital"}.get(source, source))' \
  2>/dev/null || echo "not reported")"
AGENT_LAYOUT="$(printf '%s' "${STATUS}" | "${PYTHON}" -c 'import json, sys
mode = (json.load(sys.stdin).get("agents") or {}).get("mode")
print({"in-process": "one LMA per hospital and a GMA, inside the API process", "network": "one LMA per hospital and a GMA, each in its own process"}.get(mode, mode))' \
  2>/dev/null || echo "not reported")"

# --- optional verification -------------------------------------------------

if [[ "${MODE}" != "run" ]]; then
  bold "Running the frontend smoke test"
  SMOKE_ARGS=("http://127.0.0.1:${UI_PORT}/")
  [[ -n "${SHOT_DIR}" ]] && SMOKE_ARGS+=(--screenshots "${SHOT_DIR}")
  set +e
  (cd "${APP_ROOT}/frontend" && node smoke.mjs "${SMOKE_ARGS[@]}")
  SMOKE_STATUS=$?
  set -e
  case "${SMOKE_STATUS}" in
    0) ;;
    2) dim "(smoke test skipped: no browser available)" ;;
    *) echo "The frontend smoke test failed." >&2; exit "${SMOKE_STATUS}" ;;
  esac
  [[ -n "${SHOT_DIR}" && "${SMOKE_STATUS}" == 0 ]] && echo "Screenshots written to ${SHOT_DIR}"
  [[ "${MODE}" == "check" ]] && exit 0
  echo
fi

# --- the tour --------------------------------------------------------------

cat <<TOUR

$(bold "The Dagents stroke triage demo is running")

  UI        http://127.0.0.1:${UI_PORT}
  API docs  http://127.0.0.1:${API_PORT}/docs
  planner   ${DAGENTSC_BIN:-dagentsc (on PATH)}
  records   ${STORAGE}
  agents    ${AGENT_LAYOUT}

$(dim "The records are real, de-identified patient records from the International")
$(dim "Stroke Trial (1991-1996), from three of its hospitals. The models are")
$(dim "demonstrations, not validated clinical tools, and nothing here is medical advice.")

$(bold "What to try, page by page")

  1. Consortium (the coordinator's view)
     The hospitals table shows only what each hospital reports: record counts,
     how many have an outcome, and the poor outcome rate. No patient row reaches
     this page.

     Ethical Guard: choose a hospital, press "Ask the guard", then change one
     control and press it again. Watch the Strategy column for systolic_bp.

       untick "Requester verified"     generalize:1  ->  redact
       Granularity "table"             generalize:1  ->  aggregate_only:20
       Granularity "model update"                    ->  clip_contribution:1
       Granularity "table", Cohort size 5            ->  DENY

     The cohort minimum applies to column, table and model-update requests.
     A cell or row request is about one patient, so a small cohort does not
     deny it. The panel shows how many records the Guard would release and
     which fields, never the values. Each decision comes from the OCaml
     planner. Changing the policy means changing the classification, not this
     app's code.

     Federated pilot: press "Run the pilot". It runs analytics, an evaluation of
     the model in use, five training rounds and a validation of the candidate.
     Read the release gates: the decision is whatever they decide. If it is
     "release", the "Approve release" button makes the candidate the model the
     hospitals use.

  2. A hospital page (what that hospital's staff see)
     Where its records are stored, its running statistics, the model in use and
     its local metrics, the worklist, its charts, its audit log, and what it sent
     in each round of the pilot.

  3. Add a record (the data-entry page, from a hospital page)
     Do not enter real patient information. Save a record, go back to the
     hospital page, and see it listed and counted. Record its outcome with
     "Yes" or "No"; the next pilot then trains and evaluates on it.

$(dim "Ctrl-C stops both servers.")

TOUR

wait
