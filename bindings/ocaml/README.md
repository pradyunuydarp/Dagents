# Dagents OCaml Functional Modules

The OCaml workspace contains deterministic functional kernels for Dagents. These modules are not long-running services. They compile, validate, and plan work that Python, Spring Boot, LMA, GMA, and Kubernetes-facing services execute through explicit process or service boundaries.

## Design Goals

- Keep planning logic pure, typed, and reproducible.
- Emit normalized contracts that runtime services can execute without repeating compiler decisions.
- Keep source I/O, training, persistence, auth, and orchestration outside OCaml.
- Make every model, pipeline step, source kind, and workload kind explicit through algebraic data types.
- Preserve the existing integration model: services call `dagentsc` or a future internal wrapper, not direct FFI.

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

### `common_ir`

`common_ir` is the shared typed contract layer. It defines the values exchanged by all compiler modules:

- dataset records and scalar values
- source specs for inline, Postgres, MongoDB, and object storage inputs
- schema contracts and validation reports
- quality rules and quality results
- extraction plans and partition strategies
- pipeline definitions and compiled pipeline plans
- dataset profiles and model route plans
- Kubernetes workload specs and rendered workload plans

JSON codecs live beside the IR in `json_codec.ml` so the CLI and service wrappers use the same contract shape as library callers.

### `dataset_compiler`

`dataset_compiler` is the main data-engineering kernel. It provides:

- `infer_schema`: derive a stable schema from inline records.
- `metadata_of_source`: summarize a source without performing runtime I/O.
- `validate_source`: reject invalid connector selections before execution.
- `compile_extraction_plan`: lower source-specific selections into a normalized extraction plan.
- `build_profile`: infer feature fields, numeric/categorical fields, partitions, and suggested models.
- `validate_schema_contract`: compare actual schemas to required and optional contract fields.
- `evaluate_quality_rules`: evaluate non-null, uniqueness, numeric bound, glob-match, and allowed-value rules.
- `compile_transform_plan`: compute output schema for a declarative transform sequence.
- `apply_transform_plan`: execute simple record-level transforms for local or test-time workflows.

This module deliberately plans connector work instead of opening database connections or object storage readers. Runtime adapters remain responsible for I/O.

### `pipeline_compiler`

`pipeline_compiler` validates and lowers workflow DAGs:

- detects duplicate step ids
- rejects cycles
- verifies dependencies exist
- topologically orders steps
- assigns execution targets such as local process or Python service

The pipeline service can execute the compiled plan without rebuilding DAG rules in Python.

### `model_router`

`model_router` maps `dataset_profile` and `task_type` into a model route plan:

- candidates by task family
- selected default model
- packaging mode: inline service call, Kubernetes job, or long-running deployment

This keeps LMA and GMA model selection consistent after profiling.

### `manifest_compiler`

`manifest_compiler` renders deterministic Kubernetes YAML from typed workload specs:

- `Deployment`
- `Job`
- `CronJob`
- `Service`
- `ConfigMap`

Core service remains the public orchestration facade. The OCaml compiler is the deterministic manifest backend.

## CLI Surface

`dagentsc` exposes the functional modules over stdin/file JSON boundaries:

```sh
dagentsc manifest compile --input workload.json --output json
dagentsc pipeline compile --input pipeline.json --output json
dagentsc model route --task anomaly_detection --output json
dagentsc dataset profile --scope-id source-a --output json
dagentsc dataset source validate --input source.json
dagentsc dataset source metadata --input source.json
dagentsc dataset source extract --input source.json
dagentsc dataset schema validate --records records.json --contract contract.json
dagentsc dataset quality evaluate --records records.json --rules rules.json
dagentsc dataset transform compile --records records.json --operations operations.json
dagentsc dataset transform apply --records records.json --operations operations.json
```

Use `--input -`, `--records -`, or similar flags to read JSON from stdin.

## Data-Engineering API Shape

The new data-engineering layer follows a compile-then-execute pattern:

1. Validate source specs.
2. Compile a normalized extraction plan.
3. Infer or validate schema.
4. Evaluate quality rules.
5. Compile transforms and inspect the output schema.
6. Apply transforms locally when record materialization is appropriate.
7. Profile the resulting dataset and route it to model execution.

This creates a typed path from heterogeneous data source configuration to ML execution planning while keeping runtime adapters replaceable.

## Contract Examples

Source extraction input:

```json
{
  "sourceId": "orders",
  "kind": "postgres",
  "connectionRef": { "connectionId": "warehouse" },
  "selection": {
    "table": "public.orders",
    "columns": ["id", "amount"],
    "where": "amount > 0"
  },
  "batching": { "batchSize": 500, "maxRecords": 1000 },
  "options": { "partitionField": "id" }
}
```

Quality rules:

```json
[
  { "ruleId": "id_present", "field": "id", "operator": "non_null", "severity": "error" },
  { "ruleId": "amount_positive", "field": "amount", "operator": { "kind": "min_value", "value": 0 }, "severity": "error" }
]
```

Transform operations:

```json
[
  { "kind": "cast_fields", "casts": { "amount": "float" } },
  { "kind": "rename_fields", "mappings": { "amount": "amount_usd" } },
  { "kind": "drop_fields", "fields": ["raw_payload"] }
]
```

## Build And Test

```sh
opam exec -- dune build ./bin/dagentsc.exe
opam exec -- dune test
```

## Extension Guidelines

- Add new source kinds to `common_ir` first, then update JSON parsing, source validation, metadata planning, extraction planning, and tests.
- Add new quality operators as data constructors, not string-only conditionals.
- Keep connector access outside OCaml unless it is a deliberate service boundary change.
- Add tests at the compiler boundary: invalid inputs, normalized output contracts, and deterministic ordering.
- Prefer returning reports and plans over raising exceptions, except for invalid compile requests that cannot produce a meaningful plan.
