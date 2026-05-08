# Dagents NL2SQL Demo Runbook

## Fast Local Demo

```bash
services/nl2sql-demo/scripts/run_local_demo.sh
```

Open `http://127.0.0.1:5173`.

In a second terminal:

```bash
services/nl2sql-demo/scripts/probe_api.sh
```

Use this mode when you mainly want to show the UI, SQL output, and functional trace.

## Full Compose Demo

```bash
services/nl2sql-demo/scripts/run_compose_demo.sh
```

Open `http://127.0.0.1:5173`.

Probe the backend:

```bash
services/nl2sql-demo/scripts/probe_api.sh
```

Stop the stack:

```bash
services/nl2sql-demo/scripts/stop_compose_demo.sh
```

Use this mode when you want the Dagents service status strip and backend trace to show the full framework stack.

## Functional Layer Outputs

```bash
docs/demo/run_functional_layer_demo.sh
```

The script refreshes output files under `docs/demo/expected/`, including source validation, extraction planning, schema validation, quality evaluation, transform planning, pipeline planning, model routing, and manifest compilation.
