# Dagents NL2SQL demo

This demo turns a natural language question into SQL. It shows how to build an
NL2SQL product on top of Dagents: the app owns its UI and its SQL models, and
Dagents provides validation, planning, service checks and workload compilation.

**Live:** <https://pradyunuydarp.github.io/Dagents/nl2sql-demo/>

The published page has no backend. It replays responses recorded from the real
services, with the OCaml planner built, and names the commit they came from.
Only the bundled samples were recorded; an edited question gets no answer. The
recorded run uses the rule-based fallback adapter, not a GPU model.

A shared link to the page shows the image in `frontend/public/social-preview.png`.
That image and the icons beside it are rendered from `design/social/`; that
folder's README says how to change them.

## What it uses from Dagents

- `dagents.common.domain`: shared Pydantic contracts and base models.
- `dagents.common.infrastructure.dagents_runner`: the bridge to the OCaml planner.
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

`run_local_demo.sh` starts core-service, pipeline-service, model-service, the
LMA and the GMA on the addresses in `env/.env.shared`, then the backend on port
8070, then the UI on port 5173. model-service needs torch. Without it the
script prints a warning and the page shows model-service as unavailable. The
script uses the planner that `DAGENTSC_BIN` names, or builds it with `opam exec
-- dune build` when the variable is not set. Ctrl-C stops every process.

## Check it in a browser

```bash
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  services/nl2sql-demo/scripts/run_local_demo.sh --check
```

`--check` starts the same services and the backend on free loopback ports,
builds the UI, serves the bundle with `vite preview` and runs
`frontend/smoke.mjs` in Chromium. The smoke test checks that:

1. the status strip reads 5/5 reachable;
2. the sample list holds every sample the backend offers;
3. each sample shows SQL that starts with `SELECT`, the DDL with a `CREATE
   TABLE` for each table, the prompt, and a trace of 22 steps with no warning;
4. an edited question reaches the backend;
5. schema JSON that does not parse shows an error;
6. the theme toggle switches between light and dark;
7. with pipeline-service stopped, the strip reads 4/5 and only the step
   "pipeline-service register and run" is a warning;
8. nothing logs a console error or fails a request.

`--check` points `NL2SQL_MODELS_DIR` at an empty folder, so the backend offers
only the rule-based fallback adapter (`heuristic_demo`). The check does not
test an adapter loaded from a model zip file.

The smoke test prints `ok  <check>` for each check that passes. It exits 1 on a
failure, and 2 with `SKIP` when it finds no Chromium or no `playwright-core`;
the script then says "smoke test skipped" and exits 0. Set `CHROMIUM_PATH` to
use a browser of your own. The CI job `nl2sql-e2e` fails on a skip.

The published page is a replay. To check a replay build the way Pages serves
it, record a run, check the recording, build under the base path and drive it:

```bash
.venv/bin/python scripts/capture_demo_recordings.py --demo nl2sql   # uses ports 8000-8070
.venv/bin/python scripts/check_demo_recordings.py nl2sql
cd services/nl2sql-demo/frontend
DEMO_BASE_PATH=/Dagents/nl2sql-demo/ VITE_DAGENTS_STATIC=1 npm run build
node smoke.mjs --replay dist --base /Dagents/nl2sql-demo/
```

With `--replay`, the smoke test serves `dist` under the base path and answers
404 for a missing file, as Pages does. It checks that the banner names the
recording's commit, that every sample with every model replays its SQL and its
trace, that an edited question shows "Not recorded", and that no request goes
to `/api/` or to another origin. A trace step may be a warning only when it
calls a service the recording lists as not running during the capture, such as
model-service on a machine without torch. `check_demo_recordings.py` refuses
any other warning.

`tests/test_nl2sql_stack_e2e.py` at the repository root runs the same services
as processes, without a browser, and checks what each framework call returned.
core-service runs the planner through a script that records each command, so
the test fails when core-service falls back to its Python renderer. Like
`--check`, it uses an empty models folder, so it runs only the fallback adapter.
It needs the planner and torch:

```bash
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  .venv/bin/python -m unittest tests.test_nl2sql_stack_e2e -v
```

`services/nl2sql-demo/tests` checks the backend with the framework calls
replaced by stubs, and needs neither.

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
