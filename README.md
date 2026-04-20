# Dagents

Shared agentic framework extracted from product repos such as Watchdog and intended to be reused across future systems like Datalytics, with a primary focus on ML automation and workload generation.

## Current Scope

- `agents/`: reusable LMA and GMA ML-orchestration services with layered application boundaries
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

## Layout

```text
agents/
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

## First Consumers

- Watchdog
- Datalytics
