# Dagents

Shared agentic framework extracted from product repos such as Watchdog and intended to be reused across future systems like Datalytics, with a primary focus on ML automation and workload generation.

## Current Scope

- `agents/`: reusable LMA and GMA ML-orchestration services with layered application boundaries
- `agents/common/extensions/`: the interface consumer apps extend the framework through
- `bindings/ocaml/`: the typed planning layer — validation, DAG planning, model routing, manifest
  rendering, ethical-restriction planning, and federated round governance
- `apps/healthcare-demo/`: a self-contained stroke-triage demo app built on the framework
- `services/core-service/`: framework service catalog and Kubernetes manifest generator
- `services/model-service/`: generic anomaly-model training and inference service
- `services/pipeline-service/`: reusable JSON pipeline orchestration and ML workflow service
- `contracts/grpc/dagents/agents/v1/`: shared control-plane protobuf contract
- `docs/agents/`: architecture notes for the LMA/GMA control plane

## Design Intent

This repo is the generic home for:

- source-level and assimilated-data model agents
- reusable ML orchestration patterns
- shared contracts between agents
- reusable pipeline execution services
- data-analysis and model-training utilities that should not live inside a single product codebase
- parameterized Kubernetes manifest generation for framework workloads
- governance that decides what protection a request needs and enforces it with an audit trail
- federated round control: who may take part, when aggregation is permitted, what a candidate must
  clear before release

## Layout

```text
agents/
apps/
bindings/
contracts/
docs/
env/
services/
```

## Implemented Services

- LMA: per-source dataset profiling and model execution
- GMA: assimilated-data profiling and aggregate model execution
- Core service: framework service catalog, topology endpoint, workload compilation, and Kubernetes manifest generation
- Pipeline service: definition registry, async pipeline runs, filtering, summarization, dataset profiling, and model job execution
- Model service: train jobs, classification/regression/forecasting checks, and source-backed dataset execution

## Governance and Federation

Two framework capabilities are worth calling out, because they are what a consumer would otherwise
rebuild per project:

- **Governance (GRAILS)** — an Ethical-Restriction Rails planner decides what protection a request
  needs from the data's sensitivity, the requester's trust, and how much is being asked for. An
  Ethical Guard applies that decision and records it. The planner is typed OCaml, so adding a
  sensitivity level or a strategy fails to compile until every combination is handled; the Guard is
  Python, because enforcing needs real data and an audit sink. The Guard fails closed.
- **Federated rounds** — signed round manifests, site eligibility, quorum, aggregation readiness,
  and release gates. Dagents governs a federation rather than running one: the distributed protocol
  belongs to a specialist runtime behind an adapter. Aggregation produces a candidate, never a
  release.

Consumers reach both through `agents/common/extensions` by contributing feature contracts, data
classifications, condition packs, pipeline steps, and model adapters. See
[`apps/healthcare-demo/`](apps/healthcare-demo/README.md) for a worked example.

## Container Deployment

The repo now includes:

- separate Docker images for `lma` and `gma`
- Docker images for the framework `core-service`, `model-service`, and `pipeline-service`
- a top-level `docker-compose.yml` for multi-container local or cloud-like deployment

Start the local stack with committed env files:

```bash
docker compose --env-file env/.env.compose up --build
```

Service runtime config is sourced from `env/.env.shared` plus per-service env files under `env/`.

## Architecture Studies

- OCaml adoption study and phased materialization plan: [`docs/architecture/ocaml-adoption-plan.md`](docs/architecture/ocaml-adoption-plan.md)
- PlantUML sources and rendered diagrams: `docs/diagrams/puml/`
- Datalytics backend migration guide: [`docs/integrations/datalytics-backend-guide.md`](docs/integrations/datalytics-backend-guide.md)

## Demo Apps

- [`services/nl2sql-demo/`](services/nl2sql-demo/README.md) — natural language to SQL, showing the
  service-integration shape
- [`apps/healthcare-demo/`](apps/healthcare-demo/README.md) — governed federated stroke triage
  across three hospitals, showing the extension shape. Synthetic data only; not clinically
  validated, and not a medical device.

## First Consumers

- Watchdog
- Datalytics
