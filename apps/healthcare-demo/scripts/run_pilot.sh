#!/usr/bin/env bash
# Run one governed federated pilot and print the result, without starting a server.
#
# This is the fastest way to see what the framework does: three hospitals, four
# rounds, and a release decision, with every exclusion and rejection explained.
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
