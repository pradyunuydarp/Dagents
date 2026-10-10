# Dagents Presentation Demo Runbook

This demo package is designed for a 25-minute course presentation: 20 minutes of demo/talk and 5 minutes of Q&A. It covers the whole Dagents app while keeping the main grading focus on the OCaml functional planner layer.

Both apps are also published. Use them if something will not start locally. The stroke demo
is a good opener, because it runs against a deployed API and database:

- framework site: <https://pradyunuydarp.github.io/Dagents/>
- stroke triage, calling its deployed API: <https://pradyunuydarp.github.io/Dagents/healthcare-demo/>
- NL2SQL, replaying a recorded run: <https://pradyunuydarp.github.io/Dagents/nl2sql-demo/>
- the learning guide, for background questions: <https://pradyunuydarp.github.io/Dagents/#/learn>

The API sleeps when idle, so open the stroke demo a minute before presenting. The first request
takes about 20 seconds, and the page shows a banner while it waits.

## Prerequisites

- OCaml toolchain available through `opam`.
- Docker Desktop or Docker Engine for the full-stack demo.
- `curl` for service calls.
- Optional: `jq` for prettier JSON while presenting.

## Recommended Demo Order

1. Start with the architecture docs:
   - `docs/demo/app-architecture-walkthrough.md`
   - `docs/demo/functional-modules-walkthrough.md`
2. Run the OCaml-centered demo:
   ```bash
   bash docs/demo/run_functional_layer_demo.sh
   ```
3. Run the full app demo:
   ```bash
   bash docs/demo/run_full_stack_demo.sh
   ```
4. If time or Docker is unstable, use the quick fallback:
   ```bash
   bash docs/demo/run_demo_quick.sh
   ```
5. Run the governance and federation demo, which needs no Docker at all:
   ```bash
   bash docs/demo/run_governance_demo.sh
   ```

   This step shows the typed OCaml layer at work. The governance planner gives the same request
   different strategies as trust and granularity change. A federated pilot then rejects a
   candidate model that scores higher than the current one, because two safety gates fail. Both
   decisions come from `dagentsc`, not from the calling service.

## What To Say While Running The Demo

The main message: Dagents uses OCaml for pure, typed planning. Python and Java services still do runtime work, but OCaml handles deterministic transformations such as validation, source extraction planning, quality decisions, pipeline DAG planning, model routing, and manifest rendering.

Use this contrast:

- Python services: own APIs, runtime orchestration, model training, and I/O.
- OCaml functional modules: own typed contracts and pure planning logic.
- `dagentsc`: lets services call the OCaml layer through JSON and subprocesses without direct FFI.

## Full Stack Demo Commands

The full demo script builds the OCaml CLI, starts Docker Compose, checks health, calls service catalog/topology, registers and runs a pipeline, calls model-service dataset APIs, calls LMA/GMA examples, and compiles a workload plan through core-service.

```bash
bash docs/demo/run_full_stack_demo.sh
```

Useful options:

```bash
START_STACK=0 bash docs/demo/run_full_stack_demo.sh
RUN_MODEL_TRAIN=1 bash docs/demo/run_full_stack_demo.sh
```

`RUN_MODEL_TRAIN=1` enables the heavier `/api/v1/train` call. Keep it off for the live presentation unless the environment is already warm and tested.

## Functional Layer Demo Commands

```bash
bash docs/demo/run_functional_layer_demo.sh
```

This script shows:

- source validation
- extraction plan generation
- schema validation
- severity-aware quality reports
- transform planning and application
- pipeline DAG planning
- cycle rejection
- model routing
- Kubernetes manifest rendering

## Saved Outputs

Scripts write saved outputs into:

```text
docs/demo/expected/
```

If a live call fails during the presentation, open the matching saved output and continue the explanation. The saved output is part of the hybrid demo strategy.

## Cleanup

```bash
docker compose --env-file env/.env.compose down
```

## Common Failure Recovery

- If Docker is slow, switch to `run_demo_quick.sh`.
- If model training is slow, keep `RUN_MODEL_TRAIN=0` and show `/api/v1/datasets`.
- If a service health check fails, use the corresponding file under `docs/demo/expected/`.
- If anyone asks about “compiler,” clarify that this project uses the term only as domain-specific translation/planning, not programming-language compilation.
