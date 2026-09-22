#!/usr/bin/env bash
# Run the healthcare demo end to end: backend, frontend, and a guided tour.
#
# One command, no Docker and no database. Starts the API, starts the Vite dev
# server in front of it, waits until both actually answer, and prints what to
# click and what each panel is showing.
#
#   scripts/run_frontend_demo.sh            # start it and print the tour
#   scripts/run_frontend_demo.sh --verify   # run the smoke test first, then start
#   scripts/run_frontend_demo.sh --check    # run the smoke test and exit
#
# Ctrl-C stops both servers.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${APP_ROOT}/../.." && pwd)}"

MODE="run"
case "${1:-}" in
  --verify) MODE="verify" ;;
  --check) MODE="check" ;;
  "") ;;
  *) echo "unknown option: $1" >&2; exit 2 ;;
esac

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
  echo "Without it the Ethical Guard denies every request. That is correct behaviour," >&2
  echo "but every panel in the UI would show a denial, which is not a demo." >&2
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

# Kill a process and everything it spawned.
#
# Killing the PID alone is not enough: `npm run dev` becomes `sh -c vite` which
# becomes a node process, and signalling the outermost shell leaves the dev
# server holding the port. Ctrl-C would look like it worked, and the next run
# would fail to bind.
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

bold "Starting the backend on :${API_PORT}"
(
  cd "${APP_ROOT}/backend"
  PYTHONPATH="${DAGENTS_HOME}:${APP_ROOT}/backend" \
    "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${API_PORT}"
) >"${LOG_DIR}/backend.log" 2>&1 &
BACKEND_PID=$!
wait_for "http://127.0.0.1:${API_PORT}/api/v1/health" "backend" "${LOG_DIR}/backend.log"

bold "Starting the frontend on :${UI_PORT}"
(
  cd "${APP_ROOT}/frontend"
  HEALTHCARE_DEMO_API_URL="http://127.0.0.1:${API_PORT}" npm run dev -- --port "${UI_PORT}"
) >"${LOG_DIR}/frontend.log" 2>&1 &
FRONTEND_PID=$!
wait_for "http://127.0.0.1:${UI_PORT}/" "frontend" "${LOG_DIR}/frontend.log"

# --- optional verification -------------------------------------------------

if [[ "${MODE}" != "run" ]]; then
  bold "Running the frontend smoke test"
  set +e
  (cd "${APP_ROOT}/frontend" && node smoke.mjs "http://127.0.0.1:${UI_PORT}/")
  SMOKE_STATUS=$?
  set -e
  case "${SMOKE_STATUS}" in
    0) ;;
    2) dim "(smoke test skipped: no browser available)" ;;
    *) echo "The frontend smoke test failed." >&2; exit "${SMOKE_STATUS}" ;;
  esac
  [[ "${MODE}" == "check" ]] && exit 0
  echo
fi

# --- the tour --------------------------------------------------------------

cat <<TOUR

$(bold "Dagents healthcare demo is running")

  UI       http://127.0.0.1:${UI_PORT}
  API      http://127.0.0.1:${API_PORT}/docs
  planner  ${DAGENTSC_BIN:-dagentsc (on PATH)}

$(dim "All patient data is synthetic. The scoring rule is a demonstration, not a")
$(dim "validated triage model, and nothing here is clinical advice.")

$(bold "What to look at, in order")

  1. Consortium
     Three hospitals with deliberately different populations. Riverside sees an
     older, sicker cohort; Northgate records less completely; Lakeside is small.
     Identical sites would never exercise the drift checks or the fairness gate.

  2. Ethical Guard  —  the fastest way to see the governance layer
     Press "Ask the guard", then change one input and press it again. Watch the
     strategy column, not just the verdict.

       untick "verified"        generalize:1  ->  redact
                                lower trust hardens the strategy

       granularity "table"      generalize:1  ->  aggregate_only:20
                                a table can only come back as a group

       granularity "model update"             ->  clip_contribution:1
                                an update is bounded, never released raw.
                                This is the fifth granularity, the extension
                                this project adds to the published framework.

       cohort 5                               ->  DENY
                                below the classification's floor of 20 the
                                request is refused however trusted the caller

     Every one of those decisions comes from the typed OCaml planner. None of
     them is a branch in this app's code, and changing the policy means editing
     a classification, not a code path.

  3. Federated pilot  —  press "Run governed pilot"
     Four rounds: analytics, baseline evaluation, training, then cross-site
     validation of the candidate. Each shows its manifest digest, its quorum,
     and any site excluded with the reason why.

     Then read the release gates. The candidate beats its baseline on AUC and
     is still rejected, because the subgroup-fairness and sensitivity gates
     fail. That is the framework working: aggregation produces a candidate,
     never a release.

     Open "What each site returned". Counts, a bounded norm, approved metrics,
     and a pointer that resolves only at the site. No patient-level column.

  4. Local worklist
     The clinician-facing view, which never leaves the hospital. Cases are
     re-ordered so an urgent one reaches a specialist sooner; nothing is
     removed from the queue, and every row carries the basis for its score.

$(dim "Ctrl-C stops both servers.")

TOUR

wait
