#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
COMPOSE_ENV="${COMPOSE_ENV:-$ROOT_DIR/env/.env.compose}"
if [ -f "$COMPOSE_ENV" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$COMPOSE_ENV"
  set +a
fi
API_BASE="${API_BASE:-http://127.0.0.1:${NL2SQL_DEMO_BACKEND_HOST_PORT:-8070}}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/docs/demo/expected}"
OUTPUT_FILE="$OUTPUT_DIR/nl2sql-generate.json"
ALLOW_MODEL_FALLBACK="${ALLOW_MODEL_FALLBACK:-false}"

mkdir -p "$OUTPUT_DIR"

payload='{
  "question": "Which customers have the highest total order value?",
  "model_id": "codet5p_spider_model",
  "use_dagents_services": true,
  "tables": [
    {
      "name": "customers",
      "columns": [
        { "name": "customer_id", "dtype": "INTEGER" },
        { "name": "customer_name", "dtype": "TEXT" },
        { "name": "region", "dtype": "TEXT" }
      ]
    },
    {
      "name": "orders",
      "columns": [
        { "name": "order_id", "dtype": "INTEGER" },
        { "name": "customer_id", "dtype": "INTEGER" },
        { "name": "order_total", "dtype": "FLOAT" },
        { "name": "created_at", "dtype": "TIMESTAMP" }
      ]
    }
  ]
}'

printf '$ curl -fsS -X POST %s/api/v1/generate\n' "$API_BASE"
curl -fsS -X POST "$API_BASE/api/v1/generate" \
  -H "Content-Type: application/json" \
  -d "$payload" |
  python3 -m json.tool |
  tee "$OUTPUT_FILE"

ALLOW_MODEL_FALLBACK="$ALLOW_MODEL_FALLBACK" python3 - "$OUTPUT_FILE" <<'PY'
import json
import os
import sys

path = sys.argv[1]
payload = json.load(open(path, encoding="utf-8"))
required_steps = {
    "Dagents SourceSpec validation",
    "Dagents extraction planning",
    "Dagents schema contract validation",
    "Dagents quality rules",
    "Dagents pipeline DAG planning",
    "Dagents model route planning",
    "core-service workload compile",
    "pipeline-service register and run",
    "GMA register NL2SQL LMA",
    "GMA LMA heartbeat",
    "LMA source registration and validation",
    "GMA source registration and validation",
    "LMA source profile",
    "LMA source model job",
    "GMA assimilated profile",
    "GMA aggregate model job",
    "GMA desired deployment and sync",
    "GMA aggregate run dispatch",
}
trace = payload.get("dagents_trace", [])
names = {step.get("name") for step in trace}
missing = sorted(required_steps - names)
warnings = [step for step in trace if step.get("status") != "ok"]

if "SELECT" not in payload.get("sql", "").upper():
    raise SystemExit("Probe failed: response did not include generated SQL")
allow_model_fallback = os.environ.get("ALLOW_MODEL_FALLBACK", "false").lower() in {"1", "true", "yes", "on"}
if payload.get("used_fallback") and not allow_model_fallback:
    raise SystemExit(f"Probe failed: model adapter used fallback: {payload.get('fallback_detail')}")
if missing:
    raise SystemExit(f"Probe failed: missing Dagents trace steps: {', '.join(missing)}")
if warnings:
    details = "; ".join(f"{step.get('name')}: {step.get('detail')}" for step in warnings)
    raise SystemExit(f"Probe failed: Dagents trace contains non-ok steps: {details}")

if payload.get("used_fallback"):
    print(f"Fallback detail: {payload.get('fallback_detail')}")
print(f"Validated {len(trace)} Dagents trace steps.")
PY

printf '\nSaved response to %s\n' "$OUTPUT_FILE"
