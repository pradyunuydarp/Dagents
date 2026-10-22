---
name: ci
description: Strict rules for the Dagents CI pipeline and the Pages publishing pipeline, and for what counts as a verified change — the GitHub Actions workflows in .github/workflows/, the guard scripts in scripts/, the generated service inventory and demo recordings, and the commands each suite runs. Use before editing a workflow, adding a test suite or a job, changing a requirements file, or reporting that a change passes. States the repo's polyglot layout, its test loop, and the ways a green run here has been wrong in the past.
---

# Dagents CI

These rules are binding. The main one: **a green run must show that the change
works.** The rest follows from it. Workflow comments and run summaries follow
the `writing` skill. Which docs a code change must update, and the docs checks
CI runs, are in the `docs-sync` skill.

## 1. What the pipeline covers

Each layer of Dagents has its own job and its own test loop:

| Layer | Path | Runner needs | Suite |
|---|---|---|---|
| **OCaml planners**: pure validate, compile, route, render | `bindings/ocaml/` | opam + OCaml 5.1 | `dune test` |
| **Python agents**: LMA, GMA, control plane, governance, federation | `dagents/` | Python 3.11 + `dagentsc` | `dagents/tests` |
| **Framework services**: core, pipeline, model | `services/` | Python 3.11 (+ torch for model) | `services/<name>/tests` |
| **Spring services**: JVM control and core APIs | `services/spring-services/` | JDK 21 + Maven + `dagentsc` | `mvn verify` |
| **Demo apps**: healthcare, NL2SQL | `apps/`, `services/nl2sql-demo/` | Python 3.11 + `dagentsc` + a PostgreSQL 16 service | their own suites |
| **Frontends**: the site and the two demo UIs | `site/`, `*/frontend/` | Node 22 + a browser | `npm run build`, `npm run check`, `npm run smoke` |
| **Cross-service contracts**: the endpoint inventory and API conventions | `tests/` | every Python service's dependencies + `dagentsc` + JDK 21 + Maven + Node 22 | `tests/` |
| **Python package**: the `dagents` sdist and wheels | `pyproject.toml`, `hatch_build.py`, `dagents/` | Python 3.11 to 3.13 + a static `dagentsc` | `packaging` job: build, `scripts/check_dist.py`, clean-venv smoke, `dagents/tests` on the newest versions the extras allow |

Python calls the planners through `dagentsc`, so the planner binary is a **build
artifact that most jobs need**. The `planners` job builds it once and uploads
it as `dagentsc`; `agents`, `framework-services`, `contracts`, `spring`,
`demo-apps`, `frontends` and `deploy-smoke` download it.
It also builds the planner with `--profile static` and uploads it as
`dagentsc-static`, which the `packaging` job puts into a Linux wheel.

## 2. Ways a green run has been wrong, and the defence for each

Do not remove any of these defences. Each exists because the failure happened.

### a. Important tests skipped, and the run was still green

The governance and federation tests run against the real `dagentsc` binary,
because a stub would only prove that the code calls something. They skip, with
a message, when the binary is missing. That is right on a developer's machine
and **wrong in CI**.

The same holds for two other kinds of test: the Postgres tests skip without a
test database URL, and the chart tests skip without matplotlib.

Defence: `scripts/ci_no_skipped_tests.py` reads the verbose test log and fails
on any skip in a forbidden category. The suites must run with `-v`; the guard
treats a log without verbose output as a failure.

| `--forbid` | Fails on | Where CI forbids it |
|---|---|---|
| `planner` | a skip because `dagentsc` is missing | `agents`, `demo-apps` |
| `database` | a skip whose reason starts `database test skipped` | `demo-apps` (it has a Postgres 16 service) |
| `charts` | a skip because matplotlib is not installed | `agents` |

`--require-ran MODULE.CLASS` fails unless that test class finished at least one
test. It catches a class that skipped in `setUpClass` or stopped running for a
reason no pattern matches. On success the guard prints each allowed skip reason
with a count (the pagila suites and the deployed-API tests).

```bash
python -m unittest discover -s dagents/tests -t . -v 2>&1 | tee agents-suite.log
python scripts/ci_no_skipped_tests.py --forbid planner --forbid charts \
  --require-ran dagents.tests.test_governance.EthicalGuardTests agents-suite.log
```

`scripts/ci_no_skipped_planner_tests.py LOG` still works; it runs the guard with
`--forbid planner`. `tests/test_ci_guards.py` reads the real skip reasons from
the test files and checks the guard against the four shapes unittest prints
them in, so the guard cannot stop matching without a test failing. It also
checks that every `--require-ran` class in a workflow exists, that every test
class needing a database (found by reading the test files) is in the
`demo-apps` `--require-ran` lists, and that each required module runs in the
step that writes the log. **A new database test class goes in both
`ci.yml` and `healthcare-data.yml`**, which must list the same classes.

### b. Two implementations of one behaviour, only one tested

`core-service` renders manifests with the OCaml compiler when `dagentsc` is
available and with a Python fallback when it is not. Containers have `dagentsc`
on PATH, so **the OCaml path is the one deployed**. The two renderers drifted
apart because the suite only tested the fallback.

Defence: the `framework-services` job runs the core-service suite **twice**,
with and without `DAGENTSC_BIN`. Every behaviour with two implementations gets
the same assertions run against both. If they cannot be kept in step, delete one.

The healthcare demo calls its agents through two transports: in process
(`httpx.ASGITransport`, as published) and over HTTP (`--agents network`,
compose). The `demo-apps` job runs the suite in process, and
`apps/healthcare-demo/tests/test_network_mode.py` starts the GMA, three LMAs
and the API as uvicorn processes against the job's PostgreSQL 16 service and
checks that their pilot equals an in-process pilot on the same database, and
that an approval reaches every LMA process. It also checks that an entry and
its outcome reach the overview's counts through the GMA at once, and that the
next pilot uses the record in every round. That holds only while the GMA keeps
no site summary (`GMA_SUMMARY_TTL_SECONDS=0`), so the test checks that setting
too.

`tests.test_network_mode.NetworkOutageTests` kills one LMA process with
SIGKILL while the demo runs, then starts it again on the same port. While the
LMA is down, its hospital page and the overview answer the app's 503, with
`X-Dagents-App` and the app's words. The other hospital pages answer, the
status report says the LMA does not answer, and the pilot answers 200 and says
in words why no round met its quorum. After the restart, every page and the
status report answer again, and the next pilot includes the hospital and
equals the recorded pilot. A process that a test starts again gets a port
below the range Linux gives outgoing connections
(`/proc/sys/net/ipv4/ip_local_port_range`). A port in that range can be taken
by any outgoing connection while the process is stopped, and the restart then
cannot bind it.

`tests.test_network_mode.NetworkModeTests`,
`tests.test_network_mode.NetworkApprovalTests` and
`tests.test_network_mode.NetworkOutageTests` are in the `--require-ran` lists
of both `ci.yml` and `healthcare-data.yml`, with
`tests.test_agents_wiring.PostgresWiringTests` and
`tests.test_concurrency.PostgresPilotWhileWritingTests`.
`tests.test_pilot_parity.PilotParityTests` compares every number of a pilot
with a fixture recorded before the demo used the agents.

Adding a hospital while the demo runs starts real processes, so its tests do
too. `tests.test_network_deploy.NetworkDeployTests` starts core-service, the
GMA, three LMAs and the API with the deployer on, then takes these steps in
order:

1. It adds Hospital 290 and runs a pilot with it.
2. It kills the new LMA. `GET /api/v1/deployments` reports it `stopped`, and
   the same `POST /api/v1/deployments` starts it again.
3. It kills the GMA and starts it again on its port. The GMA has forgotten the
   new LMA's address. The next pilot sends the address again, includes the
   hospital and keeps the model in use.
4. It kills and starts the GMA a second time. This time the overview sends the
   address.

`NetworkOutageTests` and `NetworkDeployTests` stop a process between two
requests, never during a round. The GMA starts again with no round in
progress, and the demo keeps its rounds in memory, so these tests do not check
round recovery. They post no entry while an LMA is down. The overview's 503
gives the app's general message and does not name the hospital. The deploy
panel's restart button is driven in a browser only against a stopped LMA that
`smoke.mjs` simulates.

Both workflows require `NetworkDeployTests` to run. `tests/test_ci_guards.py`
finds it although its database fixture is a base class imported from
`test_network_mode`. Three classes skip when something the runner has is
missing, so the demo-apps job's planner step requires them to run:
`ComposeOverrideTests` skips without the Docker command line and its compose
plugin (it checks that only the API gets the Docker socket and that the API and
the UI are published on 127.0.0.1 only), `ComposeRequestTests` skips without
the planner, and `NetworkScriptDeployTests` skips without bash and curl. The
`deploy-smoke` job runs
`run_frontend_demo.sh --deploy --check` against PostgreSQL 16 and fails unless
the smoke test prints `ok  deploy: hospital pl-290 joined`.

A GMA that keeps its rounds in Postgres is restarted in two ways, and both run
in the `demo-apps` job's "Framework database tests" step and in
`healthcare-data.yml`. `dagents/tests/test_gma_restart.py` builds a new GMA app
in the test process at each restart point. `dagents/tests/test_gma_process_restart.py`
starts the GMA (`python -m dagents.gma`, settings from the environment) and
three LMAs as processes on loopback ports 9800-9899, kills the GMA's process
group with SIGKILL and starts it again on its port. Every database class of
both modules is in both `--require-ran` lists. `tests/test_ci_guards.py` finds
them although their tables fixture opens the database through a helper
imported from `test_round_store`.

### c. A convention written down but not checked

Defence: the `contracts` job. `scripts/service_inventory.py --check` regenerates
the endpoint inventory from the FastAPI routing tables and the Spring
controllers and fails if it changed. `tests/test_service_inventory.py` also
checks that the framework services use only `/api/v1/...`, that the LMA/GMA
short and versioned paths still exist and return the same result, and that the
Spring services mirror their Python counterparts.

Any endpoint change means regenerating the inventory:

```bash
.venv/bin/python scripts/service_inventory.py --write   # then commit both files
```

The same job checks the API reference: `scripts/api_reference.py --check`
regenerates `docs/reference/api-reference.json`, and
`tests/test_api_examples.py` makes every captured example request again with
the planner. The job downloads `dagentsc` for that, and the guard requires
`tests.test_api_examples.ApiExampleReplayTests` to have run, so a missing
planner fails the job instead of skipping the replay. The replay also starts
spring-control-service from its jar (the `runner` in its request file), so the
job sets up JDK 21 and builds that jar first.

The same job builds the spring-core-service jar and starts it.
`tests/test_spring_services_e2e.py` runs it as two JVM processes, one with
`DAGENTS_OCAML_MANIFEST_CLI` naming a wrapper around the planner and one
without, and compares their answers over HTTP with what `dagentsc manifest
compile` prints for the same request. The class skips without Java, the jar or
the planner, so the guard requires
`tests.test_spring_services_e2e.SpringCoreProcessTests` to have run. Before
this test existed, the jar did not start (Spring found two beans of one type),
and the gateway threw the planner's answer away on every request. Every JUnit
test passed, because none loaded the application context and none checked
something only the planner produces. The test does not build or start the
container image, so it does not check the image's `/usr/local/bin/dagentsc` or
its Java 25 runtime (CI runs JDK 21). The timeout and the fallback for output
that is not a plan are tested only in `ProcessManifestCompilerGatewayTest`,
with stand-in planner scripts.

The `spring` job runs `mvn verify` with the planner: it waits for `planners`,
downloads `dagentsc` and sets `DAGENTSC_BIN`. `ProcessManifestCompilerGatewayTest`
fails when `DAGENTSC_BIN` names a file it cannot run, and skips only when the
variable is unset and the dune build is missing. Maven passes a build in which
a test skipped, so the next step reads that test's Surefire report and fails on
a skip. A `@SpringBootTest` class in each module starts the full application
context, so a configuration that cannot start fails `mvn verify`.
`SpringCiWiringTests` in the same Python file checks this wiring. It also runs
the report step on reports in which the test passed, skipped or is missing, and
with no report at all.

### d. A skipped browser test reported as a pass

Both browser checks (`apps/healthcare-demo/frontend/smoke.mjs` and
`site/check.mjs`) exit **2** and print `SKIP` when no browser is available. The
demo script turns that into a friendly message and exits 0. That is right for a
developer and wrong for CI.

Defence: the `frontends` job installs a browser, sets `CHROMIUM_PATH`, and fails
if the smoke log contains a skip or no passing assertions. `npm run check`
returns exit 2 to the step directly, which fails it.

Never let a job accept exit 2, and never report a skipped browser test as a pass.

### e. A guide diagram that does not draw

The site renders the learning guide from `docs/learn/*.md`, including Mermaid
diagrams. A diagram with a syntax error builds without complaint and only fails
when a browser draws it.

Defence: `site/check.mjs`, run by the `frontends` job, visits every guide page
and fails if a diagram does not draw, an image does not load, a link between
pages or a heading anchor is missing, raw Markdown shows, or the console logs an
error.

### f. A published demo that shows nothing useful

`pages.yml` publishes NL2SQL as a static site that replays a recording, and the
healthcare demo too when its API is unavailable. A recording can be written
without errors and still be useless: without the planner the Guard fails closed,
every probe is denied, and the page shows guard controls that change nothing.

Defence: `scripts/capture_demo_recordings.py` refuses to run without
`dagentsc`, and `scripts/check_demo_recordings.py` checks the recording's
content before the deploy: a guard matrix that covers every hospital with both
permits and denials, at least three different field strategies, a pilot that
reached its release gates, a framework status that reports every store ready,
every hospital's dashboard, worklist, entered records, audit log, entry form,
chart list and charts (as SVG, in both themes), each hospital's dashboard and
audit log recorded again after the pilot, consortium charts recorded after the
pilot, real SQL, a non-empty trace, and at least one reachable framework
service. For the healthcare demo it also requires the agent calls behind the
answers: `agents.mode` in the framework status, `framework_calls` on the
status, the overview, each probe and each dashboard (each naming that
hospital's LMA), and in the pilot the GMA's `POST /api/v1/federation/rounds`,
a `jobs:run` call (or `jobs:execute`) to every hospital's LMA, and a run row
the hospital accepted (or an execute row) in dispatch for every hospital in the
round reports (the pilot panel's dispatch tables read those rows). An entry with a field outside the seven allowed ones, a path that is not
a route template, or any `://` is refused. The capture refuses to write a
recording that fails these checks. `tests/test_publishing_guards.py` builds an
all-denied recording on purpose and checks that the checker refuses it.

The replay finds a response by its exact method, path and query string. So the
hospital-page paths are defined once, in
`capture_demo_recordings.hospital_view_paths`; the checker requires each one,
and a test reads every `useResource` and `getJson` call in the healthcare
frontend and fails if the UI sends a GET path the checker does not require.
Change a query string in the UI and in that function together.

The Pages build also fails if a demo's `dist/` lacks its `recording.json`,
because a demo without its recording never finishes loading.

### g. A published demo pointed at an API that is down or misconfigured

The healthcare demo is published calling a deployed API. Three problems are easy
to miss: the service is asleep; the service is up but generates its cohorts in
process, so the page would show invented data as live; or the service does not
allow browser requests from the Pages origin. A plain `curl` cannot see the last
one, because it sends no `Origin` header.

Defence: the Pages build probes the API before building the frontend. It must
answer, report `cohort_source: postgres` and `ready: true` (every hospital store
can be read and holds records), return worklist rows for the first hospital,
and send `Access-Control-Allow-Origin` for the Pages origin. **If any check
fails, the build clears the address and publishes the demo as a replay**, with
a warning and a note in the run summary. None of these checks fails the deploy.
`tests/test_publishing_guards.py` runs the probe step against a local stand-in
for the API.

They did fail the deploy at first. That let a setting on an external service
stop the framework site and the other demo from publishing. The replay is a
correct result, because the page says it is a replay and names the commit.
`healthcare-api-check.yml` is the workflow to run when you want a failing check
for the API.

### h. A deployed address copied into the docs

The healthcare API's address used to be written in the READMEs, the guide and
`CLAUDE.md`. Each copy was one more place to update when the service moved, and
the page showed it to every visitor.

Defence: the address lives only in `env/.env.published`, which the Pages build
reads. `tests/test_publishing_guards.py` reads the host from that file and fails
if it appears in any tracked text file outside `env/` and `.github/`. The docs
call an API run locally, and the page never shows the address. The same test
file checks that the README links every `*_SITE_URL` in `env/.env.published`.

### i. Three frontends drifting apart visually

The design system is shared by generated copies, not imports, because the
healthcare demo must stay extractable. A copy edited directly would drift.

Defence: `scripts/sync_design_system.py --check` in both workflows, and a test
that every token is defined on bare `:root` before any dark block. A token
defined only in a dark block puts one theme's text on the other theme's
background, and nobody notices until they open that theme.

### j. Code merged while the site described the old behaviour

The Pages workflow publishes the framework site on every push to `main`. A code
change without its docs leaves the site wrong until someone notices. The
generated docs have their own tests (failure c), but nothing checked that the
prose changed at all.

Defence: the `docs-impact` job runs `scripts/check_docs_impact.py` on pull
requests (`base.sha` to `head.sha`) and on pushes to `main` (`before` to
`sha`). It fails when the change touches a code area in its `AREAS` map and
none of that area's docs, unless a commit message in the range has a line
`Docs-Impact: none [area] (reason)` that names the area. A line exempts only
the areas it names. The job checks out with `fetch-depth: 0`, because it diffs
against the base and reads every commit message in the range. The
commits reach the script through `env:`, never pasted into `run:`. When
`before` is all zeros or missing from the clone (a force push), the script
checks the head commit alone and says so. A manual run skips the job, and a
skipped job does not fail the `ci` gate.

It proves a decision was made, not that the right page changed.
`tests/test_docs_impact.py` tests the decision, the trailer, the git range, the
area map against the real tree, and this job's shape. The map and the trailer
rules are in the `docs-sync` skill.

### k. A shared link that shows no picture

A LinkedIn share of the framework site once showed "Image unavailable": the
page had no `og:image`. Crawlers also ignore a relative `og:image`, and a local
build can only write the base path, so a tag can be present and still useless.

Defence: the published address reaches each build as `PUBLIC_SITE_URL`, never
written in the HTML (the `frontend` skill, section 3b). `pages.yml` runs
`scripts/check_link_previews.py` three times:

- **Resolve the published addresses** (`addresses --github-env`) reads each
  `*_PUBLIC_SITE_URL` from `env/.env.published` and stops the deploy when one
  is missing or is not a plain https address ending in `/`. The Vite plugin
  also fails the build when an address's path differs from the base path.
- **Every page has a link preview at its published address** (`built --tree
  _site`) checks each built `index.html` for the absolute `og:url`,
  `og:image`, `og:image:secure_url` and `twitter:image`, no placeholder left,
  a 1200 x 630 card under 300 KB, and every icon.
- After the deploy, **Each published page's link preview image loads**
  (`published`) reads the `og:image` from each live page and requests it.
  Anything but `200` with an `image/` content type and a body fails the run.

The checks are a script, not inline shell, so a test can show that each one
fails. An earlier inline version of the built-page step printed `ok` for a
page that failed. `tests/test_social_previews.py` (in the `contracts` job) runs each command on
good and broken inputs, the last one against a local stand-in that answers 404
or `text/html`. It also checks the tags, the plugin's refusals and that each
committed PNG is current with its source in `design/social/`. The plugin tests
need Node, so the `contracts` job sets up Node and requires
`tests.test_social_previews.SocialMetaPluginTests` to have run.
`site/check.mjs` loads the built site's card and icons. Neither can see a
crawler's cache: LinkedIn and WhatsApp keep the preview they fetched first.

### l. An image without the planner

The LMA and GMA images had no `dagentsc`. Every suite passed, because the jobs
set `DAGENTSC_BIN` to the planner artifact. In the compose stack the Guard
denied every request and every round plan failed.

Defence: `tests/test_planner_in_images.py`, run by the `contracts` job. CI
builds no images, so it reads the Dockerfiles. For each Python image it follows
the imports from the module uvicorn runs, through the code the image copies.
If they reach `dagents/common/infrastructure/dagents_runner.py`, the image must
build `dagentsc.exe` in an earlier stage and copy it to
`/usr/local/bin/dagentsc` in its final stage. The test also fails if the walk
stops finding the images known to call the planner, or if a Python image starts
in a way the walk cannot read.

### m. A Pages deploy that GitHub stalled, and a retry that could hide a failure

Run 41 of `pages.yml` failed in **Deploy to Pages**. The deployment stayed at
`updating_pages` until `actions/deploy-pages` reached its limit of ten minutes
and cancelled it. The build had passed, and re-running the failed job deployed
the same artifact in under 15 seconds. The fault was on GitHub's side, but it
left a red check on `main`.

Defence: the deploy job tries once more.

- The first `actions/deploy-pages` step has `continue-on-error: true`. If it
  fails, the job waits 30 seconds and runs the action again. The action cancels
  a deployment it gives up on, so the second attempt starts clean.
- The second attempt has no `continue-on-error`, so its failure fails the job.
- A step that may fail without failing the job can turn a failure into a green
  run. So **One deploy attempt succeeded** reads both outcomes through `env:`
  and passes only when the first attempt succeeded, or when it failed and the
  second succeeded. When the second attempt was needed, it writes a warning and
  a note in the run summary.
- The two checks of the published pages come after that step and have no
  `if:`, so they run only after a deploy succeeded.

`tests/test_social_previews.py` checks the order and the conditions of these
steps, and runs the guard step's script on the two passing combinations of
outcomes and on failing ones.

If both attempts fail, re-run the failed jobs of that run
(`POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs`). The
Pages artifact is kept for one day; after that, re-run the whole run. A new run
of the same commit also publishes, but it adds a green check beside the red
one, and the commit keeps showing the failure.

### n. A package that works in the checkout but not when installed

Every suite imports `dagents` from the checkout and sets `DAGENTSC_BIN`. None
of them can see a wheel that lacks a module, ships a second top-level package,
or cannot find the planner it bundles. The agents job also pins the lowest
FastAPI and uvicorn the `lma` and `gma` extras accept, while pip gives a user
the newest. FastAPI 0.143 keeps an included router as one entry in
`app.routes`, so nine tests that read `app.routes` found no routes and failed
there, while the agents job passed. `dagents/tests/fastapi_routes.py` now
lists the routes on both versions.

Defence: the `packaging` job builds the sdist, builds a Linux wheel from it
with the static planner inside, and runs `scripts/check_dist.py` (only
`dagents/` and its `.dist-info`, no tests, a correct `RECORD`, the planner
present, executable and matching the tag, exactly one `Development Status`
classifier and the one for the stage, a description that shows `pip install
dagents`, and the hatchling version pinned in `.github/build-constraints.txt`).
It then installs the wheel into a clean virtual environment for each supported
Python, outside the checkout, with no `PYTHONPATH` and no `DAGENTSC_BIN`, and
runs `scripts/smoke_installed_package.py --expect-planner bundled`: the
runner must find the bundled planner, the installed package must read no
`env/` file, the Guard must get real decisions, the getting-started example
must print the guide's outputs, and the LMA and GMA must answer `/health`
without a planner warning. The smoke test's first check reads the `dagents`
distribution's `RECORD`: it may list only `dagents/`, its `.dist-info`, its
two commands and the bytecode pip compiled. With `PYTHONPYCACHEPREFIX` set,
pip writes that bytecode outside site-packages, so the check allows `.pyc`
files there and in `__pycache__` folders. Python never imports such a file as
a module of its own. A wheel built from the sdist without a planner must
make the Guard deny and each agent log that it has no planner
(`--expect-planner none`). Last, the job installs the wheel with all its
extras on Python 3.13, removes the package, and runs
`dagents/tests` from the checkout on those newest versions, with the planner
and chart skips forbidden. The release workflow runs the build and smoke
checks on each platform before it publishes.

The same step caught a missing extra. `compose_apply.py` imports PyYAML only
when it parses a Compose file, and the agents job installs PyYAML by hand, so
every suite in the checkout passed while the `[all]` install had no PyYAML and
the appliers' tests failed with `ModuleNotFoundError`. The `compose` extra now
installs it, and `tests/test_packaging.py` checks that `all` names every other
extra.

The package was renamed from `agents` so that it installs next to
openai-agents, whose top-level module is `agents`. The smoke test's first
check used to fail whenever any `agents` module was installed, so no step
could run it in an environment that has openai-agents. Now the check reads
only the `dagents` distribution's files, and one step of the `packaging` job
installs the wheel with `openai-agents==0.24.0`, runs `pip check`, and runs
the smoke test with `--agents-from openai-agents`. That option fails unless
`agents` comes from openai-agents and imports in the same process as
`dagents`. `tests/test_release_workflow.py` checks that the step exists, and
`tests/test_packaging.py` runs the file check and the option on synthetic
distributions.

## 3. The test loop

CI runs the same commands a contributor runs. Write the failing test first,
watch it fail, make it pass, then run the loop below before pushing. A push that
turns CI red costs a cycle and the reviewers' trust.

```bash
# 1. planners: the fastest loop; no Docker, database or GPU
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test && cd -

# 2. agents, with the planner available so nothing skips
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  .venv/bin/python -m unittest discover -s dagents/tests -t . -v

# 3. the service suite you changed; core-service both ways
PYTHONPATH=.:services/core-service .venv/bin/python \
  -m unittest discover -s services/core-service/tests -t services/core-service/tests
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  PYTHONPATH=.:services/core-service .venv/bin/python \
  -m unittest discover -s services/core-service/tests -t services/core-service/tests

# 3b. the healthcare demo, with the planner and a test database, so nothing skips
cd apps/healthcare-demo && DAGENTSC_BIN=$PWD/../../bindings/ocaml/_build/default/bin/dagentsc.exe \
  HEALTHCARE_DEMO_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres \
  PYTHONPATH=../..:backend ../../.venv/bin/python -m unittest discover -s tests -t . -v && cd -

# 4. cross-service contracts, if you changed a route
.venv/bin/python scripts/service_inventory.py --check
.venv/bin/python -m unittest discover -s tests -t .

# 5. the JVM side, if you changed it
mvn --batch-mode -f services/spring-services/pom.xml verify

# 6. a frontend, if you changed it
apps/healthcare-demo/scripts/run_frontend_demo.sh --check
cd site && npm run build && npm run check && cd -

# 7. the docs changed with the code (the docs-sync skill)
.venv/bin/python scripts/check_docs_impact.py --base origin/main

# 8. the package, if you changed pyproject.toml, hatch_build.py, the runner or
#    the package layout: the commands under "Releasing" in CLAUDE.md
.venv/bin/python -m unittest tests.test_packaging tests.test_release_workflow
```

`dune` is not on PATH, so always use `opam exec --`. Python tests import as
`dagents.common...`, so run them from the repository root with the gitignored
`.venv/`. Each service has its own `requirements.txt` and there is no root-level
one, so CI installs dependencies per job.

**Always report the number of skipped tests.** "Tests pass" with the governance
tests skipped is not a pass.

## 4. Rules for changing the pipeline

- **A new suite needs a job.** Tests under a path no job runs are never run.
- **A new job goes in the `ci` gate's `needs` list.** That job is the single
  required check; a job missing from it can fail without blocking anything.
- **No test may need the network.** Pin fixtures; never download a model or a
  dataset in a test. The Hugging Face provider tests check the *refusals*
  (missing dependency, download not permitted), so they never need the Hub.
- **An optional dependency CI installs must not leave a test skipped.** The
  `framework-services` job installs
  `services/model-service/requirements-optional-models.txt`, then fails if the
  Hugging Face tests still skipped, because that means the requirements file and
  the test's own check disagree.
- **Prefer a guard script to a comment.** A rule the pipeline does not check
  stops being followed (failure c). Put the check in `scripts/`, test it in
  `tests/`, and call it from the workflow.
- **Use `set -o pipefail` in every step that pipes a command into `tee`.** The
  default shell is `bash -e` without pipefail, so `cmd | tee log` reports
  `tee`'s exit status and hides a failing `cmd`. `tests/test_ci_guards.py`
  fails on a `| tee` without it.
- **Never put a secret, a repository variable or a dispatch input directly into
  a `run:` script.** `${{ vars.X }}` pastes text into the shell before bash runs,
  so a value like `; curl …` would run with the workflow's token. Pass the value
  through `env:`, use `"$X"`, and validate it. `healthcare-data.yml` passes its
  `keep_entries` input through `env:` and compares it as a string, and
  `pages.yml` refuses an API address that contains a query string or
  credentials.
- **A repository variable is not a secret store.** Variables are not masked, so
  every run that reads one prints it, and on a public repository the log is
  public. A credential pasted into a variable must be rotated, not just removed.
- **Keep jobs independent.** Each job installs only what it needs, so a
  contributor can reproduce one job locally.
- Pin actions to a major version (`actions/checkout@v4`,
  `ocaml/setup-ocaml@v3`) and cache per ecosystem (`pip`, `npm`, `maven`,
  `dune-cache`). **`release.yml` is the exception:** its publish jobs can mint
  a PyPI upload credential, so every action there is pinned to a full commit
  SHA with its version in a comment (`@df4cb1c... # v6.0.3`), and
  `tests/test_release_workflow.py` fails on any other form. To update one,
  read the commit of the new tag (`git ls-remote --tags
  https://github.com/actions/checkout`, the `^{}` line for an annotated tag)
  and change the SHA and the comment together. The build backend is pinned the
  same way: every `python -m build` step in both workflows sets
  `PIP_CONSTRAINT` and `PIP_BUILD_CONSTRAINT` to `.github/build-constraints.txt`,
  and the test fails on a build step without them.
- **Only a publish job gets `id-token: write`**, and only in a run on a
  `refs/tags/v*` ref, whether a tag push or a manual run on the tag started it.
  In `release.yml` those are `publish-testpypi`, `publish-pypi` and
  `github-release`. Two jobs may write: `release-tag` has `contents: write`
  and `actions: write`, to push the tag and start the run on it, and
  `github-release` has `contents: write`, to create the release. Every other
  job has `contents: read` only. Never add a PyPI token or password;
  publishing uses Trusted Publishing.

## 5. Releasing

`.github/workflows/release.yml` publishes the `dagents` package. A release
starts in one of two ways:

- **A push of the tag `v<version>`.**
- **A manual run on `main` with the `release_tag` input ticked.** That run has
  one job, `release-tag`. It runs `scripts/release_version.py --create-tag`,
  which creates the annotated tag `v<version>` on the run's commit and pushes
  it with the workflow token. Then the job starts the workflow again with
  `workflow_dispatch` on `refs/tags/v<version>`. GitHub starts no push run for
  a tag pushed with the workflow token, so the job has to start the run itself.

Either way, the run that publishes is a run on the tag, so the environments'
rules for `v*` tags apply to it. A manual run without the input, on any
branch, is a dry run that publishes nothing. The input works only on `main`;
on any other branch or tag, the version job stops the run.

`--create-tag` stops when the run's commit is no longer the head of `main`. It
asks the remote with `git ls-remote`, because `actions/checkout` moves its copy
of `origin/main` to the run's commit. It also stops when the tag names another
commit, because a released version never moves. When the tag already names the
run's commit, it leaves the tag alone and the job starts another release run.
That run cannot publish different files: TestPyPI skips files it has and the
hash check compares them, PyPI accepts a file identical to one it has without
changing anything and refuses a different file with the same name, and the
GitHub Release step fails when the release exists. After a run that failed
before publishing, the new run is a normal release run.

The release run that the tag job starts has `github-actions[bot]` as its
actor. So the tag message names the person who started the tag job and links
to that run (`--requested-by`, `--run-url`), and in every run on a tag the
version job writes the tag's tagger and message to the run summary
(`--summary`), inside a code block, because anyone who can push a tag writes
that text.

The run that creates the tag has a concurrency group of its own,
`release-tag`. Every other run uses `release-<ref>`, so runs on one tag wait
for each other. In the group of `main`, the tag run would wait behind a dry
run, and GitHub cancels a waiting run when another run joins the group.

A tag ruleset that restricts creations would refuse the tag the workflow
token pushes, so `v*` creations stay open to anyone with write access. A
`v*` tag on a commit that is not on `main` runs that commit's workflow file,
which can skip the checks, and the environments admit it. The `pypi` reviewer,
who checks on GitHub that the run's commit is on `main`, is then the only
check before PyPI. Until the owner adds that reviewer, such a run uploads
without one. CLAUDE.md, "Who can publish", has the details.

The order of a release run is fixed: the tag must equal `__version__` and the
tagged commit must be on `main` (`scripts/release_version.py`), the planners
build on their own platforms, the wheels build from the sdist,
every file is checked and every wheel is smoke-tested on its platform, then
TestPyPI, a hash check of what TestPyPI serves
(`scripts/check_published_files.py`), PyPI, the same check against PyPI, and
the GitHub Release. Never reorder these steps, never publish from another
workflow, and never upload by hand.

`tests/test_release_workflow.py` evaluates every job's `if:` for each
combination of event (push or manual), ref (main, a branch, a release tag,
another tag) and input. It fails when a run that is not on a `v*` tag would
publish, when `release-tag` would run anywhere but in a ticked manual run on
`main`, or when that run would start any other job. It evaluates the
concurrency group for the same combinations, and runs the version job's
steps and the dispatch step with stand-in `python` and `gh` commands.
`tests/test_packaging.py` runs `--create-tag` against a bare repository and
`--summary` against annotated and lightweight tags.

The steps for a maintainer, including the one-time Trusted Publisher setup,
are in the "Releasing" section of `CLAUDE.md`.

## 6. Known limits

- **Pages must be enabled by a repository admin.** The workflow token can deploy
  to an existing Pages site but cannot create one, so `configure-pages` may fail
  and the base path falls back to the repository name. Everything before the
  deploy still runs. The deploy returns 404 until Settings → Pages → Source is
  set to GitHub Actions.
- **Docker and Kubernetes are not tested.** `docker compose --env-file
  env/.env.compose up --build` and Minikube validation are manual. Minikube is
  the main item in `TODO.md`, blocked on local disk space (see `CHANGES.md`). CI
  tests the code and the contracts, not the deployment. The one check on the
  images is static: they contain the planner when their service calls it (§2l).
- **The pagila suites skip in CI**, because CI has no Postgres with the pagila
  dataset. These skips are expected, and the guard allows them. A change to
  source adapters is not fully tested by CI. The healthcare stores, the site
  isolation and the role scoping do run in CI: the `demo-apps` job has a
  PostgreSQL 16 service container, emulates Supabase's roles with a
  non-superuser, and fails if any database test skipped.
- **The NL2SQL frontend has no browser test.** Its job builds the bundle, which
  only proves that it compiles.
- **CI does not test the deployed healthcare API.** `tests/test_deployed_api.py`
  runs only when `HEALTHCARE_DEPLOYED_API` is set, so it skips in the
  `demo-apps` job and runs in `healthcare-api-check.yml`. This keeps CI from
  failing because a free-tier container is asleep. It also means a green CI run
  says nothing about the deployed API. The deployed tests never write hospital
  data: they enter no records, record no outcomes and approve no model. Their
  probes and pilot add audit records and model versions, as a visitor's do. Every fault that deployment has had (an
  image without the Postgres driver, a port nothing routed to, a CORS value with
  a stray parenthesis) passed every test here and showed up on the first request
  to the running service. Run the probe after changing what the image installs,
  the port it binds, or what it reads.

When you report on a CI run, say which of these limits applied.
