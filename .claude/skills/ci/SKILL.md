---
name: ci
description: Strict rules for the Dagents CI pipeline and for what counts as a verified change — the GitHub Actions workflow in .github/workflows/ci.yml, the guard scripts in scripts/, the generated service inventory, and the commands each suite runs. Use before editing a workflow, adding a test suite or a job, changing a requirements file, or reporting that a change passes. States the repo's polyglot layout, its TDD loop, and the specific ways a green run here has been false in the past.
---

# Dagents CI

This skill is binding, not advisory. Its single rule: **a green run must mean
something.** Everything below follows from that.

## 1. What the pipeline has to cover

Dagents is polyglot by layer, so CI is too. Each layer owns what it is best at
and each has its own feedback loop:

| Layer | Path | Runner needs | Suite |
|---|---|---|---|
| **OCaml planners** — pure validate/compile/route/render | `bindings/ocaml/` | opam + OCaml 5.1 | `dune test` |
| **Python agents** — LMA, GMA, control plane, governance, federation | `agents/` | Python 3.11 + `dagentsc` | `agents/tests` |
| **Framework services** — core, pipeline, model | `services/` | Python 3.11 (+ torch for model) | `services/<name>/tests` |
| **Spring services** — JVM control and core façades | `services/spring-services/` | JDK 21 + Maven | `mvn verify` |
| **Demo apps** — healthcare, NL2SQL | `apps/`, `services/nl2sql-demo/` | Python 3.11 + `dagentsc` | own suites |
| **Frontends** — React + Vite | `*/frontend/` | Node 22 + a browser | `npm run build`, `npm run smoke` |
| **Cross-service contracts** — the endpoint inventory and API conventions | `tests/` | every Python service's deps | `tests/` |

Python reaches OCaml through the `dagentsc` JSON subprocess boundary, so the
planner is a **build artifact every downstream job depends on**, not an optional
extra. `planners` builds it once and uploads it; `agents`, `framework-services`,
`demo-apps` and `frontends` download it.

## 2. The four false greens — each has a defence in the workflow

Do not remove any of these. Each one is there because the repo actually shipped
the failure.

### a. A suite green because its most important tests skipped

The governance and federation tests run against the real `dagentsc` binary,
because stubbing the planner would prove the code calls something, not that the
governance holds. They skip with a message when the binary is unreachable, which
is right for a developer and **wrong for CI**.

Defence: `scripts/ci_no_skipped_planner_tests.py` parses the verbose suite log
and fails the run on any planner-related skip. The suites must therefore run with
`-v` — the guard treats a non-verbose log as a failure rather than passing on a
log it cannot inspect.

```bash
python -m unittest discover -s agents/tests -t . -v 2>&1 | tee agents-suite.log
python scripts/ci_no_skipped_planner_tests.py agents-suite.log
```

`tests/test_ci_guards.py` tests the guard itself against the real skip message,
so the guard cannot silently stop matching.

### b. Two implementations of one behaviour, only one under test

`core-service` compiles manifests through the OCaml compiler when `dagentsc` is
reachable and through a Python fallback when it is not. Containers put `dagentsc`
on PATH, so **the OCaml path is the deployed one** — and the two renderers
diverged silently because the suite only ever exercised the fallback.

Defence: the `framework-services` job runs the core-service suite **twice**, with
and without `DAGENTSC_BIN`. Any behaviour with two implementations gets the same
assertions run against both. If they cannot be kept in step, delete one.

### c. A convention documented in prose and enforced nowhere

Defence: the `contracts` job. `scripts/service_inventory.py --check` regenerates
the endpoint inventory from the live FastAPI routing tables and the Spring
controllers and fails on drift; `tests/test_service_inventory.py` additionally
asserts that the framework services are uniformly `/api/v1/...`, that the
LMA/GMA legacy-plus-versioned pairs still exist and agree, and that the Spring
surfaces mirror their Python counterparts.

Any endpoint change means regenerating the inventory:

```bash
.venv/bin/python scripts/service_inventory.py --write   # then commit both files
```

### d. A skip reported as a pass

`apps/healthcare-demo/frontend/smoke.mjs` exits **2** and prints `SKIP` when no
browser is available. The demo script maps that to a friendly message and exits
0, which is right for a developer and wrong for CI.

Defence: the `frontends` job installs a browser, points `CHROMIUM_PATH` at it,
and fails if the log contains a skip or contains no passing assertions.

Never make a job tolerate exit 2, and never report a skipped smoke test as a
pass in your own summary either.

## 3. The TDD loop CI enforces

CI runs the same commands a contributor runs. Write the failing assertion first,
watch it fail, make it pass — then run the loop below before pushing. A push that
turns CI red costs a cycle and the reviewers' trust.

```bash
# 1. planners — fastest feedback in the repo; pure, no Docker, no DB, no GPU
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test && cd -

# 2. agents, with the planner reachable so nothing skips
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  .venv/bin/python -m unittest discover -s agents/tests -t . -v

# 3. the service suite you touched — core-service BOTH ways
PYTHONPATH=.:services/core-service .venv/bin/python \
  -m unittest discover -s services/core-service/tests -t services/core-service/tests
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  PYTHONPATH=.:services/core-service .venv/bin/python \
  -m unittest discover -s services/core-service/tests -t services/core-service/tests

# 4. cross-service contracts, if you touched a route
.venv/bin/python scripts/service_inventory.py --check
.venv/bin/python -m unittest discover -s tests -t .

# 5. the JVM side, if you touched it
mvn --batch-mode -f services/spring-services/pom.xml verify

# 6. the frontend, if you touched it
apps/healthcare-demo/scripts/run_frontend_demo.sh --check
```

`dune` is not on PATH — always `opam exec --`. Python tests import as
`agents.common...`, so run from the repository root, with the gitignored `.venv/`
rather than system Python. Each service has its own `requirements.txt`; there is
no root-level one, and CI installs per job for that reason.

**Report the skip count, always.** "Tests pass" with the governance tests skipped
is a false report, whoever is reading it.

## 4. Rules for changing the pipeline

- **A new suite needs a job, or it is not run.** Adding tests under a path no job
  discovers is the same as not writing them.
- **A new job goes in the `ci` gate's `needs` list.** That job is the single
  required check, so a job left out of it can fail without blocking anything.
- **No test may need the network.** Pin fixtures; never fetch a model or dataset
  inside a test. The Hugging Face provider tests assert the *refusals* — missing
  dependency, download not permitted — precisely so they never need the Hub.
- **An optional dependency that CI installs must not leave a test skipped.** The
  `framework-services` job installs
  `services/model-service/requirements-optional-models.txt` and then fails if the
  Hugging Face tests skipped anyway, because that means the requirements file and
  the test's own check have drifted apart.
- **Prefer a guard script to a comment.** A rule the pipeline does not check is a
  rule that will decay; that is failure mode (c) above. Put it in `scripts/`,
  give it a test in `tests/`, and call it from the workflow.
- **Keep jobs independently runnable.** Each installs only what it needs, so a
  contributor can reproduce one job locally without provisioning the whole repo.
- Pin action versions to a major tag (`actions/checkout@v4`,
  `ocaml/setup-ocaml@v3`) and cache per ecosystem (`pip`, `npm`, `maven`,
  `dune-cache`).

## 5. Known limits, stated rather than hidden

- **Docker and Kubernetes are not covered.** `docker compose --env-file
  env/.env.compose up --build` and Minikube validation are still manual; the
  latter is the main item in `TODO.md`, blocked on local disk capacity (see
  `CHANGES.md`). CI proves the code and the contracts, not the deployment.
- **The Postgres-backed suites skip in CI**, because no Postgres with the pagila
  dataset is provisioned. Those skips are legitimate and the planner guard
  ignores them — but they are skips, and a change to source adapters is not
  fully verified by a green run alone.
- **The NL2SQL frontend has no browser smoke test.** Its job builds the bundle,
  which proves it compiles and nothing more.

When you report on a CI run, say which of these applied. That is the difference
between a green run that means something and one that only looks like it does.
