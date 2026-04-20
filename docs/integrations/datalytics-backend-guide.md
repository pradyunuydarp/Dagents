# Dagents Backend Integration Guide for Datalytics

## Purpose

This guide describes how an external product such as Datalytics should adopt Dagents as its backend framework layer.

Use Dagents when you need:

- reusable local and aggregate ML orchestration
- source-agnostic dataset ingestion
- standard model execution and evaluation APIs
- reusable workflow orchestration
- declarative Kubernetes workload generation

The target integration model is:

- Datalytics keeps product-specific business logic, UI, and tenant semantics
- Dagents owns reusable agent control, source adapters, ML execution, pipeline execution, and workload planning

## Runtime Layout

The framework is split into:

- `agents/lma`: local source-scoped execution
- `agents/gma`: aggregate and fleet-wide coordination
- `services/model-service`: training, checks, and model jobs
- `services/pipeline-service`: reusable workflow execution
- `services/core-service`: service catalog, topology, and Kubernetes manifest generation
- `services/spring-services`: Spring Boot control/core facades
- `bindings/ocaml`: typed compiler and manifest-planning modules

## Environment Model

Dagents no longer relies on hardcoded ports or service addresses in runtime configuration. Python services load env files automatically, and Docker Compose is expected to be started with an explicit compose env file.

### Env files

Committed env files live under `env/`:

- `env/.env.shared`
- `env/.env.lma`
- `env/.env.gma`
- `env/.env.model-service`
- `env/.env.pipeline-service`
- `env/.env.core-service`
- `env/.env.spring-control-service`
- `env/.env.spring-core-service`
- `env/.env.compose`

### Compose startup

Use:

```bash
docker compose --env-file env/.env.compose up --build
```

`env/.env.compose` drives compose-time host/container port bindings. The per-service `env_file` entries in `docker-compose.yml` drive container runtime configuration.

### Required env fields

Shared URLs in `env/.env.shared`:

- `LMA_PUBLIC_URL`
- `GMA_PUBLIC_URL`
- `MODEL_SERVICE_PUBLIC_URL`
- `PIPELINE_SERVICE_PUBLIC_URL`
- `CORE_SERVICE_PUBLIC_URL`
- `SPRING_CONTROL_SERVICE_PUBLIC_URL`
- `SPRING_CORE_SERVICE_PUBLIC_URL`
- `LMA_INTERNAL_URL`
- `GMA_INTERNAL_URL`
- `MODEL_SERVICE_INTERNAL_URL`
- `PIPELINE_SERVICE_INTERNAL_URL`
- `CORE_SERVICE_INTERNAL_URL`
- `SPRING_CONTROL_SERVICE_INTERNAL_URL`
- `SPRING_CORE_SERVICE_INTERNAL_URL`

Python runtime network fields:

- `LMA_API_HOST`
- `LMA_API_PORT`
- `LMA_GMA_ENDPOINT`
- `GMA_API_HOST`
- `GMA_API_PORT`
- `API_HOST`
- `API_PORT`

Core-service dependency fields:

- `LMA_URL`
- `GMA_URL`
- `MODEL_SERVICE_URL`
- `PIPELINE_SERVICE_URL`

Model-service fields:

- `MODEL_ARTIFACT_DIR`
- `RAW_DATA_DIR`
- `MODEL_DEVICE`
- `RANDOM_SEED`

Spring facade fields:

- `SERVER_PORT`
- `DAGENTS_CONTROL_APP_NAME`
- `DAGENTS_CONTROL_ENVIRONMENT`
- `DAGENTS_CORE_APP_NAME`
- `DAGENTS_CORE_ENVIRONMENT`
- `DAGENTS_CORE_CONTROL_SERVICE_URL`
- `DAGENTS_CORE_LMA_URL`
- `DAGENTS_CORE_GMA_URL`
- `DAGENTS_CORE_MODEL_SERVICE_URL`
- `DAGENTS_CORE_PIPELINE_SERVICE_URL`
- `DAGENTS_OCAML_MANIFEST_CLI`

Compose binding fields in `env/.env.compose`:

- `LMA_HOST_PORT`
- `GMA_HOST_PORT`
- `MODEL_SERVICE_HOST_PORT`
- `PIPELINE_SERVICE_HOST_PORT`
- `CORE_SERVICE_HOST_PORT`
- `SPRING_CONTROL_SERVICE_HOST_PORT`
- `SPRING_CORE_SERVICE_HOST_PORT`
- `LMA_API_PORT`
- `GMA_API_PORT`
- `MODEL_SERVICE_CONTAINER_PORT`
- `PIPELINE_SERVICE_CONTAINER_PORT`
- `CORE_SERVICE_CONTAINER_PORT`
- `SPRING_CONTROL_SERVICE_CONTAINER_PORT`
- `SPRING_CORE_SERVICE_CONTAINER_PORT`

## Extension Model

Dagents is designed around replaceable ports and high-cohesion modules. Prefer implementing interfaces and registering them through composition roots instead of branching inside service logic.

### Primary Python ports

Defined in [ports.py](/Users/pradyundevarakonda/Developer/Dagents/agents/common/application/ports.py):

- `ConnectionResolver`
- `SourceCatalog`
- `SourceAdapter`
- `SourceResolver`
- `JobExecutor`
- `ArtifactStore`

Additional repositories are defined per bounded context:

- LMA state and messaging in [state.py](/Users/pradyundevarakonda/Developer/Dagents/agents/lma/infrastructure/state.py) and [messaging.py](/Users/pradyundevarakonda/Developer/Dagents/agents/lma/infrastructure/messaging.py)
- GMA control-plane persistence in [persistence.py](/Users/pradyundevarakonda/Developer/Dagents/agents/gma/infrastructure/persistence.py)
- Pipeline repositories in [pipeline_service.py](/Users/pradyundevarakonda/Developer/Dagents/services/pipeline-service/app/services/pipeline_service.py)
- Model-job persistence in [training_service.py](/Users/pradyundevarakonda/Developer/Dagents/services/model-service/app/services/training_service.py)

### How to extend interfaces

Implement a new adapter when a new data source or storage backend is needed.

Example pattern for a new source kind:

1. Add a new discriminated source model under `agents/common/domain/sources.py`.
2. Implement `SourceAdapter` with `validate`, `discover`, and `scan`.
3. Register the adapter in `DefaultSourceResolver`.
4. Keep source credentials indirect through `ConnectionRef`.
5. Return normalized `RecordBatch` values only. Do not leak driver-native rows into application logic.

### How polymorphism is used

Polymorphism is registry-driven, not inheritance-heavy.

- `SourceAdapter.kind` selects connector behavior by source kind.
- repositories are injected behind protocols, so in-memory and durable implementations are swappable
- manifest compilation is isolated behind a compiler boundary
- Spring and OCaml paths can replace Python implementations without changing REST contracts

This is the intended pattern for Datalytics too:

- keep product-specific policy in thin application services
- dispatch to reusable framework ports using composition, not conditional branching

### How to create custom classes

Use narrow classes that own one responsibility:

- `DatalyticsSnowflakeAdapter` implements `SourceAdapter`
- `PostgresArtifactStore` implements the artifact persistence contract
- `DatalyticsTelemetrySink` implements telemetry forwarding
- `DatalyticsManifestPolicy` enriches `WorkloadCompileRequest` before calling core-service

Do not:

- add Datalytics-specific conditionals to `DefaultSourceResolver`
- put raw secrets into `SourceSpec`
- overload generic DTOs with product-only fields

## Source and Dataset Contracts

The standard source contract is `SourceSpec`.

Core fields:

- `source_id`
- `kind`
- `connection_ref`
- `selection`
- `format`
- `schema_hint`
- `batching`
- `checkpoint`
- `options`

Supported kinds:

- `inline`
- `postgres`
- `mongodb`
- `object_storage`

`DatasetInput` supports:

- `inline_records`
- `source`
- `source_id`

That means Datalytics can pass data in three ways:

- inline for tests and synthetic jobs
- by reference to a stored source
- by embedding a full source spec in a one-off request

## Standard REST APIs

### LMA

Health and control:

- `GET /api/v1/health`
- `POST /bundles/deploy`
- `POST /run`
- `GET /runs`

Source and ML:

- `POST /api/v1/sources`
- `GET /api/v1/sources`
- `GET /api/v1/sources/{source_id}`
- `POST /api/v1/sources/{source_id}:validate`
- `POST /api/v1/datasets:profile`
- `POST /api/v1/model-jobs`
- `GET /api/v1/model-jobs`

Important request fields:

- `scope_id`
- `dataset`
- `feature_fields`
- `label_field`
- `task_type`
- `model_family`
- `hyperparameters`

Important response fields:

- `dataset_profile.record_count`
- `dataset_profile.partition_count`
- `run.scope_kind`
- `run.model_family`
- `run.metrics`
- `run.artifact_uri`

### GMA

Control-plane:

- `PUT /api/v1/agents/{agent_id}/registration`
- `POST /api/v1/agents/{agent_id}/heartbeats`
- `POST /api/v1/agents/{agent_id}/telemetry`
- `PUT /api/v1/agents/{agent_id}/desired-deployment`
- `POST /api/v1/agents/{agent_id}/deployment-sync`
- `GET /api/v1/agents`
- `GET /api/v1/agents/{agent_id}`
- `GET /overview`

Aggregate source and ML:

- `POST /api/v1/sources`
- `GET /api/v1/sources`
- `GET /api/v1/sources/{source_id}`
- `POST /api/v1/sources/{source_id}:validate`
- `POST /api/v1/datasets:profile`
- `POST /api/v1/model-jobs`
- `GET /api/v1/model-jobs`

### Model service

Catalog and training:

- `GET /api/v1/health`
- `GET /api/v1/datasets`
- `POST /api/v1/train`
- `POST /api/v1/model-jobs`
- `GET /api/v1/model-jobs`
- `GET /api/v1/model-jobs/{job_id}`

Evaluation checks:

- `POST /api/v1/checks/classification`
- `POST /api/v1/checks/regression`
- `POST /api/v1/checks/forecasting`

Request shape for checks:

- `dataset` or `dataset_name`
- `feature_fields`
- `label_field` for classification/regression
- `sequence_field` and ordered numeric features for forecasting
- `model_family`
- `test_size`
- `hyperparameters`

Response shape for checks:

- classification: `accuracy`, `precision`, `recall`, `f1`, `roc_auc`, `average_precision`
- regression: `rmse`, `mae`, `r2`
- forecasting: `rmse`, `mae`, `r2`, `series_length`

### Pipeline service

- `GET /api/v1/health`
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

Built-in step kinds:

- `enrich_context`
- `filter_items`
- `summarize_items`
- `project_fields`
- `profile_dataset`
- `run_model_job`

### Core service

- `GET /api/v1/health`
- `GET /api/v1/services`
- `GET /api/v1/topology`
- `POST /api/v1/manifests/pods`
- `POST /api/v1/workloads:compile`
- `GET /api/v1/workload-plans/{plan_id}`

## Kubernetes Manifest Generation

The per-resource manifest extensions in this section are implemented and verified on the Python core-service API in `services/core-service`.

The Spring/OCaml manifest path currently remains compatible with the base workload contract and should be treated as a follow-on parity task before relying on the extended per-resource fields through that path.

The Python workload compiler request shape is centered on `WorkloadCompileRequest` and `WorkloadComponent`.

Core workload fields:

- `plan_id`
- `namespace`
- `components`
- `include_services`
- `include_config_maps`

Component fields:

- `name`
- `image`
- `kind`
- `replicas`
- `schedule`
- `ports`
- `env`
- `args`
- `resources`
- `generated_resources`
- `service_account_name`
- `service_type`
- `config_map_data`

### Per-resource generation

`generated_resources` enables component-level resource synthesis.

Supported generated values:

- `Service`
- `ConfigMap`
- `ServiceAccount`

Rules:

- top-level `include_services` and `include_config_maps` still work as defaults
- `generated_resources` adds resource generation per component even if top-level flags are false
- `service_account_name` binds the generated or referenced service account into the workload pod spec
- `config_map_data` is rendered into the generated config map
- `service_type` controls the generated `Service` object

Generated manifest response fields:

- `component_name`
- `kind`
- `deployment_yaml`
- `service_yaml`
- `config_map_yaml`
- `service_account_yaml`
- `combined_yaml`

## Integration Pattern for Datalytics

Recommended adoption order:

1. Register Datalytics data sources through `SourceSpec`.
2. Implement any missing connectors as `SourceAdapter`s.
3. Move reusable ML checks to model-service APIs.
4. Move orchestrated backend jobs into pipeline-service definitions.
5. Use GMA for fleet-wide deployment/telemetry state.
6. Generate Kubernetes manifests from core-service instead of hand-writing product-specific YAML.

### Suggested Datalytics-specific classes

- a Datalytics connection resolver that maps product connection ids to secure credentials
- adapters for any Datalytics-only sources
- a policy layer that translates tenant/workspace metadata into `scope_id`, `scope_kind`, and `SourceSpec`
- a repository layer for durable GMA and pipeline persistence

### What should stay outside Dagents

- Datalytics-specific authorization
- product billing and tenancy policy
- UI workflows
- consumer-specific incident semantics

## Example Flow

1. Datalytics stores a reusable Postgres or object-storage source.
2. LMA profiles the source with `POST /api/v1/datasets:profile`.
3. Datalytics calls model-service checks or submits a `model-job`.
4. Datalytics registers a reusable pipeline definition that chains profile and model steps.
5. GMA tracks deployment intent and telemetry for LMA instances.
6. Core-service compiles Kubernetes resources for the selected agents and supporting services.

## Development Rules for Consumers

- Keep domain models product-agnostic.
- Push infrastructure concerns behind protocols and repositories.
- Prefer adapter registration over `if source.kind == ...` chains.
- Persist only declarative source definitions and connection references, never raw secrets.
- Treat `RecordBatch` as the internal normalization boundary.
- Use env files for all addresses, ports, paths, and external service URLs.
- Keep Kubernetes intent declarative through `WorkloadCompileRequest`.

## Verification Commands

Useful local commands:

```bash
python -m unittest tests.test_manifest_service tests.test_manifest_api -q
python -m unittest agents.tests.test_control_plane agents.tests.test_postgres_api agents.tests.test_agent_api -q
python -m unittest discover -s services/pipeline-service/tests -q
python -m unittest discover -s services/model-service/tests -q
mvn -q test
docker compose --env-file env/.env.compose config
```

For containerized startup validation:

```bash
docker build -t dagents-test-core -f services/core-service/Dockerfile .
docker build -t dagents-test-lma -f agents/lma/Dockerfile .
docker build -t dagents-test-gma -f agents/gma/Dockerfile .
```

This guide is the intended starting point for Datalytics backend migration. The next step after framework stabilization is to map Datalytics domain concepts onto `SourceSpec`, `DatasetInput`, pipeline definitions, and workload plans without copying product-specific logic into Dagents.
