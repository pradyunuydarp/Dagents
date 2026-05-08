# Dagents App Architecture Walkthrough

Dagents is a reusable framework for ML automation, data engineering workflows, local/global agent orchestration, and workload generation. The demo should explain the app as a set of cooperating runtime services plus one functional planner layer.

## Runtime Stack

```text
Client / Demo Scripts
        |
        v
Core Service  ---> Kubernetes workload plans and topology
        |
        +--> Pipeline Service ---> JSON workflow registration and execution
        |
        +--> Model Service ------> reusable ML training and checks
        |
        +--> LMA ----------------> local source-scoped profiling/model runs
        |
        +--> GMA ----------------> fleet-wide coordination and aggregate runs
        |
        +--> dagentsc -----------> OCaml functional planner layer
```

## Main Components

### `agents/lma`

The Local Monitoring Agent runs close to a source boundary such as a tenant, service, event stream, or local environment. It profiles source-scoped data, runs local models, and emits model-run metadata. In the demo, LMA represents the “local execution” side of the framework.

### `agents/gma`

The Global Monitoring Agent coordinates across local agents. It registers LMA capabilities, accepts heartbeat/telemetry data, profiles assimilated datasets, and runs aggregate model workflows. In the demo, GMA represents the “global coordination” side.

### `services/core-service`

Core service is the framework facade for service catalog, topology, and workload planning. It can call the OCaml `dagentsc` manifest planner and fall back to Python generation when the CLI is unavailable. In the demo, it shows how typed workload specs become deployment YAML.

### `services/pipeline-service`

Pipeline service registers JSON workflows and executes them over payloads. Its validation path can call `dagentsc pipeline compile`, which means the service can reuse the OCaml DAG planner instead of duplicating all dependency logic.

### `services/model-service`

Model service owns reusable ML training and evaluation. It stays Python-first because PyTorch, preprocessing, and numerical workloads belong in the Python ecosystem. In the demo, it shows that the functional planner does not replace ML execution; it plans around it.

### `bindings/ocaml`

The OCaml layer contains pure functional planner modules. It validates and translates structured specs into execution plans. It does not connect to databases, train models, or deploy Kubernetes resources.

## Docker Compose Role

`docker-compose.yml` starts the local app stack:

- `gma` on port `8020`
- `lma` on port `8010`
- `model-service` on port `8000`
- `pipeline-service` on port `8030`
- `core-service` on port `8040`
- optional Spring services on ports `8050` and `8060`

Use this command:

```bash
docker compose --env-file env/.env.compose up -d --build
```

## End-to-End Flow To Explain

1. A user defines a source, records, or workflow.
2. OCaml validates and plans deterministic pieces.
3. Pipeline, LMA, GMA, and model-service execute runtime work.
4. Core service renders workload plans for deployment.
5. The same typed contracts keep local and aggregate behavior aligned.
