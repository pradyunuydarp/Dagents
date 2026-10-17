# Step 1: Set up

This step builds the planner, creates a Python environment and starts the
services. At the end, every service answers its health check and the worked
example runs.

## What you need

| Tool | Version | Used for |
|---|---|---|
| Python | 3.11 | The agents, the services and the example |
| OCaml and opam | OCaml 5.1 | Building the planner, `dagentsc` |
| curl | any | Calling the services in this guide |

You do not need Docker, a database or a GPU for Steps 1 to 7.

## 1. Get the code

Dagents is not published as a package, so your application uses a copy of the
repository:

```bash
git clone https://github.com/pradyunuydarp/Dagents.git
cd Dagents
```

Run every command in this guide from the repository root.

## 2. Build the planner

```bash
opam install dune yojson
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && cd -
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
```

`DAGENTSC_BIN` tells every Python process where the planner is. Without it,
Python looks for `dagentsc` on `PATH`. Check that the planner answers:

```bash
$DAGENTSC_BIN model route --task classification --output json
```

Expected output:

```json
{
  "selectedModel": "random_forest",
  "candidates": [ "random_forest", "xgboost", "naive_bayes" ],
  "packagingMode": "inline_service_call"
}
```

## 3. Create the Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install -r dagents/lma/requirements.txt -r dagents/gma/requirements.txt \
  -r services/core-service/requirements.txt -r services/pipeline-service/requirements.txt
```

These packages are enough for Steps 2 to 7, except the model service. The model
service also needs `services/model-service/requirements.txt`, which installs
PyTorch and scikit-learn. On Linux the default PyTorch package brings the CUDA
libraries with it, about 6 GB in all.

The example and the agents import the framework as `dagents.common...`, so
Python must find the repository root. Each command below sets
`PYTHONPATH=.` for that.

## 4. Configuration and ports

Every service reads `env/.env.shared` and its own file in `env/`. A variable
you export yourself takes precedence over the file.

| Service | Port | Env file | What it does |
|---|---|---|---|
| LMA (local agent) | 8010 | `env/.env.lma` | Guard, profiling and model runs at one data source |
| GMA (global agent) | 8020 | `env/.env.gma` | Site enrolment, federated rounds, release decisions |
| pipeline-service | 8030 | `env/.env.pipeline-service` | Pipeline definitions and runs |
| core-service | 8040 | `env/.env.core-service` | Workload plans, Kubernetes manifests and Docker Compose files |
| model-service | 8000 | `env/.env.model-service` | Model families, training and checks |

Each service has a `*_PUBLIC_URL` (the address from your machine) and a
`*_INTERNAL_URL` (the address inside Docker Compose) in `env/.env.shared`. Read
addresses from there. Never write a URL or a port into your code.

## 5. Start the services

Open one terminal per service. In each one, go to the repository root and
export `DAGENTSC_BIN` as in section 2. Then:

```bash
# Terminal 1: the LMA
.venv/bin/python -m uvicorn dagents.lma.main:app --port 8010

# Terminal 2: the GMA
.venv/bin/python -m uvicorn dagents.gma.main:app --port 8020

# Terminal 3: core-service
PYTHONPATH=. .venv/bin/python -m uvicorn app.main:app --app-dir services/core-service --port 8040

# Terminal 4: pipeline-service
PYTHONPATH=. .venv/bin/python -m uvicorn app.main:app --app-dir services/pipeline-service --port 8030
```

The model service starts the same way with `--app-dir services/model-service
--port 8000`, once its requirements are installed.

Check that each one answers:

```bash
curl -s http://127.0.0.1:8010/health
curl -s http://127.0.0.1:8020/health
curl -s http://127.0.0.1:8040/api/v1/health
curl -s http://127.0.0.1:8030/api/v1/health
```

Expected output, one line per service:

```text
{"status":"ok","agent_type":"LMA","deployed_bundles":0,"runs":0,"model_runs":0,"active_bundle":null}
{"status":"ok","agent_type":"GMA","engine":"in-process-simulator"}
{"status":"ok","service":"dagents-core-service","environment":"development","transport":"http"}
{"status":"ok","service":"dagents-pipeline-service","environment":"development","transport":"http"}
```

Every Python service also serves its interactive documentation at `/docs`, for
example <http://127.0.0.1:8020/docs>.

## 6. Run the worked example once

```bash
PYTHONPATH=. .venv/bin/python examples/getting-started/step1_register.py
```

It prints the extension it registered and checks each store's orders. The next
step explains the output. The example does not need the services from
section 5. It uses the framework as a Python library.

## What can go wrong

| What you see | Why | What to do |
|---|---|---|
| `ModuleNotFoundError: No module named 'dagents'` | Python cannot find the repository root | Run from the repository root with `PYTHONPATH=.` |
| Every Guard decision is `deny` with "The planner could not be reached, so the Guard denied the request." | `DAGENTSC_BIN` is not set in that terminal, or points to a missing file | Export it again (section 2). The Guard fails closed on purpose |
| `[Errno 98] error while attempting to bind on address ... address already in use` | Another process uses the port | Stop it, or choose another port with `--port` |
| `opam: command not found`, or `dune` is too old | The OCaml tools are missing | Install opam, then OCaml 5.1, then `opam install dune yojson` |

Next: [Step 2: Describe your data](02-describe-your-data.md).
