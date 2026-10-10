# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What Dagents is

Dagents is a reusable framework that other applications build on. It runs a
computation agent at each data source, combines their outputs through a global
agent, and produces workload plans that backends can deploy.

In short: compute at each source, combine the results centrally, make decisions
with typed planners, and deploy through services.

- **LMA** (local monitoring agent; the presentation material sometimes calls it
  CMA, constraint monitoring agent): one per data source (a tenant, database,
  service, event stream or environment). It profiles data, splits work, runs
  local models, publishes summaries, and enforces governance at three of the
  four guard boundaries.
- **GMA** (global monitoring agent): registers LMAs, combines their outputs,
  runs aggregate models across sources, coordinates dispatch, and runs federated
  rounds and release decisions.
- **Framework services** (core, pipeline, model): stable APIs, so applications
  call Dagents instead of rebuilding profiling, orchestration, model routing and
  manifest generation.
- **OCaml planners**: pure, typed compilers for validation, DAG planning, model
  routing, Kubernetes manifests, governance (ethical restrictions) and federated
  rounds.

Intended consumers: Watchdog, Datalytics, the NL2SQL demo app and the healthcare
stroke-triage demo app, both in this repository.

## The main architectural rule

Each language does the work it suits best:

| Layer | Owns | Never owns |
|---|---|---|
| **OCaml** (`bindings/ocaml/`) | Pure planning: validate, compile, route, render | Database connections, training loops, API servers |
| **Python** (`agents/`, `services/`) | FastAPI services, model training and inference, source I/O, runtime state | Deterministic planning rules |
| **Spring Boot** (`services/spring-services/`) | Orchestration APIs, policy entry points, external integration | Machine learning |

Decisions go in the OCaml planners. Anything with side effects goes in Python or
Java.

Services call OCaml by running `dagentsc` with JSON on standard input, never
through FFI. A separate process isolates failures, lets each side be upgraded on
its own, and keeps the Kubernetes deployment simple.

## Repository map

```text
agents/
  common/        shared domain contracts + dagents_runner (the dagentsc bridge)
  lma/           Local Monitoring Agent service
  gma/           Global Monitoring Agent service
  tests/         agent + control-plane + runner tests
bindings/ocaml/  the planners (dune workspace)
  lib/common_ir           shared typed IR + JSON codecs
  lib/dataset_compiler    source validation, profiling, schema contracts, quality, transforms
  lib/pipeline_compiler   DAG validation + topological ordering
  lib/model_router        dataset profile + task -> model family + packaging mode
  lib/manifest_compiler   typed workload spec -> Kubernetes YAML
  lib/governance_compiler GRAILS ethical restrictions: what protection a request needs
  lib/federation_compiler federated round manifests, eligibility, quorum, release gates
  bin/dagentsc.ml         the command-line program services call
contracts/grpc/  shared LMA/GMA protobuf contract
services/
  core-service/       catalog, topology, workload compilation, manifest generation
  pipeline-service/   pipeline registry, validation, async runs
  model-service/      training, checks, benchmark datasets, model jobs
  spring-services/    Spring Boot control + core services
  nl2sql-demo/        demo app that uses the framework's services
apps/
  healthcare-demo/    self-contained stroke-triage demo with its own README, tests and
                      compose file; scripts/extract_repo.sh turns it into its own repository
site/            the framework site: home page, the learning guide, the API reference
design/          the design system source, copied into each frontend by a script
tests/           checks that span more than one service
docs/
  learn/          the learning guide, rendered by the site (Markdown + Mermaid)
  agents/         LMA/GMA architecture
  architecture/   OCaml adoption plan, Python-vs-OCaml comparison
  demo/           demo scripts + recorded inputs and expected outputs
  presentation/   the project deck (.pptx), outline, talk track, PlantUML sources
  reports/        LaTeX reports and PDFs
env/             committed per-service env files (no secrets)
```

## Commands

### OCaml planners

`dune` is not on PATH, so always use `opam exec --`:

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test
```

The binary is written to `bindings/ocaml/_build/default/bin/dagentsc.exe`. Most
OCaml tests need no Docker, database or GPU, because the modules are pure. This
is the fastest test loop in the repo, so prefer it.

### Python tests

Tests import as `agents.common...`, so run them from the repository root.
FastAPI and the other dependencies are in the gitignored `.venv/`, not system
Python:

```bash
.venv/bin/python -m unittest discover -s agents/tests -t .
```

If `.venv/` is missing (a fresh clone), create it and install the requirements
you need. Each service has its own `requirements.txt`; there is no root-level
one.

Each service has its own suite under `services/<name>/tests/`. The NL2SQL suite
adds its backend to the path itself, so it also runs from the root.

`tests/` at the root holds checks that span more than one service, such as the
endpoint inventory and the API conventions. They need every Python service
importable, while `agents/tests` needs only the agent requirements:

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

`services/model-service/requirements-optional-models.txt` installs the Hugging
Face stack. Without it, the provider tests that need it skip. Install it to test
the real `local_files_only` path.

The governance and federation tests run against the real `dagentsc` binary, not
a stub, because a stub would only prove that the code calls something. They find
the dune build themselves and skip, with a message, when it is missing. Check
the skip count: a run that skipped them has not tested governance.

**Run the service suites with and without `DAGENTSC_BIN`.** `core-service`
renders manifests with the OCaml compiler when the binary is available and with
a Python fallback when it is not. Containers have `dagentsc` on PATH, so the
OCaml path is the one deployed. The two renderers once drifted apart unnoticed,
because the suite only tested the fallback:

```bash
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  PYTHONPATH=.:services/core-service .venv/bin/python \
  -m unittest discover -s services/core-service/tests -t services/core-service/tests
```

Any behaviour with two implementations needs the same assertions run against
both. If they cannot be kept in step, delete one.

The healthcare demo has its own suite:

```bash
cd apps/healthcare-demo && PYTHONPATH=../..:backend ../../.venv/bin/python -m unittest discover -s tests -t .
```

### CI

`.github/workflows/ci.yml` runs the same commands, one job per layer: `planners`
(builds OCaml, runs `dune test`, and uploads `dagentsc` for the other jobs),
`agents`, `framework-services` (core-service twice, with and without
`DAGENTSC_BIN`), `contracts`, `spring`, `demo-apps`, `frontends`, and a `ci`
gate job that is the single required check.

Two guard scripts catch runs that look green but are not:

```bash
python -m unittest discover -s agents/tests -t . -v 2>&1 | tee suite.log
python scripts/ci_no_skipped_planner_tests.py suite.log   # fails on a planner-related skip
.venv/bin/python scripts/service_inventory.py --check      # fails when a route is undocumented
```

Docker and Minikube are not in CI; see **Current state**.

### The published sites

`.github/workflows/pages.yml` publishes three trees to GitHub Pages: the
framework site at the root and the two demos below it. The addresses are in
`env/.env.published`:

| | |
|---|---|
| framework site | <https://pradyunuydarp.github.io/Dagents/> |
| stroke-triage demo | <https://pradyunuydarp.github.io/Dagents/healthcare-demo/> (calls the live API) |
| NL2SQL demo | <https://pradyunuydarp.github.io/Dagents/nl2sql-demo/> (replays a recording) |
| healthcare API | <https://dagents-healthcare-api.onrender.com> (Postgres-backed, planner in the image) |

NL2SQL has no deployed backend, so the workflow records a real one first:

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && cd -
.venv/bin/python scripts/capture_demo_recordings.py --demo all   # needs the planner
.venv/bin/python scripts/check_demo_recordings.py                # refuses a useless recording
cd site && npm install && npm run build && npm run check         # the site, checked in a browser
```

The capture script refuses to run without `dagentsc`. The Guard fails closed, so
a recording made without the planner would hold only denials. The checker
refuses a recording whose guard controls change nothing. Recordings are
gitignored; the workflow makes them.

The site's learning guide is `docs/learn/*.md`. `npm run check` visits every
guide page in Chromium and fails if a Mermaid diagram does not draw, an image
does not load, or a link between pages is broken.

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

`docs/demo/inputs/` holds request payloads and `docs/demo/expected/` the recorded
responses. Use them as fixtures when changing planner or service output.

The healthcare demo has its own scripts, which need no Docker and no database:

```bash
apps/healthcare-demo/scripts/run_frontend_demo.sh          # API + UI + a guided tour
apps/healthcare-demo/scripts/run_frontend_demo.sh --check  # start, smoke-test the UI, exit
apps/healthcare-demo/scripts/run_pilot.sh                  # no UI: one pilot, printed
apps/healthcare-demo/scripts/run_local_demo.sh             # the API alone on :8080
```

All of them need `dagentsc`. Without it the Ethical Guard denies every request,
which is correct but makes every panel a denial, so the scripts check first.

The frontend smoke test (`apps/healthcare-demo/frontend/smoke.mjs`) drives the
real UI against a live backend. It checks that each of the guard's three
controls changes the strategy, runs a full pilot, and fails on any console
error. A typecheck and a bundle only prove that the app compiles. The smoke test
exits 2 and prints `SKIP` when no browser is available; report that as skipped,
not passed.

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
| framework site (dev) | 5175 |

Configuration comes from `env/.env.shared` plus a file per service. Never
hardcode URLs or ports. Each service has a `*_PUBLIC_URL` (host) and a
`*_INTERNAL_URL` (compose network).

## Conventions

### Agent layout

`agents/lma/` and `agents/gma/` use the same layout, and new agents should too:

```text
domain/          typed models, no I/O
application/     use cases
adapters/        code for specific technologies
infrastructure/  messaging, storage, concrete implementations
config.py di.py main.py    wiring
```

Keep `domain/` pure. Put infrastructure behind interfaces, so in-memory delivery
can be replaced by a message broker and a database without restructuring.

### Calling the OCaml layer from Python

Use `agents/common/infrastructure/dagents_runner.py`. It converts keys between
snake_case and camelCase and runs the subprocess. The binary comes from
`DAGENTSC_BIN`, or from `dagentsc` on PATH (containers install it there; local
runs point at the dune build).

### Adding to the OCaml layer

1. Add the type to `common_ir` first, then its JSON parsing, then the compiler
   module, then tests.
2. Model choices as algebraic data types, not string comparisons. Exhaustive
   matching is the reason this layer is in OCaml.
3. Return reports and plans instead of raising. Keep exceptions for requests
   that cannot produce a meaningful plan.
4. Test at the compiler boundary: invalid inputs, the shape of the output, and
   deterministic ordering.

### LMA/GMA route pairs

Both agents expose short paths (`/health`, `/datasets/profile`, `/models/run`)
and versioned ones (`/api/v1/health`, `/api/v1/datasets:profile`,
`/api/v1/model-jobs`). This is deliberate. When adding an endpoint, add both
forms and make them call the same handler.

The framework services (`core`, `pipeline`, `model`) use only `/api/v1/...`.

### The service inventory

`docs/reference/service-inventory.{json,md}` lists every endpoint the Python and
Java services expose. It is generated from the code (the FastAPI routing tables
and the Spring controllers), so any route change means regenerating it:

```bash
.venv/bin/python scripts/service_inventory.py --write   # then commit both files
```

`tests/test_service_inventory.py` fails when the files are out of date. It also
checks the conventions above: the framework services use only versioned paths,
the LMA/GMA pairs exist and return the same result, and the Spring services
mirror their Python counterparts.

### The model inventory

The OCaml `model_router` decides which model family a dataset and task should
get. Because it is pure, it can choose families this runtime cannot run.
`services/model-service/app/ml/inventory.py` records which `(family, task)`
pairs are `implemented`, which provider runs each (`pytorch`, `scikit_learn`,
`huggingface` or `extension`), and which are `planned`, with what they would
need. `GET /api/v1/model-families` serves it.

Rules:

- **Every family the planner can choose needs an entry.** `planned` is a valid
  entry; a missing one is not. The vocabulary test reads the OCaml
  `string_of_model_family` mapping from source, so it needs no `dagentsc` build.
- **`inventory.py` imports nothing heavy.** Entry points are `module:attribute`
  strings resolved when used, so listing families does not import torch. A test
  checks this.
- **Providers share loading code only.** Adapters in `app/ml/providers/` all
  implement `load()` and `is_loaded`. An anomaly scorer, a classifier and an
  embedding model have different `predict` signatures, so there is no shared
  `predict`.
- **Downloads are off by default.** The Hugging Face adapter refuses a cache miss
  with a typed error naming the checkpoint. `DAGENTS_ALLOW_MODEL_DOWNLOAD=1` (or
  `allow_download=True`) allows the download. Never make a test need the Hub.
- Do not add a second list of family names. `checks.py` and `pipeline.py` read
  the inventory, so there is one source of truth.

### The design system

`design/dagents-design-system.css` is the single source.
`scripts/sync_design_system.py --write` copies it into each frontend, and
`tests/test_publishing_guards.py` fails if a copy differs. The copies exist
because `apps/healthcare-demo/` must stay extractable and cannot import across
the tree. Edit the source, never a copy.

The style is an instrument panel. The most important rule: **colour carries
meaning only.** Colours mark the decision states (permit, narrow, deny) and
links. A decorative colour could be mistaken for a decision, so there is no
accent colour; hierarchy comes from type, weight and thin rules.

### Published demos replay a real run or call a real API

Both demo frontends have a transport in `src/api.ts`. It calls the backend
locally, and when built with `VITE_DAGENTS_STATIC=1` it replays
`public/recording.json`. The healthcare demo has a third mode: built with
`VITE_HEALTHCARE_API_BASE`, it calls the deployed API, which reads its cohorts
from the Supabase encounter store. The Pages workflow takes that address from
`env/.env.published` (committed, because it is a public URL, not a secret) or
from a `HEALTHCARE_API_BASE` repository variable. Rules:

- **Never fake a response.** A request the recording does not hold raises
  `NotRecordedError`, and the UI says so.
- **Never record without the planner.** The Guard fails closed, so the recording
  would hold only denials. The capture script and the checker both refuse.
- **A live build never falls back to the recording**, even when the API is
  down; it reports the outage. The workflow chooses once, at build time: it
  probes the API, publishes the replay if the API does not answer, and logs why.
- **The page shows the data source the backend reports**
  (`/api/v1/framework/status`), so a reader can tell stored records from
  generated ones.
- The page names the commit it was recorded from.

### Governance: where each part lives

Governance follows the same split as the rest of the code:

- **Deciding** is pure, so it is in `bindings/ocaml/lib/governance_compiler`.
  Sensitivity, trust, granularity and strategy are algebraic data types, and
  `select_strategy` matches every combination. Adding a level fails to compile
  until every combination is handled.
- **Enforcing** needs real data and an audit log, so it is in
  `agents/common/application/ethical_guard.py`. The Guard applies the plan,
  drops every field the request did not ask for, and writes an audit record that
  includes the digest of the previous record.
- **Policy** is configuration: a `DataClassification` contributed by an
  extension or registered at runtime. Changing a field's sensitivity changes no
  code path.

The Guard **fails closed**. If the planner cannot be reached, it denies the
request and records the denial. Never add a fallback that permits a request when
the planner fails.

A site's local runner receives the guarded payload, not its raw records, even
though the data never leaves the site. The code that runs a round comes from the
coordinator, so it sees only what the coordinator may see. This costs some
accuracy, which the healthcare demo measures. A deployment that finds the cost
too high should change the field's sensitivity in its classification, not this
code.

### Federated rounds

`bindings/ocaml/lib/federation_compiler` makes every deterministic decision:
eligibility, quorum, aggregation readiness and release gates.
`agents/common/application/federation.py` holds state and side effects and calls
the planner for the rest. Do not write a planning rule again in Python.

The federated protocol belongs to a dedicated runtime behind `FederationEngine`.
The in-process engine is a simulator for tests and demos; do not turn it into a
federated optimizer.

Two rules always hold, and both have tests: aggregation produces a candidate,
never a release; and a release gate whose metric is missing blocks the release.

### Extending the framework from an application

An application contributes through `agents/common/extensions`, never by patching
the framework. An extension supplies feature contracts, data classifications,
condition packs, named pipeline steps and named model adapters. Registration is
explicit, and two extensions registering the same id is an error, because
otherwise a guard decision would depend on import order.

`apps/healthcare-demo/backend/app/extension.py` is the reference: about eighty
declarative lines that import no framework internals. If a new application needs
something that does not fit this interface, improve the interface instead of
working around it.

### Scope

Product-specific code belongs in Dagents only if several consumers can reuse it.
The two demos show the boundary:

- NL2SQL: the app owns its UI and SQL generation; Dagents owns validation,
  planning, service checks and workload compilation. See
  `services/nl2sql-demo/backend/app/services/dagents_orchestrator.py` for how an
  app calls the services.
- Healthcare: the app owns its clinical feature contract, scoring rule, FHIR
  mapping and intended-use statement; Dagents owns governance, federation and
  everything generic. See `apps/healthcare-demo/backend/app/extension.py` for
  the extension.

Nothing in `agents/` knows what a stroke is, and nothing in the healthcare app
implements a quorum rule. Apply that test to a new capability: if it would need a
domain word in `agents/`, it belongs in the application.

## Writing style

All text in this repository uses easy, professional English: the site, the demo
UIs, READMEs and docs, the learning guide, code comments, commit messages and
pull request text.

- Short sentences and plain words. Define a term the first time you use it.
- Facts and numbers instead of adjectives. Check each claim against the code or
  a measured result.
- Descriptive headings, and lists or tables for steps and comparisons.
- State a limitation once, plainly, where it applies.
- No slogans ("A framework, not a product backend"), aphorisms, dramatic
  metaphors, or words such as "honest", "crucially" and "simply".

The `writing` skill has the full rules, with examples.

## Current state

The agents deliver messages in memory. Health and control endpoints come first;
messaging and storage sit behind interfaces, and broker-backed infrastructure is
deferred.

Open work is in `TODO.md`. The main item is live Kubernetes validation on
Minikube, which is blocked on local Docker Desktop disk space (see `CHANGES.md`).
`generate_manifests_local.py` produces `dagents-workloads.yaml` without starting
the whole stack.

## Repository skills

`.claude/skills/` holds four repository skills. They are binding where they say
**never**:

- `backend`: the layer rule, where each kind of decision may live, the
  governance and federation rules, and the test loop for each layer.
- `frontend`: what a UI may own, the design system, the framework site and its
  guide, and why a typecheck and a bundle are not enough (and exit 2 is not a pass).
- `ci`: the pipeline's shape, the false greens it defends against, and the rules
  for changing a workflow.
- `writing`: plain-language rules for every text in the repository.

They are first-party repository skills; no third-party marketplace plugin is
installed.

## Reference docs

- `docs/learn/` — the learning guide: federated learning, governance, the
  architecture, the APIs, and running Dagents yourself
- `AGENTS.md` — the long contributor guide, with the most detail on boundaries
- `docs/reference/service-inventory.md` — every endpoint the Java and Python services expose
- `.claude/skills/{backend,frontend,ci,writing}/SKILL.md` — the rules for each area
- `docs/agents/lma-gma-architecture.md` — agent responsibilities and run flow
- `docs/architecture/ocaml-adoption-plan.md` — why OCaml, where it is used, what stays out
- `bindings/ocaml/README.md` — module map, CLI commands, contract examples
- `docs/demo/app-architecture-walkthrough.md` and `functional-modules-walkthrough.md`
- `docs/presentation/dagents-project-presentation.pptx` — the project deck
- `docs/presentation/healthcare-case-study/` — the stroke case study and the federated use
  case, including the GRAILS sections the governance layer implements
- `apps/healthcare-demo/README.md` — the demo app, its boundary table, and its limits
