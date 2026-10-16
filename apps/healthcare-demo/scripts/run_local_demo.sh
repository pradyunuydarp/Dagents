#!/usr/bin/env bash
# Run the healthcare stroke-triage demo API locally, with no Docker.
#
#   scripts/run_local_demo.sh                             # the agents run inside the API process
#   scripts/run_local_demo.sh --agents network            # the agents run as their own processes
#   scripts/run_local_demo.sh --agents network --deploy   # and a hospital can join while it runs
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
# --deploy (which implies --agents network) lets Hospital 290 join while the
# demo runs, from the consortium page or POST /api/v1/deployments. The script
# checks first that nothing answers on the hospital's port
# (HEALTHCARE_DEMO_LMA_PL_290_PORT), then also starts core-service on
# 127.0.0.1 (HEALTHCARE_DEMO_CORE_SERVICE_PORT), which renders the new LMA's
# Compose file. It makes the hospital's tokens with the others, gives the GMA
# the hospital's one accepted address (GMA_JOINABLE_SITE_ENDPOINTS) and its
# token entries, writes the new LMA's env file (readable by its owner only, in
# a folder only its owner can enter), and starts the API on 127.0.0.1 with
# HEALTHCARE_DEMO_DEPLOYER=process. The API then starts the new LMA itself.
# Without --deploy the API runs with HEALTHCARE_DEMO_DEPLOYER=off, whatever
# the environment says. On exit the script also stops a hospital's LMA that
# the API left running, from its pid file.
#
# The only hard dependency is the OCaml planner binary: the Ethical Guard fails
# closed without it, which is the correct behaviour and also means nothing
# useful runs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
ENV_FILE="${APP_ROOT}/env/.env.healthcare-demo"

AGENTS=""
DEPLOY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --agents)
      AGENTS="${2:-}"
      shift 2 || { echo "--agents needs in-process or network" >&2; exit 2; }
      ;;
    --agents=*) AGENTS="${1#--agents=}"; shift ;;
    --deploy) DEPLOY="1"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
if [[ -n "${DEPLOY}" ]]; then
  if [[ -n "${AGENTS}" && "${AGENTS}" != "network" ]]; then
    echo "--deploy needs --agents network: a hospital that joins runs its LMA as a process of its own." >&2
    exit 2
  fi
  AGENTS="network"
fi
AGENTS="${AGENTS:-in-process}"
case "${AGENTS}" in
  in-process|network) ;;
  *) echo "--agents must be in-process or network, not '${AGENTS}'" >&2; exit 2 ;;
esac

# The framework lives in the Dagents checkout. Inside the Dagents repository
# that is three directories up; extracted into its own repository, DAGENTS_HOME
# says where it is.
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${APP_ROOT}/../.." && pwd)}"
if [[ ! -d "${DAGENTS_HOME}/agents/common" ]]; then
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
# A run that can start a process listens on this machine only.
[[ -n "${DEPLOY}" ]] && API_HOST="127.0.0.1"

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
  # Only --deploy turns the deployer on, and it needs network mode.
  export HEALTHCARE_DEMO_DEPLOYER="off"
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

# The hospitals, as the app defines them, and the ones that can join while it runs.
SITES="$("${PYTHON}" -c 'from app.domain.dataset import SITES; print(" ".join(site.site_id for site in SITES))')"
JOINING=""
if [[ -n "${DEPLOY}" ]]; then
  JOINING="$("${PYTHON}" -c 'from app.domain.dataset import JOINING_SITES; print(" ".join(site.site_id for site in JOINING_SITES))')"
  if [[ ! -d "${DAGENTS_HOME}/services/core-service" ]]; then
    echo "--deploy needs core-service, which is not in ${DAGENTS_HOME}/services/core-service." >&2
    exit 1
  fi
fi

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

# Whether something accepts a TCP connection on 127.0.0.1 at this port.
port_answers() {
  timeout 2 bash -c "exec 3<>/dev/tcp/127.0.0.1/$1" 2>/dev/null
}

GMA_PORT="$(port HEALTHCARE_DEMO_GMA_PORT)"
GMA_URL="http://127.0.0.1:${GMA_PORT}"
ADMIN_TOKEN="$(new_token)"
ENDPOINTS=""
LMA_COORDINATOR_TOKENS=""
GMA_SITE_TOKENS=""
JOINABLE=""
CORE_PORT=""
if [[ -n "${DEPLOY}" ]]; then
  CORE_PORT="$(port HEALTHCARE_DEMO_CORE_SERVICE_PORT)"
  # Checked before anything starts: the API would refuse to start a hospital
  # on a port that answers for something else.
  for site in ${JOINING}; do
    key="$(site_key "${site}")"
    lma_port="$(port "HEALTHCARE_DEMO_LMA_${key}_PORT")"
    if port_answers "${lma_port}"; then
      echo "Port ${lma_port} already answers, so the LMA of ${site} could not start there." >&2
      echo "Stop what is using it, or set HEALTHCARE_DEMO_LMA_${key}_PORT to another port." >&2
      exit 1
    fi
  done
  if port_answers "${CORE_PORT}"; then
    echo "Port ${CORE_PORT} already answers, so core-service could not start there." >&2
    echo "Stop what is using it, or set HEALTHCARE_DEMO_CORE_SERVICE_PORT to another port." >&2
    exit 1
  fi
fi
for site in ${SITES} ${JOINING}; do
  key="$(site_key "${site}")"
  lma_port="$(port "HEALTHCARE_DEMO_LMA_${key}_PORT")"
  printf -v "LMA_PORT_${key}" '%s' "${lma_port}"
  printf -v "LMA_SITE_TOKEN_${key}" '%s' "$(new_token)"
  printf -v "LMA_COORDINATOR_TOKEN_${key}" '%s' "$(new_token)"
  printf -v "GMA_SITE_TOKEN_${key}" '%s' "$(new_token)"
  coordinator_name="LMA_COORDINATOR_TOKEN_${key}"
  gma_site_name="GMA_SITE_TOKEN_${key}"
  if [[ " ${JOINING} " == *" ${site} "* ]]; then
    JOINABLE="${JOINABLE:+${JOINABLE},}${site}=http://127.0.0.1:${lma_port}"
  else
    ENDPOINTS="${ENDPOINTS:+${ENDPOINTS},}${site}=http://127.0.0.1:${lma_port}"
  fi
  LMA_COORDINATOR_TOKENS="${LMA_COORDINATOR_TOKENS:+${LMA_COORDINATOR_TOKENS},}${site}=${!coordinator_name}"
  GMA_SITE_TOKENS="${GMA_SITE_TOKENS:+${GMA_SITE_TOKENS},}${site}=${!gma_site_name}"
done
SITE_COUNT="$(wc -w <<<"${SITES} ${JOINING}" | tr -d ' ')"

LOG_DIR="$(mktemp -d)"
PIDS=()
# Where the API keeps the new LMA's log, its pid file and its env file. Only
# this user can enter it.
DEPLOY_DIR=""
if [[ -n "${DEPLOY}" ]]; then
  DEPLOY_DIR="${LOG_DIR}/deploy"
  mkdir -m 700 "${DEPLOY_DIR}"
fi

# The process group a process leads, from /proc, or empty when it is gone.
process_group() {
  local stat
  stat="$(cat "/proc/$1/stat" 2>/dev/null)" || return 0
  stat="${stat##*) }"
  printf '%s' "${stat}" | cut -d' ' -f3
}

# Stop each process named in a pid file of the deploy folder that still leads
# its own process group: a hospital's LMA the API started in a session of its
# own and did not stop, because it crashed or was killed.
stop_left_behind() {
  local file pid signal
  [[ -n "${DEPLOY_DIR}" && -d "${DEPLOY_DIR}" ]] || return 0
  for signal in TERM KILL; do
    for file in "${DEPLOY_DIR}"/*.pid; do
      [[ -f "${file}" ]] || continue
      pid="$(tr -cd '0-9' <"${file}")"
      [[ -n "${pid}" && "$(process_group "${pid}")" == "${pid}" ]] || continue
      kill "-${signal}" -- "-${pid}" 2>/dev/null || true
    done
    if [[ "${signal}" == "TERM" ]]; then
      sleep 2
    fi
  done
  return 0
}

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
  stop_left_behind
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

if [[ -n "${DEPLOY}" ]]; then
  # core-service renders the new LMA's Compose file. Its package is also
  # named app, so it runs with its own PYTHONPATH, and it gets no token.
  (
    cd "${DAGENTS_HOME}/services/core-service"
    export PYTHONPATH="${DAGENTS_HOME}:${DAGENTS_HOME}/services/core-service"
    exec "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${CORE_PORT}" --log-level warning
  ) >"${LOG_DIR}/core-service.log" 2>&1 &
  PIDS+=("$!")
  CORE_INDEX=$((${#PIDS[@]} - 1))
  echo "  core-service http://127.0.0.1:${CORE_PORT}"

  # The new LMA's env file: its tokens, its database and the GMA's address.
  # The values reach the writer in its environment, never in its arguments.
  for site in ${JOINING}; do
    key="$(site_key "${site}")"
    site_token_name="LMA_SITE_TOKEN_${key}"
    coordinator_name="LMA_COORDINATOR_TOKEN_${key}"
    gma_site_name="GMA_SITE_TOKEN_${key}"
    site_database_name="HEALTHCARE_DEMO_SITE_${key}_DATABASE_URL"
    LMA_DATABASE_URL="${!site_database_name:-${DATABASE_URL}}" \
      LMA_GMA_ENDPOINT="${GMA_URL}" \
      LMA_GMA_TOKEN="${!gma_site_name}" \
      LMA_SITE_TOKEN="${!site_token_name}" \
      LMA_COORDINATOR_TOKEN="${!coordinator_name}" \
      "${PYTHON}" "${APP_ROOT}/scripts/write_agent_settings.py" env-file "${DEPLOY_DIR}/lma-${site}.env" \
      LMA_DATABASE_URL LMA_GMA_ENDPOINT LMA_GMA_TOKEN LMA_SITE_TOKEN LMA_COORDINATOR_TOKEN >/dev/null
  done
fi

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
  if [[ -n "${JOINABLE}" ]]; then
    # The one address the GMA accepts for each hospital that can join, and
    # one round's jobs sent to every hospital at once.
    export GMA_JOINABLE_SITE_ENDPOINTS="${JOINABLE}"
    export GMA_DISPATCH_CONCURRENCY="${SITE_COUNT}"
  fi
  exec "${PYTHON}" -m uvicorn agents.gma.main:app --host 127.0.0.1 --port "${GMA_PORT}" --log-level warning
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
    exec "${PYTHON}" -m uvicorn agents.lma.main:app --host 127.0.0.1 --port "${!port_name}" --log-level warning
  ) >"${LOG_DIR}/lma-${site}.log" 2>&1 &
  PIDS+=("$!")
  printf '  lma-%-8s http://127.0.0.1:%s\n' "${site}" "${!port_name}"
done

if [[ -n "${DEPLOY}" ]]; then
  wait_for "http://127.0.0.1:${CORE_PORT}/api/v1/health" "core-service" "${LOG_DIR}/core-service.log" \
    "${PIDS[${CORE_INDEX}]}"
  for site in ${JOINING}; do
    printf '  lma-%-8s http://127.0.0.1:%s (started when the hospital joins)\n' "${site}" \
      "$(port "HEALTHCARE_DEMO_LMA_$(site_key "${site}")_PORT")"
  done
fi
GMA_INDEX=$(( ${#PIDS[@]} - 1 - $(wc -w <<<"${SITES}") ))
wait_for "${GMA_URL}/api/v1/health" "The GMA" "${LOG_DIR}/gma.log" "${PIDS[${GMA_INDEX}]}"
index=$((GMA_INDEX + 1))
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
  for site in ${SITES} ${JOINING}; do
    key="$(site_key "${site}")"
    port_name="LMA_PORT_${key}"
    site_token_name="LMA_SITE_TOKEN_${key}"
    export "HEALTHCARE_DEMO_LMA_${key}_URL=http://127.0.0.1:${!port_name}"
    export "HEALTHCARE_DEMO_LMA_${key}_TOKEN=${!site_token_name}"
  done
  if [[ -n "${DEPLOY}" ]]; then
    export HEALTHCARE_DEMO_DEPLOYER="process"
    export HEALTHCARE_DEMO_API_HOST="${API_HOST}"
    export HEALTHCARE_DEMO_CORE_SERVICE_URL="http://127.0.0.1:${CORE_PORT}"
    export HEALTHCARE_DEMO_DEPLOY_DIR="${DEPLOY_DIR}"
    export HEALTHCARE_DEMO_DEPLOY_ENV_DIR="${DEPLOY_DIR}"
  else
    # Only --deploy turns the deployer on, whatever the environment says.
    export HEALTHCARE_DEMO_DEPLOYER="off"
    unset HEALTHCARE_DEMO_CORE_SERVICE_URL HEALTHCARE_DEMO_DEPLOY_DIR HEALTHCARE_DEMO_DEPLOY_ENV_DIR
  fi
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
