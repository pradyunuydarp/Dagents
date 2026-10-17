# Step 8: Deploy

This step covers what has to be true when your application runs outside your
laptop: the planner in every container that decides, configuration from the
environment, one store and one Guard per source, who may call the agents and
services, where the agents run, and browser access.

## 1. Put the planner in every container that decides

The Guard, round planning and release decisions call `dagentsc`. A container
without it denies every Guard request and stops every round. Build the planner
in a first stage and copy it onto `PATH`, as `services/core-service/Dockerfile`
does:

```dockerfile
FROM ocaml/opam:debian-ocaml-5.0 as ocaml-builder
USER root
RUN apt-get update && apt-get install -y m4 && apt-get clean
USER opam
WORKDIR /home/opam/src
COPY --chown=opam:opam bindings/ocaml ./bindings/ocaml
WORKDIR /home/opam/src/bindings/ocaml
RUN opam install -y dune yojson
RUN opam exec -- dune build ./bin/dagentsc.exe

FROM python:3.11-slim
...
COPY --from=ocaml-builder /home/opam/src/bindings/ocaml/_build/default/bin/dagentsc.exe /usr/local/bin/dagentsc
```

The core, pipeline, LMA, GMA, NL2SQL and stroke demo images do this. An image
built from your own Dockerfile needs the same stage and `COPY` line if its
service calls the planner. Without them its Guard denies every request.
`tests/test_planner_in_images.py` checks the repository's Dockerfiles for it.

An image that installs the `dagents` package from PyPI on Linux x86_64 or
Linux aarch64 gets the planner inside the wheel and needs no OCaml stage
([Install the package](00-install.md#2-check-that-the-package-has-a-planner)).
Add `RUN python -m dagents` after the install: it exits with status 1 when it
finds no planner, so the build stops.

An LMA without the planner also refuses every federated job. A GMA without it
cannot plan a round, decide readiness, evaluate release gates or change a model
version's status, and its release Guard denies every request. A route that only
asks the planner, such as `restrictions:plan` or `rounds:plan`, answers `503`
with the code `planner_unavailable`.

Check a running container with the same command as in Step 1:

```bash
docker compose --env-file env/.env.compose exec core-service dagentsc model route --task classification --output json
```

## 2. Run the framework with Docker Compose

```bash
docker compose --env-file env/.env.compose up --build
```

This starts the agents, the framework services, the Spring services and the
NL2SQL demo on the ports from Step 1. `env/.env.compose` sets the host ports.
Docker Compose is not run in CI, so treat it as a starting point for your own
deployment, not as a tested one.

## 3. Configure from the environment

- **Addresses.** Every service has a `*_PUBLIC_URL` (from the host) and a
  `*_INTERNAL_URL` (inside the Compose network) in `env/.env.shared`, for
  example `GMA_PUBLIC_URL=http://127.0.0.1:8020` and
  `GMA_INTERNAL_URL=http://gma:8020`. Read the one that matches where your code
  runs. Never write a URL or a port into your code.
- **Precedence.** A service loads `env/.env.shared` and its own file, and a
  variable already set in the environment wins. Set production values in the
  environment, and keep the committed files free of secrets. An LMA or a GMA
  from the installed `dagents` package reads no env file, only the
  environment.
- **One connection per source.** Give each source's store its own database
  login, and read it from a variable per source. The stroke demo uses
  `HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`, for example
  `HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL`.
- **Agent settings.** An LMA reads `LMA_*` variables and a GMA reads `GMA_*`
  variables when it starts, not when its module is imported. An LMA serves one
  site when `LMA_SITE_ID` and `LMA_CONDITION_ID` are set. A GMA sends jobs to
  the LMAs over HTTP when `GMA_ENGINE=http`, and calls only the addresses in
  `GMA_SITE_ENDPOINTS`, so an enrolment cannot point it at another host. A
  source that starts later can join while the GMA runs: list it in
  `GMA_JOINABLE_SITE_ENDPOINTS` with the one address it will use, and send that
  address to `PUT /api/v1/federation/sites/{site_id}/address` with the GMA's
  `admin` token. The GMA accepts only the listed address, keeps it in memory,
  and calls the LMA from the next site refresh on. With tokens, the GMA also
  needs the source's entries in `GMA_LMA_TOKENS` and `GMA_SITE_TOKENS` from the
  start. The stroke demo does this when Hospital 290 joins a `--deploy` run:
  core-service renders the new LMA's Compose file, an applier starts it, and
  the app sends its address to the GMA. The full lists are in [`dagents/lma/README.md`](../../dagents/lma/README.md) and
  [`dagents/gma/README.md`](../../dagents/gma/README.md). Tokens and database
  addresses come from the environment and are never committed.

## 4. Keep each source on its own machine

The worked example runs both stores and the coordinator in one process, to keep
it short. In a deployment:

| Runs at each source | Runs at the coordinator |
|---|---|
| The source's store and database login | The round controller and its round records |
| The source's LMA: its Guard and audit chain, its running profile, its job log | The GMA: the release Guard and its audit chain, the model registry |
| The worker and the model kind that run the source's part of a round | The engine that sends jobs to the sources, and the store that holds their clipped updates until aggregation |
| `dagentsc` | `dagentsc` |

A round's model update crosses from the left column to the right one only if
the source's `before_send` Guard permits it. The round's metrics and example
count cross as the source's runner wrote them
([Step 6](06-federated-rounds.md#3-the-sources-side-the-worker)), so the runner
must put only aggregate numbers in them.

## 5. Kubernetes manifests and Compose files

Compile Kubernetes manifests for your own components with core-service
([Step 7](07-framework-services.md#1-compile-a-workload-plan-core-service)) and
apply them with `kubectl apply -f`. To add a service to a project that Docker
Compose already runs, such as one more agent next to the running GMA, ask
core-service for a Compose file
([Render a Docker Compose file](../reference/api/framework-services.md#core-service-post-api-v1-manifests-compose)).
With `"placement": "merge"`, the new service may wait for the services you list
in `existing_services`, and you pass the file after the project's own file.
List every service the project runs, so the planner can refuse a name that is
already in use. A published port listens on `127.0.0.1` unless you set
`host_ip`. From the repository root, with the stack from section 2 running:

```bash
curl -s -X POST http://127.0.0.1:8040/api/v1/manifests/compose \
  -H 'Content-Type: application/json' -d '{
    "placement": "merge",
    "existing_services": ["gma", "lma", "model-service", "pipeline-service", "core-service",
      "spring-control-service", "spring-core-service", "nl2sql-demo-backend", "nl2sql-demo-frontend"],
    "components": [{
      "name": "store-south-agent",
      "image": "example/returns-store-agent:1.0.0",
      "env": [{"name": "STORE_ID", "value": "store-south"}],
      "ports": [{"container_port": 8010, "host_port": 8011}],
      "depends_on": [{"service": "gma", "condition": "service_healthy"}]
    }]
  }' | python3 -c "import json, sys; print(json.load(sys.stdin)['compose_yaml'])" > store-south.yml
docker compose --env-file env/.env.compose -f docker-compose.yml -f store-south.yml up -d store-south-agent
```

A program can apply a rendered file itself, for example to add a service when
a person presses a button. `dagents/common/infrastructure/compose_apply.py` has
two appliers for a file that holds one service. Each takes the `compose_yaml`
text exactly as core-service returned it, so your program never writes Compose:

| Applier | What it runs | What it needs |
|---|---|---|
| `DockerComposeApplier` | `docker compose --file F config --quiet <service>` to check the file, then `docker compose --project-name P --file F up --detach --no-build --pull never --no-deps --wait <service>` | Docker, and the image already built |
| `LocalProcessApplier` | The service's `command`, with its env files and `environment`, as a local process in a new process group; output goes to `<service>.log`, and the process id to `<service>.pid` while it runs | The program in its allowlist. No Docker. |

`--no-deps` starts the new service alone, so the services it needs must already
run. Give the service its own project and the running project's network
(`"placement": "own"`, `"network"` set to that network and
`"external_network": true`), so `docker compose down` on the project does not
remove it. The Docker applier runs `up` even when the service runs already:
Compose keeps a container whose file and image are unchanged, which the
applier reports as adopted, and recreates one started from an older file or
image. When a start fails, it removes the container it created.

Both appliers apply only what the planner renders:

- The Docker applier refuses a service key the manifest compiler never
  renders, such as `privileged`, `pid`, `volumes` or `network_mode`, and a
  top-level network other than `{}` or `external: true`, before Docker reads
  the file. With those keys a file could give the container control of the
  machine that runs Docker.
- The process applier refuses `depends_on`, any key it cannot apply, a
  program that is not in its allowlist, and a `$` that is not written `$$`,
  because it reads no variable from the environment.
- Put secrets in an env file (`env_files` in the request), so the rendered
  file holds none. Each applier reads an `env_file` only at an absolute path
  inside the folders it was given (`env_dirs`), after following symlinks, and
  only as `NAME=VALUE` lines with a bare value (no space, quote, `$`, `#` or
  backslash) or a value in single quotes. `format_env_file` writes that form.
  An error names the line number, never the line.

A failure raises `ComposeApplyError` with a plain message, the last 20 lines of
the tool's output (`output_tail`) and, for a process that exited, its log file
(`log_path`). Neither applier checks the service's health: ask the service. A
process in a new process group outlives a parent that dies without calling
`close`, so whatever supervises your program can stop it with the pid in
`<service>.pid`. The applier tests run the Docker applier with a fake runner,
so they start no container. CI uses the Docker command line only to check
Compose files with `docker compose config`, which needs no running container.
The stroke demo uses both appliers to add a hospital while it runs
([its README](../../apps/healthcare-demo/README.md)).

Both kinds of file are generated and tested, but CI deploys neither. The plan
for checking the Kubernetes manifests on a live cluster is in `TODO.md`.

## 6. Who may call the agents and services

**The agents accept bearer tokens.** An LMA or a GMA started with tokens needs
the header `Authorization: Bearer <token>` on every route except `/health`.
Each token has one scope:

| Agent | Scope | Setting | Held by | May call |
|---|---|---|---|---|
| LMA | `site` | `LMA_SITE_TOKEN` | The site's staff and the site's own application | Every route except the coordinator routes |
| LMA | `coordinator` | `LMA_COORDINATOR_TOKEN` | The GMA, which reads it from `GMA_LMA_TOKENS` | The registration and the job routes, plus the site description, the site summary and the audit chain summary |
| GMA | `admin` | `GMA_ADMIN_TOKEN` | The study operator | Every route except the update upload, which takes only the site's own token |
| GMA | `site`, bound to one site | `GMA_SITE_TOKENS` | That site's LMA, which reads it from `LMA_GMA_TOKEN` | Its own site's update upload, the released model, and the LMA reports (registration, heartbeats, telemetry, deployment sync) for an agent registered with its own site in `scope.site` |

A missing or unknown token answers `401`, and a token of the wrong scope, or a
site token used for another site, answers `403`. A token has at least 16
characters, and the agents compare tokens in constant time. An agent started
without tokens answers every caller, as before tokens existed, and logs a
warning. Set `LMA_REQUIRE_TOKENS=1` and `GMA_REQUIRE_TOKENS=1` in a
deployment, so an agent refuses to start without tokens. The full rules are in
the API reference, under
[Tokens, errors and calls between agents](../reference/api/agents.md#tokens-errors-and-calls-between-agents).

What the tokens do not do:

- **The framework services check no identity.** The core, pipeline and model
  services have no token, API key or login. Run them on a private network or
  behind a gateway that authenticates each caller.
- **A token names a role, not a person.** The `requester` of a Guard request is
  still input to the planner's trust assessment, not proof of identity.
- **A GMA on the simulator takes a posted result as sent from an `admin`
  caller.** A result with `"code_verified": true` counts toward readiness when
  the round selected that site, and a result from a site the round did not
  select answers `403`. Its release evaluations record `metrics_source:
  request`, so no approval can rest on them. With the HTTP engine, every result
  comes from a site's LMA, and a posted result answers `409`.

## 7. Run the agents in your process or as separate processes

The same client code reaches the agents in two ways
(`dagents/common/infrastructure/agent_client.py`):

| | In process | Separate processes |
|---|---|---|
| Built with | `AgentNetwork.in_process()`, then `network.add_app(name, create_app(...))` for each agent | `AgentNetwork.over_http({name: base_url, ...})`; each agent runs under uvicorn with its settings |
| A call goes through | The agent's real routes, request checks and token checks, through `httpx.ASGITransport`, without a socket | HTTP, with one kept-alive client per agent |
| Use it for | One application that hosts its agents, such as a demo on one server | Sources on their own machines |
| Example | `examples/getting-started/shop/agents.py` ([Step 6](06-federated-rounds.md#6-run-the-round-through-the-lma-and-gma)); the stroke demo's published API (`apps/healthcare-demo/backend/app/services/agents.py`) | `dagents-lma` and `dagents-gma` from the installed package, or `PYTHONPATH=. uvicorn dagents.lma.main:app` and `dagents.gma.main:app` in a clone, configured from the environment; `apps/healthcare-demo/scripts/run_local_demo.sh --agents network` |

Rules that apply in process:

- **Build each agent completely when you create it.** `httpx.ASGITransport`
  sends no lifespan events, so `create_app` sets everything up at once.
- **Call the clients from a thread**, such as a sync FastAPI handler, never
  from the network's own event loop.
- **Cap the requests you let in.** The agents' sync handlers share one thread
  limiter (`thread_tokens`, 64 by default). A handler that calls another agent
  holds its thread while it waits. With 40 tokens, 60 concurrent outer requests
  that each made a nested call deadlocked in a measurement; with 64 they
  finished. Every call has a timeout (30 seconds, or 120 seconds for a job, a
  dispatch, a site refresh or the site summaries), so a starved call ends with
  `AgentUnavailableError` instead of waiting forever. The stroke demo lets 12
  public requests run at once (`HEALTHCARE_DEMO_MAX_IN_FLIGHT`). A request
  beyond that waits up to 20 seconds for a slot, then gets `503` with
  `Retry-After: 60`. The demo refuses to start with fewer than 4 × the cap + 8
  thread tokens, because one public request can hold up to four agent threads.
- **Hold no lock or database connection of your own while you call an
  agent.** In process, an agent can read the same store your code writes. The
  stroke demo finishes a write, releases its lock, and then asks the
  hospital's LMA to read the store again (`POST /api/v1/site/source:refresh`).
- **Close the network** with `network.close()` when you stop.

The stroke demo runs both ways from the same code. With
`HEALTHCARE_DEMO_AGENTS=in-process` (the default, and the published demo) it
builds the three LMAs and the GMA in its own process, on the first request
that needs them, and makes their tokens then. With `HEALTHCARE_DEMO_AGENTS=network` it reads each agent's address and
token from the environment (`HEALTHCARE_DEMO_GMA_URL`, `HEALTHCARE_DEMO_GMA_TOKEN`,
`HEALTHCARE_DEMO_LMA_<SITE>_URL`, `HEALTHCARE_DEMO_LMA_<SITE>_TOKEN`), and needs
a database, because separate processes cannot share an in-memory store.
`apps/healthcare-demo/tests/test_network_mode.py` runs a pilot both ways
against the same database and checks that the numbers are equal.

Over HTTP, a call is sent once more when a kept-alive connection closed before
the answer came. These routes refuse a repeat with `409`, so a repeat of them
cannot act twice: a round dispatch with a used round id, a second update upload
from a site, a second offer or execution of the same job at an LMA, a second
aggregate of a round, a second release evaluation of a round, and an approval
of a version that is no longer a candidate. The other routes that change state
store the same thing again, or add one more record (a Guard enforcement or a
probe adds an audit record). An error from another agent reaches your
code as `AgentError` with `status`, `code` and `detail`; no message holds a
host, a port or a token.

## 8. Browser access (CORS)

The framework agents and services send no CORS headers, so a web page on
another origin cannot call them. Let your web page call your own backend, and
let the backend call Dagents. If your backend serves a page on another origin,
allow that origin by name. The stroke demo does this with FastAPI's
`CORSMiddleware` and a setting, `HEALTHCARE_DEMO_CORS_ORIGINS`, in
[`apps/healthcare-demo/backend/app/main.py`](../../apps/healthcare-demo/backend/app/main.py).

## 9. State that survives a restart

The agents can keep some of their state in Postgres. Each one runs every query
as one role, in a transaction that starts with `SET LOCAL ROLE`:

| Agent | Kept in Postgres with | Always in memory |
|---|---|---|
| LMA | The audit chain (`LMA_AUDIT_TABLE`, `LMA_AUDIT_ANCHOR_TABLE`) and the job log (`LMA_JOB_LOG_TABLE`), with `LMA_DATABASE_URL` and `LMA_SITE_ROLE` | The records it read and the running profile, which it rebuilds from the site's source |
| GMA | The model registry (`GMA_MODEL_TABLE`) and the release Guard's audit chain (`GMA_AUDIT_TABLE`, `GMA_AUDIT_ANCHOR_TABLE`), with `GMA_DATABASE_URL` and `GMA_DATABASE_ROLE` | Enrolments, rounds and the clipped updates of rounds still running |

The agents do not create these tables, and the `dagents` package has no
migrations. Create each table before you start the agent, as the "Postgres
tables" section of the [LMA README](../../dagents/lma/README.md#postgres-tables)
and the [GMA README](../../dagents/gma/README.md#postgres-tables) shows. Until
then, the routes that use a missing table answer `503` with the code
`store_not_set_up`.

A GMA restart during a round loses the round and its stored updates, so the
round cannot be aggregated. It fails closed. Model versions, release decisions
and audit chains survive.

If your application runs the controller and the Guard inside its own service,
as in paths B and C, give them persistent stores:

| Part | In memory | Persistent option |
|---|---|---|
| Audit log | `InMemoryAuditLog` | `PostgresAuditSink`, or your own `AuditSink` |
| Site enrolments | `InMemorySiteRegistry` | Your own class with `save`, `get` and `list` (`SiteRegistry` protocol) |
| Round records | `InMemoryRoundRepository` | Your own class with `save`, `create`, `update`, `get` and `list` (`RoundRepository` protocol). `create` and `update` must be atomic: the controller relies on them to refuse a reused round id and to keep results that arrive at the same time |

Pass them to `GovernanceService(audit=...)` and
`FederatedRoundController(engine, sites=..., rounds=...)`.

## What can go wrong

| What you see | Why | What to do |
|---|---|---|
| Every Guard request in a container is denied | `dagentsc` is not on the container's `PATH` | Add the build stage and the copy line from section 1 |
| A service calls `http://127.0.0.1:8020` inside Compose and gets no answer | It used the public address inside the network | Use the `*_INTERNAL_URL` value |
| The browser console shows a CORS error | The page calls an agent or service directly | Call your own backend, or allow the page's origin there |
| Rounds and audit records are gone after a deploy | The agent keeps them in memory | Set the Postgres settings in section 9; rounds always stay in memory |
| `401` with the code `unauthorized` | The agent has tokens, and the call has no `Authorization: Bearer` header or a token the agent does not accept | Send the token for the route's scope (section 6) |
| `403` with the code `forbidden` | The token's scope may not call the route, or a site's token was used for another site | Use the token of the right scope |
| An agent logs "runs without tokens: every caller may call every route" | No token is configured | Set the tokens, and `LMA_REQUIRE_TOKENS=1` or `GMA_REQUIRE_TOKENS=1` |
| `AgentUnavailableError` in your application, or a round result `failed` with "the job run failed: ..." | An agent did not answer in time, or has no address configured | Check the agent is running and its address is in `GMA_SITE_ENDPOINTS` or `LMA_GMA_ENDPOINT`, or was accepted from `GMA_JOINABLE_SITE_ENDPOINTS`; in process, cap concurrent requests (section 7) |
| `403` `not_joinable` or `address_differs`, or `409` `configured_elsewhere`, from `PUT /api/v1/federation/sites/{site_id}/address` | The site is not in `GMA_JOINABLE_SITE_ENDPOINTS`, the address is not the listed one, or the site is configured with another address | Send the exact address the setting lists; a configured site needs no address at run time |
| `store-south.yml` holds only `None` | The planner found issues, so `compose_yaml` is null | Read `issues` in the answer: each names the service and the rule it breaks |

Next: [Step 9: Test your integration](09-test-your-integration.md).
