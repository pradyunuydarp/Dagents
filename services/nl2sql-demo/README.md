# Dagents NL2SQL demo

This demo turns a natural language question into SQL. It shows how to build an
NL2SQL product on top of Dagents: the app owns its UI and its SQL models, and
Dagents provides validation, planning, service checks and workload compilation.

**Live:** <https://pradyunuydarp.github.io/Dagents/nl2sql-demo/>

The published page has no backend. It replays responses recorded from the real
services, with the OCaml planner built, and names the commit they came from.
Only the bundled samples were recorded; an edited question gets no answer. The
recorded run uses the rule-based fallback adapter, not a GPU model.

## What it uses from Dagents

- `agents.common.domain`: shared Pydantic contracts and base models.
- `agents.common.infrastructure.dagents_runner`: the bridge to the OCaml planner.
- `bindings/ocaml`: source validation, extraction planning, schema validation,
  quality rules, pipeline planning, model routing and manifest rendering.
- The framework services. The UI shows a status check of each one, and a trace
  of every call:
  - core-service: catalog and topology lookup, and workload manifest planning;
  - pipeline-service: registers and runs a schema profiling workflow before
    SQL generation;
  - model-service: the shared model catalog and jobs (the NL2SQL adapters load
    their own model files);
  - LMA and GMA: schema profiling for one source and across sources.

## Model files

The app looks for zip files in `./models`:

- CodeQwen LoRA adapters use the Qwen chat prompt
  `Context: {DDL}\n\nQuestion: {question}`, with a system instruction to
  return only SQL.
- CodeT5+ and T5 models use `question: {question} context: {DDL}`.

The real model adapters are implemented. When the optional packages, a GPU or
the base model download are missing, the app uses a rule-based fallback adapter,
so the demo always runs. To try the real adapters, install the optional
packages:

```bash
.venv/bin/pip install -r services/nl2sql-demo/backend/requirements-optional-models.txt
```

## Run locally

Scripts:

```bash
services/nl2sql-demo/scripts/run_local_demo.sh
services/nl2sql-demo/scripts/probe_api.sh
```

With Docker Compose:

```bash
services/nl2sql-demo/scripts/run_compose_demo.sh
services/nl2sql-demo/scripts/probe_api.sh
services/nl2sql-demo/scripts/stop_compose_demo.sh
```

Backend only:

```bash
PYTHONPATH=services/nl2sql-demo/backend:. \
  DAGENTSC_BIN=bindings/ocaml/_build/default/bin/dagentsc.exe \
  NL2SQL_MODELS_DIR=models \
  .venv/bin/uvicorn app.main:app --app-dir services/nl2sql-demo/backend --reload --port 8070
```

Frontend only:

```bash
cd services/nl2sql-demo/frontend
npm install
VITE_NL2SQL_API_BASE=http://127.0.0.1:8070 npm run dev
```

Open `http://127.0.0.1:5173`.

## Docker Compose

The top-level compose file includes `nl2sql-demo-backend` and
`nl2sql-demo-frontend`:

```bash
docker compose --env-file env/.env.compose up --build nl2sql-demo-backend nl2sql-demo-frontend
```

Open `http://127.0.0.1:5173`.
