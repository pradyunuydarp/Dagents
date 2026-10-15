#!/usr/bin/env bash
# Run the healthcare demo as separate processes, with no Docker and no database:
#
#   gma          the Dagents GMA service (agents.gma.main), on 8020
#   lma-uk-134   one hospital each, HEALTHCARE_DEMO_ROLE=lma, on 8082, 8083, 8084
#   lma-it-002
#   lma-ch-245
#   app          the API the frontend calls, HEALTHCARE_DEMO_ROLE=app, on 8080,
#                and on 8081 the routes only the hospitals call
#
# The app runs the study through the GMA's API. The GMA sends each round's
# jobs to the hospitals, and each hospital trains on its own records and sends
# its bounded update to the app's internal port. The app forwards each
# hospital's pages to that hospital. Ctrl-C stops all five.
#
#   scripts/run_split_demo.sh
#
# GMA_API_PORT, HEALTHCARE_DEMO_API_PORT, HEALTHCARE_DEMO_INTERNAL_PORT and
# HEALTHCARE_DEMO_LMA_PORTS ("8082 8083 8084") change the ports. Every process
# binds 127.0.0.1: the calls between them have no authentication.
#
# With --deploy, the run can also add Hospital 290 (pl-290) while it runs:
#
#   scripts/run_split_demo.sh --deploy
#
# It also starts core-service (services/core-service, on 8040), whose planner
# renders the new hospital's Compose file. The GMA accepts the new hospital's
# address (GMA_FEDERATION_JOINABLE_SITES), and the app turns on the deployer
# (HEALTHCARE_DEMO_DEPLOYER=process): POST /api/v1/deployments, or the "Add
# Hospital 290" button on the consortium page, starts the hospital's LMA on
# 8085 as a child process of the app. CORE_SERVICE_PORT and
# HEALTHCARE_DEMO_JOINING_PORT change the two ports. The script stops before it
# starts anything when the joining port already answers.
#
# scripts/run_frontend_demo.sh --split sources this file and calls
# split_start, so the UI runs in front of the same five processes. It sets
# SPLIT_DEPLOY=1 for --deploy.
set -euo pipefail

SPLIT_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPLIT_APP_ROOT="$(cd "${SPLIT_HERE}/.." && pwd)"
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${SPLIT_APP_ROOT}/../.." && pwd)}"

#: The hospitals, in display order. Each gets the next port in HEALTHCARE_DEMO_LMA_PORTS.
SPLIT_SITES=(uk-134 it-002 ch-245)
#: The hospital a run started with --deploy can add while it runs. It listens on
#: HEALTHCARE_DEMO_JOINING_PORT.
SPLIT_JOINING_SITE="pl-290"
#: 1 when the run can add SPLIT_JOINING_SITE (--deploy).
SPLIT_DEPLOY="${SPLIT_DEPLOY:-0}"
SPLIT_PIDS=()
SPLIT_NAMES=()

# Fail with a plain message when the framework or the planner is missing.
split_preflight() {
  if [[ ! -d "${DAGENTS_HOME}/agents/common" || ! -d "${DAGENTS_HOME}/agents/gma" ]]; then
    echo "Could not find the Dagents framework, which has the GMA service." >&2
    echo "Set DAGENTS_HOME to your Dagents checkout, for example:" >&2
    echo "  DAGENTS_HOME=~/src/Dagents $0" >&2
    return 1
  fi
  local planner="${DAGENTSC_BIN:-${DAGENTS_HOME}/bindings/ocaml/_build/default/bin/dagentsc.exe}"
  if [[ ! -x "${planner}" ]] && ! command -v dagentsc >/dev/null 2>&1; then
    echo "The dagentsc planner is not built." >&2
    echo "Build it first:" >&2
    echo "  cd ${DAGENTS_HOME}/bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe" >&2
    echo >&2
    echo "Without it the Ethical Guard denies every request and the GMA cannot plan a round." >&2
    return 1
  fi
  [[ -x "${planner}" ]] && export DAGENTSC_BIN="${planner}"
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to wait for the processes to start." >&2
    return 1
  fi
  PYTHON="${PYTHON:-${DAGENTS_HOME}/.venv/bin/python}"
  [[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"
  export PYTHON
}

# Kill a process and everything it started.
split_kill_tree() {
  local pid="$1" signal="${2:-TERM}" child
  [[ -z "${pid}" ]] && return 0
  for child in $(pgrep -P "${pid}" 2>/dev/null || true); do
    split_kill_tree "${child}" "${signal}"
  done
  kill "-${signal}" "${pid}" 2>/dev/null || true
}

split_stop() {
  local pid waited=0
  for pid in "${SPLIT_PIDS[@]}"; do split_kill_tree "${pid}" TERM; done
  while (( waited < 20 )); do
    local alive=0
    for pid in "${SPLIT_PIDS[@]}"; do kill -0 "${pid}" 2>/dev/null && alive=1; done
    (( alive == 0 )) && break
    sleep 0.25
    waited=$((waited + 1))
  done
  for pid in "${SPLIT_PIDS[@]}"; do
    split_kill_tree "${pid}" KILL
    wait "${pid}" 2>/dev/null || true
  done
  SPLIT_PIDS=()
}

split_wait_for() {
  local url="$1" name="$2" log="$3" tries=60 i
  for ((i = 0; i < tries; i++)); do
    if curl -sf -o /dev/null "${url}"; then return 0; fi
    sleep 1
  done
  echo "${name} did not come up at ${url}. Last log lines:" >&2
  tail -20 "${log}" >&2
  return 1
}

# Whether something accepts a TCP connection on 127.0.0.1 at this port.
split_port_answers() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null
}

# Start one process in the background. Arguments: name, log, working directory,
# then `env` assignments and the command.
split_spawn() {
  local name="$1" log="$2" dir="$3"
  shift 3
  (cd "${dir}" && exec env "$@") >"${log}" 2>&1 &
  SPLIT_PIDS+=("$!")
  SPLIT_NAMES+=("${name}")
}

# Start the five processes and wait until each answers its health check.
# Argument: the directory for the logs. Sets SPLIT_APP_URL.
split_start() {
  local log_dir="$1"
  local gma_port="${GMA_API_PORT:-8020}"
  local app_port="${HEALTHCARE_DEMO_API_PORT:-8080}"
  local internal_port="${HEALTHCARE_DEMO_INTERNAL_PORT:-8081}"
  local -a lma_ports
  read -r -a lma_ports <<<"${HEALTHCARE_DEMO_LMA_PORTS:-8082 8083 8084}"
  if (( ${#lma_ports[@]} != ${#SPLIT_SITES[@]} )); then
    echo "HEALTHCARE_DEMO_LMA_PORTS needs ${#SPLIT_SITES[@]} ports, one per hospital." >&2
    return 1
  fi

  local pythonpath="${DAGENTS_HOME}:${SPLIT_APP_ROOT}/backend"
  local app_url="http://127.0.0.1:${app_port}"
  local internal_url="http://127.0.0.1:${internal_port}"
  local sites="" i
  for i in "${!SPLIT_SITES[@]}"; do
    sites+="${sites:+,}${SPLIT_SITES[$i]}=http://127.0.0.1:${lma_ports[$i]}"
  done

  # With --deploy: core-service, the address the GMA accepts for the joining
  # hospital, and the app's deployer. Without it, the deployer stays off.
  local core_port="${CORE_SERVICE_PORT:-8040}"
  local joining_port="${HEALTHCARE_DEMO_JOINING_PORT:-8085}"
  local joining="${SPLIT_JOINING_SITE}=http://127.0.0.1:${joining_port}"
  local -a gma_deploy=() app_deploy=(HEALTHCARE_DEMO_DEPLOYER=off)
  if [[ "${SPLIT_DEPLOY}" == 1 ]]; then
    if [[ ! -d "${DAGENTS_HOME}/services/core-service" ]]; then
      echo "Could not find core-service in ${DAGENTS_HOME}/services/core-service." >&2
      echo "--deploy needs it: its planner renders the new hospital's Compose file." >&2
      return 1
    fi
    if split_port_answers "${joining_port}"; then
      echo "Port ${joining_port} already answers. Hospital ${SPLIT_JOINING_SITE} listens there when it joins." >&2
      echo "Stop what is using it, or set HEALTHCARE_DEMO_JOINING_PORT to a free port." >&2
      return 1
    fi
    gma_deploy=(GMA_FEDERATION_JOINABLE_SITES="${joining}")
    app_deploy=(
      HEALTHCARE_DEMO_DEPLOYER=process
      HEALTHCARE_DEMO_JOINING_LMA_URLS="${joining}"
      CORE_SERVICE_PUBLIC_URL="http://127.0.0.1:${core_port}"
      HEALTHCARE_DEMO_DEPLOY_DIR="${log_dir}/deploy"
    )
    split_spawn core-service "${log_dir}/core-service.log" "${DAGENTS_HOME}/services/core-service" \
      PYTHONPATH="${DAGENTS_HOME}:${DAGENTS_HOME}/services/core-service" \
      "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${core_port}"
  fi

  split_spawn gma "${log_dir}/gma.log" "${DAGENTS_HOME}" \
    PYTHONPATH="${DAGENTS_HOME}" GMA_API_HOST=127.0.0.1 GMA_API_PORT="${gma_port}" \
    GMA_FEDERATION_SITES="${sites}" ${gma_deploy[@]+"${gma_deploy[@]}"} \
    "${PYTHON}" -m uvicorn agents.gma.main:app --host 127.0.0.1 --port "${gma_port}"

  # Every request reaches a hospital from the app or the GMA, so the
  # per-client limits are off there. The app keeps them.
  for i in "${!SPLIT_SITES[@]}"; do
    split_spawn "lma-${SPLIT_SITES[$i]}" "${log_dir}/lma-${SPLIT_SITES[$i]}.log" "${SPLIT_APP_ROOT}/backend" \
      PYTHONPATH="${pythonpath}" HEALTHCARE_DEMO_ROLE=lma HEALTHCARE_DEMO_SITE_ID="${SPLIT_SITES[$i]}" \
      HEALTHCARE_DEMO_COORDINATOR_URL="${internal_url}" HEALTHCARE_DEMO_API_PORT="${lma_ports[$i]}" \
      HEALTHCARE_DEMO_RATE_LIMIT_ENTRIES=0 HEALTHCARE_DEMO_RATE_LIMIT_PROBES=0 \
      HEALTHCARE_DEMO_RATE_LIMIT_PILOTS=0 HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS=0 \
      HEALTHCARE_DEMO_DEPLOYER=off \
      "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${lma_ports[$i]}"
  done

  split_spawn app "${log_dir}/app.log" "${SPLIT_APP_ROOT}/backend" \
    PYTHONPATH="${pythonpath}" HEALTHCARE_DEMO_ROLE=app HEALTHCARE_DEMO_API_PORT="${app_port}" \
    HEALTHCARE_DEMO_INTERNAL_HOST=127.0.0.1 HEALTHCARE_DEMO_INTERNAL_PORT="${internal_port}" \
    GMA_PUBLIC_URL="http://127.0.0.1:${gma_port}" HEALTHCARE_DEMO_LMA_URLS="${sites}" \
    "${app_deploy[@]}" \
    "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${app_port}"

  echo "Processes:"
  if [[ "${SPLIT_DEPLOY}" == 1 ]]; then
    printf '  %-12s %s\n' "core-service" \
      "http://127.0.0.1:${core_port}   (Dagents core-service, renders Compose files, log ${log_dir}/core-service.log)"
  fi
  printf '  %-12s %s\n' "gma" "http://127.0.0.1:${gma_port}   (Dagents GMA, log ${log_dir}/gma.log)"
  for i in "${!SPLIT_SITES[@]}"; do
    printf '  %-12s %s\n' "lma-${SPLIT_SITES[$i]}" \
      "http://127.0.0.1:${lma_ports[$i]}   (hospital ${SPLIT_SITES[$i]}, log ${log_dir}/lma-${SPLIT_SITES[$i]}.log)"
  done
  printf '  %-12s %s\n' "app" "${app_url}   (the API the frontend calls, log ${log_dir}/app.log)"
  printf '  %-12s %s\n' "" "${internal_url}   (the app's routes for the hospitals: models and updates)"
  if [[ "${SPLIT_DEPLOY}" == 1 ]]; then
    printf '  %-12s %s\n' "lma-${SPLIT_JOINING_SITE}" \
      "http://127.0.0.1:${joining_port}   (hospital ${SPLIT_JOINING_SITE}, joins when you press Add Hospital 290; log ${log_dir}/deploy/lma-${SPLIT_JOINING_SITE}.log)"
  fi
  echo

  if [[ "${SPLIT_DEPLOY}" == 1 ]]; then
    split_wait_for "http://127.0.0.1:${core_port}/api/v1/health" "core-service" "${log_dir}/core-service.log"
  fi
  split_wait_for "http://127.0.0.1:${gma_port}/health" "The GMA" "${log_dir}/gma.log"
  for i in "${!SPLIT_SITES[@]}"; do
    split_wait_for "http://127.0.0.1:${lma_ports[$i]}/api/v1/health" "Hospital ${SPLIT_SITES[$i]}" \
      "${log_dir}/lma-${SPLIT_SITES[$i]}.log"
  done
  split_wait_for "${app_url}/api/v1/health" "The app" "${log_dir}/app.log"
  SPLIT_APP_URL="${app_url}"
}

split_main() {
  while (( $# > 0 )); do
    case "$1" in
      --deploy) SPLIT_DEPLOY=1 ;;
      *) echo "unknown option: $1" >&2; return 2 ;;
    esac
    shift
  done
  split_preflight
  local log_dir
  log_dir="$(mktemp -d)"
  trap 'trap - EXIT INT TERM; split_stop; rm -rf "'"${log_dir}"'"' EXIT
  # Exit on a signal, which runs the EXIT trap. A trap that only stopped the
  # processes would leave the loop below running over none.
  trap 'exit 130' INT
  trap 'exit 143' TERM

  echo "Dagents healthcare stroke-triage demo, one process per part"
  echo "  framework : ${DAGENTS_HOME}"
  echo "  planner   : ${DAGENTSC_BIN:-dagentsc (on PATH)}"
  echo
  split_start "${log_dir}"
  echo "All ${#SPLIT_PIDS[@]} processes answer. API docs: ${SPLIT_APP_URL}/docs"
  echo
  echo "The records are de-identified records from the International Stroke Trial (1991-1996),"
  echo "kept in memory per hospital unless HEALTHCARE_DEMO_DATABASE_URL is set."
  echo "Nothing here is a validated clinical model or medical advice."
  echo
  if [[ "${SPLIT_DEPLOY}" == 1 ]]; then
    echo "To add Hospital 290 (${SPLIT_JOINING_SITE}) while the demo runs:"
    echo "  curl -X POST ${SPLIT_APP_URL}/api/v1/deployments -H 'Content-Type: application/json' -d '{\"site_id\": \"${SPLIT_JOINING_SITE}\"}'"
    echo
    echo "Ctrl-C stops all six, and the hospital you add."
  else
    echo "Ctrl-C stops all five."
  fi

  # Stop everything when one process exits.
  while true; do
    local i
    for i in "${!SPLIT_PIDS[@]}"; do
      if ! kill -0 "${SPLIT_PIDS[$i]}" 2>/dev/null; then
        echo "${SPLIT_NAMES[$i]} stopped. Last log lines:" >&2
        tail -20 "${log_dir}/${SPLIT_NAMES[$i]}.log" >&2 || true
        exit 1
      fi
    done
    sleep 1
  done
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  split_main "$@"
fi
