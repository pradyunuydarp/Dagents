#!/usr/bin/env bash
# Run the healthcare stroke-triage demo API locally, with no Docker.
#
#   scripts/run_local_demo.sh                    # the agents run inside the API process
#   scripts/run_local_demo.sh --agents network   # the agents run as their own processes
#
# The demo runs on one Dagents LMA (local monitoring agent) per hospital and one
# GMA (global monitoring agent) for the study, and calls them through their HTTP
# APIs.
#
# By default the API builds the four agents in its own process and calls their
# routes without a socket. Without HEALTHCARE_DEMO_DATABASE_URL each hospital
# keeps its records in memory, so no database is needed.
#
# With --agents network the script starts the GMA and each hospital's LMA with
# uvicorn on 127.0.0.1, each on the port env/.env.healthcare-demo gives it, then
# the API, which calls them over HTTP. It makes new tokens for each run and
# hands each process only its own, in its environment; it never prints them.
# This mode needs HEALTHCARE_DEMO_DATABASE_URL, with the migrations applied and
# the records loaded (scripts/apply_migrations.py, scripts/seed_supabase.py),
# because separate processes cannot share an in-memory store. Ctrl-C stops
# every process the script started.
#
# The only hard dependency is the OCaml planner binary: the Ethical Guard fails
# closed without it, which is the correct behaviour and also means nothing
# useful runs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
ENV_FILE="${APP_ROOT}/env/.env.healthcare-demo"

AGENTS="in-process"
while [[ $# -gt 0 ]]; do
  case "$1" in
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

# The framework lives in the Dagents checkout. Inside the Dagents repository
# that is three directories up; extracted into its own repository, DAGENTS_HOME
# says where it is.
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${APP_ROOT}/../.." && pwd)}"
if [[ ! -d "${DAGENTS_HOME}/dagents/common" ]]; then
  echo "Could not find the Dagents framework." >&2
  echo "Set DAGENTS_HOME to your Dagents checkout, for example:" >&2
  echo "  DAGENTS_HOME=~/src/Dagents $0" >&2
  exit 1
fi

PLANNER="${DAGENTSC_BIN:-${DAGENTS_HOME}/bindings/ocaml/_build/default/bin/dagentsc.exe}"
if [[ ! -x "${PLANNER}" ]] && ! command -v dagentsc >/dev/null 2>&1; then
  echo "The dagentsc planner is not built." >&2
  echo "Build it first:" >&2
  echo "  cd ${DAGENTS_HOME}/bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe" >&2
  echo >&2
  echo "Without it the Ethical Guard denies every request, which is correct but not a demo." >&2
  exit 1
fi
[[ -x "${PLANNER}" ]] && export DAGENTSC_BIN="${PLANNER}"

PYTHON="${PYTHON:-${DAGENTS_HOME}/.venv/bin/python}"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi

export PYTHONPATH="${DAGENTS_HOME}:${APP_ROOT}/backend"

# A setting from the environment, or else from env/.env.healthcare-demo.
setting() {
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
    printf '%s' "${!name}"
  elif [[ -f "${ENV_FILE}" ]]; then
    sed -n "s/^${name}=//p" "${ENV_FILE}" | tail -n 1
  fi
}

# A port from the environment or the env file. No port is written in this script.
port() {
  local value
  value="$(setting "$1")"
  if [[ ! "${value}" =~ ^[0-9]+$ ]]; then
    echo "$1 must be a port number. Set it in the environment or in env/.env.healthcare-demo." >&2
    return 1
  fi
  printf '%s' "${value}"
}

API_HOST="${HEALTHCARE_DEMO_API_HOST:-0.0.0.0}"
API_PORT="$(port HEALTHCARE_DEMO_API_PORT)"

# banner AGENTS RECORDS: what runs where.
banner() {
  echo "Dagents healthcare stroke-triage demo"
  echo "  framework : ${DAGENTS_HOME}"
  echo "  planner   : ${DAGENTSC_BIN:-dagentsc (on PATH)}"
  echo "  api       : http://127.0.0.1:${API_PORT}/docs"
  echo "  agents    : $1"
  echo
  echo "The records are de-identified records from the International Stroke Trial (1991-1996),"
  echo "$2"
  echo "Nothing here is a validated clinical model or medical advice."
  echo
}

if [[ "${AGENTS}" == "in-process" ]]; then
  export HEALTHCARE_DEMO_AGENTS="in-process"
  banner "in the API process" "kept in memory per hospital unless HEALTHCARE_DEMO_DATABASE_URL is set."
  exec "${PYTHON}" -m uvicorn app.main:app --host "${API_HOST}" --port "${API_PORT}"
fi

# --- Network mode: the GMA, one LMA per hospital, then the API ----------------

DATABASE_URL="${HEALTHCARE_DEMO_DATABASE_URL:-}"
if [[ -z "${DATABASE_URL}" ]]; then
  echo "--agents network needs HEALTHCARE_DEMO_DATABASE_URL." >&2
  echo "Agents in separate processes cannot read another process's in-memory store." >&2
  echo "Apply the migrations and load the records first:" >&2
  echo "  SUPABASE_DB_URL=... ${APP_ROOT}/scripts/apply_migrations.py" >&2
  echo "  SUPABASE_DB_URL=... ${APP_ROOT}/scripts/seed_supabase.py" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required to wait for the agents to start." >&2
  exit 1
fi

# The hospitals, as the app defines them.
SITES="$("${PYTHON}" -c 'from app.domain.dataset import SITES; print(" ".join(site.site_id for site in SITES))')"

# uk-134 as it appears in a variable name: UK_134.
site_key() { printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_'; }

new_token() { "${PYTHON}" -c 'import secrets; print(secrets.token_urlsafe(32))'; }

# Export every NAME=value line of the first argument.
export_lines() {
  local line
  while IFS= read -r line; do
    [[ -n "${line}" ]] && export "${line?}"
  done <<<"$1"
}

GMA_PORT="$(port HEALTHCARE_DEMO_GMA_PORT)"
GMA_URL="http://127.0.0.1:${GMA_PORT}"
ADMIN_TOKEN="$(new_token)"
ENDPOINTS=""
LMA_COORDINATOR_TOKENS=""
GMA_SITE_TOKENS=""
for site in ${SITES}; do
  key="$(site_key "${site}")"
  lma_port="$(port "HEALTHCARE_DEMO_LMA_${key}_PORT")"
  printf -v "LMA_PORT_${key}" '%s' "${lma_port}"
  printf -v "LMA_SITE_TOKEN_${key}" '%s' "$(new_token)"
  printf -v "LMA_COORDINATOR_TOKEN_${key}" '%s' "$(new_token)"
  printf -v "GMA_SITE_TOKEN_${key}" '%s' "$(new_token)"
  coordinator_name="LMA_COORDINATOR_TOKEN_${key}"
  gma_site_name="GMA_SITE_TOKEN_${key}"
  ENDPOINTS="${ENDPOINTS:+${ENDPOINTS},}${site}=http://127.0.0.1:${lma_port}"
  LMA_COORDINATOR_TOKENS="${LMA_COORDINATOR_TOKENS:+${LMA_COORDINATOR_TOKENS},}${site}=${!coordinator_name}"
  GMA_SITE_TOKENS="${GMA_SITE_TOKENS:+${GMA_SITE_TOKENS},}${site}=${!gma_site_name}"
done

LOG_DIR="$(mktemp -d)"
PIDS=()

cleanup() {
  trap - EXIT INT TERM
  local pid waited=0
  for pid in "${PIDS[@]:-}"; do
    [[ -n "${pid}" ]] && kill -TERM "${pid}" 2>/dev/null || true
  done
  while (( waited < 40 )); do
    local running=0
    for pid in "${PIDS[@]:-}"; do
      [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null && running=1
    done
    (( running == 0 )) && break
    sleep 0.25
    waited=$((waited + 1))
  done
  for pid in "${PIDS[@]:-}"; do
    [[ -n "${pid}" ]] && kill -KILL "${pid}" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  rm -rf "${LOG_DIR}"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

wait_for() {
  local url="$1" name="$2" log="$3" pid="$4" tries=120
  for ((i = 0; i < tries; i++)); do
    if curl -sf --noproxy '*' -o /dev/null "${url}"; then return 0; fi
    if ! kill -0 "${pid}" 2>/dev/null; then break; fi
    sleep 0.5
  done
  echo "${name} did not start." >&2
  if [[ -n "${log}" ]]; then
    echo "Last log lines:" >&2
    tail -20 "${log}" >&2
  fi
  return 1
}

banner "the GMA and one LMA per hospital, each in its own process" \
  "kept in Postgres, one schema per hospital (HEALTHCARE_DEMO_DATABASE_URL)."

# Each process starts in a subshell that exports only that process's settings
# and tokens, so no process holds another's token.
(
  settings="$("${PYTHON}" -m app.services.agents gma --database)"
  export_lines "${settings}"
  export GMA_DATABASE_URL="${DATABASE_URL}"
  export GMA_SITE_ENDPOINTS="${ENDPOINTS}"
  export GMA_LMA_TOKENS="${LMA_COORDINATOR_TOKENS}"
  export GMA_SITE_TOKENS="${GMA_SITE_TOKENS}"
  export GMA_ADMIN_TOKEN="${ADMIN_TOKEN}"
  exec "${PYTHON}" -m uvicorn dagents.gma.main:app --host 127.0.0.1 --port "${GMA_PORT}" --log-level warning
) >"${LOG_DIR}/gma.log" 2>&1 &
PIDS+=("$!")
echo "  gma          http://127.0.0.1:${GMA_PORT}"

for site in ${SITES}; do
  key="$(site_key "${site}")"
  port_name="LMA_PORT_${key}"
  site_token_name="LMA_SITE_TOKEN_${key}"
  coordinator_name="LMA_COORDINATOR_TOKEN_${key}"
  gma_site_name="GMA_SITE_TOKEN_${key}"
  # A hospital with its own login uses it; the others use the shared one.
  site_database_name="HEALTHCARE_DEMO_SITE_${key}_DATABASE_URL"
  (
    settings="$("${PYTHON}" -m app.services.agents lma "${site}" --database)"
    export_lines "${settings}"
    export LMA_DATABASE_URL="${!site_database_name:-${DATABASE_URL}}"
    export LMA_GMA_ENDPOINT="${GMA_URL}"
    export LMA_GMA_TOKEN="${!gma_site_name}"
    export LMA_SITE_TOKEN="${!site_token_name}"
    export LMA_COORDINATOR_TOKEN="${!coordinator_name}"
    exec "${PYTHON}" -m uvicorn dagents.lma.main:app --host 127.0.0.1 --port "${!port_name}" --log-level warning
  ) >"${LOG_DIR}/lma-${site}.log" 2>&1 &
  PIDS+=("$!")
  printf '  lma-%-8s http://127.0.0.1:%s\n' "${site}" "${!port_name}"
done

wait_for "${GMA_URL}/api/v1/health" "The GMA" "${LOG_DIR}/gma.log" "${PIDS[0]}"
index=1
for site in ${SITES}; do
  port_name="LMA_PORT_$(site_key "${site}")"
  wait_for "http://127.0.0.1:${!port_name}/api/v1/health" "The LMA of ${site}" \
    "${LOG_DIR}/lma-${site}.log" "${PIDS[${index}]}"
  index=$((index + 1))
done

(
  export HEALTHCARE_DEMO_AGENTS="network"
  export HEALTHCARE_DEMO_GMA_URL="${GMA_URL}"
  export HEALTHCARE_DEMO_GMA_TOKEN="${ADMIN_TOKEN}"
  for site in ${SITES}; do
    key="$(site_key "${site}")"
    port_name="LMA_PORT_${key}"
    site_token_name="LMA_SITE_TOKEN_${key}"
    export "HEALTHCARE_DEMO_LMA_${key}_URL=http://127.0.0.1:${!port_name}"
    export "HEALTHCARE_DEMO_LMA_${key}_TOKEN=${!site_token_name}"
  done
  exec "${PYTHON}" -m uvicorn app.main:app --host "${API_HOST}" --port "${API_PORT}"
) &
API_PID="$!"
PIDS+=("${API_PID}")

wait_for "http://127.0.0.1:${API_PORT}/api/v1/health" "The API" "" "${API_PID}"

# Say whether the hospitals' records are ready, as the API reports it.
READY="$(curl -sf --noproxy '*' "http://127.0.0.1:${API_PORT}/api/v1/framework/status" \
  | "${PYTHON}" -c 'import json, sys; print(json.load(sys.stdin).get("ready"))' 2>/dev/null || echo "unknown")"
if [[ "${READY}" != "True" ]]; then
  echo >&2
  echo "The API answers, but the hospitals' records are not ready." >&2
  echo "Apply the migrations and load the records:" >&2
  echo "  SUPABASE_DB_URL=... ${APP_ROOT}/scripts/apply_migrations.py" >&2
  echo "  SUPABASE_DB_URL=... ${APP_ROOT}/scripts/seed_supabase.py" >&2
fi

# Stop everything when the API or any agent stops.
while true; do
  for pid in "${PIDS[@]}"; do
    if ! kill -0 "${pid}" 2>/dev/null; then
      echo "A process stopped, so the script stops the others." >&2
      for log in "${LOG_DIR}"/*.log; do
        [[ -s "${log}" ]] || continue
        echo "--- $(basename "${log}")" >&2
        tail -5 "${log}" >&2
      done
      exit 1
    fi
  done
  sleep 1
done
