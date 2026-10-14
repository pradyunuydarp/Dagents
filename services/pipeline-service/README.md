# Dagents Pipeline Service

Reusable ML pipeline orchestration service for Dagents consumers.

## Purpose

This service provides a lightweight execution layer for repeatable multi-step data workflows:

- register named pipelines
- execute stored pipelines against JSON payloads
- inspect recent runs
- apply data-analysis and ML steps such as filtering, projection, profiling, summarization, and model execution

## Built-in Step Kinds

- `enrich_context`
- `filter_items`
- `summarize_items`
- `project_fields`
- `profile_dataset`
- `run_model_job`

## API

- `GET /api/v1/health`
- `GET /api/v1/pipelines`
- `POST /api/v1/pipelines`
- `POST /api/v1/pipelines/{pipeline_id}/runs`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
- `GET /api/v1/pipeline-definitions`
- `POST /api/v1/pipeline-definitions`
- `GET /api/v1/pipeline-definitions/{pipeline_id}`
- `POST /api/v1/pipeline-definitions/{pipeline_id}:validate`
- `POST /api/v1/pipeline-runs`
- `GET /api/v1/pipeline-runs`
- `GET /api/v1/pipeline-runs/{run_id}`
- `POST /api/v1/sources`
- `GET /api/v1/sources`
- `GET /api/v1/sources/{source_id}`
- `POST /api/v1/sources/{source_id}:validate`

Runtime settings are loaded from `env/.env.shared` and `env/.env.pipeline-service`.

## How the service checks and orders a pipeline

The OCaml pipeline compiler decides whether a pipeline's step graph is valid and
in what order its steps run. The service calls it with `dagentsc pipeline
compile` and has no ordering rule of its own.

Paths in this section and the next are under `/api/v1`.

- **Registering** (`POST /pipelines` or `POST /pipeline-definitions`): the
  service asks the planner first and stores the pipeline only if the planner
  accepts the graph.
- **Running** (`POST /pipelines/{pipeline_id}/runs`): the service asks the
  planner again and runs the steps in the order it returns.
- **Queueing** (`POST /pipeline-runs`): the service asks the planner before it
  queues the run, so a planner problem is reported on this request.

The planner rejects a graph that has a duplicate step id, a dependency on a step
that does not exist, or a cycle. It reports the first problem it finds.

Without the planner the service cannot check or order a pipeline, so these
requests return `503`. The container image has `dagentsc` on its PATH. To run
the service outside a container, build the planner and set `DAGENTSC_BIN`:

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && cd -
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
```

## Error responses

An error from the service has the body `{"detail": "<one sentence>"}`. When a
request body does not match the schema, FastAPI returns `422` and `detail` is a
list of field errors instead.

| Route | Success | Errors |
|---|---|---|
| `POST /pipelines`, `POST /pipeline-definitions` | `200` | `422` graph rejected, `503` |
| `POST /pipeline-definitions/{pipeline_id}:validate` | `200` | `404`, `422` graph rejected, `503` |
| `POST /pipelines/{pipeline_id}/runs` | `200` | `404`, `422` step failed, `503` |
| `POST /pipeline-runs` | `202` | `404`, `422` without `pipeline_id` or graph rejected, `503` |

| Status | Cause | Example `detail` |
|---|---|---|
| `404` | No pipeline is registered under that id | `Unknown pipeline: nope` |
| `422` | The planner rejected the graph | `Cyclic dependency: a -> b -> a (each step depends on the next)` |
| | | `Duplicate step id: a` |
| | | `Step b depends on missing, which is not a step of this pipeline` |
| `422` | A step in a synchronous run failed because of its config or the payload | `Step s (summarize_items) failed: Aggregation sum requires a field` |
| `503` | The planner cannot be run | `The pipeline planner is not available, so the pipeline cannot be checked or run. Try again later.` |

A step failure is a `ValueError`, `TypeError` or `LookupError` raised by the
step. Examples are an aggregation without `field`, an unknown condition
`operator`, and a condition that compares a number with a string. Any other
exception in a step returns `500`.

A queued run that fails at a step still returns `202` when it is queued. The run
then has the status `failed`, and its `error` holds the same message a
synchronous run returns with `422`.

The `503` response does not include the planner's own error, because it can
contain a file path. The service logs it as a warning.
