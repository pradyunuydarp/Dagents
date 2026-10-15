#!/usr/bin/env bash
# Run the healthcare demo end to end: the API, the UI, and a short tour.
#
# One command, no Docker. Starts the API, starts the Vite dev server in front of
# it, waits until both answer, and prints what to try on each page. Without
# HEALTHCARE_DEMO_DATABASE_URL the API keeps each hospital's records in memory,
# loaded from the committed International Stroke Trial extract, so no database
# is needed.
#
#   scripts/run_frontend_demo.sh                        # start it and print the tour
#   scripts/run_frontend_demo.sh --verify               # run the smoke test first, then start
#   scripts/run_frontend_demo.sh --check                # run the smoke test and exit
#   scripts/run_frontend_demo.sh --screenshots <dir>    # run the smoke test, save the README
#                                                       # screenshots to <dir>, and exit
#
# Add --split to any of these to run the backend as five processes instead of
# one: the Dagents GMA, one LMA per hospital and the app the UI calls
# (scripts/run_split_demo.sh starts them). For example:
#
#   scripts/run_frontend_demo.sh --split --check
#
# Add --deploy instead to run the split backend with core-service as well, so
# that the consortium page can add Hospital 290 while the demo runs
# (scripts/run_split_demo.sh --deploy). It implies --split. The UI's dev
# server then listens on 127.0.0.1 only, because adding a hospital starts a
# process on this machine. With --check, the smoke test also adds the
# hospital and runs a pilot with it (--expect-join pl-290):
#
#   scripts/run_frontend_demo.sh --deploy --check
#
# HEALTHCARE_DEMO_API_PORT and HEALTHCARE_DEMO_UI_PORT change the ports (8080
# and 5174 by default). Ctrl-C stops every server.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${APP_ROOT}/../.." && pwd)}"

MODE="run"
SHOT_DIR=""
SPLIT=0
DEPLOY=0
while (( $# > 0 )); do
  case "$1" in
    --verify) MODE="verify" ;;
    --check) MODE="check" ;;
    --split) SPLIT=1 ;;
    --deploy)
      SPLIT=1
      DEPLOY=1
      ;;
    --screenshots)
      MODE="check"
      SHOT_DIR="${2:-}"
      if [[ -z "${SHOT_DIR}" ]]; then
        echo "--screenshots needs a directory" >&2
        exit 2
      fi
      shift
      mkdir -p "${SHOT_DIR}"
      SHOT_DIR="$(cd "${SHOT_DIR}" && pwd)"
      ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
dim() { printf '\033[2m%s\033[0m\n' "$*"; }

# --- preflight -------------------------------------------------------------

if [[ ! -d "${DAGENTS_HOME}/agents/common" ]]; then
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

API_PORT="${HEALTHCARE_DEMO_API_PORT:-8080}"
UI_PORT="${HEALTHCARE_DEMO_UI_PORT:-5174}"
export HEALTHCARE_DEMO_API_PORT="${API_PORT}"

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
  if (( SPLIT )) && declare -F split_stop >/dev/null; then split_stop; fi
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

if (( SPLIT )); then
  # The Dagents GMA, one LMA per hospital, and the app on API_PORT. With
  # --deploy also core-service, and the app can add Hospital 290.
  # shellcheck source=run_split_demo.sh
  source "${HERE}/run_split_demo.sh"
  split_preflight
  if (( DEPLOY )); then
    export SPLIT_DEPLOY=1
    bold "Starting core-service, the GMA, the three hospitals and the API on :${API_PORT}"
  else
    bold "Starting the GMA, the three hospitals and the API on :${API_PORT}"
  fi
  split_start "${LOG_DIR}"
else
  bold "Starting the API on :${API_PORT}"
  (
    cd "${APP_ROOT}/backend"
    PYTHONPATH="${DAGENTS_HOME}:${APP_ROOT}/backend" \
      "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${API_PORT}"
  ) >"${LOG_DIR}/backend.log" 2>&1 &
  BACKEND_PID=$!
  wait_for "http://127.0.0.1:${API_PORT}/api/v1/health" "The API" "${LOG_DIR}/backend.log"
fi

bold "Starting the UI on :${UI_PORT}"
if (( DEPLOY )); then
  # The page can start a process on this machine, so its dev server listens on
  # 127.0.0.1 only. The app accepts the request only from a page on this
  # machine, and the dev server passes on the page's Origin.
  (
    cd "${APP_ROOT}/frontend"
    HEALTHCARE_DEMO_API_URL="http://127.0.0.1:${API_PORT}" \
      "${APP_ROOT}/frontend/node_modules/.bin/vite" --host 127.0.0.1 --port "${UI_PORT}" --strictPort
  ) >"${LOG_DIR}/frontend.log" 2>&1 &
else
  (
    cd "${APP_ROOT}/frontend"
    HEALTHCARE_DEMO_API_URL="http://127.0.0.1:${API_PORT}" npm run dev -- --port "${UI_PORT}" --strictPort
  ) >"${LOG_DIR}/frontend.log" 2>&1 &
fi
FRONTEND_PID=$!
wait_for "http://127.0.0.1:${UI_PORT}/" "The UI" "${LOG_DIR}/frontend.log"

# Where the records are, as the API reports it: in memory without a database
# URL, or one Postgres schema per hospital with one.
STORAGE="$(curl -sf "http://127.0.0.1:${API_PORT}/api/v1/framework/status" \
  | "${PYTHON}" -c 'import json, sys
source = json.load(sys.stdin).get("cohort_source")
print({"in-memory": "in memory, one store per hospital", "postgres": "in Postgres, one schema per hospital"}.get(source, source))' \
  2>/dev/null || echo "not reported")"

# --- optional verification -------------------------------------------------

if [[ "${MODE}" != "run" ]]; then
  bold "Running the frontend smoke test"
  SMOKE_ARGS=("http://127.0.0.1:${UI_PORT}/")
  [[ -n "${SHOT_DIR}" ]] && SMOKE_ARGS+=(--screenshots "${SHOT_DIR}")
  # With --deploy the test also adds the hospital, after every other check.
  (( DEPLOY )) && SMOKE_ARGS+=(--expect-join "${SPLIT_JOINING_SITE}")
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

if (( DEPLOY )); then
  LAYOUT="six processes: core-service, the Dagents GMA, one LMA per hospital, and the API"
elif (( SPLIT )); then
  LAYOUT="five processes: the Dagents GMA, one LMA per hospital, and the API"
else
  LAYOUT="one process"
fi
if (( DEPLOY )); then
  UI_HOST="127.0.0.1 only"
else
  UI_HOST="every interface"
fi

cat <<TOUR

$(bold "The Dagents stroke triage demo is running")

  UI        http://127.0.0.1:${UI_PORT}   (listens on ${UI_HOST})
  API docs  http://127.0.0.1:${API_PORT}/docs
  planner   ${DAGENTSC_BIN:-dagentsc (on PATH)}
  records   ${STORAGE}
  backend   ${LAYOUT}

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

  4. Add a hospital (the last panel of the consortium page)
     $(if (( DEPLOY )); then
       echo 'Press "Add Hospital 290". The panel lists each step: the Compose file'
       echo '     core-service rendered, the new LMA starting and answering, the GMA'
       echo '     accepting its address and enrolling it, and whether the next round'
       echo '     selects it. Then run the pilot again: it includes Hospital 290.'
     else
       echo 'The panel lists Hospital 290 and says why it offers no button. Start the'
       echo '     demo with --deploy to add it while the demo runs.'
     fi)

$(dim "Ctrl-C stops every server.")

TOUR

wait
