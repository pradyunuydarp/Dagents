# Step 7: Call the framework services

This is path A. Your application calls three HTTP services and uses their
answers. You can call them from any language. They do not run the Guard: each
service sees the payload you send it, so send only data you may share with it,
or run the service inside the same boundary as the data.

| Service | Port | Use it to |
|---|---|---|
| core-service | 8040 | Compile a workload plan and render Kubernetes manifests |
| pipeline-service | 8030 | Register a pipeline of steps, validate it and run it on a payload |
| model-service | 8000 | List the model families this runtime can run, train models and run checks |

The framework services use only versioned paths (`/api/v1/...`). Every endpoint
and field is in the API reference for the
[framework services](../reference/api/framework-services.md): the
[workload compiler](../reference/api/framework-services.md#workload-compiler),
[pipeline definitions](../reference/api/framework-services.md#pipeline-definitions),
[pipeline runs](../reference/api/framework-services.md#pipeline-runs) and
[training and checks](../reference/api/framework-services.md#training-and-checks).

## How the NL2SQL demo uses them

The NL2SQL demo turns a question into SQL. It owns its user interface and its
SQL generation, and calls Dagents for validation, planning, service checks and
workload compilation:

![How the NL2SQL demo calls the Dagents services](../presentation/puml/nl2sql_dagents_integration.png)

Its `DagentsOrchestrator` makes plain HTTP calls. For example, it compiles its
own workload like this:

<!-- source: services/nl2sql-demo/backend/app/services/dagents_orchestrator.py -->
```python
    def _compile_demo_workload(self) -> dict[str, Any]:
        return self._post_json(
            f"{self.settings.core_service_url}/api/v1/workloads:compile",
            {
                "plan_id": "nl2sql-demo",
                "namespace": "dagents-demo",
                "include_services": True,
                "include_config_maps": True,
                "components": [
                    {
                        "name": "nl2sql-demo-backend",
                        "image": "dagents/nl2sql-demo-backend:local",
                        "kind": "Deployment",
                        "replicas": 1,
                        "ports": [{"name": "http", "container_port": self.settings.api_port}],
                        "env": [{"name": "NL2SQL_DEFAULT_MODEL_ID", "value": self.settings.default_model_id}],
                        "generated_resources": ["Service", "ConfigMap"],
                        "config_map_data": {"dagents.integration": "core,pipeline,model,lma,gma,ocaml"},
                    }
                ],
            },
        )
```

It reads each service's address from its settings, never from a literal in the
code.

## 1. Compile a workload plan (core-service)

Describe each component you want to run. Save this as `workload.json`:

```json
{
  "plan_id": "returns-pilot",
  "namespace": "retail",
  "include_services": true,
  "include_config_maps": true,
  "components": [
    {
      "name": "store-north-agent",
      "image": "example/returns-store-agent:1.0.0",
      "kind": "Deployment",
      "replicas": 1,
      "ports": [{"name": "http", "container_port": 8010}],
      "env": [{"name": "STORE_ID", "value": "store-north"}],
      "generated_resources": ["Service", "ConfigMap"],
      "config_map_data": {"classification_id": "retail-orders"}
    }
  ]
}
```

Compile it, then fetch the stored plan and write its YAML to a file:

```bash
curl -s -X POST http://127.0.0.1:8040/api/v1/workloads:compile \
  -H 'Content-Type: application/json' -d @workload.json > plan.json
curl -s http://127.0.0.1:8040/api/v1/workload-plans/returns-pilot \
  | python3 -c "import json, sys; print(json.load(sys.stdin)['combined_yaml'])" > returns-pilot.yaml
```

`plan.json` holds one entry per component, with its Deployment, Service and
ConfigMap as separate YAML strings, and `combined_yaml` with all of them.
`returns-pilot.yaml` starts:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: store-north-agent
  namespace: retail
spec:
  replicas: 1
  selector:
    matchLabels:
      app: store-north-agent
  template:
    metadata:
      labels:
        app: store-north-agent
    spec:
      containers:
      - name: main
        image: example/returns-store-agent:1.0.0
        ports:
        - name: http
          containerPort: 8010
        env:
        - name: STORE_ID
          value: "store-north"
        resources:
          requests:
            cpu: 250m
            memory: 256Mi
          limits:
            cpu: 1
            memory: 1Gi
```

It goes on with a `Service` on port 8010 and a `ConfigMap` named
`store-north-agent-config`. Apply it with `kubectl apply -f returns-pilot.yaml`.

core-service renders the YAML with the planner when it can reach `dagentsc`,
and with a Python fallback when it cannot. Both give the same YAML, and the
core-service tests check that. Deployed containers have `dagentsc` on `PATH`,
so they use the planner. Plans are kept in memory: after a restart,
`GET /api/v1/workload-plans/returns-pilot` answers `404` with
`{"detail":"Unknown workload plan: returns-pilot"}`. Use
`POST /api/v1/manifests/pods` to render YAML without storing a plan.

## 2. Run a pipeline (pipeline-service)

A pipeline is a list of steps that depend on each other. The service checks
that the steps form a valid graph (no duplicate step, no missing dependency, no
cycle) and runs them in dependency order. It runs six kinds of step: `enrich_context`, `filter_items`, `summarize_items`, `project_fields`,
`profile_dataset` and `run_model_job`. Save this as `pipeline.json`:

```json
{
  "pipeline_id": "returns-summary",
  "description": "Count returned orders and their mean value.",
  "steps": [
    {
      "step_id": "keep_returned",
      "kind": "filter_items",
      "config": {
        "items_field": "orders",
        "conditions": [{"field": "returned", "operator": "eq", "value": true}]
      }
    },
    {
      "step_id": "summarize",
      "kind": "summarize_items",
      "depends_on": ["keep_returned"],
      "config": {
        "items_field": "orders",
        "target_field": "summary",
        "aggregations": [
          {"operation": "count", "alias": "returned_orders"},
          {"operation": "avg", "field": "order_value", "alias": "mean_value"}
        ]
      }
    }
  ]
}
```

Register it, then run it on a payload:

```bash
curl -s -X POST http://127.0.0.1:8030/api/v1/pipelines -H 'Content-Type: application/json' -d @pipeline.json
curl -s -X POST http://127.0.0.1:8030/api/v1/pipelines/returns-summary/runs -H 'Content-Type: application/json' -d '{
  "payload": {"orders": [
    {"order_value": 50.0, "category": "shoes", "returned": false},
    {"order_value": 110.0, "category": "clothing", "returned": true},
    {"order_value": 180.0, "category": "shoes", "returned": true}
  ]}
}'
```

The run answers, shortened:

```json
{
    "pipeline_id": "returns-summary",
    "status": "completed",
    "final_payload": {
        "orders": [
            {"order_value": 110.0, "category": "clothing", "returned": true},
            {"order_value": 180.0, "category": "shoes", "returned": true}
        ],
        "summary": {"returned_orders": 2, "mean_value": 145.0}
    },
    "step_results": [
        {"step_id": "keep_returned", "kind": "filter_items", "status": "completed",
         "output": {"items_field": "orders", "input_count": 3, "output_count": 2}},
        {"step_id": "summarize", "kind": "summarize_items", "status": "completed",
         "output": {"target_field": "summary", "summary_keys": ["mean_value", "returned_orders"]}}
    ],
    "error": null
}
```

The payload in this example has already been through a store's Guard: the order
values are rounded to the nearest 10.

## 3. Choose a model family (model-service and the planner)

The planner chooses a model family for a task. The model service says which
families this runtime can run. Ask both:

```bash
$DAGENTSC_BIN model route --task classification --output json
curl -s http://127.0.0.1:8000/api/v1/model-families
```

The planner picks `random_forest`, with `xgboost` and `naive_bayes` as other
candidates (output in [Step 1](01-setup.md#2-build-the-planner)). The model
service lists, for classification, the implemented families `linear`,
`naive_bayes` and `random_forest`, each run by scikit-learn. The planner can
choose a family this runtime cannot run, such as `xgboost`. The model service
lists it as `planned`, with what it would need. Check the list before you use a
family.

The model service also trains models (`POST /api/v1/model-jobs`) and runs
one-shot checks (`POST /api/v1/checks/classification`). Both read the dataset
you send, so run the model service inside the data's boundary.

## What can go wrong

| What you see | Why | What to do |
|---|---|---|
| `422 Unprocessable Entity` | The body does not match the request model; the answer's `detail` names the field | Compare the body with the API reference, or open the service's `/docs` page |
| `404` with `Unknown workload plan` | The service restarted and lost its plans, or the `plan_id` differs | Compile the plan again |
| A pipeline step kind is refused | The pipeline service runs only its six built-in kinds | Run your own step in your application, before or after the pipeline |
| The manifests differ between your laptop and a container | One used the Python fallback and the other the planner | Set `DAGENTSC_BIN` locally. The test suites run both and expect the same YAML |

Next: [Step 8: Deploy](08-deploy.md).
