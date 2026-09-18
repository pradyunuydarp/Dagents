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
  environment). Profiles data, partitions work, runs source-level models, publishes summaries,
  and enforces governance at three of the four guard boundaries.
- **GMA** (Global Monitoring Agent) — registers LMAs, assimilates their outputs, runs aggregate
  models across sources, coordinates dispatch, and owns federated round control and release
  governance.
- **Framework services** — core / pipeline / model, exposing stable APIs so consumer backends
  call Dagents instead of rebuilding profiling, orchestration, routing, and manifest generation.
- **OCaml planners** — pure, typed compilers for validation, DAG planning, model routing,
  Kubernetes manifest rendering, ethical-restriction planning, and federated round governance.

Intended consumers: Watchdog, Datalytics, the in-repo NL2SQL demo app, and the healthcare
stroke-triage demo app.

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
  lib/governance_compiler GRAILS Ethical-Restriction Rails: what protection a request needs
  lib/federation_compiler federated round manifests, eligibility, quorum, release gates
  bin/dagentsc.ml         CLI entrypoint services shell out to
contracts/grpc/  shared LMA/GMA protobuf contract
services/
  core-service/       catalog, topology, workload compilation, manifest generation
  pipeline-service/   pipeline registry, validation, async runs
  model-service/      training, checks, benchmark datasets, model jobs
  spring-services/    Spring Boot control + core services
  nl2sql-demo/        demo app proving a real app can consume the framework
apps/
  healthcare-demo/    self-contained stroke-triage demo; its own README, tests, and compose,
                      extractable into a standalone repo via scripts/extract_repo.sh
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

The governance and federation tests run against the real `dagentsc` binary rather than a stub —
stubbing the planner would prove the code calls something, not that the governance holds. They
find the dune build automatically and skip with a message if it is missing, so check the skip
count: a suite that silently skips its governance tests is green without having proved anything.

**Run the service suites both with and without `DAGENTSC_BIN`.** `core-service` compiles manifests
through the OCaml compiler when the binary is reachable and through a Python fallback when it is
not, and containers put `dagentsc` on PATH — so the OCaml path is the deployed one. The two
renderers had silently diverged, and nobody noticed because the suite only ever exercised the
fallback:

```bash
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  PYTHONPATH=.:services/core-service .venv/bin/python \
  -m unittest discover -s services/core-service/tests -t services/core-service/tests
```

Any behaviour with two implementations needs the same assertions run against both. If they cannot
be kept in step, delete one.

The healthcare demo has its own suite:

```bash
cd apps/healthcare-demo && PYTHONPATH=../..:backend ../../.venv/bin/python -m unittest discover -s tests -t .
```

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

The healthcare demo has its own entrypoints, which need no Docker and no database:

```bash
apps/healthcare-demo/scripts/run_pilot.sh        # one governed federated pilot, printed
apps/healthcare-demo/scripts/run_local_demo.sh   # the API on :8080
```

Both need `dagentsc` built. Without it the Ethical Guard denies every request — correct behaviour,
but nothing useful runs, so the scripts check and say so.

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
| healthcare-demo backend | 8080 |
| healthcare-demo frontend | 5174 |

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

### Governance: where a decision lives

Governance follows the same split as everything else, and the split is the whole design.

- **Deciding** is pure, so it lives in `bindings/ocaml/lib/governance_compiler`. Sensitivity,
  trust, granularity, and strategy are algebraic data types, and `select_strategy` matches on the
  full triple exhaustively. Adding a level fails to compile until every combination is handled.
- **Enforcing** needs real data and an audit sink, so it lives in
  `agents/common/application/ethical_guard.py`. The Guard applies the plan, projects away anything
  the request did not ask for, and writes a digest-chained audit record.
- **Policy** is configuration, not code: a `DataClassification` contributed by an extension or
  registered at runtime. Changing a field's sensitivity changes no code path.

The Guard **fails closed**. If the planner cannot be reached it denies the request and records the
denial. Do not add a fallback that permits on planner failure; an enforcement layer whose absence
grants access is not one.

A site's local runner is handed the guarded payload, not its raw records, even though that data
never leaves the site. The code running a round is the coordinator's, so what it observes is what
the coordinator observes. This costs measurable accuracy — the healthcare demo quantifies it — and
the lever for a deployment that finds the cost too high is the field's sensitivity in its
classification, not this code path.

### Federated rounds

`bindings/ocaml/lib/federation_compiler` owns every deterministic decision: eligibility, quorum,
aggregation readiness, release gates. `agents/common/application/federation.py` owns state and
side effects and delegates the rest — do not re-derive a planning rule in Python.

The federated protocol itself belongs to a specialist runtime behind `FederationEngine`. The
in-process engine is a simulator for tests and demos; do not grow it into a federated optimizer.

Two invariants hold throughout, and both have tests: aggregation produces a candidate and never a
release, and a release gate whose metric is absent blocks rather than passing.

### Extending the framework from a consumer app

A consumer contributes through `agents/common/extensions`, never by patching the framework. An
extension supplies feature contracts, data classifications, condition packs, named pipeline steps,
and named model adapters. Registration is explicit and conflicts are errors: two extensions
claiming one id would make a guard decision depend on import order.

`apps/healthcare-demo/backend/app/extension.py` is the reference — about eighty declarative lines,
importing no framework internal. If a new consumer needs something that will not fit through this
interface, that is a signal about the interface, not a reason to reach around it.

### Scope discipline

Product-specific logic does not belong in Dagents unless it is genuinely reusable across
consumers. Two demos mark the boundary from different directions:

- NL2SQL: the app owns its UI and SQL generation; Dagents owns validation, planning, service
  checks, and workload compilation. Read
  `services/nl2sql-demo/backend/app/services/dagents_orchestrator.py` for the service-integration
  shape.
- Healthcare: the app owns its clinical feature contract, scoring rule, FHIR mapping, and
  intended-use statement; Dagents owns governance, federation, and everything generic. Read
  `apps/healthcare-demo/backend/app/extension.py` for the extension shape.

Nothing in `agents/` knows what a stroke is, and nothing in the healthcare app re-implements a
quorum rule. That is the test to apply to a new capability: if it would need a domain word in
`agents/`, it belongs in the consumer.

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
- `docs/presentation/healthcare-case-study/` — the stroke case study and the federated use case,
  including the GRAILS sections the governance layer implements
- `apps/healthcare-demo/README.md` — the demo app, its boundary table, and its honest limits
