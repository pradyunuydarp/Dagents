# Deploying this demo

The published demo is not a mock. It is this app, running on a host, reading synthetic encounters
out of a Postgres database, with the OCaml planner in the image making every governance and
federation decision. This describes how that is wired and where each credential lives.

Running it locally needs none of this — no database, no host, no account. See the README.

## The four pieces

| Piece | What it is | Defined in |
|---|---|---|
| Encounter store | Supabase Postgres holding the three sites' cohorts | `supabase/migrations/` |
| API | this FastAPI app plus `dagentsc`, as a Docker service | `render.yaml`, `backend/Dockerfile` |
| Frontend | the Vite build, published to GitHub Pages under the framework site | `.github/workflows/pages.yml` |
| Schema + seed | a manually dispatched workflow that migrates and seeds | `.github/workflows/healthcare-data.yml` |

Everything is synthetic. The generator in `backend/app/domain/synthetic.py` stays the source of
what a cohort looks like, and the seed script calls it rather than inventing its own
distributions — so the per-site differences, the recording gaps and the site that reports glucose
in mg/dL without saying so are all still there to be caught. **Storing these records in a real
database does not make them real patients, and no real record is ever used.** A demonstration of
privacy controls carrying real patient data would be self-refuting.

## Where the credentials live

No secret is in the repository, and none is in this file. Each lives in exactly one place:

| Name | Lives in | Used by |
|---|---|---|
| `SUPABASE_DB_URL` | GitHub → Settings → Secrets and variables → Actions → **Secrets** | the migrate-and-seed workflow |
| `HEALTHCARE_DEMO_DATABASE_URL` | the API service's own environment on the host | the deployed API |
| `HEALTHCARE_DEMO_CORS_ORIGINS` | the same | the deployed API |
| `HEALTHCARE_API_BASE` (optional) | GitHub → the same page → **Variables** | the Pages build, overriding `env/.env.published` |

`env/.env.published` at the repository root holds the API's address. That is deliberate: it is a
published URL — it ends up in the page source, readable by anyone who opens the demo — so it is
configuration rather than a credential, and keeping one committed copy beats a literal in a
workflow. The repository variable overrides it without a commit, which is how a branch gets
pointed at a staging API.

### Use the pooler, not the direct host

Supabase shows `db.<project-ref>.supabase.co` first, and that host resolves to an **IPv6 address
only**. GitHub Actions runners and most hosts' egress are IPv4-only, so it is unreachable from
both, and the failure arrives as a bare `Network is unreachable` with nothing pointing at the
cause. Use the session pooler:

```
postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
```

The username carries the project ref, and a password with special characters has to be
percent-encoded. `connection_hint()` in `backend/app/services/encounters.py` prints this when a
connection to the direct host fails, so the next person loses minutes rather than an afternoon.

### `HEALTHCARE_DEMO_CORS_ORIGINS` takes origins, not page URLs

A browser's `Origin` header is scheme, host, and port — never a path. So
`https://example.github.io/Dagents/healthcare-demo/`, which is what you get by
copying the address bar, matches nothing: the API refuses every request before it
arrives, and the published page looks down rather than blocked. The right value
is `https://example.github.io`.

The app normalizes each entry to its origin for exactly this reason, and
`GET /api/v1/framework/status` reports the list it ended up with, so the setting
can be checked in one request instead of inferred from a blank page. Leaving the
variable unset allows any origin, which is a reasonable default for a read-only
demo holding no credentials.

## Standing it up

1. **Create the database.** Nothing to do beyond creating the Supabase project; the schema is
   applied by the workflow, not by hand, so what is in the database is what is committed.

2. **Apply the schema and seed it.** Actions → *Healthcare data* → Run workflow. It checks the
   migrations still match `stroke-triage-features-v2` **before** applying anything, applies each
   migration once and records it in `public.schema_migrations`, seeds from the generator, and then
   reads the database back to confirm three sites and non-zero recording gaps. A seeded database
   with no missingness would make the data-quality path unreachable and nothing else would notice.

3. **Deploy the API.** Render → New → Blueprint → point it at `apps/healthcare-demo/render.yaml`.
   It builds `backend/Dockerfile`, which compiles `dagentsc` in a first stage — the service cannot
   work without it, because the Guard fails closed and an image missing the planner denies every
   request. Set `HEALTHCARE_DEMO_DATABASE_URL` when prompted.

4. **Point the published frontend at it.** Set `HEALTHCARE_DEMO_PUBLIC_API_URL` in
   `env/.env.published` and push. The Pages workflow does the rest.

## What the publish gate checks

Before the healthcare demo is published against a live API, the Pages workflow probes it. Two
kinds of failure, treated differently on purpose:

- **It cannot be woken** — the deploy continues and the demo publishes as a replay of its
  capture, labelled as one on the page, with a warning and a line in the run summary. Failing the
  whole deploy would also stop the framework site and the other demo from shipping over somebody
  else's outage.
- **It answers, but wrongly** — `cohort_source` is not `supabase` (it is generating cohorts in
  process, so the page would claim live data for invented data), the worklist is empty, or the
  API does not allow browser requests from the Pages origin (every request would be blocked and
  the demo would look down rather than misconfigured). Each fails the deploy, because each is a
  one-line fix on the service and nobody goes looking for it behind a warning.

## Cold starts

The API runs on a free tier that stops the container after about fifteen minutes without traffic,
so most visitors arrive at a sleeping service and the first request is the one that wakes it. The
frontend waits this out and says what it is waiting for: `liveFetch` in `frontend/src/api.ts`
retries only the statuses that mean the request never reached the app, for up to ninety seconds,
and the page shows a banner while it does.

What it will not do is fall back to the recording. A capture from another commit presented as live
data would misreport both the data and the framework's behaviour, and the reader would have no way
to tell. A live build that cannot reach its backend says so.

## Re-seeding

Run *Healthcare data* again. It replaces the encounters by default rather than adding to them: a
reseed that silently doubled every cohort would move every site above its floor and quietly change
what the demo demonstrates. Pass `append` to keep what is there.
