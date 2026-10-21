# LMA Changelog

## Unreleased

- `SiteResult.metrics` and `SiteResult.update_norm` refuse NaN and infinity,
  because the GMA stores rounds as JSON. A local run whose model kind reports
  such a number now ends as a `failed` result whose reason names the
  `ValidationError`, and `jobs:execute` and `jobs:run` answer 200. Before, the
  job log refused the result and the call answered 500.
- Each planner call has limits: `DAGENTSC_TIMEOUT_SECONDS` (default 10),
  `DAGENTSC_MAX_INPUT_BYTES` and `DAGENTSC_MAX_OUTPUT_BYTES` (default 16 MiB
  each). The runner stops a planner that runs past the timeout or prints too
  much, with every process it started, and refuses an input over its limit
  before the planner starts. Each is a planner failure: the Guard denies the
  request and records the denial. Before, a planner that hung blocked the
  caller with no limit.
- Every audit record names the planner: `planner_version` (the `dagents`
  version) and `planner_sha256` (the SHA-256 of the planner binary), denials
  included. Records stored before have neither field, and their chains still
  verify, in memory and in Postgres.
- `DAGENTSC_SHA256` pins the planner binary's SHA-256. With another hash the
  runner starts no planner, the Guard denies every request, and the LMA logs
  its "has no planner" warning with both hashes when it is built.
- The LMA refuses to start when a planner limit is zero, negative or not a
  number, or when `DAGENTSC_SHA256` is not 64 hexadecimal digits.
- The Guard denies a request whose plan names a strategy it cannot apply (an
  unknown name, a missing or extra parameter, or a `generalize` level above
  308), and records the denial. Before, it withheld that field and applied the
  rest of the plan. For the same reason, `GET /site/summary` reports such a
  plan's label rate as `withheld`.
- The Guard denies a request, and records the denial, when applying the plan to
  the records fails, for example on an integer too large for a float. Before,
  `enforce` raised and wrote no audit record.
- A planner answer that nests more than 200 levels deep is a planner failure,
  on every supported Python version: a planner route answers `503`
  `planner_unavailable`. So does `restrictions:plan` when the planner's answer
  is not a restriction plan. Before, both answered `500`.
- The runner hashes only a regular file. A `DAGENTSC_BIN` that names a device
  or a FIFO has no hash, and the Guard denies at once. The hash is kept per
  path, size, modification time and change time, so a binary rewritten in place
  with its old modification time is hashed again.
- The Postgres job log and audit sink check their table names with one shared
  helper, `table_identifier` in `dagents/common/infrastructure/postgres_roles.py`.
  They accept and refuse the same names as before. The audit sink's error now
  starts "the audit table must be", and a table name that is not a string
  raises `ValueError` instead of `AttributeError`.
- The Python package is now `dagents` (it was `agents`), so every import and
  `uvicorn` module path of the framework starts with `dagents.`:
  `uvicorn dagents.lma.main:app`. So does any `LMA_EXTENSIONS` entry that
  named a module inside the framework; an application's own extension, such as
  `app.extension:HealthcareStrokeExtension`, keeps its path. The name `agents`
  belongs to another project on PyPI. The routes and settings are unchanged.
- `python -m dagents.lma`, and the `dagents-lma` command of an installed
  package, serve the LMA on `--host` and `--port`, else `LMA_API_HOST` and
  `LMA_API_PORT`, else `127.0.0.1:8010`.
- The planner runner also looks for the planner a platform wheel bundles at
  `dagents/_bin/dagentsc`, after `DAGENTSC_BIN` and before `dagentsc` on PATH.
- When the LMA is built and the runner cannot start a planner (none is
  found, or `DAGENTSC_BIN` names a file it cannot run), it logs one warning
  that names the reason, `DAGENTSC_BIN` and `python -m dagents`. Before, it
  started without a word and its Guard denied every request.
- An installed `dagents` package reads no env file: `Settings.from_env()`
  loads `env/.env.shared` and `env/.env.lma` only from a source tree (a
  checkout, or an image that copies `dagents/` and `env/` side by side). In
  `site-packages` another distribution could install an `env/` folder and so
  change the LMA's settings.
- `POST /federation/jobs:run` and `POST /api/v1/federation/jobs:run` offer a
  job and, when the site accepts it, run it, in one call. The answer is a
  `JobRun`: the acceptance and, for an accepted job, the result. The checks,
  Guard decisions, job log entries and update upload are those of
  `jobs:offer` then `jobs:execute`, which stay.
- When the GMA fails to store a training update (a 5xx, or a body it did not
  accept), the job's `refusal_reason` names the GMA's status and code only,
  never its detail, which can quote a store error naming a database host or a
  hosting vendor. A refusal (403, 404 or 409) keeps the GMA's reason.
- The planner computes a round's digest once per process: LMAs in one process
  that are offered the same round at the same time wait for the first one's
  answer. A planner that could not be run is asked again, so the job is still
  refused.
- `GET /site/model` also returns the released version's `parent_version`,
  `created_at` and `release_decision`, as the GMA's registry records them.
- The LMA can serve one site. With `LMA_SITE_ID` and `LMA_CONDITION_ID` it reads
  the site's records through a site source that an extension contributes, plans
  against the condition pack's classification in strict mode, and serves the
  site routes (`/site`, `/site/summary`, `/site/profile`, `/site/source:refresh`,
  `/site/model`, `/site/model:evaluate`, `/governance/restrictions:probe`,
  `/governance/audit:summary`) and the federated job routes
  (`/federation/registration`, `/federation/jobs:offer`,
  `/federation/jobs:execute`, `/federation/jobs`), each with a short and an
  `/api/v1` path.
- A training job's clipped update goes to the GMA's update store over HTTP
  (`adapters/http_update_channel.py`). The job log records each refused offer and
  each result, in memory or in a Postgres table (`infrastructure/job_log.py`).
- Routes take bearer tokens of the `site` and `coordinator` scopes when tokens
  are configured. Without tokens the LMA runs open, as before.
- `config.py` reads nothing at import; `Settings.from_env()` reads the `LMA_*`
  variables. `app_factory.create_app(settings, deps)` builds one LMA, so one
  process can serve several.
- Errors are answered as `{detail, code}`. A planner failure on
  `restrictions:plan` now answers 503 `planner_unavailable` instead of an
  unhandled 500.
- Stored source definitions come back with their connection options redacted.
- The image builds the `dagentsc` planner and puts it on `PATH`.
- Added initial Local Monitoring Agent scaffold
- Added layered package layout and placeholder FastAPI entrypoint
