# GMA Changelog

## Unreleased

- Each planner call has limits: `DAGENTSC_TIMEOUT_SECONDS` (default 10),
  `DAGENTSC_MAX_INPUT_BYTES` and `DAGENTSC_MAX_OUTPUT_BYTES` (default 16 MiB
  each). The runner stops a planner that runs past the timeout or prints too
  much, with every process it started, and refuses an input over its limit
  before the planner starts. Each is a planner failure: the release Guard
  denies and records the denial, and a planner route answers `503`
  `planner_unavailable`. Before, a planner that hung blocked the caller with no
  limit.
- Every audit record of the release Guard names the planner:
  `planner_version` (the `dagents` version) and `planner_sha256` (the SHA-256
  of the planner binary), denials included. Records stored before have neither
  field, and their chains still verify, in memory and in Postgres.
- `DAGENTSC_SHA256` pins the planner binary's SHA-256. With another hash the
  runner starts no planner, and the GMA logs its "has no planner" warning with
  both hashes when it is built.
- The GMA refuses to start when a planner limit is zero, negative or not a
  number, or when `DAGENTSC_SHA256` is not 64 hexadecimal digits.
- The release Guard denies a request whose plan names a strategy it cannot
  apply (an unknown name, a missing or extra parameter, or a `generalize`
  level above 308), and records the denial. It also denies, and records, a
  request when applying the plan to the records fails. Before, `enforce`
  raised and wrote no audit record.
- A planner answer nested too deeply to read is a planner failure: a planner
  route such as `rounds:plan` answers `503` `planner_unavailable`. Before, it
  answered `500`.
- The runner hashes only a regular file. A `DAGENTSC_BIN` that names a device
  or a FIFO has no hash, and the release Guard denies at once. The hash is kept
  per path, size, modification time and change time, so a binary rewritten in
  place with its old modification time is hashed again.

- The Python package is now `dagents` (it was `agents`), so every import and
  `uvicorn` module path of the framework starts with `dagents.`:
  `uvicorn dagents.gma.main:app`. So does any `GMA_EXTENSIONS` entry that
  named a module inside the framework; an application's own extension, such as
  `app.extension:HealthcareStrokeExtension`, keeps its path. The name `agents`
  belongs to another project on PyPI. The routes and settings are unchanged.
- `python -m dagents.gma`, and the `dagents-gma` command of an installed
  package, serve the GMA on `--host` and `--port`, else `GMA_API_HOST` and
  `GMA_API_PORT`, else `127.0.0.1:8020`.
- The planner runner also looks for the planner a platform wheel bundles at
  `dagents/_bin/dagentsc`, after `DAGENTSC_BIN` and before `dagentsc` on PATH.
- When the GMA is built and the runner cannot start a planner (none is
  found, or `DAGENTSC_BIN` names a file it cannot run), it logs one warning
  that names the reason, `DAGENTSC_BIN` and `python -m dagents`. Before, it
  started without a word and its Guard denied every request.
- An installed `dagents` package reads no env file: `Settings.from_env()`
  loads `env/.env.shared` and `env/.env.gma` only from a source tree (a
  checkout, or an image that copies `dagents/` and `env/` side by side). In
  `site-packages` another distribution could install an `env/` folder and so
  change the GMA's settings.
- New setting `GMA_JOINABLE_SITE_ENDPOINTS`: sites that may join while the GMA
  runs, each with the one address the GMA accepts for it. New routes
  `PUT /federation/sites/{site_id}/address`, `DELETE
  /federation/sites/{site_id}/address` and `GET /federation/site-addresses`,
  with their `/api/v1` forms, all for the `admin` token. An accepted address
  goes into the agent network, so the site refresh, the summaries and round
  dispatch call that LMA. Refusals: `403` `not_joinable`, `403`
  `address_differs`, `409` `configured_elsewhere`, `409` `configured`, `422`.
  No answer repeats an address. The GMA refuses to start with a joinable site
  that is also configured, has a bad address, lacks its tokens or has no HTTP
  network to join.
- The HTTP engine sends each selected site one `POST /api/v1/federation/jobs:run`
  call per round instead of `jobs:offer` then `jobs:execute`. A round's
  dispatch has one row per site, with the step `run`. A run call that fails
  counts the site as not having accepted the job.
- With the HTTP engine, a dispatched round carries the planner's readiness: the
  engine collects every result while it dispatches, so the GMA asks the planner
  then and stores the answer on the round. `aggregate` still asks again. If the
  readiness call fails, the round is closed and its updates are deleted.
- A dispatch row's reason for a failed job call keeps the LMA's status and
  error code only. The LMA's detail, which can quote a store error naming a
  database host, goes to the GMA's log with addresses removed.
- An update upload takes the model kind the round's dispatch loaded, so a
  training round reads its starting model from the model store once instead of
  once per site.
- New setting `GMA_SUMMARY_TIMEOUT_SECONDS` (default 30): how long the site
  directory waits for each LMA. An LMA that takes longer is reported as not
  reachable.
- A round-flow release evaluation returns the release Guard's whole decision:
  `guard` gains `request_id`, `boundary`, `payload` (one row with the size of the
  candidate's change), `withheld_fields` and `transformed_fields`.
- The GMA can run a study's rounds over HTTP. With `GMA_ENGINE=http` its engine
  (`adapters/http_federation_engine.py`) offers each round's job to the selected
  sites' LMAs and runs it where it was accepted, up to
  `GMA_DISPATCH_CONCURRENCY` sites at a time. A missing LMA, a timeout or an
  HTTP error gives a `failed` result with its reason; a refusal gives a
  `rejected` result. Each call is stored on the round as a dispatch row.
- A training round's clipped updates go to a write-only update store
  (`PUT /federation/rounds/{round_id}/updates/{site_id}`). Aggregation checks
  each update's digest, combines the updates with the planner's weights through
  the model kind an extension contributes, registers the candidate and deletes
  the updates. `POST /federation/rounds/{round_id}:discard` deletes them too.
- New model version routes (`/model-versions`, `/model-versions:released`,
  `/model-versions/{version}`, `PUT /model-versions/{version}`,
  `/model-versions/{version}:approve`). Every status change comes from the
  planner's `federation model lifecycle` command, and an approval refusal
  carries the planner's `refusal` and sentence.
- Release evaluation has a round flow: with a `condition_id`, the GMA reads the
  gates from the condition pack and the metrics from the rounds, runs the
  `before_release` Guard and the lifecycle rule, and stores the decision with
  the model version. It answers a `ReleaseEvaluation`. Without a
  `condition_id` it evaluates the gates in the body as before, runs no Guard
  and changes no model version.
- New site routes: `POST /federation/sites:refresh` enrols each configured LMA
  from its own registration, and `GET /federation/sites/summaries` collects
  what each LMA reports, with its audit chain state when asked.
- `GET /governance/audit:summary` reports the audit chain's state and length.
  `GET /health` names the engine.
- A reused round id answers 409, a result from a site the plan did not select
  403, and a result for an aggregated or closed round 409. The readiness route
  no longer saves its answer.
- Routes take bearer tokens of the `admin` and `site` scopes when tokens are
  configured; a `site` token is bound to its site. Without tokens the GMA runs
  open, as before. `GMA_REQUIRE_TOKENS=1` refuses to start without one.
- `GMA_ALLOW_SOURCE_READS=0` makes the source, dataset profile and model job
  routes answer 403. Stored source definitions come back with their connection
  options redacted.
- A GMA with condition packs (from `GMA_EXTENSIONS` or an application's
  registry) refuses a request whose classification is not the registered one.
- Model versions and the release Guard's audit chain can be kept in Postgres
  (`GMA_DATABASE_URL`, `GMA_DATABASE_ROLE`, `GMA_MODEL_TABLE`,
  `GMA_AUDIT_TABLE`, `GMA_AUDIT_ANCHOR_TABLE`).
- `config.py` reads nothing at import; `Settings.from_env()` reads the `GMA_*`
  variables. `app_factory.create_app(settings, deps)` builds one GMA, so one
  process can serve several, and the routes are in `routes.py`.
- Errors are answered as `{detail, code}`. A planner that cannot be run answers
  503 `planner_unavailable` instead of an unhandled 500.
- The image builds the `dagentsc` planner and puts it on `PATH`.
- Added initial Global Monitoring Agent scaffold
- Added layered package layout and placeholder FastAPI entrypoint
