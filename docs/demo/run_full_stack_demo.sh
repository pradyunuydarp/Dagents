#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INPUT_DIR="$ROOT_DIR/docs/demo/inputs"
EXPECTED_DIR="$ROOT_DIR/docs/demo/expected"
COMPOSE_ENV="$ROOT_DIR/env/.env.compose"

CORE_URL="${CORE_URL:-http://127.0.0.1:8040}"
PIPELINE_URL="${PIPELINE_URL:-http://127.0.0.1:8030}"
MODEL_URL="${MODEL_URL:-http://127.0.0.1:8000}"
LMA_URL="${LMA_URL:-http://127.0.0.1:8010}"
GMA_URL="${GMA_URL:-http://127.0.0.1:8020}"
SPRING_CONTROL_URL="${SPRING_CONTROL_URL:-http://127.0.0.1:8050}"
SPRING_CORE_URL="${SPRING_CORE_URL:-http://127.0.0.1:8060}"
START_STACK="${START_STACK:-1}"
RUN_MODEL_TRAIN="${RUN_MODEL_TRAIN:-0}"

section() {
  printf '\n\n== %s ==\n' "$1"
}

curl_json() {
  local label="$1"
  local fallback="$2"
  shift 2
  local temp_output
  temp_output="$(mktemp)"
  printf '\n$ curl %s\n' "$*"
  if curl -fsS "$@" | tee "$temp_output"; then
    mv "$temp_output" "$EXPECTED_DIR/$label"
    return 0
  fi
  rm -f "$temp_output"
  printf '\n[demo fallback] live call failed; saved example follows if available:\n'
  if [ -f "$EXPECTED_DIR/$fallback" ]; then
    cat "$EXPECTED_DIR/$fallback"
  else
    printf 'No saved fallback file found for %s\n' "$fallback"
  fi
}

mkdir -p "$EXPECTED_DIR"

section "Build the OCaml planner used by app containers"
(cd "$ROOT_DIR/bindings/ocaml" && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test)

if [ "$START_STACK" = "1" ]; then
  section "Start the full Dagents Docker Compose stack"
  (cd "$ROOT_DIR" && docker compose --env-file "$COMPOSE_ENV" up -d --build)
  printf 'Waiting briefly for health checks to settle...\n'
  sleep 8
else
  section "Skipping Docker startup because START_STACK=$START_STACK"
fi

section "Health checks across the app"
curl_json gma-health.json gma-health.json "$GMA_URL/health" || true
curl_json lma-health.json lma-health.json "$LMA_URL/health" || true
curl_json model-health.json model-health.json "$MODEL_URL/api/v1/health" || true
curl_json pipeline-health.json pipeline-health.json "$PIPELINE_URL/api/v1/health" || true
curl_json core-health.json core-health.json "$CORE_URL/api/v1/health" || true
curl_json spring-control-health.json spring-control-health.json "$SPRING_CONTROL_URL/api/v1/health" || true
curl_json spring-core-health.json spring-core-health.json "$SPRING_CORE_URL/api/v1/health" || true

section "Core service catalog and topology"
curl_json core-services.json core-services.json "$CORE_URL/api/v1/services" || true
curl_json core-topology.json core-topology.json "$CORE_URL/api/v1/topology" || true

section "Pipeline service: register, validate, and run a workflow"
curl_json pipeline-register.json pipeline-register.json -H "Content-Type: application/json" -d @"$INPUT_DIR/app-pipeline-definition.json" "$PIPELINE_URL/api/v1/pipeline-definitions" || true
curl_json pipeline-validate.json pipeline-validate.json -X POST "$PIPELINE_URL/api/v1/pipeline-definitions/orders-risk-demo:validate" || true
curl_json pipeline-run.json pipeline-run.json -H "Content-Type: application/json" -d @"$INPUT_DIR/app-pipeline-run.json" "$PIPELINE_URL/api/v1/pipelines/orders-risk-demo/runs" || true

section "Model service: list datasets and optional lightweight train request"
curl_json model-datasets.json model-datasets.json "$MODEL_URL/api/v1/datasets" || true
if [ "$RUN_MODEL_TRAIN" = "1" ]; then
  curl_json model-train.json model-train.json -H "Content-Type: application/json" -d @"$INPUT_DIR/model-train-request.json" "$MODEL_URL/api/v1/train" || true
else
  printf 'Skipping /api/v1/train by default. Set RUN_MODEL_TRAIN=1 to run the heavier training endpoint.\n'
fi

section "LMA and GMA API examples"
curl_json lma-profile.json lma-profile.json -H "Content-Type: application/json" -d @"$INPUT_DIR/lma-dataset-profile.json" "$LMA_URL/api/v1/datasets:profile" || true
curl_json lma-model-run.json lma-model-run.json -H "Content-Type: application/json" -d @"$INPUT_DIR/lma-model-run.json" "$LMA_URL/api/v1/model-jobs" || true
curl_json gma-registration.json gma-registration.json -X PUT -H "Content-Type: application/json" -d @"$INPUT_DIR/gma-registration.json" "$GMA_URL/api/v1/agents/lma-demo/registration" || true
curl_json gma-profile.json gma-profile.json -H "Content-Type: application/json" -d @"$INPUT_DIR/gma-assimilated-profile.json" "$GMA_URL/api/v1/datasets:profile" || true

section "Core service: workload manifest generation through dagentsc-backed planner"
curl_json core-workload-plan.json core-workload-plan.json -H "Content-Type: application/json" -d @"$INPUT_DIR/core-workload-compile.json" "$CORE_URL/api/v1/workloads:compile" || true

section "Full stack demo complete"
printf 'Cleanup command: docker compose --env-file %s down\n' "$COMPOSE_ENV"
