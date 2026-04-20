# OCaml Adoption Plan for Dagents Binding Layers

## Executive Summary

OCaml is a strong fit for the **functional kernels** inside Dagents, but not for the entire framework.

The best use of OCaml here is in the layers that:

- compile declarative workflow definitions into executable plans
- normalize and validate heterogeneous dataset schemas
- route dataset profiles to appropriate model families
- generate deterministic Kubernetes manifests from parameterized workload specs

It is **not** the best tool for:

- heavy numerical training and inference, which should stay in Python/PyTorch
- broad service orchestration, auth, or CRUD-heavy APIs, which should stay in Spring Boot
- UI concerns, which should stay outside this scope

The right architecture is therefore a **polyglot Dagents**:

- Python for model execution, dataset transformation, and fast ML experimentation
- Spring Boot for parameterized orchestration, policy entrypoints, and external service integration
- OCaml for strongly typed compilers, planners, validators, and manifest generators

## Reference Diagrams

### System Context

![OCaml system context](/Users/pradyundevarakonda/Developer/Dagents/docs/diagrams/puml/ocaml-system-context.png)

Source: [ocaml-system-context.puml](/Users/pradyundevarakonda/Developer/Dagents/docs/diagrams/puml/ocaml-system-context.puml)

### Component Placement

![OCaml component placement](/Users/pradyundevarakonda/Developer/Dagents/docs/diagrams/puml/ocaml-component-placement.png)

Source: [ocaml-component-placement.puml](/Users/pradyundevarakonda/Developer/Dagents/docs/diagrams/puml/ocaml-component-placement.puml)

### Materialization Phases

![OCaml materialization phases](/Users/pradyundevarakonda/Developer/Dagents/docs/diagrams/puml/ocaml-materialization-phases.png)

Source: [ocaml-materialization-phases.puml](/Users/pradyundevarakonda/Developer/Dagents/docs/diagrams/puml/ocaml-materialization-phases.puml)

## Why OCaml Fits Dagents

Dagents is increasingly becoming a framework that composes:

- dataset extraction rules
- feature contracts
- model selection rules
- pipeline DAGs
- Kubernetes workload specs

Those concerns are mostly **transformational**, not stateful. They are closer to compilers than CRUD services.

OCaml brings concrete advantages there:

- **Algebraic data types** let us model pipeline steps, workload kinds, model capabilities, and dataset shapes explicitly.
- **Exhaustive pattern matching** forces every step kind, model family, and manifest target to be handled deliberately.
- **Immutability-first style** reduces accidental mutation while compiling plans and manifests.
- **Compiler-guided refactoring** matters when the framework accumulates many model families and workload variants.
- **Deterministic pure functions** make property-based testing and reproducible plan generation easier.
- **Fast native binaries** are useful for compile-heavy workflows without introducing JVM startup or Python interpreter overhead.

## Where OCaml Should Be Placed

### 1. Pipeline Compiler Layer

Best placement: between `services/pipeline-service` API models and actual execution/runtime dispatch.

Responsibility:

- validate pipeline DAGs
- normalize step dependencies
- compile high-level pipeline DSL into an intermediate representation
- resolve which steps can run locally, in Python services, or on Kubernetes jobs

Why OCaml:

- DAG normalization and static validation are naturally functional
- exhaustive pattern matching is valuable as step types grow
- deterministic IR generation is easier to test than ad hoc mutable Python planning

What remains in Python:

- actual pipeline execution
- model run invocation
- data wrangling that relies on the Python ML ecosystem

### 2. Dataset Schema and Extraction Compiler

Best placement: shared binding layer used by LMA and GMA before model execution.

Responsibility:

- infer dataset profiles from source data
- validate feature contracts
- partition large datasets into execution-ready shards
- compile source-specific extraction configs into normalized data-extraction plans

Why OCaml:

- source schemas and extraction rules benefit from typed ASTs
- easier to guarantee no missing case when supporting `tabular`, `time_series`, `text`, and `hybrid`
- safer evolution of profile-to-model routing logic

What remains in Python:

- actual batch extraction implementations
- feature engineering that uses pandas, PyTorch, or tokenizer ecosystems

### 3. Model Capability Router

Best placement: a shared planner called by LMA and GMA after dataset profiling.

Responsibility:

- map dataset profile + task type + execution context to allowed model families
- select local vs aggregate execution strategy
- choose packaging mode: inline service call, Kubernetes Job, or long-running Deployment

Why OCaml:

- routing is a constrained decision problem, not a training problem
- decision trees over model/task/dataset/workload kinds are safer in a typed functional language
- easier to keep LMA and GMA behavior consistent from one shared rules engine

What remains in Python:

- training and inference execution inside `model-service`

### 4. Kubernetes Manifest Compiler

Best placement: under `services/core-service` as a compiler backend invoked by the parameterized orchestration API.

Responsibility:

- compile workload specs into Kubernetes `Deployment`, `Job`, `CronJob`, `Service`, and `ConfigMap` manifests
- enforce framework-wide defaults for resources, labels, ports, and environment variables
- generate distinct manifests for:
  - LMA source runners
  - GMA aggregate runners
  - model-service workers
  - pipeline-service workers

Why OCaml:

- Kubernetes YAML generation is closer to program synthesis than web orchestration
- strongly typed manifest builders reduce invalid YAML and missing fields
- deterministic rendering makes diff-based review and GitOps easier

What remains in Spring Boot:

- external API surface
- tenant-aware parameter collection
- auth, auditing, and persistence of workload intents

## Where OCaml Should Not Be Introduced First

OCaml should **not** be the first choice for:

- `services/model-service`: training and inference should stay Python-first
- `services/nlp-service`-style ML code in consumer repos
- Spring Boot orchestration endpoints that are mostly request routing
- direct low-level infrastructure monitoring such as heartbeats or telemetry streams

That would increase operational complexity without improving the hardest correctness problems.

## Recommended Repo Placement

The cleanest layout is a new top-level area dedicated to functional kernels:

```text
bindings/
└── ocaml/
    ├── common-ir/
    ├── dataset-compiler/
    ├── pipeline-compiler/
    ├── model-router/
    └── manifest-compiler/
```

This is better than placing OCaml directly under `services/` because these modules are **not services first**. They are reusable compilers/planners that can later be surfaced as:

- embedded CLI tools
- internal libraries
- sidecars
- dedicated internal services

## Integration Model

### Preferred Integration Pattern

Use **service or process boundaries**, not direct FFI.

Recommended bindings:

- `core-service` calls the OCaml manifest compiler over an internal process boundary or local HTTP/gRPC wrapper
- `pipeline-service` calls the OCaml pipeline compiler and model router before execution
- `lma` calls the OCaml dataset compiler and model router for source-level runs
- `gma` calls the same OCaml modules for assimilated-data planning

This is preferable to embedding OCaml into Python or JVM runtimes directly because:

- failure modes stay isolated
- upgrades are independent
- contracts become explicit
- Kubernetes deployment is simpler

### Shared Contract Strategy

Use Dagents contracts to represent:

- `DatasetProfile`
- `ModelExecutionRequest`
- `ModelPlan`
- `WorkloadSpec`
- `KubernetesManifestBundle`

OCaml should consume and emit only these normalized contracts.

## Advantages by Existing Dagents Module

### LMA

Best OCaml contribution:

- source extraction planning
- feature contract validation
- model family routing for per-source workloads

Advantage:

- LMA becomes a reliable planner for large per-source batches instead of ad hoc Python branching logic

### GMA

Best OCaml contribution:

- aggregate dataset composition plans
- workload partitioning for assimilated datasets
- global model routing and execution-mode selection

Advantage:

- GMA logic stays deterministic as it combines many sources and many model families

### Core Service

Best OCaml contribution:

- Kubernetes manifest compiler
- workload spec validator
- label/resource/default synthesis

Advantage:

- Spring Boot remains the orchestration façade while the most correctness-sensitive YAML generation lives in a typed compiler

### Pipeline Service

Best OCaml contribution:

- DAG compiler
- static validation of step graphs
- lowering high-level ML pipeline intent into executable plans

Advantage:

- the pipeline API can remain simple while the execution plan remains precise and testable

## Materialization Plan

### Phase 1: Establish OCaml Functional Kernel Boundary

Deliver:

- `bindings/ocaml/common-ir`
- shared typed IR for dataset profiles, model plans, and workload specs
- contract mapping tests against current Dagents Python/Spring models

Exit criteria:

- Python and Spring services can serialize into one shared IR

### Phase 2: Build Manifest Compiler First

Deliver:

- `bindings/ocaml/manifest-compiler`
- support for `Deployment`, `Job`, `CronJob`, and `Service`
- integration hook from `services/core-service`

Reason to start here:

- highest correctness value
- lowest dependency on ML runtime internals
- immediate benefit for Watchdog and Datalytics Kubernetes automation

### Phase 3: Build Pipeline Compiler

Deliver:

- `bindings/ocaml/pipeline-compiler`
- DAG validation and lowering into execution IR
- integration hook from `services/pipeline-service`

Exit criteria:

- pipeline-service no longer relies on ad hoc in-memory interpretation for complex workflow planning

### Phase 4: Build Dataset Compiler and Model Router

Deliver:

- `bindings/ocaml/dataset-compiler`
- `bindings/ocaml/model-router`
- integration hooks from `agents/lma` and `agents/gma`

Exit criteria:

- LMA source-level model planning and GMA aggregate model planning use the same rules engine

### Phase 5: Add Kubernetes-Targeted ML Workload Generation

Deliver:

- model-run workloads emitted as Kubernetes manifests from core-service
- profile-aware workload generation for:
  - source runners
  - aggregate runners
  - batch Jobs
  - scheduled CronJobs

Exit criteria:

- from one parameterized core-service request, Dagents can generate workload manifests for the right ML execution topology

## Suggested First OCaml Modules

If we start small, the first three OCaml packages should be:

1. `manifest-compiler`
2. `pipeline-compiler`
3. `model-router`

That sequence maximizes value while minimizing ecosystem disruption.

## Risks and Mitigations

### Risk: Polyglot operational overhead

Mitigation:

- introduce OCaml only behind narrow contracts
- keep deployment units small
- avoid direct in-process embedding at first

### Risk: Team ramp-up cost

Mitigation:

- keep OCaml scope limited to compiler/planner modules
- preserve Python and Spring for the majority of day-to-day product work

### Risk: Contract drift between languages

Mitigation:

- treat the shared IR as first-class contracts
- generate tests for Python/Java/OCaml round-tripping

## Final Recommendation

Yes, Dagents should adopt OCaml, but selectively.

The best use is **not** to rewrite services. The best use is to introduce OCaml as a **functional compiler layer** for:

- pipeline planning
- dataset/profile normalization
- model routing
- Kubernetes manifest synthesis

That gives Dagents the strongest benefits of functional programming exactly where correctness, determinism, and compositional reasoning matter most.
