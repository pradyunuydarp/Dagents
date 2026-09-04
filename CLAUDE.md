# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What Dagents Is

Dagents is a **reusable framework**, not a product backend. It deploys a computation agent
per data source, combines their outputs through a global agent, and produces workload plans
that real backends can deploy.

The one-line model: **compute locally, combine globally, plan with typed functional modules,
deploy through services.**

- **LMA** (Local Monitoring Agent, sometimes called CMA / Constraint Monitoring Agent in the
  presentation material) — one per source boundary (tenant, database, service, event stream,
  environment). Profiles data, partitions work, runs source-level models, publishes summaries.
- **GMA** (Global Monitoring Agent) — registers LMAs, assimilates their outputs, runs aggregate
  models across sources, coordinates dispatch.
- **Framework services** — core / pipeline / model, exposing stable APIs so consumer backends
  call Dagents instead of rebuilding profiling, orchestration, routing, and manifest generation.
- **OCaml planners** — pure, typed compilers for validation, DAG planning, model routing, and
  Kubernetes manifest rendering.

Intended consumers: Watchdog, Datalytics, and the in-repo NL2SQL demo app.

## The Central Architectural Rule

**Polyglot by layer. Each language owns what it is best at.**

| Layer | Owns | Never owns |
|---|---|---|
| **OCaml** (`bindings/ocaml/`) | Pure planning: validate, compile, route, render | DB sockets, training loops, API servers |
| **Python** (`agents/`, `services/`) | FastAPI surfaces, ML training/inference, source I/O, runtime state | Deterministic planning rules |
| **Spring Boot** (`services/spring-services/`) | Orchestration APIs, policy entrypoints, external integration | ML execution |

Dagents is neither OCaml-first nor Python-first. It is **planner-first where planning matters,
runtime-first where side effects matter.**

Services call OCaml through a **JSON subprocess boundary** (`dagentsc`), never FFI. This keeps
failure isolation, upgrade independence, and simple Kubernetes deployment.

## Repository Map

```text
agents/
  common/        shared domain contracts + dagents_runner (the dagentsc bridge)
  lma/           Local Monitoring Agent service
  gma/           Global Monitoring Agent service
  tests/         agent + control-plane + runner tests
bindings/ocaml/  the functional planning layer (dune workspace)
  lib/common_ir           shared typed IR + JSON codecs
  lib/dataset_compiler    source validation, profiling, schema contracts, quality, transforms
  lib/pipeline_compiler   DAG validation + topological ordering
  lib/model_router        dataset profile + task -> model family + packaging mode
  lib/manifest_compiler   typed workload spec -> Kubernetes YAML
  bin/dagentsc.ml         CLI entrypoint services shell out to
contracts/grpc/  shared LMA/GMA protobuf contract
services/
  core-service/       catalog, topology, workload compilation, manifest generation
  pipeline-service/   pipeline registry, validation, async runs
  model-service/      training, checks, benchmark datasets, model jobs
  spring-services/    Spring Boot control + core services
  nl2sql-demo/        demo app proving a real app can consume the framework
docs/
  agents/         LMA/GMA architecture
  architecture/   OCaml adoption plan, Python-vs-OCaml comparison
  demo/           runnable demo scripts + recorded inputs/expected outputs
  presentation/   the project deck (.pptx), outline, talk track, PlantUML sources
  reports/        LaTeX reports and PDFs
env/             committed per-service env files (no secrets)
```

## Commands

### OCaml functional layer

`dune` is not on PATH — always go through `opam exec --`.

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test
```

The built binary lands at `bindings/ocaml/_build/default/bin/dagentsc.exe`. Most OCaml tests
need no Docker, database, or GPU, because the modules are pure planners — this is the fastest
feedback loop in the repo, so prefer it.

### Python tests

Tests import as `agents.common...`, so run from the repo root. FastAPI and friends live in the
gitignored `.venv/`, not system Python:

```bash
.venv/bin/python -m unittest discover -s agents/tests -t .
```

If `.venv/` is missing (fresh clone), create it and install the service requirements you need —
each service has its own `requirements.txt`; there is no root-level one.

Per-service suites live under `services/<name>/tests/`. The NL2SQL suite inserts its own
backend path, so it runs from the root too.

### Full stack

```bash
docker compose --env-file env/.env.compose up --build
```

### Demos

```bash
bash docs/demo/run_functional_layer_demo.sh   # OCaml planner output
bash docs/demo/run_full_stack_demo.sh         # whole stack via compose
bash docs/demo/run_demo_quick.sh              # fallback: build + tests + a few planner calls
services/nl2sql-demo/scripts/run_local_demo.sh  # NL2SQL app, no Docker
```

`docs/demo/inputs/` holds request payloads and `docs/demo/expected/` the recorded responses —
useful as fixtures when changing planner or service output shapes.

### Service ports

| Service | Port |
|---|---|
| model-service | 8000 |
| lma | 8010 |
| gma | 8020 |
| pipeline-service | 8030 |
| core-service | 8040 |
| spring-control-service | 8050 |
| spring-core-service | 8060 |
| nl2sql-demo backend | 8070 |
| nl2sql-demo frontend | 5173 |

Config comes from `env/.env.shared` plus a per-service file. Never hardcode URLs or ports —
they are env-driven by design, with distinct `*_PUBLIC_URL` (host) and `*_INTERNAL_URL`
(compose network) values.

## Conventions

### Agent layering

Both `agents/lma/` and `agents/gma/` use the same shape, and new agents should too:

```text
domain/          pure typed models, no I/O
application/     orchestration and use-cases
adapters/        technology-facing boundaries
infrastructure/  messaging, persistence, concrete implementations
config.py di.py main.py    composition root
```

Keep `domain/` pure. Push infrastructure behind interfaces so the repo can move from in-memory
delivery to broker-backed and persisted deployments without a structural rewrite.

### Calling the OCaml layer from Python

Go through `agents/common/infrastructure/dagents_runner.py`. It handles snake_case ↔ camelCase
key conversion and the subprocess plumbing. The binary is resolved from `DAGENTSC_BIN`, falling
back to `dagentsc` on PATH (containers install it there; local runs point at the dune build).

### Adding to the OCaml layer

1. Add the type to `common_ir` **first**, then JSON parsing, then the compiler module, then tests.
2. Model choices as algebraic data types, not string conditionals — exhaustive matching is the
   whole point of putting this layer in OCaml.
3. Return reports and plans rather than raising; reserve exceptions for compile requests that
   cannot produce a meaningful plan.
4. Test at the compiler boundary: invalid inputs, normalized output contracts, deterministic ordering.

### LMA/GMA route duplication

Both agents expose legacy short paths (`/health`, `/datasets/profile`, `/models/run`) *and*
versioned equivalents (`/api/v1/health`, `/api/v1/datasets:profile`, `/api/v1/model-jobs`).
This is deliberate. When adding an endpoint, add both forms and keep them delegating to the
same handler.

The framework services (`core`, `pipeline`, `model`) are uniformly `/api/v1/...`.

### Scope discipline

Product-specific logic does not belong in Dagents unless it is genuinely reusable across
consumers. NL2SQL is the reference for the boundary: the app owns its UI and SQL generation;
Dagents owns validation, planning, service checks, and workload compilation. Read
`services/nl2sql-demo/backend/app/services/dagents_orchestrator.py` to see the intended
integration shape before wiring a new consumer.

## Current State

In-memory delivery across the agent layer — health and control endpoints first, messaging and
persistence behind interfaces, broker-backed infrastructure deferred.

Known open work is tracked in `TODO.md`; the main item is live Kubernetes validation on
Minikube, which is currently blocked on local Docker Desktop disk capacity (see `CHANGES.md`
for the full findings). `generate_manifests_local.py` exists as a workaround that produces
`dagents-workloads.yaml` without booting the whole stack.

## Reference Docs

- `AGENTS.md` — the long-form contributor guide; the deepest single source on boundaries
- `docs/agents/lma-gma-architecture.md` — agent responsibilities and run flow
- `docs/architecture/ocaml-adoption-plan.md` — why OCaml, where it goes, what stays out
- `bindings/ocaml/README.md` — module map, CLI surface, contract examples
- `docs/demo/app-architecture-walkthrough.md` and `functional-modules-walkthrough.md`
- `docs/presentation/dagents-project-presentation.pptx` — the framing used for the project deck
