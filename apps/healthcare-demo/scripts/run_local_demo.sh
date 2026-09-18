#!/usr/bin/env bash
# Run the healthcare stroke-triage demo locally, with no Docker and no database.
#
# The pilot composes Dagents in-process, so the only hard dependency is the
# OCaml planner binary: the Ethical Guard fails closed without it, which is the
# correct behaviour and also means nothing useful runs.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"

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
PORT="${HEALTHCARE_DEMO_API_PORT:-8080}"

echo "Dagents healthcare stroke-triage demo"
echo "  framework : ${DAGENTS_HOME}"
echo "  planner   : ${DAGENTSC_BIN:-dagentsc (on PATH)}"
echo "  api       : http://127.0.0.1:${PORT}/docs"
echo
echo "All patient data is synthetic. Nothing here is a validated clinical model."
echo

exec "${PYTHON}" -m uvicorn app.main:app --host "${HEALTHCARE_DEMO_API_HOST:-0.0.0.0}" --port "${PORT}"
