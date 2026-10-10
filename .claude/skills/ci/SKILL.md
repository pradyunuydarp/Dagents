---
name: ci
description: Strict rules for the Dagents CI pipeline and the Pages publishing pipeline, and for what counts as a verified change — the GitHub Actions workflows in .github/workflows/, the guard scripts in scripts/, the generated service inventory and demo recordings, and the commands each suite runs. Use before editing a workflow, adding a test suite or a job, changing a requirements file, or reporting that a change passes. States the repo's polyglot layout, its test loop, and the ways a green run here has been wrong in the past.
---

# Dagents CI

These rules are binding. The main one: **a green run must show that the change
works.** The rest follows from it. Workflow comments and run summaries follow
the `writing` skill.

## 1. What the pipeline covers

Each layer of Dagents has its own job and its own test loop:

| Layer | Path | Runner needs | Suite |
|---|---|---|---|
| **OCaml planners**: pure validate, compile, route, render | `bindings/ocaml/` | opam + OCaml 5.1 | `dune test` |
| **Python agents**: LMA, GMA, control plane, governance, federation | `agents/` | Python 3.11 + `dagentsc` | `agents/tests` |
| **Framework services**: core, pipeline, model | `services/` | Python 3.11 (+ torch for model) | `services/<name>/tests` |
| **Spring services**: JVM control and core APIs | `services/spring-services/` | JDK 21 + Maven | `mvn verify` |
| **Demo apps**: healthcare, NL2SQL | `apps/`, `services/nl2sql-demo/` | Python 3.11 + `dagentsc` | their own suites |
| **Frontends**: the site and the two demo UIs | `site/`, `*/frontend/` | Node 22 + a browser | `npm run build`, `npm run check`, `npm run smoke` |
| **Cross-service contracts**: the endpoint inventory and API conventions | `tests/` | every Python service's dependencies | `tests/` |

Python calls the planners through `dagentsc`, so the planner binary is a **build
artifact that most jobs need**. The `planners` job builds it once and uploads
it; `agents`, `framework-services`, `demo-apps` and `frontends` download it.

## 2. Ways a green run has been wrong, and the defence for each

Do not remove any of these defences. Each exists because the failure happened.

### a. Important tests skipped, and the run was still green

The governance and federation tests run against the real `dagentsc` binary,
because a stub would only prove that the code calls something. They skip, with
a message, when the binary is missing. That is right on a developer's machine
and **wrong in CI**.

Defence: `scripts/ci_no_skipped_planner_tests.py` reads the verbose test log and
fails on any planner-related skip. The suites must run with `-v`; the guard
treats a log without verbose output as a failure.

```bash
python -m unittest discover -s agents/tests -t . -v 2>&1 | tee agents-suite.log
python scripts/ci_no_skipped_planner_tests.py agents-suite.log
```

`tests/test_ci_guards.py` tests the guard against the real skip message, so the
guard cannot stop matching without a test failing.

### b. Two implementations of one behaviour, only one tested

`core-service` renders manifests with the OCaml compiler when `dagentsc` is
available and with a Python fallback when it is not. Containers have `dagentsc`
on PATH, so **the OCaml path is the one deployed**. The two renderers drifted
apart because the suite only tested the fallback.

Defence: the `framework-services` job runs the core-service suite **twice**,
with and without `DAGENTSC_BIN`. Every behaviour with two implementations gets
the same assertions run against both. If they cannot be kept in step, delete one.

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
content before the deploy: both permits and denials, at least three different
field strategies, a pilot that reached its release gates, real SQL, a non-empty
trace, and at least one reachable framework service.
`tests/test_publishing_guards.py` builds an all-denied recording on purpose and
checks that the checker refuses it.

The Pages build also fails if a demo's `dist/` lacks its `recording.json`,
because a demo without its recording never finishes loading.

### g. A published demo pointed at an API that is down or misconfigured

The healthcare demo is published calling a deployed API. Three problems are easy
to miss: the service is asleep; the service is up but generates its cohorts in
process, so the page would show invented data as live; or the service does not
allow browser requests from the Pages origin. A plain `curl` cannot see the last
one, because it sends no `Origin` header.

Defence: the Pages build probes the API before building the frontend. It must
answer, report `cohort_source: supabase`, return rows, and send
`Access-Control-Allow-Origin` for the Pages origin. **If any check fails, the
build clears the address and publishes the demo as a replay**, with a warning
and a note in the run summary. None of these checks fails the deploy.

They did fail the deploy at first. That let a setting on an external service
stop the framework site and the other demo from publishing. The replay is a
correct result, because the page says it is a replay and names the commit.
`healthcare-api-check.yml` is the workflow to run when you want a failing check
for the API.

### h. Three frontends drifting apart visually

The design system is shared by generated copies, not imports, because the
healthcare demo must stay extractable. A copy edited directly would drift.

Defence: `scripts/sync_design_system.py --check` in both workflows, and a test
that every token is defined on bare `:root` before any dark block. A token
defined only in a dark block puts one theme's text on the other theme's
background, and nobody notices until they open that theme.

## 3. The test loop

CI runs the same commands a contributor runs. Write the failing test first,
watch it fail, make it pass, then run the loop below before pushing. A push that
turns CI red costs a cycle and the reviewers' trust.

```bash
# 1. planners: the fastest loop; no Docker, database or GPU
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test && cd -

# 2. agents, with the planner available so nothing skips
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  .venv/bin/python -m unittest discover -s agents/tests -t . -v

# 3. the service suite you changed; core-service both ways
PYTHONPATH=.:services/core-service .venv/bin/python \
  -m unittest discover -s services/core-service/tests -t services/core-service/tests
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  PYTHONPATH=.:services/core-service .venv/bin/python \
  -m unittest discover -s services/core-service/tests -t services/core-service/tests

# 4. cross-service contracts, if you changed a route
.venv/bin/python scripts/service_inventory.py --check
.venv/bin/python -m unittest discover -s tests -t .

# 5. the JVM side, if you changed it
mvn --batch-mode -f services/spring-services/pom.xml verify

# 6. a frontend, if you changed it
apps/healthcare-demo/scripts/run_frontend_demo.sh --check
cd site && npm run build && npm run check && cd -
```

`dune` is not on PATH, so always use `opam exec --`. Python tests import as
`agents.common...`, so run them from the repository root with the gitignored
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
  through `env:`, use `"$X"`, and validate it. `healthcare-data.yml` checks that
  its cohort size is an integer, and `pages.yml` refuses an API address that
  contains a query string or credentials.
- **A repository variable is not a secret store.** Variables are not masked, so
  every run that reads one prints it, and on a public repository the log is
  public. A credential pasted into a variable must be rotated, not just removed.
- **Keep jobs independent.** Each job installs only what it needs, so a
  contributor can reproduce one job locally.
- Pin actions to a major version (`actions/checkout@v4`,
  `ocaml/setup-ocaml@v3`) and cache per ecosystem (`pip`, `npm`, `maven`,
  `dune-cache`).

## 5. Known limits

- **Pages must be enabled by a repository admin.** The workflow token can deploy
  to an existing Pages site but cannot create one, so `configure-pages` may fail
  and the base path falls back to the repository name. Everything before the
  deploy still runs. The deploy returns 404 until Settings → Pages → Source is
  set to GitHub Actions.
- **Docker and Kubernetes are not tested.** `docker compose --env-file
  env/.env.compose up --build` and Minikube validation are manual. Minikube is
  the main item in `TODO.md`, blocked on local disk space (see `CHANGES.md`). CI
  tests the code and the contracts, not the deployment.
- **The Postgres-backed suites skip in CI**, because CI has no Postgres with the
  pagila dataset. These skips are expected, and the planner guard ignores them.
  A change to source adapters is not fully tested by CI.
- **The NL2SQL frontend has no browser test.** Its job builds the bundle, which
  only proves that it compiles.
- **CI does not test the deployed healthcare API.** `tests/test_deployed_api.py`
  runs only when `HEALTHCARE_DEPLOYED_API` is set, so it skips in the
  `demo-apps` job and runs in `healthcare-api-check.yml`. This keeps CI from
  failing because a free-tier container is asleep. It also means a green CI run
  says nothing about the deployed API. Every fault that deployment has had (an
  image without the Postgres driver, a port nothing routed to, a CORS value with
  a stray parenthesis) passed every test here and showed up on the first request
  to the running service. Run the probe after changing what the image installs,
  the port it binds, or what it reads.

When you report on a CI run, say which of these limits applied.
