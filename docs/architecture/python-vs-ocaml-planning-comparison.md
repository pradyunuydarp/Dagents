# Python vs OCaml Planning Comparison

Dagents uses both Python and OCaml because they solve different parts of the system well. Python owns runtime services, APIs, integration, and ML execution. OCaml owns deterministic functional planning.

## Pipeline Validation

The OCaml planner decides whether a pipeline graph is valid and in what order its steps run. The Python pipeline service calls it through `dagentsc pipeline compile` and runs the steps in the order it returns. The service once kept its own topological sort as a fallback. That copy made every decision and the planner's answer was ignored, so the copy was removed.

| Concern | Python pipeline service | OCaml planner |
| --- | --- | --- |
| Owns | Storing pipelines, running steps, run history | Graph validation and step order |
| Invalid graph | Returns `422` with the planner's message | Exits with status 3 and a JSON report |
| Planner cannot be run | Returns `503` | — |
| Type safety | Pydantic at the API boundary | The `pipeline_graph_error` type inside the planner |

## Dataset Planning

Python should own database connections, file reads, pandas/PyTorch transforms, and runtime execution. OCaml should own source validation, extraction planning, schema contracts, and quality decisions.

This separation prevents the planner from becoming a second data runtime. It also lets services inspect plans before performing side effects.

## Manifest Rendering

Python core-service can render manifests directly as a fallback. OCaml manifest planning gives deterministic YAML generation from typed workload specs.

The tradeoff is integration overhead: the service must call `dagentsc` and convert JSON keys. The benefit is that manifest rendering logic becomes smaller, typed, and reusable across Python and Spring services.

## Why The Split Works

- OCaml is used where correctness comes from type structure and exhaustive cases.
- Python is used where the ecosystem matters: APIs, ML libraries, service glue, and I/O.
- JSON subprocess integration is less elegant than in-process calls but is simple, portable, and easy to demo.

## Course Takeaway

The project demonstrates that functional programming does not need to replace an entire application. It can be introduced as a precise planner layer inside a polyglot system.
