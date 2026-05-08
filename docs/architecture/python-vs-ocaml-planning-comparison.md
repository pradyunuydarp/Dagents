# Python vs OCaml Planning Comparison

Dagents uses both Python and OCaml because they solve different parts of the system well. Python owns runtime services, APIs, integration, and ML execution. OCaml owns deterministic functional planning.

## Pipeline Validation

Python pipeline service has a fallback topological sort for local execution. OCaml provides the shared planner used through `dagentsc pipeline compile`.

| Concern | Python fallback | OCaml planner |
| --- | --- | --- |
| Runtime fit | Close to FastAPI service and request models | Isolated pure planner |
| Type safety | Pydantic at boundaries | Algebraic data types inside planner |
| Failure modes | Exceptions in service code | Deterministic invalid-plan failures |
| Testability | Good service tests | Small pure unit tests |

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
