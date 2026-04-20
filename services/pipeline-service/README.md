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
