# GMA Changelog

## Unreleased

- New settings build the round state stores on the coordinator's one kept
  connection: `GMA_ROUND_TABLE` (`PostgresRoundRepository`),
  `GMA_ENROLMENT_TABLE` (`PostgresEnrolmentStore`), and the opt-in
  `GMA_UPDATE_TABLE` with `GMA_UPDATE_ROLE` (`PostgresUpdateStore`, acting as
  that role). `GMA_UPDATE_MAX_AGE_SECONDS` limits how old a stored update may
  be. Its default of 86400 applies when the update store survives a restart.
  A GMA that keeps its updates in memory and sets no limit runs no age check
  (new `update_age_limit()` and `GmaState.update_age_limit_seconds`). A store
  in `GmaDeps` takes the place of its setting,
  and building the app still opens no connection. `create_app` refuses: a
  table without `GMA_DATABASE_URL` or `GMA_DATABASE_ROLE`; `GMA_ROUND_TABLE`
  without `GMA_MODEL_TABLE` and `GMA_AUDIT_TABLE` (or their stores in
  `GmaDeps`); `GMA_UPDATE_TABLE` without `GMA_ROUND_TABLE` or
  `GMA_UPDATE_ROLE`; `GMA_UPDATE_ROLE` equal to `GMA_DATABASE_ROLE`;
  `GMA_UPDATE_ROLE` or `GMA_UPDATE_MAX_AGE_SECONDS` without an update table;
  and `GMA_UPDATE_TABLE`, `GMA_UPDATE_MAX_AGE_SECONDS` or an update store in
  `GmaDeps` that survives a restart without `GMA_ENGINE=http`, because the
  simulator neither reads nor deletes stored updates.
  The messages name settings, never values. `GmaState` gains `round_store`,
  `enrolments` and `coordinator_lease`.
- The GMA recovers the rounds a restart left unfinished, once, with
  `FederatedRoundController.recover` (new `GmaState.ensure_recovered()`).
  Under uvicorn the lifespan runs it and waits up to 10 seconds
  (`START_RECOVERY_WAIT_SECONDS`, `GmaState.start_recovery_wait_seconds`)
  before the GMA serves. A longer recovery goes on while the GMA serves. The
  lifespan logs a failure instead of raising it, so `/health` still answers.
  In process the first
  round write runs it. The round writes (dispatch, posted results,
  `:aggregate`, `:discard`, `:evaluate-release` and the update upload) wait
  for it through an async dependency; it runs on the GMA's own handler
  threads, so a waiting request holds no server thread. While it fails they
  answer `503` with the new code `recovery_failed` (`RecoveryFailed`, which
  names the cause's code only), and the next round write tries again. Once
  the GMA serves, reads, `/health` and approval do not wait. Recovery is told
  whether the update store
  survives a restart: both update stores now have `survives_restart`
  (`False` in memory, `True` in Postgres), an optional member of the
  `UpdateStore` protocol.
- Before each dispatch, the controller asks the planner's recovery rule about
  every unfinished round whose oldest stored update is older than the age
  limit, and applies its answer (new `FederatedRoundController.expire_updates`
  and the controller's `update_max_age_seconds=`; `build_round_controller`
  takes it too). It deletes the old updates of rounds that are finished or not
  stored, and leaves out a round the controller is dispatching now. A second
  dispatch of a round id that is being dispatched gets `409`. Every keep or
  close decision still comes from the planner.
- The GMA's own thread pool (`HandlerThreads`) runs the recovery as well as
  the update uploads, and its threads are named `gma-handler`
  (`HANDLER_THREAD_NAME`) instead of `gma-upload`.
- New module `infrastructure/postgres_rounds.py` with a Postgres store for
  each of the three stores that `GmaDeps` takes: `PostgresRoundRepository`
  (`rounds`), `PostgresEnrolmentStore` (`enrolments`) and
  `PostgresUpdateStore` (`updates`). Each takes a kept connection, a role and
  a table, has `kind = "postgres"`, runs each method in one transaction acting
  as its role, and passes the same tests as its memory twin. The module holds
  each table's SQL as a constant (`ROUND_TABLE_DDL`, `ENROLMENT_TABLE_DDL`,
  `UPDATE_TABLE_DDL`); the README's "Round state tables" section has the same
  SQL, and a test checks that the two match. The GMA creates no table.
  - The settings above build them, or an application builds them and passes
    them in `GmaDeps`.
  - `PostgresRoundRepository` has `survives_restart = True`.
  - The stores read each JSON column as text and decode it row by row. A row
    whose JSON Python cannot decode (nested too deep, or a whole number of
    more than 4,300 digits), or whose round record holds a number that is not
    finite, such as `1e400`, counts as unreadable: `get` and `update` raise
    `StoreNotSetUpError` naming the round or site, `list` leaves it out with a
    warning, and `unfinished()` reports `readable=False`, so `recover()` can
    close the round from its columns.
  - `PostgresUpdateStore` takes `coordinator_role=` and raises
    `RoleScopeError` when it equals its own role. The updates table belongs
    to that separate update role, so nothing acting as the coordinator role
    can read an update vector. A discard marks the round's gate row, which
    refuses every later upload for the round, in any process and after a
    restart.
  - Every new table uses `json` columns, bound with psycopg's `Json`. A
    `jsonb` column, or a `Jsonb` parameter, returns `-0.0` as `0.0`, and
    `1e16` and larger floats as whole numbers.
- `PostgresModelStore` binds `params` with `Json`, and the README's models
  table has `params json`. With that column, a version with `-0.0` or `1e16`
  in its parameters loads in a restarted GMA. Before, a model kind whose
  digest tells these values apart refused it, because the stored parameters
  no longer matched the stored digest. A table whose `params` column is still
  `jsonb` works as before, with that limit, until you run the `ALTER` in the
  README (also `PARAMS_TO_JSON_SQL`). Rows stored before the change keep the
  values `jsonb` gave them. The `release_decision` column is unchanged.
- Both round stores refuse a round record that holds NaN or an infinite
  number, with the new `InvalidRoundRecord` (`invalid_request`, 422, a
  `ValueError`), and store nothing. JSON has neither number. Before, the memory
  store kept such a record: a NaN metric then read back as `null`, and a NaN
  `mu` made answers that show the round fail with a 500. The shared check is
  `round_record_json` in `dagents/common/application/federation.py`.
- `SiteResult.metrics`, `SiteResult.update_norm`, `AggregationMethod.mu`, and
  `GateComparison.value` and `margin` refuse NaN and infinity. A request that
  holds one gets 422, and the answer leaves those inputs out, because a JSON
  answer cannot hold them. Before, such a request got 202 or 500, depending on
  the field and the round store; an update upload with a NaN value got 500.
  With the HTTP engine, an LMA answer that holds one marks that site `failed`,
  and the round goes on.
- When the round store refuses the results the engine returned, the dispatch
  ends as an engine failure does: the round is closed with
  `the engine failed while it dispatched the round` and the engine deletes the
  round's updates. Before, the round stayed `dispatched` and kept them.
- New `checked_update(record, site_id, upload, kind)` in
  `infrastructure/update_store.py`: the checks every update store makes
  before it stores an upload. `InMemoryUpdateStore` behaves as before.
- `GmaDeps` gains `rounds`, `enrolments` and `updates`, so an application that
  builds the GMA with `create_app` can pass its own round store
  (`RoundRepository`), enrolment store (`SiteRegistry`) and update store
  (`UpdateStore`). Each one left out is built in memory, new for each app, as
  before. `build_round_controller(engine, sites=, rounds=)` in `di.py` takes
  the same stores.
- New `UpdateStore` protocol (`infrastructure/update_store.py`) with `kind`,
  `put`, `get`, `updates`, `discard`, `pending_rounds` and `pending_ages(now)`,
  the age in seconds of each round's oldest stored update. The routes and the
  HTTP engine use the protocol. `InMemoryUpdateStore` takes an optional
  `clock`. A store refuses a `put` for a round after a `discard` of that round.
- `RoundRepository` gains `unfinished()`, a summary (`round_id`, `status`,
  `phase`, `readable`) of each `planned`, `dispatched` or `collecting` round,
  oldest first, and `quarantine(round_id, reason)`, which closes such a round
  without reading its record and keeps its id reserved. An application's own
  round store needs both before it can call `recover`.
- New `FederatedRoundController.recover(updates_kept=, now=,
  update_max_age_seconds=)`. For each unfinished round, oldest first, it asks
  the planner (`dagentsc federation round recover`) whether to keep or close
  it, closes each round the planner says to close, and then deletes the stored
  updates of rounds that are no longer unfinished. The GMA calls it once
  after a restart (see the first entries).
- Aggregation with the HTTP engine deletes a round's updates once the round is
  stored as `aggregated`, or when the engine refuses the aggregation: a
  missing or changed update (`409` `aggregation_refused`), or a candidate name
  that holds another model (`409` `conflict`). When the planner refuses
  aggregation, the engine does not run and the updates stay until the round is
  discarded, as before. Before, the engine deleted them every time it ran,
  before the round was written. A store error now keeps them, so the
  aggregate can be retried: the retry combines them again and gets the
  candidate the first attempt registered.
- When no update is stored for a training round, the HTTP engine returns
  `<version_prefix>+<round_id>` if it is registered with the round's starting
  version as its parent, after it checks the digest. It does this only when
  the round store passed in `GmaDeps.rounds` has `survives_restart = True`,
  so round ids stay used across restarts. The default store in memory has
  `survives_restart = False`: after a restart a round id is free again while
  a model store in Postgres keeps the old candidate, and a new round with
  that id gets `409` `conflict` instead of the old round's candidate.
  `HttpFederationEngine` takes the answer as `round_ids_survive_restart`.
- A round-flow release evaluation sends the decision the version already holds
  to the planner's lifecycle rule. A retry after the round's write failed now
  saves the same decision on the round and changes nothing on the version, for
  a `release` and for a `reject`. Before, a retried rejection got `409`
  `not_candidate` on every attempt. A decision that differs from the stored
  one gets `409` with `refusal: decision_differs`. If the planner allows the
  same decision for the same round but changes a status, which today's rule
  never does, the registry stores nothing and answers `503`
  (`planner_unavailable`). The GMA and its `dagentsc` must come from the same
  release, because the lifecycle input has new keys.
- The Postgres model store and audit sink check their table names with the
  shared `table_identifier` in `dagents/common/infrastructure/postgres_roles.py`.
  They accept and refuse the same names as before. The audit sink's error now
  starts "the audit table must be", and a table name that is not a string
  raises `ValueError` instead of `AttributeError`.
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
