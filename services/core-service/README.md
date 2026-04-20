# Dagents Core Service

Framework-level service catalog, topology endpoint, and workload compiler for Dagents deployments.

## Purpose

This service gives cloud deployments a stable framework entrypoint for:

- health checks
- service discovery metadata
- runtime topology inspection
- parameterized Kubernetes manifest generation for framework workloads

## API

- `GET /api/v1/health`
- `GET /api/v1/services`
- `GET /api/v1/topology`
- `POST /api/v1/manifests/pods`
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

Runtime settings are loaded from `env/.env.shared` and `env/.env.core-service`.
