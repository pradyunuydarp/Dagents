# Dagents Core Service

Framework-level service catalog, topology endpoint, and workload compiler for Dagents deployments. The
workload compiler renders Kubernetes manifests and Docker Compose files.

## Purpose

This service gives cloud deployments a stable framework entrypoint for:

- health checks
- service discovery metadata
- runtime topology inspection
- parameterized Kubernetes manifest generation for framework workloads
- Docker Compose files for workloads that join a project Docker Compose already runs

## API

- `GET /api/v1/health`
- `GET /api/v1/services`
- `GET /api/v1/topology`
- `POST /api/v1/manifests/pods`
- `POST /api/v1/manifests/compose`
- `POST /api/v1/workloads:compile`
- `GET /api/v1/workload-plans/{plan_id}`

## Workload Generation

The Python core-service supports per-component generated resources in addition to base workload manifests.

Supported generated resource kinds:

- `Service`
- `ConfigMap`
- `ServiceAccount`

Relevant request fields:

- `generated_resources`
- `service_account_name`
- `service_type`
- `config_map_data`

## Docker Compose files

`POST /api/v1/manifests/compose` renders one Compose file for a list of
components. Each component has an image, a `kind` (`Deployment` or `Job`),
arguments, env files, environment variables, ports, an optional health check
and the services it waits for. The request also gives a `placement`: `own` for
a whole project, or `merge` for a file passed with `-f` after the files of a
project that runs `existing_services`.

The OCaml manifest compiler renders the file through `dagentsc manifest
compile`. It checks names, ports, host addresses, health checks and
dependencies, and when a rule fails it returns the issues with
`compose_yaml: null` and status 200. Published ports listen on `127.0.0.1`
unless a port sets `host_ip`, and every `$` in a value is written as `$$`.

When `dagentsc` cannot be started, `app/services/compose_renderer.py` renders
the same file in Python. `tests/test_compose_manifest.py` checks both
renderers against the golden files in `tests/fixtures/compose/`. When the
planner starts and fails, the route answers 500 instead.

The [API reference](../../docs/reference/api/framework-services.md#core-service-post-api-v1-manifests-compose)
describes every field and issue.

Runtime settings are loaded from `env/.env.shared` and `env/.env.core-service`.
