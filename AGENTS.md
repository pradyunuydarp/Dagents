# Dagents Agent Guide

## Purpose

Dagents is a shared agentic framework extracted from product repositories such as Watchdog and intended for reuse across future systems such as Datalytics. Its core focus is ML automation, workload generation, reusable orchestration patterns, and shared contracts between local and aggregate agents.

This repository is the generic home for:

- source-level and assimilated-data model agents
- reusable ML orchestration patterns
- shared control-plane contracts
- reusable pipeline execution services
- data-analysis and model-training utilities that should not live inside a single product codebase
- parameterized Kubernetes manifest and Docker Compose file generation for framework workloads

## Repository Scope

Top-level areas:

```text
dagents/
apps/
bindings/
contracts/
docs/
services/
```

Current scope by area:

- `dagents/`: reusable `lma` and `gma` ML-orchestration services with layered application boundaries; the
  Python package `dagents` (`pyproject.toml`, built with hatchling), released to PyPI from a tag
- `dagents/common/extensions/`: the interface consumer apps contribute domain semantics through
- `apps/healthcare-demo/`: a self-contained consumer app, extractable into its own repository
- `bindings/ocaml/`: the typed planning layer, including governance and federation compilers
- `services/core-service/`: framework service catalog, topology endpoint, and Kubernetes manifest and Docker Compose file generation
- `services/model-service/`: generic anomaly-model training and inference service
- `services/pipeline-service/`: reusable JSON pipeline orchestration and ML workflow service
- `contracts/grpc/dagents/agents/v1/`: shared control-plane protobuf contracts
- `docs/agents/`: architecture notes for the LMA/GMA control plane

## Agent Model

Dagents defines a reusable ML orchestration layer built around:

- `LMA`: Local Monitoring Agent
- `GMA`: Global Monitoring Agent

The LMA is the local execution boundary. The GMA is the aggregation and coordination boundary. This layer complements product-specific services rather than replacing them.

### LMA Responsibilities

An LMA operates near a local source scope such as a tenant, service cluster, application boundary, environment, or event stream.

Responsibilities:

- extract and profile source-scoped datasets
- partition source data for large-scale model execution
- run source-level models for anomaly detection, classification, or forecasting
- invoke supporting product services or adapters
- publish model outputs, summaries, and artifact pointers to GMA or downstream systems

Current implementation includes:

- health endpoint
- bundle deployment endpoint
- run trigger endpoint
- source dataset profiling endpoint
- source model execution endpoint
- local bundle, model-run, and run inspection endpoints
- the Guard (`restrictions:plan`, `restrictions:enforce`) and its audit log
- for an LMA started for one site (`LMA_SITE_ID`, `LMA_CONDITION_ID`): the site's
  description and summary (counts, and a label rate only when the site's
  planner permits it), its running profile, the Guard probe, the audit chain
  summary, the model in use, and the federated job routes (registration, offer,
  execute, job log). A site's LMA reads records only from its configured source
  and refuses request-supplied databases, roles, credentials and SQL

`dagents/lma/README.md` lists the settings and routes.

### GMA Responsibilities

The GMA is the fleet-wide aggregate execution and coordination agent.

Responsibilities:

- register and track deployed LMA capabilities
- assimilate source-level outputs and shared datasets
- profile assimilated datasets
- run aggregate models across multi-source or tenant-level data
- coordinate aggregate workloads and downstream automation
- correlate cross-source incidents and patterns
- prepare actionable outputs for downstream orchestration or case-management systems

Current implementation includes:

- registration endpoint
- deployment planning and sync endpoints
- assimilated dataset profiling endpoint
- aggregate model execution endpoint
- run dispatch tracking
- in-memory storage of agent registrations and model-run history
- federated sites and rounds: enrolment (also from each LMA's own registration),
  round planning and dispatch, results, readiness, aggregation, discard and
  release evaluation; with `GMA_ENGINE=http`, jobs go to the LMAs over their
  HTTP API and training updates go to a write-only update store
- site enrolments, rounds and the update store in memory, or in Postgres with
  `GMA_ENROLMENT_TABLE`, `GMA_ROUND_TABLE` and the opt-in `GMA_UPDATE_TABLE`
  (with `GMA_ENGINE=http`, `GMA_UPDATE_ROLE` and `GMA_UPDATE_MAX_AGE_SECONDS`), or in stores the
  application passes in `GmaDeps` (`enrolments`, `rounds`, `updates`)
- recovery after a restart: the GMA asks the planner about each unfinished
  round once, in its lifespan or at the first round write, and the round
  writes answer `503` `recovery_failed` while it fails; before each dispatch
  it asks the same rule about rounds with stored updates older than the age
  limit, which applies to an update store that survives a restart or when
  `GMA_UPDATE_MAX_AGE_SECONDS` is set
- retries after a store error: a retried aggregate gives the candidate the
  first attempt registered, and a retried release evaluation saves on the
  round the decision the version already holds, through the planner's
  stored-decision rule (`decision_differs` when it would differ)
- a model registry (`/model-versions`): registration, the released model with
  an `ETag`, and approval, with every status change decided by the planner's
  lifecycle rule; in memory or in Postgres

`FederatedRoundController.recover` closes the rounds a restart left unfinished,
as the planner's `federation round recover` rule decides, for a round store
that outlives the process. A GMA calls it itself; recovery assumes one GMA
process with one uvicorn worker.

`dagents/gma/README.md` lists the settings and routes.

### Starting an agent

- From PyPI: `pip install "dagents[lma,gma]"`, then `dagents-lma` (port 8010) and `dagents-gma`
  (port 8020). The wheels for Linux x86_64, Linux aarch64 and macOS arm64 include the planner;
  elsewhere `DAGENTSC_BIN` or `dagentsc` on `PATH` must provide one. `python -m dagents` prints
  the planner the runner found. An installed package reads its settings only from the process
  environment, never from `env/`.
- From a clone: build the planner, set `DAGENTSC_BIN`, and run
  `PYTHONPATH=. uvicorn dagents.lma.main:app --port 8010` or `dagents.gma.main:app --port 8020`.
  The agents then also read `env/.env.shared` and their own env file.

`docs/guide/00-install.md` covers the package route step by step.

## Shared Run Flow

The intended operating flow across the framework is:

1. GMA registers an LMA and records its scope and capabilities.
2. LMA receives or syncs a bundle or config revision.
3. LMA executes a local run over its source scope.
4. LMA optionally calls local product services.
5. LMA pushes source-level outputs to GMA or downstream storage.
6. GMA runs aggregate models over assimilated datasets.
7. Core service generates Kubernetes manifests or Docker Compose files for the required workloads.
8. Downstream orchestration systems apply manifests and consume model outcomes.

A federated round follows its own flow: the GMA plans the round with the
planner, sends each selected LMA its job in one `jobs:run` call (the LMA
checks the job and runs it when it accepts; `jobs:offer` and `jobs:execute`
take the same steps in two calls), collects the results and any clipped
training updates,
asks the planner whether the results may be combined, and makes a candidate.
The release Guard, the release gates and a person's approval decide whether
the candidate is released. `docs/agents/lma-gma-architecture.md` shows the
calls.

## Layering Conventions

Agent scaffolds under `dagents/` follow the same layered shape:

- `domain/`: pure typed models
- `application/`: orchestration and use-cases
- `adapters/`: technology-facing boundaries and contracts
- `infrastructure/`: messaging, persistence, and concrete implementations
- `config.py`, `di.py`, `main.py`: composition root

This layout is intended to keep the framework portable across products while preserving clear separation between pure domain logic, orchestration, and infrastructure.

Repository shape for the agent layer:

```text
dagents/
├── gma/
│   ├── domain/
│   ├── application/
│   ├── adapters/
│   └── infrastructure/
└── lma/
    ├── domain/
    ├── application/
    ├── adapters/
    └── infrastructure/
```

## Service Boundaries

### Core Service

Purpose:

- health checks
- service discovery metadata
- runtime topology inspection
- parameterized Kubernetes manifest generation for framework workloads
- Docker Compose files for workloads that join a project Docker Compose already runs

Current API:

- `GET /api/v1/health`
- `GET /api/v1/services`
- `GET /api/v1/topology`
- `POST /api/v1/manifests/pods`
- `POST /api/v1/manifests/compose`
- `POST /api/v1/workloads:compile`
- `GET /api/v1/workload-plans/{plan_id}`

### Model Service

Purpose:

- benchmark dataset loading
- preprocessing and PCA
- unified train/validation/test orchestration
- cross-validation and leave-one-out evaluation modes
- PyTorch model training
- hyperparameter search
- artifact persistence

Current focus is tabular anomaly detection using:

- `autoencoder`
- `variational_autoencoder`

Current API:

- `GET /api/v1/health`
- `GET /api/v1/datasets`
- `POST /api/v1/train`

Supported benchmark datasets:

- `kddcup99_http`
- `creditcard_openml`
- `mammography_openml`

### Pipeline Service

Purpose:

- register named pipelines
- execute stored pipelines against JSON payloads
- inspect recent runs
- apply data-analysis and ML steps such as filtering, projection, profiling, summarization, and model execution

Built-in step kinds:

- `enrich_context`
- `filter_items`
- `summarize_items`
- `project_fields`
- `profile_dataset`
- `run_model_job`

Current API:

- `GET /api/v1/health`
- `GET /api/v1/pipelines`
- `POST /api/v1/pipelines`
- `POST /api/v1/pipelines/{pipeline_id}/runs`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`

## Contracts

The shared control-plane contract centers on:

- dataset profiling
- model execution
- deployment planning
- workload generation

The current documented protobuf location is:

- `contracts/grpc/dagents/agents/v1/lma_gma.proto`

The broader shared contract strategy should normalize types such as:

- `DatasetProfile`
- `ModelExecutionRequest`
- `ModelPlan`
- `WorkloadSpec`
- `KubernetesManifestBundle`

## Current Delivery State

Implemented framework capabilities currently include:

- LMA source dataset profiling and source-level model execution
- GMA assimilated dataset profiling and aggregate model execution
- pipeline-level dataset profiling and model job steps
- core-service Kubernetes manifest and Docker Compose file generation for workload deployment
- GRAILS-style governance: the Ethical-Restriction Rails as a typed OCaml planner, and the Ethical
  Guard enforcing its decisions in the LMA and GMA request paths with a digest-chained audit log
- federated round control: round manifests with a digest, site eligibility, quorum, aggregation
  readiness, release gates, model version status, and whether a round that a restart left
  unfinished is kept or closed, with an adapter seam for a specialist federated runtime
- a consumer extension registry, so an app contributes feature contracts, data classifications,
  condition packs, model kinds, site sources, pipeline steps, and model adapters without forking
  the framework
- calls between agents through one client (`dagents/common/infrastructure/agent_client.py`), in
  one process through `httpx.ASGITransport` or over HTTP, with bearer tokens of fixed scopes
  (`agent_auth.py`: LMA `site` and `coordinator`, GMA `admin` and a per-site `site` token) and
  typed `{detail, code}` errors that never hold a host, a token or a connection string
- separate Docker images for `lma`, `gma`, `core-service`, `model-service`, and `pipeline-service`;
  every image whose service calls the planner contains `dagentsc`, and
  `tests/test_planner_in_images.py` checks the Dockerfiles for it
- top-level `docker-compose.yml` for multi-container local or cloud-like deployment
- role-scoped Postgres access (`dagents/common/infrastructure/postgres_roles.py`): every operation
  runs in one transaction that starts with `SET LOCAL ROLE` and checks `current_user`, and a
  Postgres audit sink keeps each site's digest chain in its own table
- vector-aware contribution controls in the Ethical Guard: `clip_contribution` and `add_noise`
  bound a model update given as a list or a map, and the audit record keeps the norm before and
  after; a federated worker sends a site's update through the `before_send` Guard to an
  `UpdateChannel` and puts only its digest and norm in the `SiteResult`
- incremental per-field statistics (`RunningProfile`), so a site updates its summary with each new
  record instead of recomputing it
- a server-side SVG chart renderer (`dagents/common/infrastructure/charts.py`, matplotlib) that
  uses the design system's tokens and returns a table view with every chart
- a published site at <https://pradyunuydarp.github.io/Dagents/> carrying the documentation, the
  generated API reference, and both demos. The stroke demo calls a deployed healthcare API that
  keeps each of three hospitals' International Stroke Trial records in its own Postgres schema,
  runs one LMA per hospital and a GMA in its own process, and has the planner in the image; the
  NL2SQL demo replays a capture, because it has no deployed backend. A workflow tests the
  deployment itself (guard levers, cohort floor, a governed pilot, the audit chains, the charts),
  because every fault that deployment has had passed the local suites. The API's address is kept only in `env/.env.published`

Deliberately **not** implemented, and named so nobody assumes otherwise:

- a federated optimizer or secure-aggregation protocol; `FederationEngine` is an adapter seam,
  the in-process engine is a simulator, and the GMA's HTTP engine moves jobs and clipped updates
  but sees each site's update and combines updates only through the model kind an application
  contributes
- differential privacy accounting; the Guard's noise strategy shows where a mechanism belongs, and
  a privacy budget is separate work
- cryptographic signing; round digests detect drift and carry no authenticity guarantee
- an append-only audit store; the chained log makes tampering detectable in a test, and production
  storage, retention, and signing are separate controls

Immediate implementation strategy across the agent layer remains:

- start with in-memory repositories and publishers
- expose health and control endpoints first
- keep messaging and persistence behind interfaces
- add broker-backed infrastructure later
- integrate with consumer-specific services after contracts stabilize

## OCaml Adoption Direction

The intended long-term architecture is polyglot rather than OCaml-first across the whole repository:

- Python for model execution, dataset transformation, and fast ML experimentation
- Spring Boot for orchestration APIs, policy entrypoints, and external service integration
- OCaml for strongly typed compilers, planners, validators, and manifest generators

OCaml is a strong fit for the functional kernels inside Dagents, especially where the system is compiling or validating declarative configurations rather than running stateful service logic.

### Recommended OCaml Responsibilities

- pipeline compiler layer between `services/pipeline-service` API models and runtime dispatch
- dataset schema and extraction compiler shared by LMA and GMA
- model capability router shared after dataset profiling
- Kubernetes manifest compiler under `services/core-service`

### Areas That Should Remain Outside OCaml First

- heavy numerical training and inference in `services/model-service`
- CRUD-heavy orchestration APIs and external integration surfaces
- product-specific UI or monitoring concerns
- low-level telemetry and heartbeat streams

### Recommended Placement

The preferred repo layout for these functional kernels is:

```text
bindings/
└── ocaml/
    ├── common_ir/
    ├── dataset_compiler/
    ├── pipeline_compiler/
    ├── model_router/
    ├── manifest_compiler/
    ├── governance_compiler/
    └── federation_compiler/
```

These modules should be treated as reusable compilers and planners first, not as top-level services.

`governance_compiler` and `federation_compiler` are the strongest cases for the layer being OCaml
rather than Python. Both decide things that must be reproducible during an audit months later, and
both express their choice sets as algebraic data types, so adding a sensitivity level, a trust
level, a granularity, or a stop condition fails to compile until every case is handled. A policy
engine keyed on strings would fall through to a default instead, and in a governance decision the
default is the branch nobody reviews.

### Integration Model

Use service or process boundaries rather than direct FFI.

Preferred integration pattern:

- `core-service` calls the OCaml manifest compiler through an internal process boundary or HTTP/gRPC wrapper
- `pipeline-service` calls the OCaml pipeline compiler and model router before execution
- `lma` calls the OCaml dataset compiler and model router for source-level runs, and the
  governance compiler before reading, training on, or sending anything
- `gma` calls the same OCaml modules for assimilated-data planning, plus the federation compiler
  for round planning, quorum, aggregation readiness, release gates, and every change of a model
  version's status

This keeps failure isolation, upgrade independence, explicit contracts, and simpler Kubernetes deployment.

Python services call the planner through `dagents/common/infrastructure/dagents_runner.py`. Each call
has limits, read from the process environment like `DAGENTSC_BIN`: `DAGENTSC_TIMEOUT_SECONDS`
(default 10), `DAGENTSC_MAX_INPUT_BYTES` and `DAGENTSC_MAX_OUTPUT_BYTES` (default 16 MiB each).
The runner stops a planner that runs or prints past them, with every process it started, and refuses
an input over its limit before the planner starts. `DAGENTSC_SHA256` pins the planner binary: with
another hash the runner starts nothing. Each of these raises `PlannerUnavailableError`, and the Guard
denies the request and records the denial. Every audit record names its planner in
`planner_version` (the `dagents` version) and `planner_sha256` (the binary's SHA-256); the digest
leaves out either field when it is null, so chains stored before the fields existed still verify.

## Working Assumptions for Contributors and Coding Agents

- Preserve the LMA/GMA split: local execution belongs in LMA, fleet-wide assimilation and coordination belongs in GMA.
- Keep product-specific logic out of Dagents unless it is truly reusable across consumers. The test
  to apply: if a capability would need a domain word in `dagents/`, it belongs in the consumer,
  reached through `dagents/common/extensions`.
- Keep deciding separate from enforcing. A deterministic rule belongs in an OCaml planner; applying
  it to real data, with an audit record, belongs in Python. Do not re-derive a planning rule in a
  service because calling out to `dagentsc` felt inconvenient.
- Never make the Ethical Guard fail open. If the planner is unreachable, the request is denied and
  the denial is recorded. Never add a fallback that permits a request when the planner fails.
- Never let aggregation imply release. A candidate model is produced by aggregation; a release
  requires its gates to pass and a human to approve. A gate whose metric is missing blocks.
- Keep each site's data in that site's store. Every read or write of a site's records goes through
  that site's store, acting as the site's role. The coordinator never reads site rows; it sees only
  what sites report (counts, rates the site's planner permits, each audit chain's state and length)
  and round results. Training labels never leave the site, and a model update never goes into a
  `SiteResult`, which the coordinator sees; only its digest and clipped norm do.
- Accept only typed data entry: enums, numeric ranges and yes/no fields, with no free text and no
  identifiers. A behaviour with two implementations (the in-memory and Postgres stores, the OCaml
  and Python manifest renderers) runs one test suite against both.
- Prefer typed contracts and deterministic planning over ad hoc branching in orchestration code.
- Keep `domain/` pure and push infrastructure concerns behind adapters and interfaces.
- Treat `services/model-service` as Python-first ML execution infrastructure.
- Treat `services/core-service` as the external orchestration façade, even if manifest synthesis moves into OCaml later.
- Treat `services/pipeline-service` as the execution surface for registered workflows, with planning logic eligible for extraction into typed compiler layers.
- Introduce new infrastructure through replaceable interfaces so the repo can evolve from in-memory delivery to broker-backed and persisted deployments without a structural rewrite.
- Write in easy, professional English: short sentences, plain words, facts and numbers instead of
  adjectives, and no slogans or dramatic phrasing. This applies to UI text, docs, comments and
  commit messages. The full rules are in `.claude/skills/writing/SKILL.md`.
- Change the docs with the code, in the same commit. The framework site publishes on every push to
  `main`, so a change is not done while a page describes the old behaviour. The `docs-sync` skill
  (`.claude/skills/docs-sync/SKILL.md`) maps each code area to its docs and gives the regenerate
  commands. CI's `docs-impact` job fails a change to a code area that changes none of its docs,
  unless a commit message says `Docs-Impact: none [area] (reason)` for that area.

## Releasing

Maintainers release the Python framework to PyPI from a tag on `main`; nobody uploads by hand. The
full steps are in the "Releasing" section of `CLAUDE.md`. In short:

- `__version__` in `dagents/__init__.py` is the only place the version is written. Versions are
  PEP 440 pre-releases until 1.0: `0.1.0a1` (alpha), then `0.x.0bN` (beta), `1.0.0rcN` and `1.0.0`.
  The `Development Status` classifier in `pyproject.toml` follows the stage. The README is the PyPI
  description, which PyPI never lets a release change, so it must be right before the tag.
- The owner starts a release from the Actions tab: workflow **Release**, **Run workflow** on
  `main`, with the box **Tag the head of main as `v<version>` and publish it** ticked. That run's
  only job creates the annotated tag `v<version>` on the head of `main` with the workflow's token
  and starts `.github/workflows/release.yml` again on the tag. The tag's message names the person
  who started the run and links to it, because the release run's actor is `github-actions[bot]`.
  Pushing the tag `v<version>` starts the same release run. The job stops when `main` has moved on
  since the run started (start a new run; a re-run checks the same commit) or when the tag already
  names another commit; when the tag already names this commit, it starts a new release run on it,
  which cannot publish different files.
- The release run on the tag stops when the tag differs from the version or the tagged commit is
  not on `main`, builds the planner on Linux x86_64, Linux aarch64 and macOS arm64, builds the
  sdist and one wheel per platform with that planner inside, checks and smoke-tests every wheel in
  a clean environment, publishes to TestPyPI first and then to PyPI, and creates a GitHub Release
  (a pre-release for `a`, `b` and `rc` versions) with the planner binaries and their checksums.
- Publishing uses Trusted Publishing: PyPI trusts the workflow through OpenID Connect, so no token
  exists to leak. The owner registered the publisher on pypi.org and test.pypi.org once. The
  intended setup also limits both environments to `v*` tags and makes the `pypi` environment wait
  for a reviewer's approval. As of 0.1.0a1 the `pypi` environment has no reviewer: that upload
  went ahead without an approval. "Trusted Publishing" in `CLAUDE.md` lists the steps still to do.
  The run that publishes is always on the tag, so these rules apply however the release started.
  A tag ruleset may restrict updates and deletions of `v*` tags, but not creations, because the
  workflow's token creates the tag. So anyone with write access can push a `v*` tag on any commit,
  whose own workflow file can skip the checks. The `pypi` reviewer, who checks on GitHub that the
  run's commit is on `main` before approving, is then the only check before PyPI; "Who can
  publish" in `CLAUDE.md` has the details.
- Every build uses the hatchling version pinned in `.github/build-constraints.txt`.
- A manual run with the box unticked is a dry run that builds and checks everything and publishes
  nothing. CI's `packaging` job builds and smoke-tests a Linux wheel on every pull request, also in an
  environment that has openai-agents (whose top-level module is `agents`), and runs the agent
  suite on the newest FastAPI and uvicorn the `lma` and `gma` extras allow.

## Learning Guide

New contributors should start with [`docs/learn/`](docs/learn/README.md). It covers federated
learning, data governance, the architecture and the APIs, with diagrams, videos, papers and
exercises. The framework site renders the same pages:
<https://pradyunuydarp.github.io/Dagents/#/learn>.

Developers who build an application on Dagents follow [`docs/guide/`](docs/guide/README.md)
(<https://pradyunuydarp.github.io/Dagents/#/guide>). Its code is copied from
`examples/getting-started/`, and `tests/test_getting_started_example.py` fails when the two
differ. A change to code that the guide shows updates the guide in the same commit; the
`docs-sync` skill (`.claude/skills/docs-sync/SKILL.md`) lists what each kind of change must update.

## First Consumers

- Watchdog
- Datalytics

Two consumers in this repository show the boundary, and both are published:

- [`apps/healthcare-demo/`](apps/healthcare-demo/README.md) — governance, federated training and
  data entry across three hospitals of the International Stroke Trial, each with its own store.
  Live: <https://pradyunuydarp.github.io/Dagents/healthcare-demo/>. The app owns its clinical
  feature contract, the dataset extract, the per-hospital stores, the seed rule, the logistic
  regression (contributed as a model kind, including how an update is checked and combined),
  the study sequence, the FHIR mapping and the intended-use statement. It calls one LMA per
  hospital and a GMA only through their HTTP APIs, with
  `dagents/common/infrastructure/agent_client.py`: in its own process through
  `httpx.ASGITransport` on the published demo, or over HTTP when the agents run as separate
  processes (`scripts/run_local_demo.sh --agents network`, its compose file). A network-mode
  run on one machine can add a fourth hospital while it runs (`--deploy`, or
  `docker-compose.deploy.yml`): core-service renders the new LMA's Compose file, an applier
  from `dagents/common/infrastructure/compose_apply.py` starts it, and the GMA accepts only the
  address its `GMA_JOINABLE_SITE_ENDPOINTS` lists. Every successful
  answer whose body is a JSON object lists the agent calls behind it (`framework_calls`); errors,
  the hospital list and the SVG charts do not. Nothing in `dagents/` knows what a stroke
  is, and nothing in the app re-implements a quorum rule.
- [`services/nl2sql-demo/`](services/nl2sql-demo/README.md) — validation, planning, service checks
  and workload compilation. Live: <https://pradyunuydarp.github.io/Dagents/nl2sql-demo/>. The app
  owns its UI and its SQL generation.
