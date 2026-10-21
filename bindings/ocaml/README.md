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
├── lib/governance_compiler
├── lib/federation_compiler
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
- workload specs, their deployment targets (Kubernetes or Docker Compose), and rendered workload and Compose plans

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

`manifest_compiler` renders a typed workload spec for the target the spec names.
`compile_for_target` matches `spec.target` once: `Kubernetes` goes to
`compile_plan` and `Compose` goes to `compile_compose_plan`.

For Kubernetes, it renders deterministic YAML for these objects:

- `Deployment`
- `Job`
- `CronJob`
- `Service`
- `ConfigMap`
- `ServiceAccount`

For Docker Compose, it renders one Compose file:

- **Placement.** `own` makes the file a whole project. `merge` makes a file
  that is passed with `-f` after the files of a project that already runs, so a
  new service can depend on a service listed in `existingServices`.
- **Network.** Every service joins one network: one the file declares, or one
  that exists already (`externalNetwork`).
- **Kinds.** A `Deployment` becomes a service with `restart: unless-stopped`,
  and a `Job` becomes one with `restart: "no"`. The other kinds have no Compose
  equivalent.
- **Ports.** A port without `hostPort` is reachable only on the network
  (`expose`). A published port binds to `127.0.0.1` unless `hostIp` says
  otherwise, because an LMA or a GMA started without tokens answers every
  caller.
- **Issues.** A spec that cannot become a Compose file still gives a plan. The
  plan lists every issue, in a fixed order, and has no file. With
  `--output json`, `dagentsc` exits with code 0. With YAML output it prints the
  issues as comments and exits with code 3, because Docker Compose merges a
  file of comments as nothing when it follows a project's own file. The issue
  codes are in `compose_issue_code_and_value`.
- **Quoting.** Every value a caller chose is written as a double-quoted YAML
  scalar, and `$` is written as `$$` so Compose passes it to the container
  instead of reading a variable. Control characters are escaped, including
  U+0085, U+2028 and U+2029, which YAML would read as line breaks. Names are checked, then written double-quoted
  too, because YAML reads a bare name such as `134`, `null` or `True` as a
  number, null or a boolean, and Compose refuses such a key.

The Kubernetes renderers ignore the Compose fields, and the Compose renderer
ignores the Kubernetes-only fields such as `resources` and `namespace`. Neither
renderer applies a file: Core service is the public orchestration facade, and
the OCaml compiler is its deterministic manifest backend.

### `governance_compiler`

`governance_compiler` implements the **Ethical-Restriction Rails** half of GRAILS (Kulkarni and
Ramanathan, AIES 2025): the part that decides what protection a request needs, without ever
touching data.

- `assess_requester`: score Know-Your-User attributes into a trust level.
- `select_strategy`: the sensitivity x trust x granularity lookup, as one exhaustive match.
- `plan_restrictions`: a full restriction plan, with per-field strategies, the overall decision,
  GRAILS' Filtering Score, the obligations the enforcing guard must discharge, and any
  request-level violations.

This is the clearest case in the repository for why the planning layer is OCaml. Adding a
sensitivity level, a trust level, or a granularity fails to compile until every combination has
been considered. A string-keyed policy engine would fall through to a default instead, and the
default in a governance decision is the one nobody reviews.

The published GRAILS granularity set is cell, row, column, and table — all of them data that can
be pointed at and read. A federated round ships none of them; it ships a model update, which is
not a row but still carries signal derived from records. `ModelUpdateGrain` is that fifth
granularity, and the invariant that it never resolves to `AllowFull` is enforced by test.

Enforcement deliberately stays outside OCaml. The **Ethical Guard** lives in the LMA and GMA
request paths, in `dagents/common/application/ethical_guard.py`, because applying a decision needs
real data, a real requester, and somewhere to write an audit record.

### `federation_compiler`

`federation_compiler` governs a federated study without running one. The distributed protocol
belongs to a specialist runtime such as NVIDIA FLARE, reached from the GMA through an adapter;
what lives here is the part that has to be deterministic and reviewable months later during an
audit.

- `round_digest`: a deterministic content digest over a round manifest. Non-cryptographic, for
  drift detection and stable round identity — explicitly not a signature.
- `compile_round_plan`: site eligibility and quorum, with a recorded reason for every exclusion.
- `evaluate_aggregation_readiness`: which contributions may enter an aggregate, and the FedAvg
  weights, reported rather than applied. Given the round plan's selection (`Selected sites`), it
  rejects a result from any other site with the reason "site was not selected for this round";
  that result counts toward neither the quorum nor the weights. `Unchecked` skips the check.
- `evaluate_release`: release gates against a candidate's metrics.
- `decide_model_lifecycle`: the status changes that follow one event on a model version
  (`registered`, `aggregated`, `release_evaluated`, `approval_requested`), or the refusal and its
  reason. A version is a candidate, released, rejected or superseded. A model registry applies
  the answer and keeps no status rule of its own. A `release_evaluated` event may carry the
  decision the version already holds (`storedDecision`). For the same round, the same decision
  changes nothing on any status, and a different one is refused as `decision_differs`, with the
  differing fields named. A stored decision for another round gives the answer the event gets
  without one.
- `decide_round_recovery`: whether a round stored before a restart is kept or closed. It reads
  the round's status and phase, whether its record could be read, whether its training updates
  were kept, whether its candidate is registered, and the age of its oldest update against a
  limit. The caller reads the clock and passes the age in. A planned, dispatched or collecting
  round whose record cannot be read is closed (`unreadable_record`). A readable planned or
  dispatched round is closed (`dispatch_interrupted`), because a dispatch cannot resume. A
  collecting training round is closed when its updates were not kept (`updates_not_kept`) or are
  older than the limit (`updates_expired`), unless its candidate is already registered.
  `FederatedRoundController.recover` in `dagents/common/application/federation.py` calls it
  once per unfinished round, and a GMA whose rounds are in a table runs `recover` once after a
  restart. Before each dispatch, `expire_updates` calls it for each unfinished round whose oldest
  stored update is older than the GMA's age limit.

Two behaviours matter most. A secure aggregation threshold raises the minimum number of
participants above the manifest's stated minimum, because the threshold is what stops the
coordinator from seeing any single site's update. And a gate whose metric the candidate never
reported is `GateNotEvaluated`, not a pass, so leaving out a subgroup measurement cannot get an
unevaluated model released.

The module's main rule: aggregation produces a candidate, never a release. The lifecycle rule
holds it: an aggregated version is always a candidate, and only an `approval_requested` event can
release one. An approval is refused, in this order, when the version has no release decision, the
gates did not decide `release`, the release Guard did not permit it, the gate metrics did not come
from the rounds, the version is not a candidate, or the model the gates compared it with is no
longer the released one.

## CLI Surface

`dagentsc` exposes the functional modules over stdin/file JSON boundaries:

```sh
dagentsc manifest compile --input workload.json --output json
dagentsc manifest compile --target compose --name lma-hospital-d --image ghcr.io/example/dagents-lma:0.1.0
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
dagentsc governance restrict --input request.json
dagentsc governance assess --input requester.json
dagentsc federation round plan --input '{"manifest": {...}, "registrations": [...]}'
dagentsc federation round digest --input '{"manifest": {...}}'
dagentsc federation round recover --input '{"status": "collecting", "phase": "training", ...}'
dagentsc federation aggregate readiness --input '{"manifest": {...}, "results": [...], "selectedSites": [...]}'
dagentsc federation release evaluate --input '{"gates": [...], "candidateMetrics": {...}}'
dagentsc federation model lifecycle --input '{"version": "...", "event": {"kind": "approval_requested", ...}}'
```

Use `--input -`, `--records -`, or similar flags to read JSON from stdin.

`--input` takes a file name or `-`; the inline JSON above shows the shape of the input.

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

Aggregation readiness with the round plan's selection (`selectedSites` is optional; without it
the selection is not checked):

```json
{
  "manifest": { "roundId": "returns-round-1", "...": "..." },
  "results": [
    { "roundId": "returns-round-1", "siteId": "store-north", "participation": "completed", "...": "..." }
  ],
  "selectedSites": ["store-north", "store-south"]
}
```

Model lifecycle, an approval:

```json
{
  "version": "returns-logreg+returns-train-2",
  "event": {
    "kind": "approval_requested",
    "status": "candidate",
    "decision": {
      "action": "release",
      "guardPermitted": true,
      "metricsSource": "rounds",
      "baselineVersion": "returns-rule-1.0"
    },
    "releasedVersion": "returns-rule-1.0"
  }
}
```

and its answer:

```json
{
  "allowed": true,
  "refusal": null,
  "reason": "returns-logreg+returns-train-2 is released; returns-rule-1.0 is superseded",
  "statusChanges": [
    { "version": "returns-logreg+returns-train-2", "fromStatus": "candidate", "toStatus": "released" },
    { "version": "returns-rule-1.0", "fromStatus": "released", "toStatus": "superseded" }
  ]
}
```

Model lifecycle, a retried release evaluation whose decision differs from the one stored for the
same round (`docs/demo/inputs/model-lifecycle-decision-differs.json`). Every key of both decisions
and of each gate result is required:

```json
{
  "version": "returns-logreg+returns-train-2",
  "event": {
    "kind": "release_evaluated",
    "status": "rejected",
    "action": "release",
    "guardPermitted": true,
    "roundId": "returns-train-2",
    "baselineVersion": "returns-rule-1.1",
    "metricsSource": "rounds",
    "gateResults": [
      { "gateId": "accuracy_floor", "outcome": "passed", "observed": 0.84, "threshold": 0.8, "detail": "accuracy=0.84 required >= 0.8" },
      { "gateId": "accuracy_improves", "outcome": "passed", "observed": 0.84, "threshold": 0.84, "detail": "accuracy=0.84 required >= baseline 0.83 + margin 0.01" }
    ],
    "storedDecision": {
      "roundId": "returns-train-2",
      "action": "reject",
      "guardPermitted": true,
      "baselineVersion": "returns-rule-1.0",
      "metricsSource": "rounds",
      "gateResults": [
        { "gateId": "accuracy_floor", "outcome": "passed", "observed": 0.84, "threshold": 0.8, "detail": "accuracy=0.84 required >= 0.8" },
        { "gateId": "accuracy_improves", "outcome": "failed", "observed": 0.84, "threshold": 0.86, "detail": "accuracy=0.84 required >= baseline 0.85 + margin 0.01" }
      ]
    }
  }
}
```

and its answer:

```json
{
  "allowed": false,
  "refusal": "decision_differs",
  "reason": "returns-logreg+returns-train-2 already has a release decision for round returns-train-2, and this one differs in action, baseline_version and gate_results",
  "statusChanges": []
}
```

Round recovery (`docs/demo/inputs/federation-round-recover-updates-expired.json`). Every key is
required; an age is `null` when no update is stored, and the limit is `null` when there is none:

```json
{
  "status": "collecting",
  "phase": "training",
  "readable": true,
  "updatesKept": true,
  "candidateRegistered": false,
  "oldestUpdateAgeSeconds": 90061,
  "updateMaxAgeSeconds": 86400
}
```

and its answer:

```json
{
  "verdict": "close",
  "reason": "updates_expired",
  "message": "The round's oldest stored update is 90061 seconds old, more than the limit of 86400 seconds, so it is closed; run it again under a new round id."
}
```

A Compose workload spec that adds one hospital's LMA to a running Compose
project (`dagentsc manifest compile --input -`). The example assumes a project
that runs the services listed in `existingServices`, among them a GMA named
`gma`, as the stroke demo's Compose file does. It also assumes an image that
contains the Dagents LMA (`dagents.lma.main:app`), and a settings file,
`lma-hospital-d.env`, with the new LMA's other `LMA_*` settings and its tokens.
The planner checks none of these assumptions: it sees only the names in
`existingServices`, not the running project, the image or the file.

In the stroke demo's own project this file does not start a working hospital:

- The demo's `healthcare_site_store` site source accepts only the demo's
  hospitals: the three it starts with (`uk-134`, `it-002` and `ch-245`) and
  the one that can join (`pl-290`). For `hospital-d` it raises an error, so
  the LMA fails at startup and Compose keeps restarting it.
- The GMA accepts a new LMA's address only for a site that its
  `GMA_JOINABLE_SITE_ENDPOINTS` lists, and only the address listed there. It
  sends no job to a site it has no address and tokens for.

The stroke demo adds `pl-290` with a request of this shape, which its API
sends to core-service while the demo runs; see "Add a hospital while the demo
runs" in [its README](../../apps/healthcare-demo/README.md).

The spec:

```json
{
  "planId": "add-hospital-d",
  "target": {
    "kind": "compose",
    "placement": "merge",
    "network": "default",
    "existingServices": ["postgres", "setup", "gma", "lma-uk-134", "lma-it-002", "lma-ch-245", "healthcare-demo-backend", "healthcare-demo-frontend"]
  },
  "components": [
    {
      "name": "lma-hospital-d",
      "image": "dagents/healthcare-demo-backend:local",
      "args": ["uvicorn", "dagents.lma.main:app", "--host", "0.0.0.0", "--port", "8085"],
      "envFiles": ["../../env/.env.shared", "lma-hospital-d.env"],
      "env": [
        {"name": "LMA_SITE_ID", "value": "hospital-d"},
        {"name": "LMA_SITE_SOURCE", "value": "healthcare_site_store"},
        {"name": "LMA_API_PORT", "value": "8085"}
      ],
      "ports": [{"name": "http", "containerPort": 8085, "hostPort": 8085}],
      "healthCheck": {"command": ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/api/v1/health')"]},
      "dependsOn": [{"service": "gma", "condition": "service_healthy"}]
    }
  ]
}
```

The Compose file it prints:

```yaml
services:
  "lma-hospital-d":
    image: "dagents/healthcare-demo-backend:local"
    command: ["uvicorn", "dagents.lma.main:app", "--host", "0.0.0.0", "--port", "8085"]
    env_file:
      - "../../env/.env.shared"
      - "lma-hospital-d.env"
    environment:
      "LMA_SITE_ID": "hospital-d"
      "LMA_SITE_SOURCE": "healthcare_site_store"
      "LMA_API_PORT": "8085"
    ports:
      - "127.0.0.1:8085:8085"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/api/v1/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    depends_on:
      "gma":
        condition: service_healthy
    networks:
      - "default"
    restart: unless-stopped
networks:
  "default": {}
```

Saved as `generated.yml`, it starts inside the running project with the
project's name and the project's own Compose file first:

```sh
docker compose -p <project> -f <the project's file> -f generated.yml up -d lma-hospital-d
```

With a project that lacks a `gma` service, Docker Compose refuses the merged
files (`service "lma-hospital-d" depends on undefined service "gma"`).

Relative paths, such as the env files, resolve against the directory of the
first file. With `"placement": "own"`, the same spec has one issue and no file,
and `dagentsc` exits with code 3:

```text
# No Compose file: the spec has issues.
# lma-hospital-d: dependency_outside_project gma
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

`opam exec -- dune build --profile static ./bin/dagentsc.exe` links the
binary statically and strips it, so one Linux build runs on any distribution,
glibc or musl. The profile is defined in `bindings/ocaml/dune`. The release
workflow builds the planner this way for the Linux wheels of the `dagents`
Python package, and CI's `planners` job uploads it as `dagentsc-static`. macOS
cannot link statically, so the macOS planner uses `--profile release`.

## Conservative Defaults in the Codecs

The governance and federation codecs default toward refusal, because a field a caller forgot is
not permission:

| Omitted field | Default | Why |
|---|---|---|
| `defaultSensitivity` | `high` | an unclassified field is the most protected until classified |
| attribute `verified` | `false` | a claimed credential is not a checked one |
| site `enrolled` | `false` | taking part has to be stated, never inferred from a record existing |
| gate `blocking` | `true` | a gate whose severity nobody stated is not advisory |
| round `phase` | `analytics` | the phase that moves the least |
| lifecycle `requestedStatus` | `candidate` | a registration that does not ask for a release does not get one |
| lifecycle `guardPermitted` | `false`, and required with a `storedDecision` | a release Guard verdict nobody recorded is not a permit |
| decision `metricsSource` | `unrecorded` | only metrics from the rounds can lead to an approval |

One optional key is not a cautious default: readiness without `selectedSites` does not check the
selection, so inputs written before the key existed give the same answer. A caller that has the
round plan should send its `selectedSites`. The same holds for `storedDecision` on a
`release_evaluated` event: without it, the event gets the answer it got before the key existed.

`federation round recover` has no defaults: every key is required.

## Extension Guidelines

- Add new source kinds to `common_ir` first, then update JSON parsing, source validation, metadata planning, extraction planning, and tests.
- Add new quality operators as data constructors, not string-only conditionals.
- Keep connector access outside OCaml unless it is a deliberate service boundary change.
- Add tests at the compiler boundary: invalid inputs, normalized output contracts, and deterministic ordering.
- Prefer returning reports and plans over raising exceptions, except for invalid compile requests that cannot produce a meaningful plan.
