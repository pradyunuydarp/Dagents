# Dagents Model Service

Project-generic anomaly-model training and inference service for Dagents consumers such as Watchdog, Datalytics, and future agents.

## Purpose

This service owns reusable ML pipeline concerns that should not be hardcoded into individual product services:

- benchmark dataset loading
- preprocessing and PCA
- unified train/validation/test orchestration
- cross-validation and leave-one-out evaluation modes
- PyTorch model training
- hyperparameter search
- artifact persistence

## Current Focus

The first implemented family is tabular anomaly detection using reconstruction-based PyTorch models:

- `autoencoder`
- `variational_autoencoder`

## Public API

- `GET /api/v1/health`
- `GET /api/v1/datasets`
- `POST /api/v1/train`

The service also has checks, model jobs and a source catalog; the
[API reference](../../docs/reference/api/framework-services.md#model-service-sources)
lists every endpoint. The source endpoints show each connection option's value
as `[redacted]`, except `role`, `sslmode` and `connect_timeout`, so their
answers never contain a password.

## Supported Benchmark Datasets

- `kddcup99_http`
- `creditcard_openml`
- `mammography_openml`

## Local Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## CLI Training

```bash
python -m app.ml.train
```

## Tests

Run the suite from the repository root:

```bash
PYTHONPATH=.:services/model-service .venv/bin/python \
  -m unittest discover -s services/model-service/tests -t services/model-service/tests -v
```

`tests/test_postgres_source_e2e.py` tests a Postgres source end to end. It
starts the service with uvicorn on a free loopback port and calls it over
HTTP. It registers a Postgres source with the login and password of a scratch
database and checks that no source answer shows the login, the password or the
database name. Then an asynchronous `autoencoder` model job trains on the
source and writes its model file, and the classification, regression and
forecasting checks score models on it. The table is built so that each check
has a known result: the class label is a threshold on a feature, the
regression label is a linear function of two features, and the forecast series
is a sine wave.

It does not cover the Postgres `role` connection option, a source sent in a
request as `dataset.source` instead of a registered one, a request's own
`max_rows` limit, or the MongoDB and object storage adapters.

It needs a PostgreSQL 16 server and a superuser connection string over TCP
with a password:

```bash
export DAGENTS_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres
```

The test creates a scratch database and a login role, and drops both
afterwards. Without the variable the class skips with a message that starts
with `database test skipped`. The `framework-services` CI job runs it against a
PostgreSQL 16 service container and fails on that skip. The service imports
torch when it starts, so the test waits up to 120 seconds for its health check;
the class takes about 45 seconds on a 4-CPU machine.
