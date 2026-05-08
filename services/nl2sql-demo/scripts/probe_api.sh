#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
API_BASE="${API_BASE:-http://127.0.0.1:8070}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/docs/demo/expected}"
OUTPUT_FILE="$OUTPUT_DIR/nl2sql-generate.json"

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

printf '\nSaved response to %s\n' "$OUTPUT_FILE"
