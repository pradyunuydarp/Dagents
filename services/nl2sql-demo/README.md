# Dagents NL2SQL Demo App

This demo app shows how to build an NL2SQL product on top of Dagents rather than as a standalone backend.

**Live:** <https://pradyunuydarp.github.io/Dagents/nl2sql-demo/>

That page has no backend, so it replays a capture: the responses were recorded from the real
services with the OCaml planner built, and the page names the commit they came from. Every trace
step, schema contract and routing decision shown is one the framework really produced. Ask it for
something the capture does not hold and it says so rather than inventing an answer — a demo that
fabricated a plan would be demonstrating the opposite of the point. The published run uses the
deterministic fallback adapter rather than a GPU model.

## What It Uses From Dagents

- `agents.common.domain`: shared Pydantic contracts and base models.
- `agents.common.infrastructure.dagents_runner`: subprocess bridge to the OCaml functional planner.
- `bindings/ocaml`: source validation, extraction planning, schema validation, quality rules, pipeline planning, model routing, and manifest rendering.
- Dagents backend services: core-service, pipeline-service, model-service, LMA, and GMA are checked and displayed in the UI as part of the app trace.
- Core-service is used for catalog/topology lookup and workload manifest planning.
- Pipeline-service is used to register and run a schema-profiling workflow before SQL generation.
- Model-service is used for reusable model catalog/job visibility while the NL2SQL-specific adapters load notebook artifacts.
- LMA and GMA are used for source-scoped and assimilated schema profiling.

## Model Artifacts

The app discovers zip files in `./models`:

- CodeQwen LoRA adapters use the Qwen chat prompt:
  `Context: {DDL}\n\nQuestion: {question}` with a SQL-only system instruction.
- CodeT5+/T5 artifacts use:
  `question: {question} context: {DDL}`

The real model adapters are implemented, but the app defaults to a deterministic fallback when local dependencies, GPU, or base model downloads are unavailable. This keeps the demo reliable while still allowing model execution when the environment supports it.

To try the real model adapters, install the optional packages into your local Python environment:

```bash
.venv/bin/pip install -r services/nl2sql-demo/backend/requirements-optional-models.txt
```

## Run Locally

Presenter-friendly scripts:

```bash
services/nl2sql-demo/scripts/run_local_demo.sh
services/nl2sql-demo/scripts/probe_api.sh
```

Docker Compose demo:

```bash
services/nl2sql-demo/scripts/run_compose_demo.sh
services/nl2sql-demo/scripts/probe_api.sh
services/nl2sql-demo/scripts/stop_compose_demo.sh
```

Backend:

```bash
PYTHONPATH=services/nl2sql-demo/backend:. \
  DAGENTSC_BIN=bindings/ocaml/_build/default/bin/dagentsc.exe \
  NL2SQL_MODELS_DIR=models \
  .venv/bin/uvicorn app.main:app --app-dir services/nl2sql-demo/backend --reload --port 8070
```

Frontend:

```bash
cd services/nl2sql-demo/frontend
npm install
VITE_NL2SQL_API_BASE=http://127.0.0.1:8070 npm run dev
```

Open `http://127.0.0.1:5173`.

## Docker Compose

The top-level compose file includes:

- `nl2sql-demo-backend`
- `nl2sql-demo-frontend`

Run:

```bash
docker compose --env-file env/.env.compose up --build nl2sql-demo-backend nl2sql-demo-frontend
```

Open `http://127.0.0.1:5173`.
