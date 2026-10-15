# Deploying this demo

The published demo is this app running on a host. It keeps each hospital's
records in its own schema of a Postgres database, and the OCaml planner in its
image makes every governance and federation decision. This page explains how the
parts connect and where each credential is kept.

A local run needs none of this: no database, no host and no account. Each
hospital then keeps its records in memory, loaded from the committed extract.
See the README.

The published API runs every hospital and the coordinator in one process
(`HEALTHCARE_DEMO_ROLE=combined` in `render.yaml`). The layout with the Dagents GMA
and one LMA per hospital runs locally or with `docker-compose.yml`; it is not
deployed, because the calls between its processes have no authentication. See
[One process per hospital](../README.md#one-process-per-hospital) in the README.

## The four parts

| Part | What it is | Defined in |
|---|---|---|
| Hospital stores | Supabase Postgres: one schema and one role per hospital, plus a coordinator schema | `supabase/migrations/` |
| API | this FastAPI app plus `dagentsc`, as a Docker service on Render | `render.yaml`, `backend/Dockerfile` |
| Frontend | the Vite build, published to GitHub Pages under the framework site | `.github/workflows/pages.yml` |
| Schema and seed | a manually started workflow that migrates, loads each hospital's records and checks the isolation | `.github/workflows/healthcare-data.yml` |

The records are the 1,014 IST records in `data/ist/ist_three_hospitals.csv`
(see [`data/ist/NOTICE.md`](../data/ist/NOTICE.md) for the source and licence).
The seed loads each hospital's rows into that hospital's schema, acting as that
hospital's role. Records entered on the page are stored the same way, with
`origin = 'entered'`.

## The database layout

Migrations `supabase/migrations/0002_site_isolation.sql` and
`0003_coordinator_records.sql` create:

| Schema | Owned by | Tables |
|---|---|---|
| `site_uk_134`, `site_it_002`, `site_ch_245` | the role with the same name | `encounters` (the records), `guard_audit` (that hospital's audit chain) |
| `coordinator` | `dagents_coordinator` | `models` (the model registry), `site_contributions` (what each hospital sent in each round), `pilot_reports` (the newest 20 pilot reports), `guard_audit` (the release Guard's audit chain) |

- The four roles are `NOLOGIN`. On PostgreSQL 16 or later the migration grants
  each one to the connecting role `WITH INHERIT FALSE, SET TRUE`: the API can
  switch to a role but does not get its privileges without switching.
- PostgreSQL 15 has no per-grant options, so there the migration uses a plain
  `GRANT`, and the connecting role inherits every site role's privileges. It can
  then read each hospital's tables without switching role. The site roles stay
  isolated from each other and from the coordinator. On Supabase this changes
  nothing in practice: its `postgres` role holds `pg_read_all_data` on any
  version (see below). The data workflow prints the server version before it
  migrates.
- The coordinator role has no access to any site schema, and a site role has no
  access to another site or to the model registry. A site may insert its own
  rows in `coordinator.site_contributions` and read only those (row-level
  security on `site_role = current_user`).
- Row-level security is on for every table. All access is revoked from
  `PUBLIC` and from Supabase's data API roles (`anon`, `authenticated`,
  `service_role`).
- The migrations refuse PostgreSQL older than 15, and each can be applied twice
  without change.
- 0002 keeps the old shared `public.encounters` and `public.hospitals` tables,
  because the previous version of the API reads them. So the data workflow can
  run while that version still serves the page (see
  [Upgrading from the synthetic-data version](#upgrading-from-the-synthetic-data-version)).
  A later migration drops them.
- Run on a database where an earlier version of 0002 ran, 0002 adds the
  `release_decision` and `decided_at` columns of `coordinator.models` with
  their checks. The migration runner skips a file it has already recorded, so on
  such a database run the file once by hand.
- The coordinator keeps the newest 20 pilot reports in
  `coordinator.pilot_reports`, so the consortium charts that show the last
  pilot still draw after the API restarts. A report holds round results, gate
  results and the training curve. It holds no hospital's audit records and no
  record of any patient.

The API runs every database operation in one transaction that starts with `SET
LOCAL ROLE <role>` and checks `current_user`, so each query runs as one
hospital or as the coordinator.

### What the coordinator sees of the audit chains

Each hospital's Guard writes its decisions to that hospital's own
`guard_audit` table. The coordinator's release Guard writes to
`coordinator.guard_audit`. (In the split layout, which is not deployed, the
Dagents GMA runs the release Guard and keeps its chain in memory.) A pilot report tells the coordinator, for each
chain, only whether it verifies and how many decisions it holds
(`site_audits` and `coordinator_audit`). The decisions themselves stay where
they were written. A hospital's own view, `GET /api/v1/hospitals/{id}/audit`,
lists its recent decisions.

How a chain is checked:

- Each check ends at a check point: the last record it covered, and how many
  records the chain held up to that record. Migration 0003 adds a
  `guard_audit_anchor` table next to each chain, and the chain's own role
  stores the check point there.
- With no stored check point, a check hashes every record. This happens the
  first time a chain is read. The in-memory stores keep their check point in
  the process, so each new process checks their chains in full once.
- With one, a check hashes that record again, counts the records up to it, and
  hashes only the records after it. A record removed or added before the check
  point changes the count, so the check hashes every record instead. The hosted
  API restarts after every idle period, and a new process continues from the
  stored check point.
- The check does not hash an earlier record again, so it does not see an edit
  to a record it already checked. `verify_chain(full=True)` in
  `agents/common/infrastructure/postgres_audit.py` hashes every record.
- A check that fails is repeated in full before the chain is reported broken.
  A failed check deletes the stored check point, so the next check is in full
  too.

The check point saves work and protects nothing: the role that can change a
chain can change its check point.

### One credential on Supabase

The API connects with one connection string, `HEALTHCARE_DEMO_DATABASE_URL`, as
the project's `postgres` role. On Supabase that role also holds
`pg_read_all_data`, so it could read every schema without switching role. The
isolation therefore binds the app's code paths, which always switch role, and
not the credential. The isolation step of the data workflow prints this.

A real deployment gives each site's LMA its own login role and database. The app
supports that: set `HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL` (for example
`HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL`, `..._IT_002_...`, `..._CH_245_...`),
and that hospital's records, audit chain and round contributions use it instead.

## Where the credentials are kept

No secret is in the repository or in this file. Each one is kept in one place:

| Name | Kept in | Used by |
|---|---|---|
| `SUPABASE_DB_URL` | GitHub → Settings → Secrets and variables → Actions → **Secrets** | the schema and seed workflow |
| `HEALTHCARE_DEMO_DATABASE_URL` | the API service's environment on Render | the deployed API |
| `HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL` (optional) | the API service's environment on Render | the deployed API, for a hospital with its own login |
| `HEALTHCARE_DEMO_CORS_ORIGINS` | the API service's environment on Render | the deployed API |
| `HEALTHCARE_API_BASE` (optional) | GitHub → the same page → **Variables** | the Pages build, instead of `env/.env.published` |

`env/.env.published` at the repository root holds the API's address. It is
committed on purpose, because the Pages build reads it. It is the only file in
the repository with that address: the docs use an API run locally, the page
never shows the address, and `tests/test_publishing_guards.py` fails if the
API's host appears in any other tracked file outside `.github/`. The repository
variable overrides it without a commit, for example to point a branch at a
staging API. Never put a credential in a repository variable: variables are not
masked, so their values appear in public run logs.

### Settings for the public demo

The public demo has no accounts, so the API limits what one visitor can do. The
defaults suit it; each setting is an environment variable on the service.

| Setting | Default | What it does |
|---|---|---|
| `HEALTHCARE_DEMO_MAX_ENTERED_PER_SITE` | 300 | records each hospital's form may add; then 409 |
| `HEALTHCARE_DEMO_RATE_LIMIT_ENTRIES` | 30 | entries and outcomes per client per minute; then 429 |
| `HEALTHCARE_DEMO_RATE_LIMIT_PROBES` | 60 | Guard probes per client per minute |
| `HEALTHCARE_DEMO_RATE_LIMIT_PILOTS` | 3 | pilots per client per minute |
| `HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS` | 10 | approvals per client per minute |
| `HEALTHCARE_DEMO_PROBES_PER_SITE_PER_HOUR` | 600 | Guard probes at one hospital in any 60 minutes, for all visitors together; then 429 |
| `HEALTHCARE_DEMO_PILOTS_PER_HOUR` | 20 | pilots in any 60 minutes, for all visitors together; then 429 |
| `HEALTHCARE_DEMO_TRUST_FORWARDED_FOR` | on when `RENDER` is set | read the client from `X-Forwarded-For` instead of the TCP peer |
| `HEALTHCARE_DEMO_FORWARDED_FOR_HOPS` | 3 when `RENDER` is set, otherwise 1 | proxies in front of the app that append to `X-Forwarded-For` |
| `HEALTHCARE_DEMO_MAX_BODY_BYTES` | 65536 | largest request body; then 413 |

`0` turns a rate limit or an hourly cap off.

The hourly caps bound how fast the database grows. Each Guard probe adds a
record to that hospital's audit chain, and each pilot adds model versions,
contributions and audit records. Nothing deletes them. With the defaults, the
three hospitals' chains grow by at most 1,800 records an hour from probes. The
caps are counted in the API's process, so they start again when the service
restarts. Beyond a cap the API answers 429 with `Retry-After` and a message
that names the limit, for example "The demo's hourly limit for pilots is
reached. Try again in about 12 minutes."

Each proxy appends to `X-Forwarded-For` the address it received the request
from. Everything to the left of those entries came from the client, which can
write any address there. So the client is the entry
`HEALTHCARE_DEMO_FORWARDED_FOR_HOPS` places from the right, and the leftmost
entry is never used. Without the setting, every visitor behind Render would
reach the app from the proxy's address and share one count.

A published test of Render's edge
([johnbeynon/header-test](https://github.com/johnbeynon/header-test)) shows three
appended entries: the client, a Cloudflare address and an internal address. To
check the value on your service, request `/api/v1/framework/status` without an
`X-Forwarded-For` header: `rate_limit_client.forwarded_for_entries` is the number
of entries the proxies appended, and it should equal `forwarded_for_hops`. The
deployed-API tests check this. The limits apply per process, so they slow casual
misuse and are not a security control.

### Adding a hospital is off on every host

A split run on your own machine can add Hospital 290 (`pl-290`) to the
consortium while the demo runs. That starts a process or a container, so the
public demo never offers it: `render.yaml` sets `HEALTHCARE_DEMO_DEPLOYER=off`,
and the app refuses to start with another value when `RENDER` is set. The
hosted API answers `GET /api/v1/deployments` with `available: false` and the
reason, and `POST /api/v1/deployments` with 403.
[Add a hospital while the demo runs](../README.md#add-a-hospital-while-the-demo-runs)
in the README describes the steps.

| Setting | Default | What it does |
|---|---|---|
| `HEALTHCARE_DEMO_DEPLOYER` | `off` | `off`; `process`, which starts the new hospital as a local process; or `compose`, which starts it as a container with `docker compose` |
| `HEALTHCARE_DEMO_JOINING_LMA_URLS` | `pl-290=http://127.0.0.1:8085` | where each hospital that can join will listen. The GMA's `GMA_FEDERATION_JOINABLE_SITES` must list the same pair |
| `HEALTHCARE_DEMO_DEPLOY_TIMEOUT_SECONDS` | 90 | how long the app waits for the new hospital to start and answer its health check |
| `HEALTHCARE_DEMO_DEPLOY_DIR` | a new temporary directory | where the app writes the Compose files it applies and, in process mode, the new hospital's log |
| `HEALTHCARE_DEMO_DEPLOY_IMAGE` | `dagents/healthcare-demo-backend:local` | the image of the new hospital's container, in compose mode |
| `HEALTHCARE_DEMO_DEPLOY_NETWORK` | `healthcare-demo_default` | the Compose network the container joins, in compose mode |

The app checks these settings when it starts, and stops with a message at the
first rule that fails:

1. `HEALTHCARE_DEMO_DEPLOYER` is `off`, `process` or `compose`. With `off`, no
   other rule applies.
2. Only `HEALTHCARE_DEMO_ROLE=app` adds hospitals.
3. `RENDER` is not set.
4. `HEALTHCARE_DEMO_JOINING_LMA_URLS` names at least one hospital, and each one
   is a hospital that can join.
5. No joining hospital is in `HEALTHCARE_DEMO_LMA_URLS`, by id or by address.
6. Each address is `http` with an explicit port.
7. In compose mode, each address is `http://lma-<id>:<port>`, and
   `HEALTHCARE_DEMO_COORDINATOR_URL` is the app's address on the Compose
   network, not 127.0.0.1 or localhost.
8. In process mode, each address is on 127.0.0.1 or localhost.

### Use a pooler host, not the direct host

Supabase shows the direct host `db.<project-ref>.supabase.co` first. That host
has an IPv6 address only, and GitHub Actions runners and most hosts can only
reach IPv4. The connection then fails with `Network is unreachable`, which does
not mention the cause. Use a pooler host:

```
postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

Port 5432 is the session pooler and port 6543 the transaction pooler. Both
work. Every store operation is one transaction that switches role with `SET
LOCAL` (`acting_as` in `agents/common/infrastructure/postgres_roles.py`), and
the connections turn off prepared statements (`prepare_threshold=None`), which a
transaction pooler requires. The user name includes the project ref, and special
characters in the password must be percent-encoded. When a connection to the
direct host fails, `connection_hint()` in `backend/app/services/db_hints.py`
prints this advice.

### `HEALTHCARE_DEMO_CORS_ORIGINS` takes origins, not page URLs

A browser's `Origin` header is the scheme, host and port, never a path. The
address bar value `https://example.github.io/Dagents/healthcare-demo/` matches
no request, so the API blocks the page and the page looks as if the API is down.
The correct value is `https://example.github.io`.

The app reduces each entry to its origin, and `GET /api/v1/framework/status`
reports the resulting list, so you can check the setting with one request. If
the variable is unset, any origin is allowed.

The app repairs three common paste mistakes: a path or trailing slash,
surrounding punctuation such as `(https://example.github.io)`, and an upper-case
host. An entry that does not look like a host is kept as written and reported,
so a real mistake stays visible.

## Setting it up

1. **Create the database.** Create the Supabase project (PostgreSQL 15 or
   newer; 16 or newer keeps the login role from inheriting the site roles). The
   workflow applies the schema, so the database always matches what is
   committed.

2. **Create the schemas and load the records.** Actions → *Healthcare data* →
   Run workflow. The job:
   - prints the project's PostgreSQL version, and warns on 15;
   - builds the planner, applies the migrations to a scratch PostgreSQL 16
     service container, as a non-superuser that stands in for Supabase's
     `postgres` role, and runs the database tests there: the schema, isolation,
     store, pilot, data-entry, API and status tests. It fails if any of them
     skipped, with the same `--require-ran` classes as CI;
   - applies each migration to the project once, recording it in
     `public.schema_migrations`;
   - loads each hospital's records as that hospital's role
     (`scripts/seed_supabase.py`) and reads each count back as that role;
   - proves the isolation on the project: reading another hospital's table, or
     any hospital's table as the coordinator, must fail with
     `InsufficientPrivilege`. It prints each hospital's count.

3. **Deploy the API.** Render → New → Blueprint, and choose
   `apps/healthcare-demo/render.yaml`. It builds `backend/Dockerfile`, which
   compiles `dagentsc` in its first stage and copies the IST extract into the
   image; without the planner, the Guard denies every request. Set
   `HEALTHCARE_DEMO_DATABASE_URL` and `HEALTHCARE_DEMO_CORS_ORIGINS` when asked.

4. **Point the published frontend at it.** Set `HEALTHCARE_DEMO_PUBLIC_API_URL`
   in `env/.env.published` and push. The Pages workflow does the rest.

## Upgrading from the synthetic-data version

The first deployment of this demo (version 2) kept synthetic records in two
shared tables, `public.encounters` and `public.hospitals`. This version keeps
the IST records in one schema per hospital. A merge to `main` starts two things
at once: Render deploys the new API (`autoDeployTrigger: commit` in
`render.yaml`), and the Pages workflow publishes the new page. So the database
must hold the new schemas and records before the merge.

Migration 0002 keeps the version 2 tables, so the old API keeps working while
the new schemas are filled. Upgrade in this order:

| Step | What to do | What a visitor sees |
|---|---|---|
| 1 | Before merging, run **Actions → Healthcare data** with **Use workflow from** set to the branch being merged and `seed` on. It tests the migrations on a scratch database, applies 0002 and 0003, loads the 1,014 IST records into the hospital schemas and proves the isolation. | The old page and the old API, unchanged. The workflow does not touch the tables the old API reads. |
| 2 | Merge the branch into `main`. Render builds and deploys the new API onto data that is already there. The Pages run that the merge starts publishes the new page. | Render serves the old API until the new one passes its health check. The Pages run probes the API before Render's deploy finishes, so it publishes the new page as a replay: the page says it replays a recorded run, names the commit and disables data entry. If Render switches first, the old page calls the new API for a few minutes, and its answers are not what the old page was built for. |
| 3 | Wait until `GET /api/v1/framework/status` reports `"store_layout": "schema-per-site"` and `"ready": true`. If it still lacks those fields after the build, the service did not deploy on its own; see [Deploys are not always automatic](#deploys-are-not-always-automatic). | The replay. |
| 4 | Run **Actions → Pages → Run workflow** on `main`. Its probe now passes. | The new page, calling the new API: each hospital's records from its own schema. |
| 5 | Run **Actions → Healthcare API check**. It fails if any check of the deployed API fails. | No change. |

A later migration drops the version 2 tables. It is not part of this upgrade.

If the new API is deployed before step 1, `GET /api/v1/framework/status`
reports `"ready": false` and `"hospitals": null`, and every request that reads
hospital records returns a JSON `503` that says the database is not set up for
this version of the app. The response carries the CORS headers and
`X-Dagents-App`, so the page shows that message at once instead of waiting for
the API. Run step 1, then steps 3 to 5.

## What the publish check tests

Before publishing the healthcare demo against the live API, the Pages workflow
checks five things:

- `/api/v1/health` answers within 12 attempts, 10 seconds apart, which allows
  for a cold start;
- the API reports `cohort_source: postgres`, so its records are in the hospital
  stores and not in process memory;
- the API reports `ready: true`: it can read every hospital's store, and each
  store holds records;
- the first hospital returns worklist rows;
- the API sends `Access-Control-Allow-Origin` for the Pages origin. The check
  sends an `Origin` header, as a browser does; without one, an API that blocks
  every browser request would look healthy.

If any check fails, the demo is published as a **replay** instead, with a
warning and a line in the run summary that names the reason. These checks never
fail the deploy, so a setting on the API cannot stop the framework site and the
other demo from publishing. The replay page says it is a replay, names its
commit, and disables data entry, so it never presents recorded data as live.

The link preview is checked separately, and it does fail the deploy. The build
writes `HEALTHCARE_DEMO_PUBLIC_SITE_URL` from `env/.env.published` into the
page's `og:url` and `og:image` tags. The workflow stops when that address is
missing, and after the deploy it requests the published image. An extracted
copy of this app sets `PUBLIC_SITE_URL` for its own build in the same way
(`frontend/social-meta.mjs`). The preview image itself,
`frontend/public/social-preview.png`, prints the Dagents address, and an
extracted copy has no render script. A copy published at another address must
replace that image. To render it for the new address, set
`HEALTHCARE_DEMO_PUBLIC_SITE_URL` in a Dagents checkout's `env/.env.published`
and run `node scripts/render_social_cards.mjs` there.

To diagnose the API, run the **Healthcare API check** workflow. It fails when the
API has a problem, and its annotations name the status, the header, the origins
the API allows, whether the stores are ready and each hospital's record count.

## Testing the deployed API

The same workflow runs `tests/test_deployed_api.py` against the deployment. It
checks that:

- the API reports reading Postgres;
- every hospital serves records and a worklist;
- the guard's controls produce different strategies, which shows the planner is
  in the image and answering;
- an aggregate below the minimum cohort is denied;
- an invalid control value returns 422, not 500;
- a pilot reports a decision that matches its own gate results (the test does
  not assume which decision);
- every hospital's audit chain is intact;
- the consortium charts are listed and one draws.

It also reports how long the first request took, compared with the 90 seconds
the page waits.

The tests never enter records, record outcomes or approve models, so they change
no hospital's records. Their probes and pilot add audit records and model
versions, as a visitor's do.

To run it yourself against any deployment:

```bash
cd apps/healthcare-demo
HEALTHCARE_DEPLOYED_API=https://… python -m unittest discover -s tests -t . -p "test_deployed_api.py" -v
```

Without `HEALTHCARE_DEPLOYED_API` it skips with a warning. These are the only
tests that check the service behind the published demo. Every fault this
deployment has had passed the rest of the suite.

### Deploys are not always automatic

`autoDeployTrigger: commit` applies only to a service that Render created from
this blueprint and linked to the branch. A service created by hand, or with
auto-deploy off, keeps serving its last image after new commits. Everything
works except the change you just made. To check, look for a field the committed
code returns that is missing from `GET /api/v1/framework/status`. Use **Manual
Deploy → Deploy latest commit** to fix it.

## Cold starts and latency

The API runs on a free tier that stops the container after about 15 minutes
without traffic, so the first request usually starts it. A measured start took
about 22 seconds. `liveFetch` in `frontend/src/api.ts` retries only requests
that never reached the app, for up to 90 seconds, and the page shows a plain
"connecting" banner while it waits.

Each store operation takes three round trips to the database: one message that
begins the transaction, switches to the site's role, applies the settings and
checks the role; the query; and `COMMIT`. A pilot makes about 145 store
operations, or about 440 round trips (144 operations and 437 round trips
measured), so its time depends on the distance to the database:

| Where the database is | Pilot time |
|---|---|
| in memory (no database) | about 1 second |
| local Postgres | about 1.3 seconds |
| through a proxy that adds a 20 ms round trip | 10.7 seconds |
| through a proxy that adds a 40 ms round trip | 19.7 seconds |

Before the opening statements were sent as one message, the same pilot took 29
seconds at 20 ms and 55 seconds at 40 ms. Keep the API and the database in the
same region.

The page never falls back to the recording. A recording from another commit
shown as live would misreport both the data and the framework. A live build that
cannot reach its API says so.

## What the API returns when something fails

| Problem | Status | Message |
|---|---|---|
| A hospital store cannot be reached, or a database operation fails | 503 | "The hospital records cannot be reached right now. Try again in a minute." |
| The database lacks a schema, table, role or grant this version needs | 503 | "The demo database is not set up for this version of the app yet, so the hospital records cannot be shown. Try again later." |
| The planner cannot run during a pilot | 503 | "The planner is not available right now, so the pilot cannot run. Try again in a minute." |
| Any other error | 500 | "The server could not complete this request." |
| The hourly cap on Guard probes at one hospital is reached | 429, with `Retry-After` | "The demo's hourly limit for Guard probes at Hospital 134 is reached. Try again in about 12 minutes." |
| The hourly cap on pilots is reached | 429, with `Retry-After` | "The demo's hourly limit for pilots is reached. Try again in about 12 minutes." |

Each one is a JSON body, `{"detail": ...}`, with the CORS headers and
`X-Dagents-App`. The page waits and retries only a request that never reached
the app, such as one sent while the service starts. An answer with
`X-Dagents-App` comes from the app, so the page shows its message at once. The
database error itself names the host and the user, so it goes to the service's
log and never into a response.

`GET /api/v1/framework/status` never returns 503. When it cannot read the
stores it reports `"ready": false` and `"hospitals": null`; when every store
can be read it lists each hospital's `record_count` and `labelled_count`.

## Seeding again

Run *Healthcare data* again. By default it replaces every hospital's rows with
the extract, which removes the records people entered. Tick `keep_entries` to
keep those and replace only the dataset rows. The audit chains and the model
registry are never touched.
