#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INPUT_DIR="$ROOT_DIR/docs/demo/inputs"
EXPECTED_DIR="$ROOT_DIR/docs/demo/expected"
DAGENTSC="$ROOT_DIR/bindings/ocaml/_build/default/bin/dagentsc.exe"

section() {
  printf '\n\n== %s ==\n' "$1"
}

run_json() {
  local name="$1"
  shift
  printf '\n$ %s\n' "$*"
  "$@" | tee "$EXPECTED_DIR/$name"
}

mkdir -p "$EXPECTED_DIR"

section "Build the OCaml functional planner CLI"
(cd "$ROOT_DIR/bindings/ocaml" && opam exec -- dune build ./bin/dagentsc.exe)

section "Run OCaml planner tests"
(cd "$ROOT_DIR/bindings/ocaml" && opam exec -- dune test)

section "1. Validate a source spec"
run_json source-valid.json "$DAGENTSC" dataset source validate --input "$INPUT_DIR/source-postgres-valid.json"

section "2. Reject an invalid source spec"
set +e
"$DAGENTSC" dataset source validate --input "$INPUT_DIR/source-invalid.json" | tee "$EXPECTED_DIR/source-invalid.json"
set -e

section "3. Compile source-specific input into a normalized extraction plan"
run_json extraction-plan.json "$DAGENTSC" dataset source extract --input "$INPUT_DIR/source-postgres-valid.json"

section "4. Validate record schema against a contract"
run_json schema-validation.json "$DAGENTSC" dataset schema validate --records "$INPUT_DIR/records-orders.json" --contract "$INPUT_DIR/schema-contract-orders.json"

section "5. Evaluate data quality rules with aggregate blocking status"
run_json quality-report.json "$DAGENTSC" dataset quality evaluate --records "$INPUT_DIR/records-orders.json" --rules "$INPUT_DIR/quality-rules-orders.json"

section "6. Compile transform plan and inspect output schema"
run_json transform-plan.json "$DAGENTSC" dataset transform compile --records "$INPUT_DIR/records-orders.json" --operations "$INPUT_DIR/transform-operations-orders.json"

section "7. Apply deterministic record transforms"
run_json transformed-records.json "$DAGENTSC" dataset transform apply --records "$INPUT_DIR/records-orders.json" --operations "$INPUT_DIR/transform-operations-orders.json"

section "8. Plan a valid pipeline DAG"
run_json pipeline-plan.json "$DAGENTSC" pipeline compile --input "$INPUT_DIR/pipeline-valid.json" --output json

section "9. Show cycle rejection for an invalid pipeline"
set +e
"$DAGENTSC" pipeline compile --input "$INPUT_DIR/pipeline-cyclic.json" --output json > "$EXPECTED_DIR/pipeline-cyclic-error.txt" 2>&1
status=$?
set -e
cat "$EXPECTED_DIR/pipeline-cyclic-error.txt"
printf 'cyclic pipeline exit status: %s\n' "$status"

section "10. Route a model from a dataset profile"
run_json model-route.json "$DAGENTSC" model route --task anomaly_detection --output json

section "11. Render Kubernetes manifests from a typed workload plan"
run_json manifest-plan.json "$DAGENTSC" manifest compile --input "$INPUT_DIR/workload-plan.json" --output json

section "Functional layer demo complete"
printf 'Saved outputs are in %s\n' "$EXPECTED_DIR"
