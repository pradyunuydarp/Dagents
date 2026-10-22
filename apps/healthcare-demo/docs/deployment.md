# Deploying this demo

The published demo is this app running on a host. It keeps each hospital's
records in its own schema of a Postgres database. The three Dagents LMAs and the
GMA run inside the API's process, and the API calls their routes through
`httpx.ASGITransport`, without a socket. The agents run the OCaml planner in the
image for every governance and federation decision. This page explains how the
parts connect and where each credential is kept.

A local run needs none of this: no database, no host and no account. Each
hospital then keeps its records in memory, loaded from the committed extract.
See the README.

## The four parts

| Part | What it is | Defined in |
|---|---|---|
| Hospital stores | Supabase Postgres: one schema and one role per hospital, plus a coordinator schema | `supabase/migrations/` |
| API | this FastAPI app, its three LMAs and its GMA in one process, plus `dagentsc`, as a Docker service on Render | `render.yaml`, `backend/Dockerfile` |
| Frontend | the Vite build, published to GitHub Pages under the framework site | `.github/workflows/pages.yml` |
| Schema and seed | a manually started workflow that migrates, loads each hospital's records and checks the isolation | `.github/workflows/healthcare-data.yml` |

The records are the 1,014 IST records in `data/ist/ist_three_hospitals.csv`,
and the 263 records of Hospital 290, the hospital a local run can add while
the demo runs, in `data/ist/ist_joining_hospitals.csv` (see
[`data/ist/NOTICE.md`](../data/ist/NOTICE.md) for the source and licence).
The seed loads each hospital's rows into that hospital's schema, acting as that
hospital's role. The hosted API never adds Hospital 290, so on the hosted
database its schema is seeded and never read. Records entered on the page are stored the same way, with
`origin = 'entered'`.

## The database layout

Migrations `supabase/migrations/0002_site_isolation.sql`,
`0003_coordinator_records.sql` and `0004_joining_site.sql` create:

| Schema | Owned by | Tables |
|---|---|---|
| `site_uk_134`, `site_it_002`, `site_ch_245` (0002) and `site_pl_290` (0004, the hospital that can join while the demo runs) | the role with the same name | `encounters` (the records), `guard_audit` (the audit chain of that hospital's LMA) |
| `coordinator` | `dagents_coordinator` | `models` (the GMA's model registry), `site_contributions` (each hospital's LMA job log: what it sent in each round), `pilot_reports` (the app's newest 20 pilot reports), `guard_audit` (the audit chain of the GMA's release Guard) |

- The five roles are `NOLOGIN`. On PostgreSQL 16 or later the migration grants
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

The API and its agents run every database operation in one transaction that
starts with `SET LOCAL ROLE <role>` and checks `current_user`, so each query
runs as one hospital or as the coordinator. The process holds four kept
connections: one per hospital store and one for the coordinator. Each LMA uses
its hospital store's connection, acting as the hospital's role, for its reads,
its audit chain and its job log. The GMA uses the coordinator's connection,
acting as `dagents_coordinator`, which has no grant on any hospital schema.

The agents need no migration of their own: they use the tables that
migrations 0002 and 0003 create. The model rows a database already holds for
the seed rule and the initial logistic regression match what the app registers
with the GMA, so the registration changes nothing, and each audit chain
continues where it ended.

### What the coordinator sees of the audit chains

Each hospital's LMA writes its Guard's decisions to that hospital's own
`guard_audit` table. The GMA's release Guard writes to
`coordinator.guard_audit`. A pilot report tells the coordinator, for each
chain, only whether it verifies and how many decisions it holds
(`site_audits` and `coordinator_audit`, which the app reads through the GMA).
The decisions themselves stay where they were written. A hospital's own view,
`GET /api/v1/hospitals/{id}/audit`, lists its recent decisions, from its LMA.

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
  `dagents/common/infrastructure/postgres_audit.py` hashes every record.
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

The agents' tokens are not on this list. In process, the API makes a new token
for each agent and scope when it first builds the agents (on the first request
that needs them), with `secrets.token_urlsafe`, and never logs them. Only
network mode reads tokens from the environment (`HEALTHCARE_DEMO_GMA_TOKEN` and
`HEALTHCARE_DEMO_LMA_<SITE>_TOKEN`); set them as secrets of the service, never
in a file.

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
| `HEALTHCARE_DEMO_MAX_IN_FLIGHT` | 12 | public requests the API works on at once; the health routes are never held back |
| `HEALTHCARE_DEMO_IN_FLIGHT_WAIT_SECONDS` | 20 | how long a request waits for a slot; then 503 with `Retry-After: 60` |

`0` turns a rate limit or an hourly cap off.

The agents have settings of their own. `render.yaml` sets each of them, with
these defaults:

| Setting | Default | What it does |
|---|---|---|
| `HEALTHCARE_DEMO_AGENTS` | `in-process` | `in-process` runs the LMAs and the GMA in the API's process; `network` calls them as separate processes and needs `HEALTHCARE_DEMO_DATABASE_URL` |
| `HEALTHCARE_DEMO_AGENT_THREAD_TOKENS` | 64 | in process, how many agent handlers may run at once across the four agents; the API refuses to start with fewer than 4 × `HEALTHCARE_DEMO_MAX_IN_FLIGHT` + 8 |
| `HEALTHCARE_DEMO_DISPATCH_CONCURRENCY` | 3 | how many hospitals the GMA sends a round's jobs to at once, from 1 to 64 |
| `HEALTHCARE_DEMO_REQUEST_TIMEOUT_SECONDS` | 2.0 | how long each agent's health check in the status report may take |
| `HEALTHCARE_DEMO_DEPLOYER` | `off` | whether a hospital can join while the demo runs. `render.yaml` sets `off`, and the API refuses to start with `process` or `compose` when `RENDER` is set or the agents run in process. The other `HEALTHCARE_DEMO_DEPLOY_*` settings are left unset. To add a hospital on one machine, see [Add a hospital while the demo runs](../README.md#add-a-hospital-while-the-demo-runs) |

A public request can make nested calls to the agents, and in process every
agent handler takes one of the shared threads. Without the cap on requests in
progress, a burst of requests could hold every thread while each waits for an
agent, and none would finish. The startup check keeps enough threads for the
cap. A request takes its slot only after its whole body has arrived, so a
client that sends a body slowly holds no slot.

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
LOCAL` (`acting_as` in `dagents/common/infrastructure/postgres_roles.py`), and
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
   - builds the planner and, on a scratch PostgreSQL 16 service container,
     runs the framework's database tests first: role scoping, the kept
     connection, the GMA's model store and registry, the LMA's job log and the
     GMA's Postgres wiring;
   - applies the migrations to the same scratch database, as a non-superuser
     that stands in for Supabase's `postgres` role, and runs the demo's
     database tests there: the schema, isolation, store, pilot, data-entry, API
     and status tests. It fails if any framework or demo test class skipped,
     with the same `--require-ran` classes as CI;
   - applies each migration to the project once, recording it in
     `public.schema_migrations`;
   - loads each hospital's records as that hospital's role
     (`scripts/seed_supabase.py`) and reads each count back as that role;
   - proves the isolation on the project: reading another hospital's table, or
     any hospital's table as the coordinator, must fail with
     `InsufficientPrivilege`. It prints each hospital's count.

3. **Deploy the API.** Render → New → Blueprint, and choose
   `apps/healthcare-demo/render.yaml`. It builds `backend/Dockerfile`, which
   compiles `dagentsc` in its first stage and copies the IST extract and the
   framework's `dagents/` package into the image; without the planner, the Guard
   denies every request. The blueprint is still one free web service: the
   agents run inside it and need no service or secret of their own. Set
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
| 1 | Before merging, run **Actions → Healthcare data** with **Use workflow from** set to the branch being merged and `seed` on. It tests the migrations on a scratch database, applies 0002, 0003 and 0004, loads the 1,014 IST records and Hospital 290's 263 records into the hospital schemas and proves the isolation. | The old page and the old API, unchanged. The workflow does not touch the tables the old API reads. |
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

## Run the published layout on one machine

The published page is a production build on another origin than the API.
`scripts/run_frontend_demo.sh` serves the page through the Vite dev server,
whose proxy sends `/api` to the API on the page's own origin. That run cannot
show a CORS setting that blocks the page, a cold start, or a database that is
not ready. `scripts/run_published_check.sh` runs the published layout on one
machine and drives it with `frontend/smoke.mjs` in Chromium:

| Part | Published | In the check |
|---|---|---|
| Page | the build under `/Dagents/healthcare-demo/` on GitHub Pages | the same build (`DEMO_BASE_PATH`, `VITE_HEALTHCARE_API_BASE`), served by `vite preview` at `http://localhost:<port>/Dagents/healthcare-demo/` |
| API | Render, with the agents in its process | `scripts/run_local_demo.sh` at `http://127.0.0.1:<port>`, with the agents in its process and the settings `render.yaml` sets, except the bind address and the two settings for Render's proxy (`HEALTHCARE_DEMO_TRUST_FORWARDED_FOR`, `HEALTHCARE_DEMO_FORWARDED_FOR_HOPS`) |
| `HEALTHCARE_DEMO_CORS_ORIGINS` | the Pages origin | the page's origin, exactly |
| Records | Supabase Postgres | a new PostgreSQL 16 database for each phase |
| Database login | Supabase's `postgres` role, which is not a superuser | `supa_like`, the stand-in for that role from `supabase/emulate_supabase_roles.sql`, as in the healthcare suite. The migrations, the seed and the API all use it. |

The page and the API are on different hosts and ports, so the browser treats
them as two origins and sends a CORS preflight before each POST, as it does on
the published site. The check runs three phases:

1. **Unmigrated.** The API's database has none of the app's tables. The
   consortium page, a hospital page and the data-entry page must each show the
   API's own 503 sentence ("The demo database is not set up for this version of
   the app yet …") once, never show the connecting banner, and send no request
   twice. The page can only tell the API's 503 from the platform's if it can
   read `X-Dagents-App` across origins.
2. **Empty.** `scripts/apply_migrations.py` has run and no records are loaded.
   The consortium page and a hospital page must each show the not-ready banner
   once, say that the store holds no records instead of showing zeros or empty
   tables, and show the reason the API gives for each chart.
3. **Seeded.** `scripts/seed_supabase.py` has loaded the records. The page
   opens before the API starts, so the browser's connections are refused and
   the page must show the "Connecting to the demo server" banner. The script
   then starts the API, and the banner must go and the hospitals table load
   within the page's 90-second wait. Then the full smoke test runs against the
   page with `--api-base`. It also checks that the page reads `X-Dagents-App`
   from the API, that a POST with `Content-Type` and `Idempotency-Key` passes
   the browser's CORS preflight, that the guard's probe and a data entry go to
   the API's origin, that the agents run in the API's process, and that the
   records are in Postgres.

To run it, give it a superuser login to a PostgreSQL 16 server, the one the
healthcare suite uses, and the planner:

```bash
export HEALTHCARE_DEMO_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  apps/healthcare-demo/scripts/run_published_check.sh
```

- It creates three databases with new names and drops them when it exits. The
  superuser login only creates, prepares and drops them. In each one the script
  adds Supabase's roles and gives `supa_like` the password the healthcare suite
  gives it, so the two can run at the same time.
- `supa_like`, the API roles and the hospitals' `NOLOGIN` roles belong to the
  whole server, so the script leaves them.
- It picks free ports for the page and the API, and never builds the planner.
- It runs `npm ci` in `frontend/` when `node_modules` is missing, and needs
  Chromium (`CHROMIUM_PATH`, as for the smoke test).
- Three runs took 172 to 200 seconds on a development machine with 4 CPUs
  that was running other test suites at the same time.
- It exits 0 when every phase passed, 1 when a check failed, and 2 when the
  smoke test could not start a browser. Exit 2 is a skip, not a pass.

The `published-layout` job in `.github/workflows/ci.yml` runs the same script
against a PostgreSQL 16 service container on every pull request. It fails
unless each phase printed its passing lines.

A failure in phase 1 or in the first checks of phase 3 usually means the API's
CORS settings in `backend/app/main.py` changed. The page needs
`X-Dagents-App` in `expose_headers`, and `Content-Type` and `Idempotency-Key`
allowed by `allow_headers`. A failure whose API log says `permission denied`
means a query ran without switching to the hospital's role, or a migration
left out a grant. A superuser login would hide both.

What the check does not cover:

- It runs no proxy in front of the API and no CDN in front of the page, so it
  cannot see a fault in Render's or GitHub's configuration. The **Healthcare
  API check** workflow tests the deployed API.
- It connects to Postgres directly, not through Supabase's pooler.
- `supa_like` does not hold `pg_read_all_data`, which Supabase also grants its
  `postgres` role (see "One credential on Supabase"). The healthcare suite
  tests the isolation with that grant.

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
about 22 seconds, before the agents ran inside the API. The API builds its
stores and its agents on the first request that needs them, not at start, so
the health check answers first. `liveFetch` in `frontend/src/api.ts` retries
only requests that never reached the app, for up to 90 seconds, and the page
shows a plain "connecting" banner while it waits.

Each store operation takes three round trips to the database: one message that
begins the transaction, switches to the site's role, applies the settings and
checks the role; the query; and `COMMIT`. A pilot makes about 425 round trips
(421 to 430 measured); the first pilot after a start makes about 50 more (473
measured). The GMA sends each round's job to the three hospitals at once, one
`jobs:run` call each, so the hospitals' database operations overlap. A pilot's
time still depends on the distance to the database. Measured at full CPU on a
development machine, with the planner; each time is the mean of three pilots:

| Where the database is | Pilot time |
|---|---|
| in memory (no database) | about 1.5 seconds |
| local Postgres | about 1.8 seconds |
| through a proxy that adds a 20 ms round trip | about 6.5 seconds |
| through a proxy that adds a 40 ms round trip | about 11 seconds |

The hosted service has a fraction of one CPU, so its pilots take longer. In a
container limited to a tenth of a CPU, a pilot took about 22 seconds through
the 20 ms proxy and about 37 seconds through a proxy that adds a 110 ms round
trip. Keep the API and the database in the same region.

The page never falls back to the recording. A recording from another commit
shown as live would misreport both the data and the framework. A live build that
cannot reach its API says so.

## What the API returns when something fails

| Problem | Status | Message |
|---|---|---|
| A hospital store cannot be reached, or a database operation fails | 503 | "The hospital records cannot be reached right now. Try again in a minute." |
| The database lacks a schema, table, role or grant this version needs | 503 | "The demo database is not set up for this version of the app yet, so the hospital records cannot be shown. Try again later." |
| The planner cannot run during a pilot | 503 | "The planner is not available right now, so the pilot cannot run. Try again in a minute." |
| The planner cannot run for a page that shows the model in use (the GMA checks each model version with it) | 503 | "The planner is not available right now. Try again in a minute." |
| A Dagents agent did not answer in time | 503 | "Part of the demo server did not answer in time. Try again in a minute." |
| A hospital's LMA could not get what it needed from the GMA, for example because the GMA's store failed | 503 | "Part of the demo server could not complete this request. Try again in a minute." |
| More public requests are in progress than the cap allows, and no slot freed in time | 503, with `Retry-After: 60` | "The demo server is busy. Try again in a minute." |
| Any other error | 500 | "The server could not complete this request." |
| The hourly cap on Guard probes at one hospital is reached | 429, with `Retry-After` | "The demo's hourly limit for Guard probes at Hospital 134 is reached. Try again in about 12 minutes." |
| The hourly cap on pilots is reached | 429, with `Retry-After` | "The demo's hourly limit for pilots is reached. Try again in about 12 minutes." |

Each one is a JSON body, `{"detail": ...}`, with the CORS headers and
`X-Dagents-App`. The page waits and retries only a request that never reached
the app, such as one sent while the service starts. An answer with
`X-Dagents-App` comes from the app, so the page shows its message at once. The
database error itself names the host and the user, so it goes to the service's
log and never into a response. An agent's error is logged the same way, with
any address in it replaced, and the visitor gets the app's own message.

`GET /api/v1/framework/status` never returns 503. When it cannot read the
stores it reports `"ready": false` and `"hospitals": null`; when every store
can be read it lists each hospital's `record_count` and `labelled_count`, as
each LMA counts them. Its `agents` field says how the agents run
(`"mode": "in-process"` on the hosted service), the GMA's engine, and whether
each LMA answered. It waits at most 15 seconds for the GMA's site summaries,
and the GMA waits at most 10 seconds for each LMA
(`GMA_SUMMARY_TIMEOUT_SECONDS`). The GMA's engine name and each agent's health
check wait at most `HEALTHCARE_DEMO_REQUEST_TIMEOUT_SECONDS` (2 seconds by
default). So an agent that does not answer makes the report say "not ready"
instead of holding the request.

## Seeding again

Run *Healthcare data* again. By default it replaces every hospital's rows with
the extract, which removes the records people entered. Tick `keep_entries` to
keep those and replace only the dataset rows. The audit chains and the model
registry are never touched.
