# Deploying this demo

The published demo is this app running on a host. It reads synthetic patient
records from a Postgres database, and the OCaml planner in its image makes every
governance and federation decision. This page explains how the parts connect and
where each credential is kept.

A local run needs none of this: no database, no host and no account. See the
README.

## The four parts

| Part | What it is | Defined in |
|---|---|---|
| Encounter store | Supabase Postgres holding the three sites' patient records | `supabase/migrations/` |
| API | this FastAPI app plus `dagentsc`, as a Docker service on Render | `render.yaml`, `backend/Dockerfile` |
| Frontend | the Vite build, published to GitHub Pages under the framework site | `.github/workflows/pages.yml` |
| Schema and seed | a manually started workflow that creates the tables and fills them | `.github/workflows/healthcare-data.yml` |

All records are synthetic. The generator in `backend/app/domain/synthetic.py`
defines what a cohort looks like, and the seed script calls it instead of
creating its own distributions. So the stored data keeps the generator's
deliberate problems for the framework to catch: differences between sites,
missing values, and one site that reports glucose in mg/dL without saying so.
No real patient record is ever used.

## Where the credentials are kept

No secret is in the repository or in this file. Each one is kept in one place:

| Name | Kept in | Used by |
|---|---|---|
| `SUPABASE_DB_URL` | GitHub → Settings → Secrets and variables → Actions → **Secrets** | the schema and seed workflow |
| `HEALTHCARE_DEMO_DATABASE_URL` | the API service's environment on Render | the deployed API |
| `HEALTHCARE_DEMO_CORS_ORIGINS` | the API service's environment on Render | the deployed API |
| `HEALTHCARE_API_BASE` (optional) | GitHub → the same page → **Variables** | the Pages build, instead of `env/.env.published` |

`env/.env.published` at the repository root holds the API's address. It is
committed on purpose: the address appears in the published page's source, so it
is configuration, not a credential. The repository variable overrides it without
a commit, for example to point a branch at a staging API. Never put a
credential in a repository variable: variables are not masked, so their values
appear in public run logs.

### Use the session pooler, not the direct host

Supabase shows the direct host `db.<project-ref>.supabase.co` first. That host
has an IPv6 address only, and GitHub Actions runners and most hosts can only
reach IPv4. The connection then fails with `Network is unreachable`, which does
not mention the cause. Use the session pooler:

```
postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

The user name includes the project ref, and special characters in the password
must be percent-encoded. When a connection to the direct host fails,
`connection_hint()` in `backend/app/services/encounters.py` prints this advice.

### `HEALTHCARE_DEMO_CORS_ORIGINS` takes origins, not page URLs

A browser's `Origin` header is the scheme, host and port, never a path. The
address bar value `https://example.github.io/Dagents/healthcare-demo/` matches
no request, so the API blocks the page and the page looks as if the API is down.
The correct value is `https://example.github.io`.

The app reduces each entry to its origin, and `GET /api/v1/framework/status`
reports the resulting list, so you can check the setting with one request. If
the variable is unset, any origin is allowed, which is acceptable for a
read-only demo that holds no credentials.

The app repairs three common paste mistakes: a path or trailing slash,
surrounding punctuation such as `(https://example.github.io)`, and an upper-case
host. An entry that does not look like a host is kept as written and reported,
so a real mistake stays visible.

## Setting it up

1. **Create the database.** Create the Supabase project. The workflow applies the
   schema, so the database always matches what is committed.

2. **Create the tables and seed them.** Actions → *Healthcare data* → Run
   workflow. Before applying anything, it checks that the migrations still match
   `stroke-triage-features-v2`. It applies each migration once and records it in
   `public.schema_migrations`, seeds from the generator, then reads the data back
   to confirm three sites and some missing values. Without missing values, the
   data quality checks would have nothing to find.

3. **Deploy the API.** Render → New → Blueprint, and choose
   `apps/healthcare-demo/render.yaml`. It builds `backend/Dockerfile`, which
   compiles `dagentsc` in its first stage; without the planner, the Guard denies
   every request. Set `HEALTHCARE_DEMO_DATABASE_URL` when asked.

4. **Point the published frontend at it.** Set `HEALTHCARE_DEMO_PUBLIC_API_URL`
   in `env/.env.published` and push. The Pages workflow does the rest.

## What the publish check tests

Before publishing the healthcare demo against the live API, the Pages workflow
checks four things:

- `/api/v1/health` answers within 12 attempts, 10 seconds apart, which allows
  for a cold start;
- the API reports `cohort_source: supabase`, so it reads the database instead of
  generating records in memory;
- the database returns rows;
- the API sends `Access-Control-Allow-Origin` for the Pages origin. The check
  sends an `Origin` header, as a browser does; without one, an API that blocks
  every browser request would look healthy.

If any check fails, the demo is published as a **replay** instead, with a
warning and a line in the run summary that names the reason. These checks never
fail the deploy, so a setting on the API cannot stop the framework site and the
other demo from publishing. The replay page says it is a replay and names its
commit, so it never presents recorded data as live.

To diagnose the API, run the **Healthcare API check** workflow. It fails when the
API has a problem, and its annotations name the status, the header and the
origins the API allows.

## Testing the deployed API

The same workflow runs `tests/test_deployed_api.py` against the deployment. It
checks that:

- the guard's controls produce different strategies, which shows the planner is
  in the image and answering;
- a cohort below the minimum is denied;
- an invalid control value returns 422, not 500;
- a full pilot produces a candidate that a blocking gate then rejects;
- every site's audit chain is intact.

It also reports how long the first request took, compared with the 90 seconds
the page waits.

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

## Cold starts

The API runs on a free tier that stops the container after about 15 minutes
without traffic, so the first request usually starts it. A measured start took
about 22 seconds. `liveFetch` in `frontend/src/api.ts` retries only requests
that never reached the app, for up to 90 seconds, and the page shows a banner
while it waits.

The page never falls back to the recording. A recording from another commit
shown as live would misreport both the data and the framework. A live build that
cannot reach its API says so.

## Seeding again

Run *Healthcare data* again. By default it replaces the records instead of
adding to them, because doubling every cohort would push every site above its
cohort minimum and change what the demo shows. Pass `append` to keep the
existing records.
