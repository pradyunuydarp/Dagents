# NL2SQL Debug Handoff

Date: 2026-05-09

## User-Facing Symptom

The NL2SQL frontend showed:

```text
Model artifact was unavailable; deterministic Dagents demo fallback generated this SQL.
```

This message was not itself a Docker crash. It was frontend text shown when the backend returned `used_fallback: true`.

The real problem was that the backend swallowed the actual model-loading exception and silently returned heuristic SQL. I started changing that so the real exception is logged and returned as `fallback_detail`.

## Errors Encountered While Debugging

### 1. `docker compose ps` failed without the compose env file

Command:

```bash
docker compose ps
```

Error:

```text
The "LMA_HOST_PORT" variable is not set. Defaulting to a blank string.
...
invalid proto:
```

Cause:

`docker-compose.yml` depends on variables from `env/.env.compose`. Running compose without `--env-file env/.env.compose` leaves port mappings blank, which makes the compose config invalid.

Fix started:

- Updated `services/nl2sql-demo/scripts/run_compose_demo.sh` to source `env/.env.compose` before using default ports.
- Updated `services/nl2sql-demo/scripts/probe_api.sh` to also source `env/.env.compose`.

Correct compose form:

```bash
docker compose --env-file env/.env.compose ps
docker compose --env-file env/.env.compose up -d --build ...
```

### 2. Docker log commands appeared to hang

Commands attempted:

```bash
docker compose --env-file env/.env.compose ps
docker compose --env-file env/.env.compose logs --tail=200 nl2sql-demo-backend
docker compose --env-file env/.env.compose logs --tail=120 lma gma pipeline-service model-service core-service
```

Observed behavior:

The commands did not return output within the short polling window before the turn was interrupted. I did not get a usable Docker log snapshot yet.

Likely next step after restart:

```bash
docker compose --env-file env/.env.compose logs --tail=300 nl2sql-demo-backend
docker compose --env-file env/.env.compose logs --tail=200 lma gma pipeline-service model-service core-service
```

### 3. Backend Docker image did not install optional model dependencies

Observed in code:

`services/nl2sql-demo/backend/Dockerfile` installed only:

```text
services/nl2sql-demo/backend/requirements.txt
```

But model adapters import:

```python
transformers
torch
peft
accelerate
safetensors
sentencepiece
```

Those are listed in:

```text
services/nl2sql-demo/backend/requirements-optional-models.txt
```

Likely failure mode:

Selecting `codet5p_spider_model` or a CodeQwen adapter inside Docker can fail during import/load, causing fallback SQL.

Fix started:

`services/nl2sql-demo/backend/Dockerfile` now copies and installs `requirements-optional-models.txt` by default via:

```dockerfile
ARG INSTALL_OPTIONAL_MODELS=true
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_OPTIONAL_MODELS" = "true" ]; then pip install --no-cache-dir -r requirements-optional-models.txt; fi
```

This needs a Docker rebuild after restart.

### 4. Backend hid the real fallback error

Original behavior:

`NL2SQLModelRegistry.generate()` caught every model-loading exception and returned fallback SQL without exposing or logging the exception.

Fix started:

- Added backend logging in `services/nl2sql-demo/backend/app/services/model_adapters.py`.
- Changed generate result to include `fallback_detail`.
- Added `fallback_detail` to `NL2SQLResponse`.
- Updated the frontend to show the actual fallback detail instead of only the generic message.

After restart, if fallback still happens, the UI/API should show something like:

```text
ImportError: ...
FileNotFoundError: No model config found under ...
OSError: ...
```

That will identify the exact model artifact issue.

### 5. Local test environment was missing FastAPI

Command:

```bash
python3 -m unittest services/nl2sql-demo/tests/test_nl2sql_api.py
```

Error:

```text
ModuleNotFoundError: No module named 'fastapi'
```

The bundled Python runtime also lacked FastAPI.

Temporary verification workaround:

```bash
python3 -m venv /tmp/dagents-nl2sql-test-venv
/tmp/dagents-nl2sql-test-venv/bin/python -m pip install -q -r services/nl2sql-demo/backend/requirements.txt
/tmp/dagents-nl2sql-test-venv/bin/python -m unittest services/nl2sql-demo/tests/test_nl2sql_api.py
```

Result:

```text
Ran 2 tests in 0.013s
OK
```

### 6. Frontend build passed

Command:

```bash
cd services/nl2sql-demo/frontend
npm run build
```

Result:

```text
✓ built
```

## LMA/GMA Clarification

Current NL2SQL integration before my changes:

- It used Dagents functional planners.
- It called pipeline-service.
- It called model-service catalog endpoints.
- It called LMA dataset profiling.
- It called GMA dataset profiling.

But it did not fully exercise the LMA/GMA control-plane model. In particular:

- It did not register the NL2SQL LMA with GMA.
- It did not heartbeat that LMA.
- It did not register/validate NL2SQL schema sources with LMA and GMA.
- It did not run LMA source model jobs.
- It did not run GMA aggregate model jobs.
- It did not plan/sync GMA desired deployment.
- It did not dispatch a GMA aggregate run.

I started expanding `DagentsOrchestrator._append_service_trace()` to use the full suite:

- `GMA register NL2SQL LMA`
- `GMA LMA heartbeat`
- `LMA source registration and validation`
- `GMA source registration and validation`
- `LMA source profile`
- `LMA source model job`
- `GMA assimilated profile`
- `GMA aggregate model job`
- `GMA desired deployment and sync`
- `GMA aggregate run dispatch`
- `GMA fleet overview`

This still needs full Docker verification after restart.

## Files Modified So Far

```text
services/nl2sql-demo/backend/Dockerfile
services/nl2sql-demo/backend/app/api/routes.py
services/nl2sql-demo/backend/app/models.py
services/nl2sql-demo/backend/app/services/dagents_orchestrator.py
services/nl2sql-demo/backend/app/services/model_adapters.py
services/nl2sql-demo/frontend/src/App.tsx
services/nl2sql-demo/scripts/probe_api.sh
services/nl2sql-demo/scripts/run_compose_demo.sh
```

## Verification Completed Before Interruption

Passed:

```bash
bash -n services/nl2sql-demo/scripts/run_compose_demo.sh \
  services/nl2sql-demo/scripts/probe_api.sh \
  services/nl2sql-demo/scripts/stop_compose_demo.sh \
  services/nl2sql-demo/scripts/run_local_demo.sh
```

Passed:

```bash
python3 -m venv /tmp/dagents-nl2sql-test-venv
/tmp/dagents-nl2sql-test-venv/bin/python -m pip install -q -r services/nl2sql-demo/backend/requirements.txt
/tmp/dagents-nl2sql-test-venv/bin/python -m unittest services/nl2sql-demo/tests/test_nl2sql_api.py
```

Passed:

```bash
cd services/nl2sql-demo/frontend
npm run build
```

Not completed:

- Rebuilding Docker images with optional model dependencies.
- Running the full compose demo.
- Running `probe_api.sh` against the rebuilt stack.
- Inspecting actual Docker logs after the rebuild.

## Next Steps After Restart

1. Rebuild and start the full stack:

```bash
services/nl2sql-demo/scripts/run_compose_demo.sh
```

2. Probe the NL2SQL API:

```bash
services/nl2sql-demo/scripts/probe_api.sh
```

The probe now validates that the Dagents trace includes the required LMA/GMA and service steps.

3. If fallback still happens, inspect:

```bash
docker compose --env-file env/.env.compose logs --tail=300 nl2sql-demo-backend
```

Also inspect the API response field:

```json
"fallback_detail": "..."
```

4. If model loading is still too heavy for the local Docker environment, keep `heuristic_demo` selected for the presentation demo and explain it as the deterministic fallback path. The Dagents orchestration trace can still demonstrate the full framework flow independently of GPU/model availability.

## Continuation Update

Date: 2026-05-09

I continued from this handoff and completed the non-Docker verification path.

### Additional Fixes Completed

1. Added a clear Docker-daemon preflight check to:

```text
services/nl2sql-demo/scripts/run_compose_demo.sh
services/nl2sql-demo/scripts/stop_compose_demo.sh
```

Now, when Docker Desktop is not running, the start script fails fast with:

```text
Docker daemon is not reachable. Start Docker Desktop and rerun this script.
```

This replaces the earlier low-level compose failure:

```text
unable to get image 'dagents-pipeline-service': Cannot connect to the Docker daemon ...
```

2. Added a backend unit test that verifies NL2SQL now exercises the LMA/GMA control-plane flow.

New test coverage in:

```text
services/nl2sql-demo/tests/test_nl2sql_api.py
```

The test verifies these trace steps are emitted:

```text
core-service catalog and topology
core-service workload compile
pipeline-service register and run
model-service catalog
GMA register NL2SQL LMA
GMA LMA heartbeat
LMA source registration and validation
GMA source registration and validation
LMA source profile
LMA source model job
GMA assimilated profile
GMA aggregate model job
GMA desired deployment and sync
GMA aggregate run dispatch
GMA fleet overview
```

It also verifies:

- the registered agent id is `nl2sql-lma`;
- the registered capabilities include `model_execution`;
- the LMA model job uses `scope_kind: source`;
- the GMA model job uses `scope_kind: assimilated`.

### Verification Completed After Continuation

Passed:

```bash
/tmp/dagents-nl2sql-test-venv/bin/python -m unittest services/nl2sql-demo/tests/test_nl2sql_api.py
```

Result:

```text
Ran 3 tests
OK
```

Passed:

```bash
cd services/nl2sql-demo/frontend
npm run build
```

Result:

```text
✓ built
```

Passed:

```bash
bash -n services/nl2sql-demo/scripts/run_compose_demo.sh \
  services/nl2sql-demo/scripts/probe_api.sh \
  services/nl2sql-demo/scripts/stop_compose_demo.sh \
  services/nl2sql-demo/scripts/run_local_demo.sh
```

Passed:

```bash
docker compose --env-file env/.env.compose config --quiet
```

### Still Blocked

Full Docker verification is still blocked because Docker Desktop is not running in this session.

Command:

```bash
services/nl2sql-demo/scripts/run_compose_demo.sh
```

Current result:

```text
Docker daemon is not reachable. Start Docker Desktop and rerun this script.
```

### Remaining Docker-Only Steps

After Docker Desktop is running:

```bash
services/nl2sql-demo/scripts/run_compose_demo.sh
services/nl2sql-demo/scripts/probe_api.sh
```

If the probe fails, inspect:

```bash
docker compose --env-file env/.env.compose logs --tail=300 nl2sql-demo-backend
docker compose --env-file env/.env.compose logs --tail=200 lma gma pipeline-service model-service core-service
```

The backend should now expose the actual model fallback cause in:

```json
"fallback_detail": "..."
```
