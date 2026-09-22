# Dagents Agent Guide

## Purpose

Dagents is a shared agentic framework extracted from product repositories such as Watchdog and intended for reuse across future systems such as Datalytics. Its core focus is ML automation, workload generation, reusable orchestration patterns, and shared contracts between local and aggregate agents.

This repository is the generic home for:

- source-level and assimilated-data model agents
- reusable ML orchestration patterns
- shared control-plane contracts
- reusable pipeline execution services
- data-analysis and model-training utilities that should not live inside a single product codebase
- parameterized Kubernetes manifest generation for framework workloads

## Repository Scope

Top-level areas:

```text
agents/
apps/
bindings/
contracts/
docs/
services/
```

Current scope by area:

- `agents/`: reusable `lma` and `gma` ML-orchestration services with layered application boundaries
- `agents/common/extensions/`: the interface consumer apps contribute domain semantics through
- `apps/healthcare-demo/`: a self-contained consumer app, extractable into its own repository
- `bindings/ocaml/`: the typed planning layer, including governance and federation compilers
- `services/core-service/`: framework service catalog, topology endpoint, and Kubernetes manifest generation
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

Current in-memory implementation includes:

- health endpoint
- bundle deployment endpoint
- run trigger endpoint
- source dataset profiling endpoint
- source model execution endpoint
- local bundle, model-run, and run inspection endpoints

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

Current in-memory implementation includes:

- registration endpoint
- deployment planning and sync endpoints
- assimilated dataset profiling endpoint
- aggregate model execution endpoint
- run dispatch tracking
- in-memory storage of agent registrations and model-run history

## Shared Run Flow

The intended operating flow across the framework is:

1. GMA registers an LMA and records its scope and capabilities.
2. LMA receives or syncs a bundle or config revision.
3. LMA executes a local run over its source scope.
4. LMA optionally calls local product services.
5. LMA pushes source-level outputs to GMA or downstream storage.
6. GMA runs aggregate models over assimilated datasets.
7. Core service generates Kubernetes manifests for the required workloads.
8. Downstream orchestration systems apply manifests and consume model outcomes.

## Layering Conventions

Agent scaffolds under `agents/` follow the same layered shape:

- `domain/`: pure typed models
- `application/`: orchestration and use-cases
- `adapters/`: technology-facing boundaries and contracts
- `infrastructure/`: messaging, persistence, and concrete implementations
- `config.py`, `di.py`, `main.py`: composition root

This layout is intended to keep the framework portable across products while preserving clear separation between pure domain logic, orchestration, and infrastructure.

Repository shape for the agent layer:

```text
agents/
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

Current API:

- `GET /api/v1/health`
- `GET /api/v1/services`
- `GET /api/v1/topology`
- `POST /api/v1/manifests/pods`

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
- core-service Kubernetes manifest generation for workload deployment
- GRAILS-style governance: the Ethical-Restriction Rails as a typed OCaml planner, and the Ethical
  Guard enforcing its decisions in the LMA and GMA request paths with a digest-chained audit log
- federated round control: signed round manifests, site eligibility, quorum, aggregation
  readiness, and release gates, with an adapter seam for a specialist federated runtime
- a consumer extension registry, so an app contributes feature contracts, data classifications,
  condition packs, pipeline steps, and model adapters without forking the framework
- separate Docker images for `lma`, `gma`, `core-service`, `model-service`, and `pipeline-service`
- top-level `docker-compose.yml` for multi-container local or cloud-like deployment

Deliberately **not** implemented, and named so nobody assumes otherwise:

- a federated optimizer or secure-aggregation protocol; `FederationEngine` is an adapter seam and
  the bundled engine is an in-process simulator
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
  for round planning, quorum, aggregation readiness, and release gates

This keeps failure isolation, upgrade independence, explicit contracts, and simpler Kubernetes deployment.

## Working Assumptions for Contributors and Coding Agents

- Preserve the LMA/GMA split: local execution belongs in LMA, fleet-wide assimilation and coordination belongs in GMA.
- Keep product-specific logic out of Dagents unless it is truly reusable across consumers. The test
  to apply: if a capability would need a domain word in `agents/`, it belongs in the consumer,
  reached through `agents/common/extensions`.
- Keep deciding separate from enforcing. A deterministic rule belongs in an OCaml planner; applying
  it to real data, with an audit record, belongs in Python. Do not re-derive a planning rule in a
  service because calling out to `dagentsc` felt inconvenient.
- Never make the Ethical Guard fail open. If the planner is unreachable, the request is denied and
  the denial is recorded. A fallback that permits on planner failure defeats the entire layer.
- Never let aggregation imply release. A candidate model is produced by aggregation; a release
  requires its gates to pass and a human to approve. A gate whose metric is missing blocks.
- Prefer typed contracts and deterministic planning over ad hoc branching in orchestration code.
- Keep `domain/` pure and push infrastructure concerns behind adapters and interfaces.
- Treat `services/model-service` as Python-first ML execution infrastructure.
- Treat `services/core-service` as the external orchestration façade, even if manifest synthesis moves into OCaml later.
- Treat `services/pipeline-service` as the execution surface for registered workflows, with planning logic eligible for extraction into typed compiler layers.
- Introduce new infrastructure through replaceable interfaces so the repo can evolve from in-memory delivery to broker-backed and persisted deployments without a structural rewrite.

## First Consumers

- Watchdog
- Datalytics
