#!/usr/bin/env bash
# Run one governed federated pilot and print the result, without starting a server.
#
# Three hospitals from the International Stroke Trial, eight rounds (analytics,
# evaluation, five training rounds and candidate validation) and a release
# decision, with every exclusion and rejection explained. Set
# HEALTHCARE_DEMO_DATABASE_URL to read the hospitals' records from Postgres;
# without it they are loaded from the committed extract.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
DAGENTS_HOME="${DAGENTS_HOME:-$(cd "${APP_ROOT}/../.." && pwd)}"

PLANNER="${DAGENTSC_BIN:-${DAGENTS_HOME}/bindings/ocaml/_build/default/bin/dagentsc.exe}"
[[ -x "${PLANNER}" ]] && export DAGENTSC_BIN="${PLANNER}"

PYTHON="${PYTHON:-${DAGENTS_HOME}/.venv/bin/python}"
[[ -x "${PYTHON}" ]] || PYTHON="$(command -v python3)"

export PYTHONPATH="${DAGENTS_HOME}:${APP_ROOT}/backend"
exec "${PYTHON}" "${APP_ROOT}/scripts/run_pilot.py" "$@"
