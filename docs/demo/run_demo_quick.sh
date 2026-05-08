#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INPUT_DIR="$ROOT_DIR/docs/demo/inputs"
DAGENTSC="$ROOT_DIR/bindings/ocaml/_build/default/bin/dagentsc.exe"

printf '\n== Quick fallback demo: build + tests ==\n'
(cd "$ROOT_DIR/bindings/ocaml" && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test)

printf '\n== Quality rules show severity-aware blocking ==\n'
"$DAGENTSC" dataset quality evaluate --records "$INPUT_DIR/records-orders.json" --rules "$INPUT_DIR/quality-rules-orders.json"

printf '\n== Transform records deterministically ==\n'
"$DAGENTSC" dataset transform apply --records "$INPUT_DIR/records-orders.json" --operations "$INPUT_DIR/transform-operations-orders.json"

printf '\n== Render workload manifests from typed specs ==\n'
"$DAGENTSC" manifest compile --input "$INPUT_DIR/workload-plan.json" --output json
