# Dagents Pipeline Service

Reusable pipeline orchestration service for Dagents consumers.

## Purpose

This service provides a lightweight execution layer for repeatable multi-step data workflows:

- register named pipelines
- execute stored pipelines against JSON payloads
- inspect recent runs
- apply simple data-analysis steps such as filtering, projection, and summarization

## Built-in Step Kinds

- `enrich_context`
- `filter_items`
- `summarize_items`
- `project_fields`

## API

- `GET /api/v1/health`
- `GET /api/v1/pipelines`
- `POST /api/v1/pipelines`
- `POST /api/v1/pipelines/{pipeline_id}/runs`
- `GET /api/v1/runs`
- `GET /api/v1/runs/{run_id}`
