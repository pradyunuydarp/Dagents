# Functional Modules Walkthrough

The functional programming contribution is under `bindings/ocaml`. These modules are domain-specific planners/translators. They are not programming-language compilers.

## Why OCaml

OCaml is used for code that benefits from:

- algebraic data types
- exhaustive pattern matching
- immutable data transformations
- pure functions
- deterministic outputs
- small testable kernels

This fits Dagents because source validation, pipeline planning, model routing, and manifest rendering are structured transformations.

## Module Map

```text
bindings/ocaml
├── lib/common_ir
├── lib/dataset_compiler
├── lib/pipeline_compiler
├── lib/model_router
├── lib/manifest_compiler
└── bin/dagentsc.ml
```

## `common_ir`

This module defines typed contracts shared by all planner modules:

- source kinds: inline, Postgres, MongoDB, object storage
- dataset records and schema fields
- quality operators and severities
- partition strategies
- pipeline steps
- model families
- workload kinds

Presentation point: instead of passing arbitrary dictionaries everywhere, the planner layer makes valid domain shapes explicit.

## `dataset_compiler`

Despite the directory name, this is a dataset planner. It handles:

- source validation
- extraction plan generation
- schema inference
- schema contract validation
- severity-aware quality reports
- transform planning and record-level transform application
- partition planning, including time-window partitioning

Presentation point: runtime adapters still do I/O. OCaml decides whether the source spec is valid and what the runtime should do.

## `pipeline_compiler`

This module plans workflow DAGs:

- rejects duplicate step IDs
- rejects missing dependencies
- rejects cycles
- topologically orders steps
- assigns execution targets

Presentation point: this is a classic place where functional programming helps because graph validation is deterministic and easy to test.

## `model_router`

This module maps dataset profiles and task types to model families and packaging modes. For example, forecasting routes toward sequence models, while anomaly detection can route toward autoencoders.

Presentation point: routing is a policy decision, not a training job, so it belongs in the functional planner.

## `manifest_compiler`

This module renders typed workload specs into Kubernetes YAML for Deployments, Jobs, CronJobs, Services, and ConfigMaps.

Presentation point: the app does not hand-build YAML in many places. It uses a deterministic renderer from typed input.

## `dagentsc`

`dagentsc` is the process boundary. It lets Python and Java services call the OCaml functional layer through JSON.

Example:

```bash
bindings/ocaml/_build/default/bin/dagentsc.exe dataset quality evaluate \
  --records docs/demo/inputs/records-orders.json \
  --rules docs/demo/inputs/quality-rules-orders.json
```

## Phrase To Use In Presentation

“The OCaml modules are not compilers for a programming language. They are typed functional planners: they translate Dagents source, pipeline, model, and workload descriptions into deterministic plans that the runtime services can execute.”
