# Step 8: Deploy

This step covers what has to be true when your application runs outside your
laptop: the planner in every container that decides, configuration from the
environment, one store and one Guard per source, who may call the services,
and browser access.

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
  environment, and keep the committed files free of secrets.
- **One connection per source.** Give each source's store its own database
  login, and read it from a variable per source. The stroke demo uses
  `HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`, for example
  `HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL`.

## 4. Keep each source on its own machine

The worked example runs both stores and the coordinator in one process, to keep
it short. In a deployment:

| Runs at each source | Runs at the coordinator |
|---|---|
| The source's store and database login | The round controller and its round records |
| The source's Guard and audit sink | The release Guard and its audit sink |
| The source's worker and runner | The federation engine's coordinator side |
| `dagentsc` | `dagentsc` |

To let the GMA service send round jobs to sources in other processes, list
them in `GMA_FEDERATION_SITES` as `site_id=url` entries separated by commas, and
set `GMA_FEDERATION_TIMEOUT_SECONDS` (default 120) to cover a source's longest
job. Each source's service must answer `POST /api/v1/federation/jobs:offer` and
`POST /api/v1/federation/jobs:execute` with its worker's `offer` and `execute`.
The [GMA README](../../agents/gma/README.md#federated-rounds-over-http) has the
details.

The stroke demo runs this split on one machine:
`apps/healthcare-demo/scripts/run_split_demo.sh` starts the Dagents GMA with
`GMA_FEDERATION_SITES` and one LMA process per hospital. Its
[README](../../apps/healthcare-demo/README.md#one-process-per-hospital) lists
what travels between the processes.

A round's model update crosses from the left column to the right one only if
the source's `before_send` Guard permits it. The round's metrics and example
count cross as the source's runner wrote them
([Step 6](06-federated-rounds.md#3-the-sources-side-the-worker)), so the runner
must put only aggregate numbers in them.

## 5. Kubernetes manifests

Compile manifests for your own components with core-service
([Step 7](07-framework-services.md#1-compile-a-workload-plan-core-service)) and
apply them with `kubectl apply -f`. The manifests are generated and tested, but
no Dagents deployment runs on a cluster in CI yet. That work is in `TODO.md`.

## 6. Who may call the services

The agents and the framework services check no identity: no token, no API key
and no login. Any caller that can reach a port gets an answer. The overview of
the API reference says the same in
[Authentication](../reference/api/overview.md#authentication). Two
consequences for a deployment:

- **The GMA takes results as sent.** A `POST` of a site result with
  `"code_verified": true` and `"privacy_checks_passed": true` counts toward
  readiness and the candidate, whoever sent it. Anyone who can reach the GMA
  can enrol a source or post a result.
- **The LMA takes requester attributes as sent.** The `requester` of a Guard
  request is input to the planner's trust assessment, not proof of identity.

Run the agents and services on a private network, or behind a gateway that
authenticates each caller, and let only your own sources reach the GMA.

## 7. Browser access (CORS)

The framework agents and services send no CORS headers, so a web page on
another origin cannot call them. Let your web page call your own backend, and
let the backend call Dagents. If your backend serves a page on another origin,
allow that origin by name. The stroke demo does this with FastAPI's
`CORSMiddleware` and a setting, `HEALTHCARE_DEMO_CORS_ORIGINS`, in
[`apps/healthcare-demo/backend/app/main.py`](../../apps/healthcare-demo/backend/app/main.py).

## 8. State that survives a restart

The LMA and GMA services keep enrolments, rounds and audit logs in memory. If
your application needs them after a restart, run the controller and the Guard
inside your own service, as in paths B and C, and give them persistent stores:

| Part | In memory | Persistent option |
|---|---|---|
| Audit log | `InMemoryAuditLog` | `PostgresAuditSink`, or your own `AuditSink` |
| Site enrolments | `InMemorySiteRegistry` | Your own class with `save`, `get` and `list` (`SiteRegistry` protocol) |
| Round records | `InMemoryRoundRepository` | Your own class with `save`, `get` and `list` (`RoundRepository` protocol) |

Pass them to `GovernanceService(audit=...)` and
`FederatedRoundController(engine, sites=..., rounds=...)`.

## What can go wrong

| What you see | Why | What to do |
|---|---|---|
| Every Guard request in a container is denied | `dagentsc` is not on the container's `PATH` | Add the build stage and the copy line from section 1 |
| A service calls `http://127.0.0.1:8020` inside Compose and gets no answer | It used the public address inside the network | Use the `*_INTERNAL_URL` value |
| The browser console shows a CORS error | The page calls an agent or service directly | Call your own backend, or allow the page's origin there |
| Rounds and audit records are gone after a deploy | The services keep them in memory | Use persistent stores (section 8) |
| A result the coordinator did not expect counts toward a round | The GMA checks no caller's identity | Put the GMA behind a gateway that admits only your sources (section 6) |

Next: [Step 9: Test your integration](09-test-your-integration.md).
