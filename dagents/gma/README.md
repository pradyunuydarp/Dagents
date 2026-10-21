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
through `httpx.ASGITransport` gets no lifespan events. It opens no database
connection: each Postgres store connects on its first operation. It refuses
settings that cannot work, such as `GMA_ENGINE=http` with no LMA addresses and
no agent network, or a table with no database (the rules are under
[Settings](#settings)). `GmaDeps` can also hand it a model store, a
coordinator database lease, an audit sink, and its own stores for rounds
(`rounds`, a `RoundRepository`), site enrolments (`enrolments`, a
`SiteRegistry`) and training updates (`updates`, an `UpdateStore`). A store
handed in takes the place of its table setting. Each store left out follows
the settings below: Postgres when its table is set, memory otherwise, new for
each app.
`examples/getting-started/shop/agents.py` builds a GMA and two LMAs this way.

The Docker image (`dagents/gma/Dockerfile`) builds `dagentsc` in a first stage
and copies it onto `PATH`. Round plans, readiness, release gates and the model
lifecycle rule come from the planner, and the release Guard fails closed
without it. A GMA built without a planner, or with a planner file whose
SHA-256 is not the one `DAGENTSC_SHA256` requires, logs a warning that says
so.

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
| `GMA_ROUND_TABLE` | none | `schema.table` for the round records; in memory without it |
| `GMA_ENROLMENT_TABLE` | none | `schema.table` for the site enrolments; in memory without it |
| `GMA_UPDATE_TABLE` | none | `schema.table` for the clipped updates of running training rounds; in memory without it. Needs `GMA_ENGINE=http` |
| `GMA_UPDATE_ROLE` | none | The role that owns the updates table. Every read and write of an update acts as this role |
| `GMA_UPDATE_MAX_AGE_SECONDS` | `86400` with a durable update store; none in memory | How old, in seconds, a round's oldest stored update may be. The 24-hour default applies when the updates are kept in a store that survives a restart (`GMA_UPDATE_TABLE`). A GMA that keeps them in memory runs no age check unless this is set. See [Recovery after a restart](#recovery-after-a-restart) |
| `GMA_ALLOW_SOURCE_READS` | `1` | `0` makes the source, dataset profile and model job routes answer `403` |
| `GMA_REQUIRE_TOKENS` | `0` | `1` refuses to start without a token |
| `DAGENTSC_BIN` | none | The planner to start. Without it, the planner in the package, then `dagentsc` on `PATH` |
| `DAGENTSC_SHA256` | none | The SHA-256 the planner file must have, as 64 hexadecimal digits. With another hash the runner starts no planner, so rounds stop and the release Guard denies every request |
| `DAGENTSC_TIMEOUT_SECONDS` | `10` | How long one planner call may run, in seconds. The runner stops a call past it, and the call fails like a missing planner |
| `DAGENTSC_MAX_INPUT_BYTES`, `DAGENTSC_MAX_OUTPUT_BYTES` | `16777216` each | The most bytes one planner call may send the planner, and the most the planner may print. Past either, the call fails like a missing planner |

`env/.env.gma` sets the names, the port and an empty
`GMA_JOINABLE_SITE_ENDPOINTS`. Set the rest in the
environment; never commit a token or a database address. A GMA whose
configured registry has condition packs runs its Guard strict about
classifications: a request whose inline classification differs from the
registered one answers `409`.

Every table setting shares one kept connection to `GMA_DATABASE_URL`. The GMA
refuses to start (`ValueError`, naming the settings and never a value) when:

- a table is set without `GMA_DATABASE_URL` (or a lease in `GmaDeps`) or
  without `GMA_DATABASE_ROLE`;
- `GMA_ROUND_TABLE` is set without `GMA_MODEL_TABLE` and `GMA_AUDIT_TABLE`,
  or their stores in `GmaDeps`. A round kept across a restart needs its
  candidate and the release Guard's chain kept too, or its release can be
  decided differently;
- `GMA_UPDATE_TABLE` is set without `GMA_ROUND_TABLE` (or a round store in
  `GmaDeps` that survives a restart), or without `GMA_UPDATE_ROLE`;
- `GMA_UPDATE_TABLE` or `GMA_UPDATE_MAX_AGE_SECONDS` is set, or `GmaDeps`
  holds an update store that survives a restart, without `GMA_ENGINE=http`.
  The simulator neither reads nor deletes stored updates, so nothing would
  ever delete them;
- `GMA_UPDATE_ROLE` equals `GMA_DATABASE_ROLE`, so a coordinator-role reader
  could read update vectors;
- `GMA_UPDATE_ROLE` is set without `GMA_UPDATE_TABLE`, or
  `GMA_UPDATE_MAX_AGE_SECONDS` without `GMA_UPDATE_TABLE` or an update store
  in `GmaDeps`;
- `GMA_UPDATE_MAX_AGE_SECONDS` is not a finite number above 0.

The `DAGENTSC_*` settings are read by the planner runner, so they apply to
every Dagents process that calls the planner. The GMA refuses to start when a
limit is zero, negative or not a number, or when `DAGENTSC_SHA256` is not 64
hexadecimal digits: `create_app` raises `ValueError` with the variable's name,
and `dagents-gma` prints it and exits with status 1. The guide's
[Planner settings](../../docs/guide/08-deploy.md#planner-settings) section
explains the defaults and how to pin the hash.

## Postgres tables

The GMA does not create its tables, and the package has no migrations. Before
you start a GMA with a table setting, create the table it names. Create the
coordinator's tables as `GMA_DATABASE_ROLE`, so that role owns them, and the
updates table as `GMA_UPDATE_ROLE`. The login in `GMA_DATABASE_URL` must be a
member of each role, and each role must be allowed to create tables in its
schema.

For a coordinator role `coord` that owns the schema `coord`, with
`GMA_MODEL_TABLE=coord.models`, `GMA_AUDIT_TABLE=coord.audit` and
`GMA_AUDIT_ANCHOR_TABLE=coord.audit_anchor`:

```sql
set role coord;

create table coord.models (
  version text primary key check (length(version) between 1 and 200),
  kind text not null check (length(kind) between 1 and 64),
  params json not null,
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

`params` is `json`, so a version's parameters come back as they were
registered. A `jsonb` column returns `-0.0` as `0.0`, and `1e16` and larger
floats as whole numbers. A model kind whose digest tells these apart then
refuses the version when a restarted GMA loads it. A kind that writes `-0.0`
into the text it hashes, as the toy linear kind in `dagents/tests` does,
refuses it for `-0.0`. A kind that hashes the JSON text as stored refuses it
for both. A models table made from an earlier version of this README has
`params jsonb`. It works as before, with that limit, until you change the
column:

```sql
alter table coord.models alter column params type json using params::json;
```

Rows stored before the change keep the values `jsonb` gave them.

### Round state tables

The GMA keeps site enrolments, rounds and the clipped updates of running
training rounds in memory, unless a table setting names a table for them or
the application that builds it passes its own stores in `GmaDeps`.
`dagents.gma.infrastructure.postgres_rounds` has a Postgres store for each,
and each passes the same tests as its memory twin.

| Store | Setting | `GmaDeps` field | Table | Owner |
|---|---|---|---|---|
| `PostgresRoundRepository` | `GMA_ROUND_TABLE` | `rounds` | rounds | the coordinator role |
| `PostgresEnrolmentStore` | `GMA_ENROLMENT_TABLE` | `enrolments` | site enrolments | the coordinator role |
| `PostgresUpdateStore` | `GMA_UPDATE_TABLE`, `GMA_UPDATE_ROLE` | `updates` | clipped updates | the update role |

`GMA_UPDATE_TABLE` is opt-in: without it, a training round that a restart cuts
between its dispatch and its aggregate is closed, and the operator runs it
again. The update role must differ from the coordinator role: the GMA refuses
to start, and `PostgresUpdateStore` raises `RoleScopeError`, when they are the
same. Give the coordinator role no grant on the updates table. Then nothing
that acts as the coordinator role can read an update vector: not a route, not
the release flow, not a reporting login. The login must be able to act as both
roles.

For the coordinator role `coord` above, and an update role `coord_updates`
that owns the schema `coord_updates`:

```sql
set role coord;

create table coord.rounds (
  seq bigint generated always as identity unique,
  round_id text primary key,
  phase text not null,
  status text not null check (status in ('planned', 'dispatched', 'collecting', 'aggregated', 'closed')),
  record_version integer not null,
  record json not null,
  closed_reason text,
  created_at bigint not null,
  updated_at bigint not null
);

create index on coord.rounds (seq) where status in ('planned', 'dispatched', 'collecting');

create table coord.site_enrolments (
  site_id text primary key,
  registration json not null,
  saved_at timestamptz not null default now()
);

reset role;
set role coord_updates;

create table coord_updates.updates (
  round_id text not null,
  site_id text not null,
  update_values json,
  digest text,
  job_digest text,
  received_at double precision not null,
  discarded boolean not null default false,
  primary key (round_id, site_id),
  check ((site_id = '') = (update_values is null))
);

reset role;
```

Each table's SQL is also a constant in the module (`ROUND_TABLE_DDL`,
`ENROLMENT_TABLE_DDL`, `UPDATE_TABLE_DDL`), and
`dagents/tests/test_round_store.py` checks that this section matches them.
The columns are `json`, for the reason given above. A round record that holds
NaN or an infinite number, which `json` cannot store, is refused with
`InvalidRoundRecord` (`422`), and the memory store refuses the same records.
The stores read each JSON column as text and decode it row by row, so a row
whose JSON Python cannot decode (nested too deep, a whole number of more than
4,300 digits) or that holds `1e400` counts as unreadable: a read of that round
fails, a list leaves it out, and recovery can close it from its columns. In
the updates table, the row whose `site_id` is `''` is the
round's gate row: after a discard it refuses every later upload for the round,
in any process.

For these tables, the settings are:

```bash
GMA_DATABASE_ROLE=coord
GMA_MODEL_TABLE=coord.models
GMA_AUDIT_TABLE=coord.audit
GMA_AUDIT_ANCHOR_TABLE=coord.audit_anchor
GMA_ROUND_TABLE=coord.rounds
GMA_ENROLMENT_TABLE=coord.site_enrolments
GMA_UPDATE_TABLE=coord_updates.updates   # optional
GMA_UPDATE_ROLE=coord_updates            # with GMA_UPDATE_TABLE
GMA_ENGINE=http                          # with GMA_UPDATE_TABLE
GMA_SITE_ENDPOINTS=site-a=http://127.0.0.1:8082,site-b=http://127.0.0.1:8083
```

An application that builds the GMA can build the stores itself and pass them:

```python
from dagents.common.infrastructure.postgres_lease import KeptConnection
from dagents.gma.app_factory import GmaDeps, create_app
from dagents.gma.infrastructure.postgres_rounds import (
    PostgresEnrolmentStore,
    PostgresRoundRepository,
    PostgresUpdateStore,
)

lease = KeptConnection(database_url, name="coordinator")  # the application closes it
gma = create_app(
    settings,
    GmaDeps(
        registry=registry,
        network=network,
        coordinator_lease=lease,  # GMA_MODEL_TABLE and GMA_AUDIT_TABLE use it too
        rounds=PostgresRoundRepository(lease, "coord", "coord.rounds"),
        enrolments=PostgresEnrolmentStore(lease, "coord", "coord.site_enrolments"),
        updates=PostgresUpdateStore(lease, "coord_updates", "coord_updates.updates", coordinator_role="coord"),
    ),
)
```

With `PostgresUpdateStore`, clipped updates stay in the table until
aggregation, a discard, recovery or the age limit deletes them. The age limit
does not delete the updates of a `collecting` training round whose candidate
is registered: they stay until its aggregate is retried or the round is
discarded. Copies also
stay in the write-ahead log and in any backup taken while they were stored.
Leave the table's data out of logical dumps (`pg_dump --exclude-table-data`).
On Supabase, the one login can read the table through `pg_read_all_data`.

When a table is missing, the routes that use it answer `503`
`store_not_set_up`. In 0.1.0a1 the message says to apply the migrations; it
means that a table in this section is missing.

## Recovery after a restart

A GMA whose round store survives a restart (`GMA_ROUND_TABLE`) finds the
rounds that the stopped process left unfinished. It recovers them once,
before it takes a round write:

- **Under uvicorn**, the lifespan starts the recovery and waits for it up to
  10 seconds (`START_RECOVERY_WAIT_SECONDS`) before the GMA serves. A
  recovery normally takes less than a second. When it takes longer, the GMA
  serves while it finishes, and logs that it started before its recovery
  finished. A failure is logged and not raised, so the GMA still starts and
  `/health` answers.
- **In process** (`httpx.ASGITransport`), no lifespan runs, so the first
  round write runs it.
- **The round writes wait for it**: dispatch, posted results, `:aggregate`,
  `:discard`, `:evaluate-release` and the update upload, in both path forms.
  It runs on the GMA's own handler threads, so a waiting request, an upload
  among them, holds no server thread. While it fails, these routes answer
  `503` `recovery_failed`, and the next round write tries again. Once the
  GMA serves, reads, `/health` and approval do not wait for it. At start,
  `/health` answers only after the lifespan's wait, which is at most 10
  seconds.

For each unfinished round, oldest first, the GMA asks the planner
(`dagentsc federation round recover`) whether to keep or close it, and tells
it whether the update store survives a restart, whether the round's candidate
is registered, and how old the round's oldest stored update is. A round the
planner closes is discarded with the planner's sentence as its reason, and its
updates are deleted; a round whose record cannot be read is closed from its
columns. Then the GMA deletes the stored updates of rounds that are finished
or not stored. Every keep or close decision comes from the planner.

Before each dispatch, the GMA also asks the same planner rule about every
unfinished round whose oldest stored update is older than
`GMA_UPDATE_MAX_AGE_SECONDS`, and deletes the old updates of finished rounds.
It leaves out a round it is dispatching now. This age check, and the age
given to the recovery, apply when the update store survives a restart
(`GMA_UPDATE_TABLE`), with a default of 24 hours, or when
`GMA_UPDATE_MAX_AGE_SECONDS` is set. A GMA that keeps its updates in memory
and sets no limit runs no age check. The planner keeps a `collecting`
training round whose candidate is registered, however old its updates are.

What resumes after a restart at each point of a study:

| The process stopped | After the restart |
|---|---|
| Between rounds | Everything resumes. With `GMA_ENROLMENT_TABLE`, the enrolments do too; without it, the next round closes for lack of quorum until the operator refreshes the sites. |
| During a dispatch (`dispatched`) | Recovery closes the round (`dispatch_interrupted`) and deletes its updates. A late upload gets `409`. The operator dispatches the round again under a new id. Its results can differ, because the candidate name and the Guard's noise seeds contain the round id. |
| While an analytics or evaluation round is `collecting` | The round resumes from its record. |
| While a training round is `collecting` | With `GMA_UPDATE_TABLE` the round resumes, and its aggregate gives the candidate an uninterrupted run gives. Without it, recovery closes the round (`updates_not_kept`), unless its candidate is already registered. |
| Inside an aggregate, after the candidate was registered | A retried aggregate returns the registered candidate, with or without `GMA_UPDATE_TABLE`. Until then the round stays `collecting` with its stored updates, and the age limit does not close it. |
| `aggregated`, before the release evaluation | Resumes. The candidate is in `GMA_MODEL_TABLE`. |
| Between the version's decision and the round's | A retried evaluation saves the same decision on the round and adds one record to the Guard's chain. |
| Decided, before approval | Resumes. Approval reads only the model store. |

`dagents/tests/test_gma_restart.py` restarts a GMA built from its settings at
these points and compares the study with an uninterrupted run, and
`dagents/tests/test_gma_process_restart.py` does the same with a GMA process
stopped with SIGKILL and started again on its port.

Limits:

- **One GMA process, with one uvicorn worker.** The image runs one. The stores
  are correct with two writers, but recovery is not: a second GMA on the same
  tables closes the first one's dispatch. The work is lost, and nothing is
  decided wrongly.
- **Stop the old process before you start the new one.** The Kubernetes
  manifest the framework renders today uses Kubernetes' default rolling
  update, which starts the new pod first.
- **After a restore from a backup**, never reuse a round id, and restore the
  audit table and its anchor together.
- **A database that stops answering holds the coordinator connection.** The
  GMA serves after its 10-second wait at start, but reads and writes that use
  the connection wait until the database answers. The statement timeout is
  enforced by the server, so it ends a slow query but not this wait.

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
| `POST /federation/rounds/{round_id}:evaluate-release` | `admin` | With a `condition_id`: the release Guard, the pack's gates and the lifecycle rule, with metrics from the study's rounds. Without one: the gates and metrics in the body only. A round takes one decision (`409` for a second); a lifecycle refusal leaves the round unchanged. A retry after the round's write failed saves the decision the version holds, and a decision that differs from it gets `409` `decision_differs` |
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
kind's `combine` with the planner's weights and registers
`<version_prefix>+<round_id>` as a candidate. The round's updates are deleted
once the round is stored as aggregated, or when the engine refuses the
aggregation: a missing or changed update (`409` `aggregation_refused`), or a
candidate name that holds another model (`409` `conflict`). When the planner
refuses aggregation, the updates stay until the round is discarded. A store
error keeps them, so the aggregate can be retried: the retry combines them
again and gets the candidate the first attempt registered. Without stored
updates, a retry returns that registered candidate only when the round store
keeps round ids across restarts (`survives_restart`), as the store
`GMA_ROUND_TABLE` builds does; the default store in memory does not. Every
result comes from a site's
`jobs:run` answer, so the
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
`planner_unavailable`, `store_unavailable`, `store_not_set_up`,
`recovery_failed` or `agent_unavailable`. A lifecycle
refusal adds `refusal`. Routes that existed before keep FastAPI's `{detail}`
for their own `400`, `404` and `422` answers. No `detail` holds a host, a token
or a connection string.

## What it stores

| Part | Where |
|---|---|
| Model versions, their status and latest release decision | `InMemoryModelStore`, or `PostgresModelStore` with `GMA_MODEL_TABLE` |
| The release Guard's audit chain | Memory, or `PostgresAuditSink` with `GMA_AUDIT_TABLE` |
| Enrolments and rounds | Memory, `PostgresEnrolmentStore` with `GMA_ENROLMENT_TABLE` and `PostgresRoundRepository` with `GMA_ROUND_TABLE`, or the stores in `GmaDeps.enrolments` and `GmaDeps.rounds` ([Round state tables](#round-state-tables)) |
| Addresses of joined sites | Memory, in the agent network |
| Clipped training updates | Memory, `PostgresUpdateStore` with `GMA_UPDATE_TABLE`, or the store in `GmaDeps.updates`, until aggregation, discard, recovery or the age limit. The age limit leaves the updates of a `collecting` training round whose candidate is registered until its aggregate is retried or it is discarded |

With the default stores, a restart during a round loses the round and its
updates, so it cannot be aggregated. Model versions, decisions and audit
chains survive in Postgres. With every table set, a study resumes after a
restart, as [Recovery after a restart](#recovery-after-a-restart) describes.

## Layout

- `domain/`: pure models and errors
- `application/`: `model_registry.py`, `release_service.py`,
  `site_directory.py`, `site_addresses.py`, `aggregation_service.py`
- `adapters/`: `http_federation_engine.py`, `telemetry_sink.py`
- `infrastructure/`: `update_store.py`, `postgres_models.py`,
  `postgres_rounds.py`, in-memory persistence
- `app_factory.py`, `routes.py`, `config.py`, `di.py`, `main.py`: wiring
