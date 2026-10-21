#!/usr/bin/env bash
# Check the stroke demo in the layout it is published in, in a real browser.
#
#   HEALTHCARE_DEMO_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres \
#     apps/healthcare-demo/scripts/run_published_check.sh
#
# The published page is a production build on one origin (GitHub Pages). It
# calls the API on another origin, and the API runs its agents in its own
# process and keeps each hospital's records in Postgres (render.yaml).
# run_frontend_demo.sh --check drives the page through the Vite dev server,
# whose proxy puts the API on the page's origin, so it cannot see a CORS
# setting, a real cold start or a database that is not ready. This script
# builds the page as the Pages workflow does, serves it on one origin, runs the
# API on another, and drives the page in three phases, each on a database of
# its own:
#
#   1. unmigrated: the database has none of the app's tables. Each page shows
#      the API's own 503 sentence at once (smoke.mjs --expect-unmigrated).
#   2. empty: the migrations ran and no records are loaded. The consortium page
#      and a hospital page show the not-ready banner, say the stores are empty
#      and give each chart's reason (smoke.mjs --expect-empty).
#   3. seeded: the records are loaded. The page opens before the API starts
#      and shows the connecting banner; the script then starts the API, and the
#      banner must go and the hospitals table load (smoke.mjs
#      --expect-cold-start). Then the full smoke test runs (smoke.mjs
#      --api-base).
#
# It needs:
#   - HEALTHCARE_DEMO_TEST_DATABASE_URL: a superuser login to a PostgreSQL 16
#     server, the one the healthcare suite uses. The script creates three
#     databases with new names and drops them when it exits. In each one it
#     adds Supabase's roles (supabase/emulate_supabase_roles.sql), as the
#     healthcare suite does, and runs the migrations, the seed and the API as
#     supa_like. supa_like stands in for the Supabase role the published API
#     connects as, which is not a superuser, so a missing grant fails here as
#     it would on Supabase. supa_like, the API roles and the hospitals'
#     NOLOGIN roles belong to the whole server; the script leaves them,
#     because another database on the server may use them.
#   - the planner: DAGENTSC_BIN, the dune build in the Dagents checkout, or
#     dagentsc on PATH. The script never builds it.
#   - Node with the frontend's packages (it runs npm ci when node_modules is
#     missing), and Chromium, which smoke.mjs finds through CHROMIUM_PATH.
#
# The API and the page each get a free port. The page is served at
# http://localhost:<port>/Dagents/healthcare-demo/, as Pages serves it under the
# framework site, and calls the API at http://127.0.0.1:<port>, so the browser
# treats them as two origins. The API allows exactly the page's origin
# (HEALTHCARE_DEMO_CORS_ORIGINS) and uses the settings render.yaml gives the
# published API, except the ones that describe Render's proxy.
#
# Exit status: 0 when every phase passed; 1 when a check failed or a process
# did not start; 2 when smoke.mjs could not start a browser and printed SKIP,
# which is not a pass.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
FRONTEND="${APP_ROOT}/frontend"
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${APP_ROOT}/../.." && pwd)}"
export DAGENTS_HOME

#: The path the Pages workflow publishes the demo under.
BASE_PATH="/Dagents/healthcare-demo/"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }

# --- preflight -------------------------------------------------------------

if [[ ! -d "${DAGENTS_HOME}/dagents/common" ]]; then
  echo "Could not find the Dagents framework. Set DAGENTS_HOME to your Dagents checkout." >&2
  exit 1
fi

PLANNER="${DAGENTSC_BIN:-${DAGENTS_HOME}/bindings/ocaml/_build/default/bin/dagentsc.exe}"
if [[ ! -x "${PLANNER}" ]] && ! command -v dagentsc >/dev/null 2>&1; then
  echo "The dagentsc planner is not built. Set DAGENTSC_BIN, or build it:" >&2
  echo "  cd ${DAGENTS_HOME}/bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe" >&2
  echo "Without it the Ethical Guard denies every request, so the page would show only denials." >&2
  exit 1
fi
[[ -x "${PLANNER}" ]] && export DAGENTSC_BIN="${PLANNER}"

if [[ -z "${HEALTHCARE_DEMO_TEST_DATABASE_URL:-}" ]]; then
  echo "Set HEALTHCARE_DEMO_TEST_DATABASE_URL to a superuser login to a PostgreSQL 16 server," >&2
  echo "for example postgresql://postgres:postgres@127.0.0.1:5432/postgres. The script makes three scratch databases." >&2
  exit 1
fi

for tool in node npm curl; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "${tool} is required." >&2
    exit 1
  fi
done

PYTHON="${PYTHON:-${DAGENTS_HOME}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"
export PYTHON

if [[ ! -d "${FRONTEND}/node_modules" ]]; then
  bold "Installing the frontend's packages"
  (cd "${FRONTEND}" && npm ci --no-audit --no-fund)
fi

# --- processes -------------------------------------------------------------

WORK="$(mktemp -d)"
API_PID=""
UI_PID=""
SMOKE_PID=""
DATABASES=""

# Replace this (background) subshell with a command in a session of its own,
# so stopping the group stops everything the command started. In a script a
# background job is not a process group leader, so setsid does not fork, and
# the job's pid ($!) is the new group's id.
become() {
  if command -v setsid >/dev/null 2>&1; then
    exec setsid "$@"
  fi
  exec "$@"
}

# Every process a process started, then the process itself.
kill_tree() {
  local pid="$1" signal="$2" child
  for child in $(pgrep -P "${pid}" 2>/dev/null || true); do
    kill_tree "${child}" "${signal}"
  done
  kill "-${signal}" "${pid}" 2>/dev/null || true
}

# Stop a process group: TERM, up to 10 seconds, then KILL.
stop_group() {
  local pid="$1" waited=0
  [[ -n "${pid}" ]] || return 0
  kill -TERM -- "-${pid}" 2>/dev/null || kill_tree "${pid}" TERM
  while kill -0 "${pid}" 2>/dev/null && (( waited < 40 )); do
    sleep 0.25
    waited=$((waited + 1))
  done
  kill -KILL -- "-${pid}" 2>/dev/null || kill_tree "${pid}" KILL
  wait "${pid}" 2>/dev/null || true
}

# databases create|drop NAME...: the scratch databases. "create" adds
# Supabase's roles to each one (supabase/emulate_supabase_roles.sql) and
# prints a connection string per database that logs in as supa_like, the
# stand-in for the Supabase role the published API connects as, which is not a
# superuser. The admin login only creates, prepares and drops the databases.
# The connection strings are never printed to the log.
databases() {
  "${PYTHON}" - "${APP_ROOT}/supabase/emulate_supabase_roles.sql" "$@" <<'PY'
import hashlib
import os
import sys

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

admin = os.environ["HEALTHCARE_DEMO_TEST_DATABASE_URL"].strip()
emulation_file, action, names = sys.argv[1], sys.argv[2], sys.argv[3:]
# Roles belong to the whole server, so supa_like is shared with the healthcare
# suite (tests/test_site_isolation.py). Its password is derived from the admin
# login in the same way, so a suite running at the same time keeps working.
password = hashlib.sha256(f"supa_like:{admin}".encode()).hexdigest()[:32]
with psycopg.connect(admin, autocommit=True, connect_timeout=10) as conn:
    for name in names:
        if action == "create":
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            with psycopg.connect(make_conninfo(admin, dbname=name), autocommit=True, connect_timeout=10) as scratch:
                scratch.execute(open(emulation_file, encoding="utf-8").read())
                scratch.execute(sql.SQL("ALTER ROLE supa_like PASSWORD {}").format(sql.Literal(password)))
            login = make_conninfo(admin, dbname=name, user="supa_like", password=password)
            with psycopg.connect(login, connect_timeout=10) as check:
                if check.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]:
                    sys.exit("supa_like is a superuser on this server, so the check would not test the grants.")
            print(login)
        else:
            conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
PY
}

cleanup() {
  trap - EXIT INT TERM
  stop_group "${SMOKE_PID}"
  stop_group "${API_PID}"
  stop_group "${UI_PID}"
  if [[ -n "${DATABASES}" ]]; then
    # shellcheck disable=SC2086 # one database name per word
    databases drop ${DATABASES} || echo "Could not drop the scratch databases: ${DATABASES}" >&2
  fi
  rm -rf "${WORK}"
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

free_port() {
  "${PYTHON}" -c 'import socket
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])'
}

wait_for() {
  local url="$1" pid="$2" tries=240
  for ((i = 0; i < tries; i++)); do
    if curl -sf --noproxy '*' -o /dev/null "${url}"; then return 0; fi
    kill -0 "${pid}" 2>/dev/null || return 1
    sleep 0.25
  done
  return 1
}

API_PORT="$(free_port)"
UI_PORT="$(free_port)"
API_URL="http://127.0.0.1:${API_PORT}"
PAGE_ORIGIN="http://localhost:${UI_PORT}"
PAGE_URL="${PAGE_ORIGIN}${BASE_PATH}"

# The settings render.yaml gives the published API, as NAME=value lines,
# without the ones that describe Render's proxy or the address to bind.
API_SETTINGS="$("${PYTHON}" - "${APP_ROOT}/render.yaml" <<'PY'
import re
import sys

LEFT_OUT = {"HEALTHCARE_DEMO_API_HOST", "HEALTHCARE_DEMO_TRUST_FORWARDED_FOR", "HEALTHCARE_DEMO_FORWARDED_FOR_HOPS"}
text = open(sys.argv[1], encoding="utf-8").read()
for name, value in re.findall(r"- key: (HEALTHCARE_DEMO_[A-Z0-9_]+)\n\s+value: (.+)", text):
    if name not in LEFT_OUT:
        print(f"{name}={value.strip().strip(chr(34))}")
PY
)"
if [[ -z "${API_SETTINGS}" ]]; then
  echo "Read no settings from render.yaml." >&2
  exit 1
fi

# start_api PHASE DATABASE_URL: the API in process, as render.yaml runs it.
start_api() {
  local phase="$1" database_url="$2" line
  (
    while IFS= read -r line; do
      [[ -n "${line}" ]] && export "${line?}"
    done <<<"${API_SETTINGS}"
    # A per-hospital database from the caller's environment would send a
    # hospital's records elsewhere.
    for line in $(compgen -v | grep -E '^HEALTHCARE_DEMO_SITE_.*_DATABASE_URL$' || true); do
      unset "${line}"
    done
    export HEALTHCARE_DEMO_DATABASE_URL="${database_url}"
    export HEALTHCARE_DEMO_CORS_ORIGINS="${PAGE_ORIGIN}"
    export HEALTHCARE_DEMO_API_HOST=127.0.0.1
    export HEALTHCARE_DEMO_API_PORT="${API_PORT}"
    cd "${APP_ROOT}/backend"
    become "${HERE}/run_local_demo.sh"
  ) >"${WORK}/api-${phase}.log" 2>&1 &
  API_PID=$!
  local started
  started="$(date +%s)"
  if ! wait_for "${API_URL}/api/v1/health" "${API_PID}"; then
    echo "The API did not start. Last log lines:" >&2
    tail -30 "${WORK}/api-${phase}.log" >&2
    return 1
  fi
  echo "The API answered $(( $(date +%s) - started )) s after it was started."
}

stop_api() {
  stop_group "${API_PID}"
  API_PID=""
}

# run_smoke ARGS...: smoke.mjs against the page; exits the script when it
# fails or skips.
run_smoke() {
  local status
  set +e
  (cd "${FRONTEND}" && node smoke.mjs "${PAGE_URL}" --api-base "${API_URL}" "$@")
  status=$?
  set -e
  check_status "${status}"
}

check_status() {
  case "$1" in
    0) ;;
    2)
      echo "SKIP: the smoke test could not start a browser, so the published layout was not checked." >&2
      exit 2
      ;;
    *)
      echo "The published-layout check failed." >&2
      [[ -n "${API_PID}" && -f "${WORK}/api-${PHASE}.log" ]] && { echo "Last API log lines:" >&2; tail -30 "${WORK}/api-${PHASE}.log" >&2; }
      exit 1
      ;;
  esac
}

# --- the page, built and served as the Pages workflow publishes it ----------

bold "Building the page for ${BASE_PATH}, calling the API on another origin"
(
  cd "${FRONTEND}"
  DEMO_BASE_PATH="${BASE_PATH}" VITE_HEALTHCARE_API_BASE="${API_URL}" \
    npm run build -- --outDir "${WORK}/dist" --emptyOutDir
) >"${WORK}/build.log" 2>&1 || { tail -30 "${WORK}/build.log" >&2; exit 1; }

bold "Serving it at ${PAGE_URL}"
(
  cd "${FRONTEND}"
  export DEMO_BASE_PATH="${BASE_PATH}"
  become ./node_modules/.bin/vite preview --host localhost --port "${UI_PORT}" --strictPort --outDir "${WORK}/dist"
) >"${WORK}/preview.log" 2>&1 &
UI_PID=$!
if ! wait_for "${PAGE_URL}" "${UI_PID}"; then
  echo "The page server did not start. Last log lines:" >&2
  tail -20 "${WORK}/preview.log" >&2
  exit 1
fi

TAG="hc_published_$("${PYTHON}" -c 'import secrets; print(secrets.token_hex(4))')"
DATABASES="${TAG}_unmigrated ${TAG}_empty ${TAG}_seeded"
DATABASE_URLS=()
while IFS= read -r line; do
  DATABASE_URLS+=("${line}")
  # shellcheck disable=SC2086 # one database name per word
done < <(databases create ${DATABASES})
if [[ "${#DATABASE_URLS[@]}" -ne 3 ]]; then
  echo "Could not create the scratch databases." >&2
  exit 1
fi

# --- 1. a database with none of the app's tables ---------------------------

PHASE="unmigrated"
bold "Phase 1: the API on a database with none of the app's tables"
start_api "${PHASE}" "${DATABASE_URLS[0]}"
run_smoke --expect-unmigrated
stop_api

# --- 2. migrated, with no records ------------------------------------------

PHASE="empty"
bold "Phase 2: the API on a migrated database with no records"
SUPABASE_DB_URL="${DATABASE_URLS[1]}" "${PYTHON}" "${HERE}/apply_migrations.py"
start_api "${PHASE}" "${DATABASE_URLS[1]}"
run_smoke --expect-empty
stop_api

# --- 3. the records loaded, and a cold start --------------------------------

PHASE="seeded"
bold "Phase 3: the records loaded; the page opens before the API starts"
SUPABASE_DB_URL="${DATABASE_URLS[2]}" "${PYTHON}" "${HERE}/apply_migrations.py"
SUPABASE_DB_URL="${DATABASE_URLS[2]}" "${PYTHON}" "${HERE}/seed_supabase.py"

READY_FILE="${WORK}/start-the-api"
(
  cd "${FRONTEND}"
  become node smoke.mjs "${PAGE_URL}" --api-base "${API_URL}" --expect-cold-start "${READY_FILE}"
) &
SMOKE_PID=$!
for ((i = 0; i < 300; i++)); do
  [[ -f "${READY_FILE}" ]] && break
  kill -0 "${SMOKE_PID}" 2>/dev/null || break
  sleep 0.2
done
if [[ -f "${READY_FILE}" ]]; then
  echo "The page shows the connecting banner; starting the API now."
  if ! start_api "${PHASE}" "${DATABASE_URLS[2]}"; then
    stop_group "${SMOKE_PID}"
    SMOKE_PID=""
    exit 1
  fi
fi
set +e
wait "${SMOKE_PID}"
STATUS=$?
set -e
SMOKE_PID=""
check_status "${STATUS}"
if [[ -z "${API_PID}" ]]; then
  echo "The page never asked for the API to start." >&2
  exit 1
fi

bold "Phase 3: the full smoke test against the published layout"
run_smoke
stop_api

bold "The published layout passed: unmigrated, empty and seeded."
