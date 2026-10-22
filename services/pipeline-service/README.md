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

The source endpoints show each connection option's value as `[redacted]`,
except `role`, `sslmode` and `connect_timeout`, so their answers never contain
a password. The pipeline endpoints do the same for a source that a step carries
itself in `config.dataset_input.source`. A run reads the stored definition,
which keeps the real values.

Runtime settings are loaded from `env/.env.shared` and `env/.env.pipeline-service`.

## Tests

Run the suite from the repository root:

```bash
PYTHONPATH=.:services/pipeline-service .venv/bin/python \
  -m unittest discover -s services/pipeline-service/tests -t services/pipeline-service/tests -v
```

`tests/test_postgres_source_e2e.py` tests a Postgres source end to end. It
starts the service with uvicorn on a free loopback port and calls it over
HTTP. It registers a Postgres source with the login and password of a scratch
database, checks that no source answer shows the login, the password or the
database name, and runs an asynchronous pipeline on the source: a
`profile_dataset` step and a `random_forest` `run_model_job` step. The run must
finish with 512 records, the source's `max_records`, from a 600-row table. A
second pipeline carries the same source in a step's `dataset_input.source`:
none of the six pipeline answers (register and list by both paths, fetch, and
check again) shows the login, and a run of it still reads the 512 records.

It does not cover the Postgres `role` connection option, the MongoDB and
object storage adapters, or a `run_model_job` step that trains a model: that
step estimates its metrics and trains nothing.

It needs a PostgreSQL 16 server and a superuser connection string over TCP
with a password:

```bash
export DAGENTS_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres
```

The test creates a scratch database and a login role, and drops both
afterwards. Without the variable the class skips with a message that starts
with `database test skipped`. The `framework-services` CI job runs it against a
PostgreSQL 16 service container and fails on that skip.
