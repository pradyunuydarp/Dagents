# Dagents

Shared agentic framework extracted from product repos such as Watchdog and intended to be reused across future systems like Datalytics.

## Current Scope

- `agents/`: reusable LMA and GMA control-plane services with layered application boundaries
- `services/core-service/`: framework service catalog and topology entrypoint
- `services/model-service/`: generic anomaly-model training and inference service
- `services/pipeline-service/`: reusable JSON pipeline orchestration and data-analysis service
- `contracts/grpc/dagents/agents/v1/`: shared control-plane protobuf contract
- `docs/agents/`: architecture notes for the LMA/GMA control plane

## Design Intent

This repo is the generic home for:

- local and global monitoring agents
- reusable agent orchestration patterns
- shared contracts between agents
- reusable pipeline execution services
- data-analysis and model-training utilities that should not live inside a single product codebase

## Layout

```text
agents/
contracts/
docs/
services/
```

## Implemented Services

- LMA: bundle deployment, run execution, telemetry publication, local run history
- GMA: registration, heartbeat handling, deployment planning, deployment sync, telemetry aggregation, run dispatch
- Core service: framework service catalog and topology endpoint
- Pipeline service: definition registry, dependency-aware execution, filtering, summarization, projection

## Container Deployment

The repo now includes:

- separate Docker images for `lma` and `gma`
- Docker images for the framework `core-service`, `model-service`, and `pipeline-service`
- a top-level `docker-compose.yml` for multi-container local or cloud-like deployment

## First Consumers

- Watchdog
- Datalytics
