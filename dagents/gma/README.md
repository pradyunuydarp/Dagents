# Global Monitoring Agent (GMA)

The GMA coordinates the LMAs (local monitoring agents) of a study or a fleet.
It enrols them, plans and dispatches federated rounds, combines what the LMAs
return, keeps the model versions, runs the release Guard and the release
gates, and handles approvals. It never reads a site's records: it sees counts,
the rates each site's planner permits, round results and model parameters.
The framework's architecture page,
[`docs/agents/lma-gma-architecture.md`](../../docs/agents/lma-gma-architecture.md),
shows where it fits, and every endpoint is in the
[API reference](../../docs/reference/api/agents.md#the-global-monitoring-agent).

## Run it

From the `dagents` package on PyPI, whose wheels for Linux x86_64, Linux
aarch64 and macOS arm64 include the planner:

```bash
pip install "dagents[gma]"
dagents-gma
```

From a clone of the repository, with the planner built:

```bash
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
PYTHONPATH=. .venv/bin/uvicorn dagents.gma.main:app --port 8020
```

`PYTHONPATH=. .venv/bin/python -m dagents.gma` does the same as `dagents-gma`.
Both listen on `--host` and `--port`, else `GMA_API_HOST` and `GMA_API_PORT`,
else `127.0.0.1:8020`. The guide's
[Install the package](../../docs/guide/00-install.md) page covers the package,
its extras and platforms without a planner.

`dagents/gma/main.py` builds the app with `create_app(Settings.from_env())`. A
GMA with nothing configured behaves as it did before these settings existed:
the in-process simulator engine, no tokens, the default extension registry,
everything in memory and source reads on.

An application can build a GMA in its own process:

```python
from dagents.gma.app_factory import GmaDeps, create_app
from dagents.gma.config import Settings

gma = create_app(
    Settings(engine="http", admin_token="...", lma_tokens={...}, site_tokens={...}, allow_source_reads=False),
    GmaDeps(registry=registry, network=network),
)
```

`create_app` builds everything at once, because an app called in process
through `httpx.ASGITransport` gets no lifespan events. It refuses settings that
cannot work, such as `GMA_ENGINE=http` with no LMA addresses and no agent
network, or a model table with no database. `GmaDeps` can also hand it a model
store, a coordinator database lease and an audit sink.
`examples/getting-started/shop/agents.py` builds a GMA and two LMAs this way.

The Docker image (`dagents/gma/Dockerfile`) builds `dagentsc` in a first stage
and copies it onto `PATH`. Round plans, readiness, release gates and the model
lifecycle rule come from the planner, and the release Guard fails closed
without it. A GMA built without a planner logs a warning that says so.

## Settings

`Settings.from_env()` loads `env/.env.shared` and `env/.env.gma` without
replacing variables already set, then reads the variables below. It loads the
files only from a source tree: a checkout, or an image that copies `dagents/`
and `env/` side by side. An installed `dagents` package reads only the process
environment.

| Variable | Default | Meaning |
|---|---|---|
| `GMA_APP_NAME`, `GMA_APP_ENV`, `GMA_LOG_LEVEL` | `dagents-gma`, `development`, `info` | Names and logging |
| `GMA_API_HOST`, `GMA_API_PORT` | none | Where `dagents-gma` listens, and where uvicorn serves the GMA in the container; the app itself does not read them |
| `GMA_ENGINE` | `in-process` | `in-process` (the simulator) or `http` (jobs go to the LMAs over their HTTP API) |
| `GMA_SITE_ENDPOINTS` | none | `SITE=URL,...`: each LMA's base address. Only these addresses are called, so an enrolment cannot point the GMA elsewhere |
| `GMA_JOINABLE_SITE_ENDPOINTS` | none | `SITE=URL,...`: sites that may join while the GMA runs, each with the one address the GMA accepts for it. See [Sites that join while the GMA runs](#sites-that-join-while-the-gma-runs) |
| `GMA_LMA_TOKENS` | none | `SITE=TOKEN,...`: the coordinator token the GMA sends to each LMA |
| `GMA_ADMIN_TOKEN` | none | The study operator's token (`admin` scope) |
| `GMA_SITE_TOKENS` | none | `SITE=TOKEN,...`: each LMA's token for its update uploads and the released model (`site` scope, bound to the site) |
| `GMA_DISPATCH_CONCURRENCY` | `3` | How many sites a round's jobs run at at once, from 1 to 64 |
| `GMA_SUMMARY_TTL_SECONDS` | `2` | How long the site directory keeps the sites' summaries |
| `GMA_SUMMARY_TIMEOUT_SECONDS` | `30` | How long the site directory waits for each LMA's answer; an LMA that takes longer is reported as not reachable |
| `GMA_EXTENSIONS` | none | `module:attribute,...`: extensions for this GMA's own registry. Without it the GMA reads the default registry |
| `GMA_DATABASE_URL`, `GMA_DATABASE_ROLE` | none | The coordinator database and the role every query runs as. The address is never shown in a log or a response |
| `GMA_MODEL_TABLE` | none | `schema.table` for the model registry; in memory without it |
| `GMA_AUDIT_TABLE`, `GMA_AUDIT_ANCHOR_TABLE` | none | `schema.table` for the release Guard's audit chain and its anchor; in memory without them |
| `GMA_ALLOW_SOURCE_READS` | `1` | `0` makes the source, dataset profile and model job routes answer `403` |
| `GMA_REQUIRE_TOKENS` | `0` | `1` refuses to start without a token |

`env/.env.gma` sets the names, the port and an empty
`GMA_JOINABLE_SITE_ENDPOINTS`. Set the rest in the
environment; never commit a token or a database address. A GMA whose
configured registry has condition packs runs its Guard strict about
classifications: a request whose inline classification differs from the
registered one answers `409`.

## Postgres tables

The GMA does not create its tables, and the package has no migrations. Before
you start a GMA with `GMA_MODEL_TABLE` or `GMA_AUDIT_TABLE`, create the tables
they name. Create them as `GMA_DATABASE_ROLE`, so that role owns them. The
login in `GMA_DATABASE_URL` must be a member of that role, and the role must be
allowed to create tables in the schema.

For a coordinator role `coord` that owns the schema `coord`, with
`GMA_MODEL_TABLE=coord.models`, `GMA_AUDIT_TABLE=coord.audit` and
`GMA_AUDIT_ANCHOR_TABLE=coord.audit_anchor`:

```sql
set role coord;

create table coord.models (
  version text primary key check (length(version) between 1 and 200),
  kind text not null check (length(kind) between 1 and 64),
  params jsonb not null,
  digest text not null check (length(digest) between 1 and 200),
  parent_version text references coord.models (version),
  created_at timestamptz not null default now(),
  status text not null check (status in ('candidate', 'released', 'rejected', 'superseded')),
  release_decision jsonb,
  decided_at timestamptz
);

-- At most one released version at a time.
create unique index models_one_release on coord.models ((true)) where status = 'released';

create table coord.audit (
  seq bigint generated always as identity primary key,
  audit_id text not null unique,
  recorded_at bigint not null,
  boundary text not null,
  decision text not null,
  permitted boolean not null,
  record jsonb not null,
  previous_digest text not null unique,
  digest text not null unique
);

create table coord.audit_anchor (
  id boolean primary key default true check (id),
  seq bigint not null,
  digest text not null,
  record_count bigint not null,
  verified_at timestamptz not null default now()
);

reset role;
```

The anchor table is optional. Without `GMA_AUDIT_ANCHOR_TABLE`, the GMA keeps
the anchor in memory, and the first check of the audit chain after a restart
reads every record.

When a table is missing, the routes that use it answer `503`
`store_not_set_up`. In 0.1.0a1 the message says to apply the migrations; it
means that a table in this section is missing.

## Routes

Each route has a short path and an `/api/v1` path that run the same handler.
The routes for sites, rounds and model versions, in their `/api/v1` form:

| Route | Token scope | What it does |
|---|---|---|
| `POST /federation/sites:refresh` | `admin` | Ask each configured LMA for its registration, at the same time, and enrol it |
| `PUT /federation/sites/{site_id}/address` | `admin` | Accept a joinable site's LMA address and add it to the agent network. `403` `not_joinable` or `address_differs`, `409` `configured_elsewhere` |
| `DELETE /federation/sites/{site_id}/address` | `admin` | Remove a joined site's address (`204`). `409` `configured` for a configured site |
| `GET /federation/site-addresses` | `admin` | The joinable sites and, for every address the GMA holds, whether it is `configured` or `joined`. No address is listed |
| `GET /federation/sites/summaries?include=audit` | `admin` | What each LMA reports: counts, the permitted label rate and, with `include=audit`, its audit chain state |
| `POST /federation/rounds` | `admin` | Plan and dispatch a round. `409` for a round id already used |
| `POST /federation/rounds/{round_id}/results` | `admin` | Record a site's result. `403` for a site the plan did not select, `409` for an aggregated or closed round, and `409` for every round when the HTTP engine collects the results from the LMAs |
| `GET /federation/rounds/{round_id}/readiness` | `admin` | Ask the planner whether the results may be combined, counting only selected sites. Stores nothing |
| `POST /federation/rounds/{round_id}:aggregate` | `admin` | Make the candidate and store it on the round. `409` `aggregation_refused` with the reason; `409` `conflict` for a round that was discarded, aggregated before or closed |
| `PUT /federation/rounds/{round_id}/updates/{site_id}` | `site`, bound to `site_id` | Store one site's clipped update for a running training round. No route reads an update back |
| `POST /federation/rounds/{round_id}:discard` | `admin` | Give up a round: delete its updates, record why (`204`) |
| `POST /federation/rounds/{round_id}:evaluate-release` | `admin` | With a `condition_id`: the release Guard, the pack's gates and the lifecycle rule, with metrics from the study's rounds. Without one: the gates and metrics in the body only. A round takes one decision (`409` for a second); a lifecycle refusal leaves the round unchanged |
| `GET /governance/audit:summary` | `admin` | Whether the GMA's audit chain verifies, and its length |
| `GET /model-versions?limit=20` | `admin` | The model in use and the newest versions, without parameters |
| `GET /model-versions:released` | `admin`, `site` | The released version with its parameters. `ETag` is its digest; `If-None-Match` gives `304` |
| `GET /model-versions/{version}` | `admin` | One version, without parameters |
| `PUT /model-versions/{version}` | `admin` | Register a version: `201` new, `200` same content, `409` different content or a lifecycle refusal |
| `POST /model-versions/{version}:approve` | `admin` | Release a candidate when the planner's lifecycle rule allows it. `409` with the refusal and its reason otherwise |

With tokens configured, every other route except `/health` needs the `admin`
token; the LMA reports (registration, heartbeats, telemetry, deployment sync)
also take a `site` token, but only for an agent registered with that token's
site in `scope.site` (`403` otherwise). Without tokens the GMA answers every
caller and logs a warning.

## The HTTP engine

With `GMA_ENGINE=http`, the engine (`adapters/http_federation_engine.py`)
sends each selected site its job, with the round's starting model from the
registry, in one `jobs:run` call: the site checks the job and runs it when it
accepts. Up to `GMA_DISPATCH_CONCURRENCY` sites are called at a time. A missing address, a timeout or an
HTTP error becomes a `failed` result with its reason, and a refusal a
`rejected` result. Each call is recorded on the round as a dispatch row (site,
step, status, milliseconds, outcome). For a training round, aggregation checks
every stored update against the digest its site reported, calls the model
kind's `combine` with the planner's weights, registers
`<version_prefix>+<round_id>` as a candidate, and always deletes the round's
updates. Every result comes from a site's `jobs:run` answer, so the
results route refuses a posted result (`409`), and only a GMA on this engine
records `metrics_source: rounds` in a release decision. The update upload runs
on a small thread pool of the GMA's own (8 threads), so an upload never waits
for the server threads that the dispatches hold.

## Sites that join while the GMA runs

A GMA reaches each LMA through its agent network. At start the network holds
the addresses in `GMA_SITE_ENDPOINTS`. `GMA_JOINABLE_SITE_ENDPOINTS` lists the
sites that may join later, in the same `SITE=URL` form. Each entry is the one
address the GMA accepts for that site.

1. The study operator starts the new site's LMA.
2. `PUT /api/v1/federation/sites/{site_id}/address` with `{"url": "..."}`
   adds the address to the agent network, in memory.
3. `POST /api/v1/federation/sites:refresh` enrols the site from its own
   registration, as for a configured site. The site summaries and round
   dispatch call it from then on.

The address route applies these rules, in order: a configured site at its own
address is accepted as `configured`; a configured site at another address gets
`409` `configured_elsewhere`; a site that is not joinable gets `403`
`not_joinable`; an address other than the listed one gets `403`
`address_differs`; a site that joined already is accepted again, with no
second client. A trailing `/` is ignored, and an address that is not a base
address gets `422`. No answer or log line repeats an address.

The GMA refuses to start when a site is in both lists, when a joinable address
is not a base address, when `GMA_ENGINE` is not `http`, when it is handed an
in-process agent network, or, with tokens, when a joinable site has no entry
in `GMA_LMA_TOKENS` or `GMA_SITE_TOKENS`: without them it could neither call
the LMA nor accept its update.

A restarted GMA forgets the joined addresses; the operator sends them again.
To take a site out, withdraw its enrolment first
(`PUT /api/v1/federation/sites/{site_id}` with `enrolled: false`), then
`DELETE /api/v1/federation/sites/{site_id}/address`. A round that still
selects a site with no address records a `failed` result without calling it.

## Errors

The GMA answers its own errors as `{"detail": ..., "code": ...}`: `401`
`unauthorized`, `403` `forbidden`, `not_joinable` or `address_differs`, `404`
`not_found`, `409` `conflict`, `aggregation_refused`, `configured_elsewhere` or
`configured`, `422` `invalid_request`, `502` `agent_error`, `503`
`planner_unavailable`, `store_unavailable`, `store_not_set_up` or
`agent_unavailable`. A lifecycle
refusal adds `refusal`. Routes that existed before keep FastAPI's `{detail}`
for their own `400`, `404` and `422` answers. No `detail` holds a host, a token
or a connection string.

## What it stores

| Part | Where |
|---|---|
| Model versions, their status and latest release decision | `InMemoryModelStore`, or `PostgresModelStore` with `GMA_MODEL_TABLE` |
| The release Guard's audit chain | Memory, or `PostgresAuditSink` with `GMA_AUDIT_TABLE` |
| Enrolments and rounds | Memory |
| Addresses of joined sites | Memory, in the agent network |
| Clipped training updates | Memory, until aggregation or discard |

A restart during a round loses the round and its updates, so it cannot be
aggregated. Model versions, decisions and audit chains survive in Postgres.

## Layout

- `domain/`: pure models and errors
- `application/`: `model_registry.py`, `release_service.py`,
  `site_directory.py`, `site_addresses.py`, `aggregation_service.py`
- `adapters/`: `http_federation_engine.py`, `telemetry_sink.py`
- `infrastructure/`: `update_store.py`, `postgres_models.py`, in-memory
  persistence
- `app_factory.py`, `routes.py`, `config.py`, `di.py`, `main.py`: wiring
