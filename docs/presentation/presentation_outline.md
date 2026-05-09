# Dagents Presentation Outline

## Slide Flow

1. Title: Dagents as a framework for source-level and multi-source computation agents.
2. Project definition: one CMA/LMA per source, one GMA for combined computation.
3. Non-technical context diagram: actors, sources, local agents, GMA, services, and runtime.
4. Framework services: core-service, pipeline-service, model-service, LMA/CMA, GMA.
5. High-level software architecture: backend, Dagents APIs, agent layer, OCaml planners, sources, and Kubernetes/runtime.
6. Integration points: how a backend plugs into Dagents APIs.
7. Ready API: real `core-service` workload compilation endpoint and example consumer call.
8. NL2SQL integration diagram: frontend/backend connections into Dagents services and `dagentsc`.
9. NL2SQL backend wiring: actual orchestration calls used during `/api/v1/generate`.
10. Functional programming rationale: why OCaml is used for planning, validation, routing, and rendering.
11. Highlighted functional layer: the OCaml modules inside the high-level Dagents architecture.
12. Low-level OCaml architecture: `dagentsc`, `common_ir`, JSON codecs, compiler modules, and outputs.
13. When OCaml runs: build time, startup configuration, request-time planning, and deployment-time manifest compilation.
14. Demo value: why the functional layer helps the NL2SQL demo.
15. Demo runbook: local demo, probe script, compose demo, and functional-layer output script.
16. Close: compute locally, combine globally, plan deterministically, deploy through services.

## Key Wording

- "CMA" is used as a presentation-friendly term for a per-source computation agent.
- The codebase implementation name for this role is `LMA`.
- The `GMA` is the multi-source aggregation and coordination agent.
- OCaml modules are not ML APIs and not long-running services. They are deterministic planner calls used by services through `dagentsc`.

## Diagrams

All architecture diagrams are PlantUML sources under `docs/presentation/puml/` and are compiled to PNGs in the same folder.
