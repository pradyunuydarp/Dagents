# API reference

This reference describes every HTTP endpoint the Dagents services expose, and
the commands of `dagentsc`, the planner program the services call. For each
endpoint it gives the parameters, the request and response fields with their
types and limits, the errors, and examples captured from real calls.

[Step 4 of the learning guide](../../learn/04-apis.md) walks through real
requests. Read it first if you are new to the APIs.

## Modules

The reference is grouped into five modules. Each module page lists its
interfaces, and each interface lists its endpoints.

| Module | Services | Ports | What it is for |
|---|---|---|---|
| [Agents](agents.md) | LMA, GMA | 8010, 8020 | The LMA runs at each data source. The GMA coordinates the LMAs, runs federated rounds and decides releases. |
| [Framework services](framework-services.md) | core-service, pipeline-service, model-service | 8040, 8030, 8000 | Service catalog and Kubernetes manifests; pipeline definitions and runs; model training, checks and jobs. |
| [Spring Boot services](spring-services.md) | spring-control-service, spring-core-service | 8050, 8060 | The agent control plane and the core APIs again, for applications on the JVM. |
| [Demo apps](demo-apps.md) | healthcare-demo-backend, nl2sql-demo-backend | 8080, 8070 | Two applications that use the framework. They show how an application calls it and are not part of it. |
| [dagentsc](dagentsc.md) | the planner program | none | The OCaml planners, run as a command that reads JSON and prints JSON. |

Terms used on every page:

- **Endpoint**: one HTTP method and path, such as `POST /api/v1/workloads:compile`.
- **Interface**: the router, controller or command group that owns a set of
  endpoints, and the classes it calls.
- **Object**: a structure the endpoints take or return, such as a workload
  component or a round manifest.
- **Planner**: `dagentsc`. Services call it with JSON on standard input to make
  every deterministic decision (see [How Dagents is built](../../learn/03-architecture.md)).
- **Guard**: the Ethical Guard, which applies the planner's governance decision
  to real data and writes an audit record.
- **Boundary**: the point where the Guard checks a request: `before_read`
  (records are read at a source), `before_train` (training or evaluation uses
  them), `before_send` (something leaves the source) or `before_release` (a
  candidate model is released). An endpoint that "passes no Guard boundary"
  runs no Guard check.

## Base URLs

Every service listens on its own port. The addresses below are the local ones
from `env/.env.shared`, except the healthcare demo's, which comes from its own
settings (`HEALTHCARE_DEMO_API_PORT`, default 8080, in
`apps/healthcare-demo/env/.env.healthcare-demo`). Inside Docker Compose the
services reach each other by service name instead, for example
`http://core-service:8040`.

| Service | Local address | Paths |
|---|---|---|
| model-service | `http://127.0.0.1:8000` | `/api/v1/...` |
| LMA | `http://127.0.0.1:8010` | `/...` and `/api/v1/...` |
| GMA | `http://127.0.0.1:8020` | `/...` and `/api/v1/...` |
| pipeline-service | `http://127.0.0.1:8030` | `/api/v1/...` |
| core-service | `http://127.0.0.1:8040` | `/api/v1/...` |
| spring-control-service | `http://127.0.0.1:8050` | `/api/v1/...` |
| spring-core-service | `http://127.0.0.1:8060` | `/api/v1/...` |
| nl2sql-demo-backend | `http://127.0.0.1:8070` | `/api/v1/...` |
| healthcare-demo-backend | `http://127.0.0.1:8080` | `/health` and `/api/v1/...` |

Every FastAPI service also serves its OpenAPI document at `/openapi.json` and an
interactive page at `/docs`.

## Run a service to try the examples

The examples on these pages call `http://127.0.0.1:<port>`. Start the service
first, from the repository root. Most endpoints call the planner, so build it
once and point `DAGENTSC_BIN` at it (see [Run it yourself](../../learn/05-hands-on.md)):

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && cd -
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
```

| Service | Command |
|---|---|
| LMA | `PYTHONPATH=. .venv/bin/uvicorn dagents.lma.main:app --port 8010` |
| GMA | `PYTHONPATH=. .venv/bin/uvicorn dagents.gma.main:app --port 8020` |
| core-service | `PYTHONPATH=.:services/core-service .venv/bin/uvicorn app.main:app --port 8040` |
| pipeline-service | `PYTHONPATH=.:services/pipeline-service .venv/bin/uvicorn app.main:app --port 8030` |
| model-service | `PYTHONPATH=.:services/model-service .venv/bin/uvicorn app.main:app --port 8000` |
| nl2sql-demo-backend | `PYTHONPATH=.:services/nl2sql-demo/backend .venv/bin/uvicorn app.main:app --port 8070` |
| healthcare-demo-backend | `apps/healthcare-demo/scripts/run_local_demo.sh` |
| Spring services | `mvn -f services/spring-services/pom.xml package`, then `java -jar` each service's jar with its `env/.env.spring-*` settings ([details](spring-services.md)) |
| Everything | `docker compose --env-file env/.env.compose up --build` |

Each Python service keeps its state in memory, so a restart clears what earlier
requests stored. The healthcare demo reads Postgres only when
`HEALTHCARE_DEMO_DATABASE_URL` is set.

## Versions and paths

- **Versioned paths.** Paths start with `/api/v1/`. The framework services
  (core, pipeline, model) have no other paths.
- **Short paths on the agents.** The LMA and the GMA also keep short paths such
  as `/health`, `/datasets/profile` and `/models/run`. Most short paths have a
  versioned twin that runs the same handler, so the two return the same body.
  Some twins use a different method or status: `PUT
  /api/v1/agents/{agent_id}/registration` is the twin of `POST /register`, and
  `POST /api/v1/model-jobs` answers `202` where `POST /models/run` answers
  `200`. The module pages show each pair on one card.
- **Paths with no twin.** Eleven short paths have no versioned twin: on the LMA,
  `GET /bundles`, `POST /bundles/deploy`, `POST /run`, `GET /runs` and
  `GET /telemetry`; on the GMA, `GET /overview`, `GET /deployments`,
  `GET` and `POST /runs/dispatch`, `GET /telemetry/recent` and
  `GET /telemetry/summary`. The source endpoints (`/api/v1/sources...`) on both
  agents and the GMA's `GET /api/v1/agents/{agent_id}` have only a versioned
  path. The card of each of these shows one path.
- **Custom methods.** A path that ends in a colon and a verb runs an action on a
  resource: `POST /api/v1/sources/{source_id}:validate`,
  `POST /api/v1/federation/rounds:plan`, `POST /api/v1/workloads:compile`. It is
  not a sub-collection.

## Requests and responses

- **JSON.** Request and response bodies are JSON. Send
  `Content-Type: application/json` with a body. The healthcare demo's chart
  endpoints are the exception: they return `image/svg+xml`.
- **Key names.** The Python services use snake_case keys (`plan_id`,
  `container_port`). The Spring services use the Java record names, which are
  camelCase (`planId`, `sourceId`). `dagentsc` reads and writes camelCase;
  `dagents/common/infrastructure/dagents_runner.py` converts between the two, so
  a Python service always shows snake_case.
- **Unknown fields.** Request models ignore keys they do not define, with four
  exceptions in the healthcare demo: a data entry, an outcome, a Guard probe and
  a pilot request refuse an unknown key with `422`.
- **State.** Nothing is persisted across restarts except in the healthcare demo
  with a database, and in an LMA or GMA whose settings name database tables for
  its audit chain, job log or model registry. Ids such as `plan_id` are yours to choose where a field
  allows it; otherwise the service makes one.

## Authentication

The LMA and the GMA check bearer tokens when their settings give them tokens:
every route except `/health` then needs `Authorization: Bearer <token>`, and
each token has a scope ([Tokens](agents.md#tokens-errors-and-calls-between-agents)).
An agent started without tokens answers every caller. No other service checks
an identity, a token or an API key. Run the services on a private network, or
behind a gateway that authenticates callers.

Some request bodies describe a requester, for example the `requester` of a
governance request. That is input to the planner's trust assessment, not proof
of identity: the service does not verify it.

The healthcare demo is public and has no accounts. To keep it usable it limits
how often each client may write (see [Rate limits](#rate-limits)), takes no free
text in the bodies whose results are written to an audit chain, and sets the
requester and purpose of a Guard probe itself.

## Errors

A failed request returns a `4xx` or `5xx` status with a JSON body. The shape of
the body depends on the service family.

| Family | Error body |
|---|---|
| Agents, framework services, NL2SQL backend | `{"detail": "Unknown workload plan: plan-missing"}` |
| Agents: tokens, sites, jobs, updates, model versions, releases, and the planner or a store that cannot be reached | `{"detail": "...", "code": "conflict"}`: the `code` names the kind of error ([the codes](agents.md#tokens-errors-and-calls-between-agents)) |
| The same, for a request that fails validation (`422`) | `{"detail": [{"type": "literal_error", "loc": ["body", "components", 0, "kind"], "msg": "...", "input": "...", "ctx": {...}}]}`: one entry per problem, with the path to the field in `loc` |
| model-service, for a model family it cannot run (`400`) | `{"detail": "...", "model_family": "...", "task": "..."}` |
| Healthcare demo | `{"detail": "There is no hospital with the id 'xx-1'."}` |
| Healthcare demo, validation (`422`) | `{"detail": "Some fields are not valid.", "errors": {"age": "must be between 16 and 110"}}`: one plain message per field |
| Spring services, validation (`422`) | `{"code": "validation_error", "message": "Path agentId must match body agent.agentId"}` |

An error the code does not handle returns `500`. The FastAPI services then send
the plain text `Internal Server Error`; the healthcare demo sends
`{"detail": "The server could not complete this request."}`.

When the planner cannot be run, the result depends on the endpoint. The Guard
fails closed: an enforcement request returns `200` with `"permitted": false`
and a `deny` decision, and the denial is recorded. A request that needs a plan
on the LMA or GMA returns `503` with the code `planner_unavailable`. The
healthcare demo returns `503` with a plain message.

| Status | Meaning | Where |
|---|---|---|
| `200` | Success | Every service |
| `201` | A record was created | Healthcare demo: entering a record; GMA: registering a model version |
| `202` | Accepted; the work may finish later | model-service and pipeline-service job submissions, and some versioned twins on the agents |
| `204` | Done; no body | GMA: discarding a round |
| `304` | The resource has not changed since the `ETag` you sent in `If-None-Match` | Healthcare demo: chart SVGs; GMA: the released model version |
| `400` | The path id and the body id differ, or the request cannot be served | LMA and GMA (classifications, sites, round results), model-service (model family), healthcare demo (a request with both `Content-Length` and `Transfer-Encoding`), Spring services (a body that is not JSON, or a value of the wrong type, with Spring's own error body) |
| `401` | No bearer token, or one the agent does not accept | LMA and GMA started with tokens |
| `403` | The token may not call this route, or the request asks for something the agent's configuration forbids | LMA and GMA |
| `404` | No resource has this id | Most services with ids. Exceptions: the LMA and GMA source lookup (`GET /api/v1/sources/{source_id}`) and the GMA's agent lookup answer `200` with `null`, and the Spring services answer `200` with an empty body |
| `409` | The request conflicts with the current state | LMA and GMA (a round id used twice, a late result, a result posted to the HTTP engine, a second offer, update, aggregate, release decision or approval, a classification that differs from the registered one, aggregation not permitted), healthcare demo (an `Idempotency-Key` reused for another record, an outcome already recorded, a full entry form, a pilot already running, a model that may not be approved) |
| `413` | The body is larger than 64 KB | Healthcare demo |
| `422` | The request does not match the schema | Every FastAPI service, and the Spring services |
| `429` | Too many requests; wait for the seconds in `Retry-After` | Healthcare demo |
| `500` | An error the code does not handle | Any service |
| `502` | Another agent answered with an error | LMA and GMA |
| `503` | A store, the planner or another agent cannot be reached | LMA, GMA and healthcare demo |

## Rate limits

Only the healthcare demo limits requests. It counts per client address, in its
own process.

| Limit | Default | Setting |
|---|---|---|
| Entering records and recording outcomes | 30 per minute per client | `HEALTHCARE_DEMO_RATE_LIMIT_ENTRIES` |
| Guard probes | 60 per minute per client | `HEALTHCARE_DEMO_RATE_LIMIT_PROBES` |
| Pilots | 3 per minute per client | `HEALTHCARE_DEMO_RATE_LIMIT_PILOTS` |
| Model approvals | 10 per minute per client | `HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS` |
| Guard probes at one hospital | 600 per hour, all clients together | `HEALTHCARE_DEMO_PROBES_PER_SITE_PER_HOUR` |
| Pilots | 20 per hour, all clients together | `HEALTHCARE_DEMO_PILOTS_PER_HOUR` |
| Pilots running at once | 1; a second gets `409` | none |
| Records entered at one hospital | 300; then `409` | `HEALTHCARE_DEMO_MAX_ENTERED_PER_SITE` |
| Request body | 64 KB; larger gets `413` | `HEALTHCARE_DEMO_MAX_BODY_BYTES` |
| Requests in progress, all clients together | 12; a request beyond it waits up to 20 seconds for a slot, then gets `503` with `Retry-After: 60`. The health routes never wait | `HEALTHCARE_DEMO_MAX_IN_FLIGHT`, `HEALTHCARE_DEMO_IN_FLIGHT_WAIT_SECONDS` |

A per-minute limit lets a client send that many requests at once, then one more
every `60 / limit` seconds. A setting of `0` turns a limit off. Over a limit,
the answer is `429` with a `Retry-After` header that gives the seconds to wait:

```json
{"detail": "Too many requests. Try again in 2 seconds."}
```

## Idempotency

`POST /api/v1/hospitals/{site_id}/encounters` in the healthcare demo takes an
optional `Idempotency-Key` header: 1 to 100 letters, digits or the characters
`.` `_` `:` `-`. Send a new key with each new record.

- The same key with the same record returns `201` and the record stored the
  first time. Nothing new is stored.
- The same key with a different record returns `409`.

No other endpoint takes the header. Sending a request to another write endpoint
twice runs it twice.

## Lists: limits and order

List endpoints take a `limit` query parameter.

- On the agents, the framework services and the GMA's round list, `limit`
  defaults to 20, and to 50 for the Guard's audit log. There is no maximum.
- The healthcare demo checks a range: a worklist 1 to 200 (default 12), an
  audit log 1 to 500 (default 50), entered records 1 to 300 (default 20), a
  planner trace 1 to 1,000 (default 50). `GET /api/v1/models` always lists the
  20 newest model versions.

There are no cursors or pages. Each endpoint card says its order, for example
"newest first".

## Browsers: CORS and the X-Dagents-App header

- The healthcare demo allows the origins in `HEALTHCARE_DEMO_CORS_ORIGINS`, or
  any origin when it is empty. It sends no cookies (`allow_credentials` is off)
  and lets a page read the `Retry-After`, `ETag` and `X-Dagents-App` headers.
- The NL2SQL backend allows any origin.
- The other services send no CORS headers, so a page on another origin cannot
  call them from a browser. Call them from a server, or through a proxy on the
  page's own origin.

The healthcare demo puts `X-Dagents-App: healthcare-demo` on every response it
sends itself, errors included. A `502`, `503` or `504` without the header came
from something in front of the app, such as a proxy while the app starts; the
published demo page retries those and shows the others at once.
