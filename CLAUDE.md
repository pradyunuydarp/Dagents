# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What Dagents is

Dagents is a reusable framework that other applications build on. It runs a
computation agent at each data source, combines their outputs through a global
agent, and produces workload plans that backends can deploy.

In short: compute at each source, combine the results centrally, make decisions
with typed planners, and deploy through services.

- **LMA** (local monitoring agent; the presentation material sometimes calls it
  CMA, constraint monitoring agent): one per data source (a tenant, database,
  service, event stream or environment). It profiles data, splits work, runs
  local models, publishes summaries, and enforces governance at three of the
  four guard boundaries.
- **GMA** (global monitoring agent): registers LMAs, combines their outputs,
  runs aggregate models across sources, coordinates dispatch, and runs federated
  rounds and release decisions.
- **Framework services** (core, pipeline, model): stable APIs, so applications
  call Dagents instead of rebuilding profiling, orchestration, model routing and
  manifest generation.
- **OCaml planners**: pure, typed compilers for validation, DAG planning, model
  routing, Kubernetes manifests and Docker Compose files, governance (ethical
  restrictions) and federated rounds.

Intended consumers: Watchdog, Datalytics, the NL2SQL demo app and the healthcare
stroke-triage demo app, both in this repository.

## The main architectural rule

Each language does the work it suits best:

| Layer | Owns | Never owns |
|---|---|---|
| **OCaml** (`bindings/ocaml/`) | Pure planning: validate, compile, route, render | Database connections, training loops, API servers |
| **Python** (`dagents/`, `services/`) | FastAPI services, model training and inference, source I/O, runtime state | Deterministic planning rules |
| **Spring Boot** (`services/spring-services/`) | Orchestration APIs, policy entry points, external integration | Machine learning |

Decisions go in the OCaml planners. Anything with side effects goes in Python or
Java.

Services call OCaml by running `dagentsc` with JSON on standard input, never
through FFI. A separate process isolates failures, lets each side be upgraded on
its own, and keeps the Kubernetes deployment simple.

## Repository map

```text
dagents/         the Python framework, packaged for PyPI as `dagents`
  common/        shared domain contracts, the extension registry, dagents_runner (the dagentsc
                 bridge), and agent_client and agent_auth (how agents call each other)
  lma/           Local Monitoring Agent service
  gma/           Global Monitoring Agent service
  tests/         agent + control-plane + runner tests (not in the wheel)
pyproject.toml   the package's metadata, built with hatchling
hatch_build.py   the build hook that puts the planner into a platform wheel
bindings/ocaml/  the planners (dune workspace)
  lib/common_ir           shared typed IR + JSON codecs
  lib/dataset_compiler    source validation, profiling, schema contracts, quality, transforms
  lib/pipeline_compiler   DAG validation + topological ordering
  lib/model_router        dataset profile + task -> model family + packaging mode
  lib/manifest_compiler   typed workload spec -> Kubernetes YAML or a Docker Compose file
  lib/governance_compiler GRAILS ethical restrictions: what protection a request needs
  lib/federation_compiler federated round manifests, eligibility, quorum, release gates,
                          model version status, round recovery after a restart
  bin/dagentsc.ml         the command-line program services call
contracts/grpc/  shared LMA/GMA protobuf contract
services/
  core-service/       catalog, topology, workload compilation, manifest generation
  pipeline-service/   pipeline registry, validation, async runs
  model-service/      training, checks, benchmark datasets, model jobs
  spring-services/    Spring Boot control + core services
  nl2sql-demo/        demo app that uses the framework's services
apps/
  healthcare-demo/    self-contained stroke-triage demo on real International Stroke Trial
                      records from three hospitals, one store per hospital, with its own
                      README, tests and compose file; scripts/extract_repo.sh turns it into
                      its own repository
examples/
  getting-started/    the worked example of docs/guide: two stores check a rule together;
                      every Python block in the guide is copied from it
site/            the framework site: home page, the learning guide, the guide to
                 building an application, the API reference, the Resources page
design/          the design system source, copied into each frontend by a script
tests/           checks that span more than one service
docs/
  learn/          the learning guide, rendered by the site (Markdown + Mermaid)
  guide/          the guide to building an application on Dagents, rendered at #/guide
  agents/         LMA/GMA architecture
  architecture/   OCaml adoption plan, Python-vs-OCaml comparison
  demo/           demo scripts + recorded inputs and expected outputs
  presentation/   teaching decks (.pptx) and their notes, resources.json (the list
                  behind the Resources page), exports/ (PDFs and previews made by
                  scripts/export_presentations.py), PlantUML sources
  reports/        LaTeX reports and PDFs
env/             committed per-service env files (no secrets)
```

## Commands

### OCaml planners

`dune` is not on PATH, so always use `opam exec --`:

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test
```

The binary is written to `bindings/ocaml/_build/default/bin/dagentsc.exe`. Most
OCaml tests need no Docker, database or GPU, because the modules are pure. This
is the fastest test loop in the repo, so prefer it.

### Python tests

Tests import as `dagents.common...`, so run them from the repository root.
FastAPI and the other dependencies are in the gitignored `.venv/`, not system
Python:

```bash
.venv/bin/python -m unittest discover -s dagents/tests -t .
```

If `.venv/` is missing (a fresh clone), create it and install the requirements
you need. Each service has its own `requirements.txt`; there is no root-level
one.

Each service has its own suite under `services/<name>/tests/`. The NL2SQL suite
adds its backend to the path itself, so it also runs from the root.

`tests/` at the root holds checks that span more than one service, such as the
endpoint inventory and the API conventions. They need every Python service
importable, while `dagents/tests` needs only the agent requirements:

```bash
.venv/bin/python -m unittest discover -s tests -t .
```

`services/model-service/requirements-optional-models.txt` installs the Hugging
Face stack. Without it, the provider tests that need it skip. Install it to test
the real `local_files_only` path.

The governance and federation tests run against the real `dagentsc` binary, not
a stub, because a stub would only prove that the code calls something. They find
the dune build themselves and skip, with a message, when it is missing. Check
the skip count: a run that skipped them has not tested governance.

**Run the service suites with and without `DAGENTSC_BIN`.** `core-service`
renders manifests with the OCaml compiler when the binary is available and with
a Python fallback when it is not. Containers have `dagentsc` on PATH, so the
OCaml path is the one deployed. The two renderers once drifted apart unnoticed,
because the suite only tested the fallback:

```bash
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  PYTHONPATH=.:services/core-service .venv/bin/python \
  -m unittest discover -s services/core-service/tests -t services/core-service/tests
```

Any behaviour with two implementations needs the same assertions run against
both. If they cannot be kept in step, delete one.

The healthcare demo has its own suite:

```bash
cd apps/healthcare-demo && PYTHONPATH=../..:backend ../../.venv/bin/python -m unittest discover -s tests -t .
```

**The Postgres tests need a database URL.** The per-hospital stores, the role
isolation and the Postgres pilot run against a real PostgreSQL 16 server. Each
test creates a scratch database, adds Supabase's roles
(`apps/healthcare-demo/supabase/emulate_supabase_roles.sql`), applies the
migrations as a non-superuser and drops everything afterwards. Point them at an
admin login:

```bash
export HEALTHCARE_DEMO_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres  # healthcare suite
export DAGENTS_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres          # dagents/tests: role scoping, the lease, the model store, the round stores, the job log, the GMA's Postgres wiring, the GMA restart tests
```

Without them these tests skip with a message that starts `database test
skipped`. The in-memory store runs the same assertions without a database, so
both store implementations are tested. The same holds for the agents' twins:
`InMemoryModelStore` and `PostgresModelStore`, `InMemoryJobLog` and
`PostgresJobLog`, and the GMA's round, enrolment and update stores
(`dagents/tests/test_round_store.py`) each run one shared suite.

### CI

`.github/workflows/ci.yml` runs the same commands, one job per layer: `planners`
(builds OCaml, runs `dune test`, and uploads `dagentsc` for the other jobs and
a statically linked `dagentsc-static` for `packaging`), `agents`,
`framework-services` (core-service twice, with and without `DAGENTSC_BIN`),
`contracts`, `spring`, `demo-apps`, `frontends`, `deploy-smoke` (the
healthcare UI in network mode with `--deploy` against PostgreSQL 16, where the
smoke test adds Hospital 290 from the page), `nl2sql-e2e` (the NL2SQL demo's
services as real processes with the planner and torch, its UI in Chromium
against them, and the replay that Pages publishes), `packaging` (builds the sdist and
a Linux wheel with the planner, installs the wheel into clean environments,
one of them with openai-agents, and runs the agent suite on the newest
versions the package's extras allow; see **Releasing**), `docs-impact`, and a
`ci` gate job that is the single required check.

`.github/workflows/release.yml` is separate from CI. It publishes the package
in a run on a `v*` tag, which a tag push or a manual run on `main` with the
`release_tag` input ticked starts. A manual run without the input is a dry
run. See **Releasing**.

`docs-impact` runs `scripts/check_docs_impact.py` on pull requests and pushes
to `main`. It fails when a change touches a code area (`dagents/`, `services/`,
`bindings/ocaml/`, `apps/`, `contracts/`, `scripts/`, `examples/`, `env/`, the
compose file, a workflow, `pyproject.toml`, `hatch_build.py` or
`.github/build-constraints.txt`, or a deck) and none of the docs that area
feeds, unless a commit message has a line `Docs-Impact: none [area] (reason)`
that names the area. Run it before pushing:

```bash
.venv/bin/python scripts/check_docs_impact.py --base origin/main
```

Two guard scripts catch runs that look green but are not:

```bash
python -m unittest discover -s dagents/tests -t . -v 2>&1 | tee suite.log
python scripts/ci_no_skipped_tests.py --forbid planner --forbid charts suite.log  # fails on a forbidden skip
.venv/bin/python scripts/service_inventory.py --check                             # fails when a route is undocumented
```

`ci_no_skipped_tests.py` reads a verbose unittest log. `--forbid` takes a
category of skip that fails the run: `planner` (no `dagentsc`), `database` (no
test database URL) or `charts` (no matplotlib). `--require-ran MODULE.CLASS`
fails unless that test class ran at least one test, which also catches a class
that skipped in `setUpClass`. The `demo-apps` job runs a Postgres 16 service
container, so it forbids database skips and requires each Postgres test class
to have run. `tests/test_ci_guards.py` finds every test class that needs a
database and fails if one is missing from that list; a new class goes in both
`ci.yml` and `healthcare-data.yml`. `scripts/ci_no_skipped_planner_tests.py
LOG` still works; it runs the guard with `--forbid planner`.

CI starts no container and runs no Minikube; it uses the Docker command line
only for `docker compose config`. See **Current state**. The suites set
`DAGENTSC_BIN`, so they pass even when an image lacks the planner. One check
covers the images without building them: `tests/test_planner_in_images.py`
follows each Python image's imports from the module uvicorn runs, and fails if
an image whose service reaches the planner does not copy `dagentsc` to
`/usr/local/bin`.

### The published sites

`.github/workflows/pages.yml` publishes three trees to GitHub Pages: the
framework site at the root and the two demos below it. The addresses are in
`env/.env.published`:

| | |
|---|---|
| framework site | <https://pradyunuydarp.github.io/Dagents/> |
| stroke-triage demo | <https://pradyunuydarp.github.io/Dagents/healthcare-demo/> (calls the deployed healthcare API) |
| NL2SQL demo | <https://pradyunuydarp.github.io/Dagents/nl2sql-demo/> (replays a recording) |

The deployed healthcare API keeps each hospital's records in its own Postgres
schema, runs the hospitals' LMAs and the GMA in its own process, and has the
planner in its image. Its address is written only in
`env/.env.published`, which the Pages build reads. Never copy it into a README,
the guide or the UI: `tests/test_publishing_guards.py` fails if the API's host
appears in any tracked file outside `env/` and `.github/`. Docs that call the
API use one run locally (`apps/healthcare-demo/scripts/run_local_demo.sh`).

NL2SQL has no deployed backend, and the healthcare demo needs a replay for when
its API is down, so the workflow records real runs first:

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && cd -
.venv/bin/python scripts/capture_demo_recordings.py --demo all   # needs the planner
.venv/bin/python scripts/check_demo_recordings.py                # refuses a useless recording
cd site && npm install && npm run build && npm run check         # the site, checked in a browser
```

The capture script refuses to run without `dagentsc`. The Guard fails closed, so
a recording made without the planner would hold only denials. The checker
refuses a recording whose guard controls change nothing, that lacks a request
the UI sends at the exact path and query string it sends it, or whose healthcare
answers do not list the agent calls behind them (`framework_calls`, with the
GMA's round call and a `jobs:run` call to each hospital's LMA in the pilot, a
run dispatch row the hospital accepted for each hospital in the round reports,
and `agents.mode` in the status), or whose `GET /api/v1/deployments` offers or
lists a deployment. The capture runs with `HEALTHCARE_DEMO_DEPLOYER=off` and
refuses to write such a recording.
`--port` sets the port of the demo backend the capture starts. Recordings are
gitignored; the workflow makes them.

**A failed deploy.** A deploy can stall on GitHub's side. So the deploy job
tries a failed deploy once more after 30 seconds, and a second failure fails the
run. If both attempts fail, re-run the failed jobs of that run. The Pages
artifact is kept for one day; after that, re-run the whole run. A new run of the
same commit adds a green check beside the red one but does not replace it, so
the commit still shows the failure.

**Link previews and icons.** A link to one of the sites, shared on LinkedIn,
WhatsApp, Slack or X, shows that site's card: the 1200 x 630 PNG named by the
page's `og:image` tag. The three cards and the logo mark are HTML and SVG files
in `design/social/` (its README lists each output and which ones are copies).
`scripts/render_social_cards.mjs` renders them with the fonts from the
`@fontsource` packages into each frontend's `public/` folder, with the favicons,
and the PNGs are committed:

```bash
cd site && npm install && cd -             # playwright-core and the fonts
node scripts/render_social_cards.mjs       # renders what changed; --force renders everything
```

Look at every changed PNG, then commit it with `design/social/rendered.json`.
`tests/test_social_previews.py` fails when a source changes without a new
render, when a card is not 1200 x 630 or is over 300 KB, when a page lacks a
tag, or when a description or alt text holds a number, as a digit or a word.
A card or a description shows structure and purpose only, never a count,
score, date or result, because those change.

Crawlers ignore a relative `og:image`. So each `index.html` writes
`%PUBLIC_SITE_URL%`, and the Vite plugin `social-meta.mjs` replaces it at build
time with the `PUBLIC_SITE_URL` variable, which the Pages workflow reads from
`env/.env.published`. A build without the variable uses the base path.
`scripts/check_link_previews.py` runs three times in the Pages workflow: it
stops the deploy when an address is missing or not plain https, when a built
page lacks an absolute tag, its card or an icon, and, after the deploy, when a
published `og:image` does not load. Pages live in the URL hash, so every page of a
site (`#/learn/...`, `#/api/...`) shares its root page's preview. LinkedIn and
WhatsApp cache previews, so a changed card can keep showing the old one. The
[LinkedIn Post Inspector](https://www.linkedin.com/post-inspector/) fetches the
page again and refreshes LinkedIn's copy; WhatsApp has no such tool.

The site's learning guide is `docs/learn/*.md`. `npm run check` visits every
guide page in Chromium and fails if a Mermaid diagram does not draw, an image
does not load, or a link between pages is broken.

The guide to building an application is `docs/guide/*.md` (`#/guide`; page
order in `site/src/guide.ts`). Its code blocks come from
`examples/getting-started/`. `tests/test_getting_started_example.py` fails when
a block marked `<!-- source: PATH -->` differs from its file, when a Python
block has no marker, or when a block marked `<!-- output: PATH -->` differs from
what the script prints with the real planner. Its example tests skip without
`dagentsc`; the `agents` CI job runs them with the planner and forbids that
skip. A code change that the guide or the example shows changes them in the
same commit (the `docs-sync` skill):

```bash
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  .venv/bin/python -m unittest tests.test_getting_started_example -v
```

The site's Resources page (`#/resources`) offers the teaching decks and reports.
Its list is `docs/presentation/resources.json`, and the build copies each file
into `dist/resources/`. The PDFs and preview images in
`docs/presentation/exports/` are made by a person, not by CI, because the
conversion needs LibreOffice Impress and someone has to look at the pages. After
changing, adding or removing a deck:

```bash
.venv/bin/python scripts/export_presentations.py --write   # needs soffice, pymupdf, pillow
```

Look at the new PDF, then commit it with `resources.json`.
`tests/test_site_resources.py` fails when a listed file changes without this
step, or when a deck or note under `docs/presentation/` is neither listed nor
excluded with a reason.

### Releasing

The Python framework (`dagents/`) is released to PyPI as `dagents`, from a
tag on `main`, by the release workflow; nobody uploads by hand. The owner
starts a release from the Actions tab, and the workflow creates the tag (see
"Make a release" below). The services in `services/`, the Spring services and
the demos are not in the package.

**Versions.** `__version__` in `dagents/__init__.py` is the only place the
version is written; the build reads it. Versions follow PEP 440 and the
project's stages: `0.1.0a1`, `0.1.0a2` for alphas, `0.x.0b1` for betas,
`1.0.0rc1` for a release candidate and `1.0.0` for general availability. One
distribution name, `dagents`, serves every stage. Every version before 1.0.0
is a pre-release, so `scripts/release_version.py` refuses a final `0.Y.Z`.
While only pre-releases exist, `pip install dagents` installs the newest one;
once 1.0.0 exists, a pre-release needs `--pre` or an exact version. The
`Development Status` classifier in `pyproject.toml` must be the one for the
stage: `3 - Alpha` for an `a` version, `4 - Beta` for a `b` or `rc` version,
and `5 - Production/Stable` for a final version. `scripts/check_dist.py` fails
when the built metadata has another one, or more than one.

**The PyPI page.** The README is the package's description on PyPI, and PyPI
never lets a release's description change after the upload. So the README on
the tagged commit must be right for that release: its status line and its
install section. `scripts/check_dist.py` fails when the description does not
show `pip install dagents`.

**What a release builds.** One sdist and three platform wheels: Linux x86_64,
Linux aarch64 and macOS arm64. Each wheel bundles the planner for its platform
at `dagents/_bin/dagentsc`. The Linux planners link statically (`dune build
--profile static`), so one wheel covers glibc and musl distributions. Other
platforms (Windows, macOS on Intel) install from the sdist and get no planner,
and the Guard denies every request until `DAGENTSC_BIN` or `dagentsc` on PATH
provides one. The build prints a notice, but pip shows a build's output only
with `-v`, so `python -m dagents` and the warning an LMA or GMA logs when it is
built are what tell the user. The GitHub Release also carries the three
planner binaries with `SHA256SUMS`.

Every build uses the backend versions pinned in `.github/build-constraints.txt`
(hatchling and its dependencies): the workflows pass the file as
`PIP_CONSTRAINT` and `PIP_BUILD_CONSTRAINT`, and `scripts/check_dist.py
--build-constraints` fails unless each wheel names the pinned hatchling. Update
the pins on purpose, as with the action pins.

The `lma` and `gma` extras accept FastAPI from 0.115.0 and uvicorn from
0.30.6, the versions the agents job pins, to below 1.0. The `packaging` job
runs `dagents/tests` again on the newest versions those ranges allow, on
Python 3.13.

**The old name.** The package was called `agents` before 0.1.0a1. Another
project owns that name on PyPI, and openai-agents installs a top-level module
called `agents`. The smoke test's first check reads only the files the
`dagents` distribution installed (its `RECORD`), so another package's `agents`
module does not fail it. The `packaging` job installs the wheel together with
`openai-agents`, runs `pip check`, and runs the smoke test with
`--agents-from openai-agents`, which checks that `agents` comes from
openai-agents and imports in the same process as `dagents`.

**Make a release:**

1. Change `__version__` (and the classifier when the stage changes), check
   that the README's status line and install section are right for this
   release, and land the change on `main` through a pull request.
2. Start the release from GitHub, after the merge:
   1. Open the repository's **Actions** tab and select the workflow
      **Release**.
   2. Select **Run workflow**. Keep **Use workflow from** on **Branch: main**.
   3. Tick the box whose label starts **Tag the head of main as
      `v<version>` and publish it**.
   4. Select the green **Run workflow** button.

   This run has one job, **Tag the version and start the release**. It
   creates the annotated tag `v<version>` on the run's commit with the
   workflow's token (`scripts/release_version.py --create-tag`), then starts
   the workflow again on the tag. The tag's message names the person who
   started the run and links to the run. The job stops, and creates nothing,
   in two cases:
   - `main` has moved on since the run started. Start a new run from **Run
     workflow**. Re-running this run checks the same commit again and stops
     again.
   - The tag already names another commit. Change the version.

   When the tag already names this commit, the job does not create it again
   and starts a new release run on it. If the job created the tag but the
   release run did not start, run the workflow on the tag: **Use workflow
   from**, **Tags**, `v<version>`, with the box unticked.

   A maintainer who can push tags can push the tag instead; it starts the
   same release run:

   ```bash
   git tag -a v0.1.0a1 -m "dagents 0.1.0a1"
   git push origin v0.1.0a1
   ```

3. The release run on `refs/tags/v<version>` runs
   `.github/workflows/release.yml`. It stops unless the tag is `v` plus the
   version and the tagged commit is on `main`
   (`scripts/release_version.py`), builds the planners on their
   own platforms, builds the sdist and the wheels from it, checks every file
   (`scripts/check_dist.py`, `twine check`), installs each wheel into clean
   environments on its platform (`scripts/smoke_installed_package.py`),
   publishes to TestPyPI, checks that TestPyPI serves the same files
   (`scripts/check_published_files.py`), publishes to PyPI, checks PyPI the
   same way and creates the GitHub Release. An `a`, `b` or `rc` version is
   marked as a pre-release. The run is on the tag, whichever way it started,
   so environment rules that allow only `v*` tags still apply. Once the owner
   has done step 3 of **Trusted Publishing**, the reviewer on `pypi` approves
   the upload to PyPI (see **Approve the upload to PyPI** below).

A manual run with the box unticked, on any branch, is a dry run: it builds
and checks everything and publishes nothing. With the box ticked on any other
branch or tag, the run stops with an error. To retry a release run that
failed, re-run its failed jobs, or run the workflow on the tag (**Use workflow
from**, **Tags**) with the box unticked.

A second run of the tag job, or a second run on the same tag, cannot publish
different files:

- TestPyPI skips files it already has, and the next job compares their
  hashes with the files this run built.
- PyPI accepts a file identical to one it has without changing anything, and
  refuses a different file with the same name.
- The GitHub Release step fails when the release exists.

So when PyPI already has the version, the new run publishes nothing new.
With a reviewer on `pypi`, it still asks the reviewer to approve. Reject that
request. When the earlier run failed before publishing, the new run is a
normal release run.

**Approve the upload to PyPI.** When the `pypi` environment has a required
reviewer, the run waits at **Publish to PyPI** until that reviewer approves.
Step 3 of **Trusted Publishing** below adds the reviewer, and that section
says whether it is done. Without a reviewer, the run uploads to PyPI without
waiting. Before approving:

1. Check that the run's commit is on `main`. The run page shows the commit.
   `gh api repos/pradyunuydarp/Dagents/compare/main...<commit> --jq .status`
   prints `behind` or `identical` for a commit on `main`, and `ahead` or
   `diverged` for any other. Check this on GitHub, not in the run's logs or
   summary. The workflow file at the tagged commit writes those, and a
   commit that is not on `main` can carry a changed workflow file.
2. Read who asked for the release. The summary of **Read the version and
   check the tag** shows the tag's tagger and message. A tag made from the
   Actions tab names the person who started the run and links to that run.
3. Reject the request when PyPI already has this version (see above).

The tag job starts the release run with the workflow token, so that run's
actor is `github-actions[bot]`, not the person who ticked the box. The
environment setting **Prevent self-review** stops the user who started a
deployment from approving it, and here that user is `github-actions[bot]`.
So the setting does not stop the person who started the release from
approving it.

**Who can publish.** Anyone with write access to the repository can start a
release from the Actions tab. The workflow token pushes the tag, and a tag
ruleset that restricts creations would refuse that push, so the setup below
restricts only updates and deletions of `v*` tags. As a result, anyone with
write access can also push a `v*` tag on any commit, including one that is
not on `main`. A run on that tag uses the workflow file of that commit, which
can leave out the version and `main` checks, and the environments admit it
because they check only the tag's name. So:

- The `pypi` reviewer is the only check before an upload to PyPI. This is why
  the reviewer checks the commit (step 1 above), and why the reviewer is a
  required part of the setup. Until the owner adds the reviewer (step 3 of
  **Trusted Publishing**), such a run can upload to PyPI.
- `testpypi` has no reviewer unless the owner adds one, so such a run can
  upload to TestPyPI. Its files can take that version's file names there,
  and a later release of the same version then fails its TestPyPI hash
  check.

Limiting tag creation to admins again would need a GitHub App token, stored
in an environment limited to `main`, instead of the workflow token. That is
not set up.

**Trusted Publishing.** The workflow holds no token. PyPI and TestPyPI trust
this repository's `release.yml` through OpenID Connect, and each upload
carries a PEP 740 attestation. The owner registered the Trusted Publishers
once, before 0.1.0a1, and they stay in place for every release:

1. On PyPI, a publisher for project `dagents`, owner `pradyunuydarp`,
   repository `Dagents`, workflow `release.yml`, environment `pypi`. It was
   added as a pending publisher on
   <https://pypi.org/manage/account/publishing/>, because the project did not
   exist yet. It now shows under the project's Publishing settings on PyPI,
   where any change is made.
2. The same on TestPyPI, with environment `testpypi`.

The owner must also do steps 3 and 4. As of 0.1.0a1 they are not done: the
PyPI upload of release run 39016551221 went ahead without a reviewer's
approval, and the repository's only ruleset protects `main`.

3. In the repository settings (Environments), on the environments
   `testpypi` and `pypi` (the 0.1.0a1 run deployed to both), sets the
   deployment branches and tags rule to selected tags with the pattern
   `v*`. On `pypi`, adds a required reviewer, so a person approves each
   upload. This step is required: see **Who can publish**. A reviewer on
   `testpypi` as well makes TestPyPI uploads wait for a person too.
4. In the repository settings (Rules, Rulesets), adds a tag ruleset that
   targets `v*` and restricts updates and deletions. Then nobody can move a
   release tag to another commit or delete it. Do not restrict creations:
   the release job creates the tag with the workflow's token, and a ruleset
   that restricts creations refuses that push.

Only the publish jobs get `id-token: write`, only the tag job gets
`actions: write`, and every action in `release.yml` is pinned to a commit;
`tests/test_release_workflow.py` checks all three.

**Build and check locally**, without publishing. The static build replaces
the planner in `_build/default/bin/`; it is the same code, so the suites can
keep using it.

```bash
.venv/bin/python -m pip install build twine
export PIP_CONSTRAINT=$PWD/.github/build-constraints.txt PIP_BUILD_CONSTRAINT=$PWD/.github/build-constraints.txt
.venv/bin/python -m build --sdist --outdir dist                       # the sdist
cd bindings/ocaml && opam exec -- dune build --profile static ./bin/dagentsc.exe && cd -
mkdir -p /tmp/dagents-sdist && tar -xzf dist/dagents-*.tar.gz -C /tmp/dagents-sdist
DAGENTS_PLANNER_BINARY=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
DAGENTS_WHEEL_PLATFORM=manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64 \
  .venv/bin/python -m build --wheel --outdir dist /tmp/dagents-sdist/dagents-*   # the wheel, from the sdist
unset PIP_CONSTRAINT PIP_BUILD_CONSTRAINT
.venv/bin/python scripts/check_dist.py dist --list --build-constraints .github/build-constraints.txt
.venv/bin/python -m twine check --strict dist/*
python3.11 -m venv /tmp/dagents-smoke
/tmp/dagents-smoke/bin/pip install "$(ls dist/*.whl)[lma,gma]"
REPO=$PWD; (cd /tmp && env -u PYTHONPATH -u DAGENTSC_BIN /tmp/dagents-smoke/bin/python -I \
  "$REPO/scripts/smoke_installed_package.py" --repo "$REPO" --expect-planner bundled)
```

Without the two `DAGENTS_*` variables the wheel is pure Python (`py3-none-any`)
and has no planner, which is what pip builds from the sdist.

### Full stack

```bash
docker compose --env-file env/.env.compose up --build
```

### Demos

```bash
bash docs/demo/run_functional_layer_demo.sh   # OCaml planner output
bash docs/demo/run_full_stack_demo.sh         # whole stack via compose
bash docs/demo/run_demo_quick.sh              # fallback: build + tests + a few planner calls
services/nl2sql-demo/scripts/run_local_demo.sh  # NL2SQL app and the framework services, no Docker
services/nl2sql-demo/scripts/run_local_demo.sh --check  # the same on free ports, then a browser check of the UI
```

`docs/demo/inputs/` holds request payloads and `docs/demo/expected/` the recorded
responses. Use them as fixtures when changing planner or service output.

The healthcare demo has its own scripts, which need no Docker and no database:

```bash
apps/healthcare-demo/scripts/run_frontend_demo.sh          # API + UI + a guided tour
apps/healthcare-demo/scripts/run_frontend_demo.sh --check  # start, smoke-test the UI, exit
apps/healthcare-demo/scripts/run_pilot.sh                  # no UI: one pilot, printed
apps/healthcare-demo/scripts/run_local_demo.sh             # the API alone on :8080
```

The API runs one LMA per hospital and a GMA in its own process, and calls them
through their routes with `httpx.ASGITransport`. With
`HEALTHCARE_DEMO_DATABASE_URL` set (migrations applied, records loaded), the
agents can run as separate processes instead, on the ports in
`apps/healthcare-demo/env/.env.healthcare-demo`:

```bash
apps/healthcare-demo/scripts/run_local_demo.sh --agents network     # GMA, three LMAs, then the API
apps/healthcare-demo/scripts/run_frontend_demo.sh --agents network  # the same, with the UI
docker compose -f apps/healthcare-demo/docker-compose.yml \
  --env-file apps/healthcare-demo/env/.env.healthcare-demo up --build  # the same layout in containers
```

The scripts make new tokens for each run and never print them. Compose's
`setup` job makes them once per volume and keeps them until
`docker compose down -v`. `apps/healthcare-demo/tests/test_network_mode.py`
runs the network layout against PostgreSQL 16, checks that its pilot equals an
in-process pilot, and approves a candidate across the processes (it needs
`HEALTHCARE_DEMO_TEST_DATABASE_URL`).

A fourth hospital, Hospital 290 (`pl-290`), can join a network-mode run while
it runs (`POST /api/v1/deployments`). core-service renders its LMA's Compose
file, and the API starts it as a local process or a container:

```bash
apps/healthcare-demo/scripts/run_frontend_demo.sh --deploy   # network mode plus core-service; the new LMA is a process
cd apps/healthcare-demo && DOCKER_GID=$(stat -c %g /var/run/docker.sock) \
  docker compose -f docker-compose.yml -f docker-compose.deploy.yml \
  --env-file env/.env.healthcare-demo up --build              # the new LMA is a container
```

It is off by default (`HEALTHCARE_DEMO_DEPLOYER=off`) and on one machine only:
the API refuses to start with it on when `RENDER` is set or the agents run in
process. The override mounts the Docker socket into the API's container, so
never use it on a shared host. `apps/healthcare-demo/tests/test_network_deploy.py`
adds the hospital to real processes against PostgreSQL 16.

All of them need `dagentsc`. Without it the Ethical Guard denies every request,
which is correct but makes every panel a denial, so the scripts check first.
Without `HEALTHCARE_DEMO_DATABASE_URL`, each hospital keeps its records in
memory, loaded from the committed extract in `apps/healthcare-demo/data/ist/`.

The frontend smoke test (`apps/healthcare-demo/frontend/smoke.mjs`) drives the
real UI against a live backend. It checks that each of the guard's three
controls changes the `systolic_bp` strategy, runs a full pilot, opens a hospital
view and checks that its charts draw, submits a data entry and checks that the
record appears, switches to the dark theme and checks that the charts are drawn
again, checks that a 503 without the app's `X-Dagents-App` header shows the
connecting banner while the app's own 503 shows at once, checks that every
"Dagents API calls" table names its agent and route and holds no address,
checks that the "Add a hospital" panel says why it offers no button, and
fails on any console error. With `--expect-join pl-290`, which
`run_frontend_demo.sh --deploy --check` passes, it also adds the hospital from
the page after its other checks and prints `ok  deploy: hospital pl-290
joined`. A typecheck
and a bundle only prove that the app compiles. The smoke test exits 2 and prints
`SKIP` when no browser is available; report that as skipped, not passed.

### Service ports

| Service | Port |
|---|---|
| model-service | 8000 |
| lma | 8010 |
| gma | 8020 |
| pipeline-service | 8030 |
| core-service | 8040 |
| spring-control-service | 8050 |
| spring-core-service | 8060 |
| nl2sql-demo backend | 8070 |
| nl2sql-demo frontend | 5173 |
| healthcare-demo backend | 8080 |
| healthcare-demo GMA (`--agents network` and compose) | 8081 |
| healthcare-demo LMAs (`uk-134`, `it-002`, `ch-245`) | 8082, 8083, 8084 |
| healthcare-demo LMA of the joining hospital (`pl-290`, `--deploy` and the deploy override) | 8085 |
| healthcare-demo core-service (`--deploy` and the deploy override) | 8040 |
| healthcare-demo frontend | 5174 |
| framework site (dev) | 5175 |

Configuration comes from `env/.env.shared` plus a file per service. Never
hardcode URLs or ports. Each service has a `*_PUBLIC_URL` (host) and a
`*_INTERNAL_URL` (compose network). The env files are read only from a source
tree (a checkout, or an image that copies `dagents/` and `env/` side by side);
an installed `dagents` package reads only the process environment
(`dagents/common/env.py`).

## Conventions

### Docs change with the code

A code change and the docs it affects land in the same commit, and a change is
not done while the framework site describes the old behaviour. The site
publishes on every push to `main`. The `docs-sync` skill maps each code area to
the docs it feeds (the API reference, the guide and its example, the learning
guide, the Resources page, the inventory and the READMEs), with the command
that regenerates each generated doc. Never edit a generated doc by hand.

### Agent layout

`dagents/lma/` and `dagents/gma/` use the same layout, and new agents should too:

```text
domain/          typed models, no I/O
application/     use cases
adapters/        code for specific technologies
infrastructure/  messaging, storage, concrete implementations
config.py di.py main.py    wiring
```

Keep `domain/` pure. Put infrastructure behind interfaces, so in-memory delivery
can be replaced by a message broker and a database without restructuring.

### Calling the OCaml layer from Python

Use `dagents/common/infrastructure/dagents_runner.py`. It converts keys between
snake_case and camelCase and runs the subprocess. It looks for the binary in
this order: `DAGENTSC_BIN` (local runs point it at the dune build), the planner
a platform wheel bundles at `dagents/_bin/dagentsc`, then `dagentsc` on PATH
(containers install it there). A `DAGENTSC_BIN` that names a missing file is
used as it is, so a wrong setting fails instead of running another planner.
When nothing is found the runner raises `PlannerUnavailableError` and the Guard
denies. `python -m dagents` prints which planner it found, and an LMA or GMA
that cannot start one logs a warning when it is built (`warn_if_no_planner`).

Each call has limits, read from the process environment like `DAGENTSC_BIN`:
`DAGENTSC_TIMEOUT_SECONDS` (default 10), `DAGENTSC_MAX_INPUT_BYTES` and
`DAGENTSC_MAX_OUTPUT_BYTES` (default 16 MiB each). The runner kills a planner
that runs or prints past them, with its process group, refuses an input over
its limit before the planner starts, and raises `PlannerUnavailableError` for
each. `DAGENTSC_SHA256` pins the binary's SHA-256: with another hash the runner
starts nothing, and `warn_if_no_planner` and `python -m dagents` say so. A
value that is not valid stops an LMA or GMA from starting. Every audit record
names its planner in `planner_version` (the package version) and
`planner_sha256`. The digest leaves out either field when it is null, so
chains stored before the fields existed still verify; a field added to
`AuditRecord` later needs the same treatment
(`DIGEST_FIELDS_LEFT_OUT_WHEN_NULL` in `ethical_guard.py`). The hash differs
between builds, so no committed file or test pins one: the captured API
examples list both fields as volatile.

An image whose service calls the planner builds it in an `ocaml/opam` stage and
copies it to `/usr/local/bin/dagentsc`. Copy that stage from
`services/core-service/Dockerfile`. `tests/test_planner_in_images.py` fails if
an image lacks it.

### Adding to the OCaml layer

1. Add the type to `common_ir` first, then its JSON parsing, then the compiler
   module, then tests.
2. Model choices as algebraic data types, not string comparisons. Exhaustive
   matching is the reason this layer is in OCaml.
3. Return reports and plans instead of raising. Keep exceptions for requests
   that cannot produce a meaningful plan.
4. Test at the compiler boundary: invalid inputs, the shape of the output, and
   deterministic ordering.

### LMA/GMA route pairs

Both agents expose short paths (`/health`, `/datasets/profile`, `/models/run`)
and versioned ones (`/api/v1/health`, `/api/v1/datasets:profile`,
`/api/v1/model-jobs`). This is deliberate. When adding an endpoint, add both
forms and make them call the same handler.

The framework services (`core`, `pipeline`, `model`) use only `/api/v1/...`.

### The service inventory

`docs/reference/service-inventory.{json,md}` lists every endpoint the Python and
Java services expose. It is generated from the code (the FastAPI routing tables
and the Spring controllers), so any route change means regenerating it:

```bash
.venv/bin/python scripts/service_inventory.py --write   # then commit both files
```

`tests/test_service_inventory.py` fails when the files are out of date. It also
checks the conventions above: the framework services use only versioned paths,
the LMA/GMA pairs exist and return the same result, and the Spring services
mirror their Python counterparts.

### The API reference

The site's API reference (`#/api`) has three sources:
`docs/reference/api-reference.json`, generated from each FastAPI app's OpenAPI
document, the Spring sources and `dagentsc.ml`; the Markdown in
`docs/reference/api/`, which places every endpoint in one interface and holds
the prose; and examples captured from real calls in
`docs/reference/api/examples/`. A route, model, docstring or field description
change means:

```bash
.venv/bin/python scripts/api_reference.py --write
DAGENTSC_BIN=... .venv/bin/python scripts/capture_api_examples.py   # needs the planner
```

`tests/test_api_reference.py` and `tests/test_api_examples.py` fail when either
is stale, when an endpoint is not placed in exactly one interface, or when an
example no longer matches the app. Write field descriptions in the code
(`Field(description=...)`), not in the Markdown. `docs/reference/api/README.md`
has the format and the rules.

### The model inventory

The OCaml `model_router` decides which model family a dataset and task should
get. Because it is pure, it can choose families this runtime cannot run.
`services/model-service/app/ml/inventory.py` records which `(family, task)`
pairs are `implemented`, which provider runs each (`pytorch`, `scikit_learn`,
`huggingface` or `extension`), and which are `planned`, with what they would
need. `GET /api/v1/model-families` serves it.

Rules:

- **Every family the planner can choose needs an entry.** `planned` is a valid
  entry; a missing one is not. The vocabulary test reads the OCaml
  `string_of_model_family` mapping from source, so it needs no `dagentsc` build.
- **`inventory.py` imports nothing heavy.** Entry points are `module:attribute`
  strings resolved when used, so listing families does not import torch. A test
  checks this.
- **Providers share loading code only.** Adapters in `app/ml/providers/` all
  implement `load()` and `is_loaded`. An anomaly scorer, a classifier and an
  embedding model have different `predict` signatures, so there is no shared
  `predict`.
- **Downloads are off by default.** The Hugging Face adapter refuses a cache miss
  with a typed error naming the checkpoint. `DAGENTS_ALLOW_MODEL_DOWNLOAD=1` (or
  `allow_download=True`) allows the download. Never make a test need the Hub.
- Do not add a second list of family names. `checks.py` and `pipeline.py` read
  the inventory, so there is one source of truth.

### The design system

`design/dagents-design-system.css` is the single source.
`scripts/sync_design_system.py --write` copies it into each frontend, together
with the link preview plugin `design/social/social-meta.mjs`, and
`tests/test_publishing_guards.py` fails if a copy differs. The copies exist
because `apps/healthcare-demo/` must stay extractable and cannot import across
the tree. Edit the source, never a copy.

The style is an instrument panel. The most important rule: **colour carries
meaning only.** Colours mark the decision states (permit, narrow, deny) and
links. A decorative colour could be mistaken for a decision, so there is no
accent colour; hierarchy comes from type, weight and thin rules. Charts add
three series tokens (`--ds-series-1` to `--ds-series-3`), used only inside
charts, that mark identity (which hospital), never a decision. Code blocks
add syntax tokens (`--ds-syntax-*`, GitHub's Primer light and dark colours),
used only inside code blocks and applied on the site by `site/src/highlight.ts`.

### Published demos replay a real run or call a real API

Both demo frontends have a transport in `src/api.ts`. It calls the backend
locally, and when built with `VITE_DAGENTS_STATIC=1` it replays
`public/recording.json`. The healthcare demo has a third mode: built with
`VITE_HEALTHCARE_API_BASE`, it calls the deployed API, which keeps each
hospital's records in its own Postgres schema. The Pages workflow takes that
address from `env/.env.published` (committed, because it is a public URL, not a
secret) or from a `HEALTHCARE_API_BASE` repository variable. Rules:

- **Never fake a response.** A request the recording does not hold raises
  `NotRecordedError`, and the UI says so.
- **Never record without the planner.** The Guard fails closed, so the recording
  would hold only denials. The capture script and the checker both refuse.
- **A live build never falls back to the recording**, even when the API is
  down; it reports the outage. The workflow chooses once, at build time: it
  probes the API, publishes the replay if the API does not answer, is not
  reading Postgres, reports a store not ready, returns no rows or refuses the
  Pages origin, and logs why.
- **The page shows the data source the backend reports**
  (`/api/v1/framework/status` returns `cohort_source`: `postgres` or
  `in-memory`), never one inferred from the configured address.
- **The page never shows the API's address**, a hosting vendor's name or
  cold-start internals. During a cold start it says it is connecting to the demo
  server.
- **Data entry is disabled in a replay**, with a plain explanation. The
  recording holds no entry responses, and a replay must not invent one.
- The page names the commit it was recorded from.

### Governance: where each part lives

Governance follows the same split as the rest of the code:

- **Deciding** is pure, so it is in `bindings/ocaml/lib/governance_compiler`.
  Sensitivity, trust, granularity and strategy are algebraic data types, and
  `select_strategy` matches every combination. Adding a level fails to compile
  until every combination is handled.
- **Enforcing** needs real data and an audit log, so it is in
  `dagents/common/application/ethical_guard.py`. The Guard applies the plan,
  drops every field the request did not ask for, and writes an audit record that
  includes the digest of the previous record.
- **Policy** is configuration: a `DataClassification` contributed by an
  extension or registered at runtime. Changing a field's sensitivity changes no
  code path. A runtime registration cannot replace an extension's
  classification (`ClassificationConflict`, `409`), and an LMA started for a
  site or a GMA with condition packs runs strict: it refuses a request whose
  inline classification differs from the registered one. Never let a caller
  choose the policy a request is checked against.

The Guard **fails closed**. If the planner cannot be reached, it denies the
request and records the denial. Never add a fallback that permits a request when
the planner fails.

A site's local runner receives the guarded payload, not its raw records, even
though the data never leaves the site. The code that runs a round comes from the
coordinator, so it sees only what the coordinator may see. This costs some
accuracy, which the healthcare demo measures. A deployment that finds the cost
too high should change the field's sensitivity in its classification, not this
code.

### Site data stays in the site's store

The healthcare demo keeps each hospital's records in a store of its own: one
Postgres schema and one `NOLOGIN` role per hospital, or an in-memory store per
hospital when no database is configured. These rules hold for both:

- **Every read or write of a site's data goes through that site's store, acting
  as that site's role.** Each database operation runs in one transaction that
  starts with `SET LOCAL ROLE` and checks `current_user`
  (`dagents/common/infrastructure/postgres_roles.py`, `acting_as`). Never use a
  session-level `SET ROLE`; a pooled connection would keep it.
- **The coordinator never reads site rows.** It sees what sites report: counts,
  rates the site's planner permits (at least the minimum cohort), round
  results, and each audit chain's state and length, never its records. The
  coordinator role has no grant on any site schema.
- **Training labels never leave the site.** A site's LMA trains on the
  Guard's `before_read` payload, inside the LMA, and returns a parameter
  update.
- **The update vector never enters `SiteResult`.** The worker sends it through
  the `before_send` Guard, which clips it, and hands the clipped vector to an
  `UpdateChannel`. The LMA's channel is `HttpUpdateChannel`, which sends it to
  the GMA's write-only update store; no route reads it back. `SiteResult`
  carries only `update_digest` and `update_norm`, because results are shown to
  the coordinator.
- **Each site's LMA reads only its own store.** In the healthcare demo it reads
  the hospital's store through the app's site source, as the hospital's role,
  and keeps its audit chain and job log on that store's connection. The GMA
  holds only the coordinator's connection.
- **Entries are typed.** The data-entry body has enums, numeric ranges and
  yes/no fields only. It accepts no free text and no identifiers, because the
  public demo has no accounts.
- **Two store implementations share one test suite.** `MemorySiteStore` and
  `PostgresSiteStore` run the same assertions; the Postgres run needs
  `HEALTHCARE_DEMO_TEST_DATABASE_URL`.

On Supabase the API connects with one credential, and that role also holds
`pg_read_all_data`. So the isolation binds the code paths (every query runs as a
site role), not the credential. A real deployment gives each site's LMA its own
login and database; the store reads an optional per-site connection string,
`HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL` (for example
`HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL`).

### Federated rounds

`bindings/ocaml/lib/federation_compiler` makes every deterministic decision:
eligibility, quorum, aggregation readiness (which counts only the sites the
plan selected), release gates, every change of a model version's status
(`dagentsc federation model lifecycle`), and whether a round that a restart
left unfinished is kept or closed (`dagentsc federation round recover`).
`dagents/common/application/federation.py` holds state and side effects and calls
the planner for the rest; the GMA's model registry
(`dagents/gma/application/model_registry.py`) applies the lifecycle answer and
keeps no status rule of its own. Do not write a planning rule again in Python.

When the model registry holds the candidate, a release evaluation sends the
decision the version already holds with the `release_evaluated` event. For the
same round, the planner allows the same decision without a status change and
refuses a different one as `decision_differs`. So a retry after a failed round
write saves the decision on the round.
`FederatedRoundController.recover` asks `round recover` about each unfinished
round and closes what it says to close. A GMA runs it once after a restart
(`GmaState.ensure_recovered`): under uvicorn in its lifespan, which waits for
it at most 10 seconds and logs a failure instead of raising it, otherwise at
the first round write. The round write routes wait for it through an async
dependency, on the GMA's own handler threads, and answer `503`
`recovery_failed` while it fails. Once the GMA serves, reads, `/health` and
approval do not wait. Before each dispatch, the controller asks the same rule
about rounds whose oldest stored update is older than
`GMA_UPDATE_MAX_AGE_SECONDS` (`expire_updates`). That age limit applies when
the update store survives a restart (24 hours by default) or when the setting
is set, and `GMA_UPDATE_TABLE` needs `GMA_ENGINE=http`, because the simulator
neither reads nor deletes stored updates. Python only picks which rounds to
ask about; every keep or close comes from the planner.
`dagents/tests/test_gma_restart.py` restarts a GMA built from its settings at
five points of a study and in crash windows, and
`dagents/tests/test_gma_process_restart.py` kills a GMA process with SIGKILL
and starts it again on its port; both compare the study with an uninterrupted
run. Recovery assumes one GMA process with one uvicorn worker.

The federated protocol belongs to a dedicated runtime behind `FederationEngine`.
The in-process engine is a simulator for tests and the getting-started example;
do not turn it into a federated optimizer. The GMA's HTTP engine
(`dagents/gma/adapters/http_federation_engine.py`) moves jobs to the LMAs and
holds clipped updates until aggregation; how updates are combined comes from
the model kind an extension contributes (`ModelKind.combine`), so no averaging
rule lives in `dagents/`. The healthcare demo runs its pilot on the HTTP engine:
its study operator (`apps/healthcare-demo/backend/app/services/pilot.py`)
calls only the GMA, and its logistic regression is the model kind
(`app/domain/model_kinds.py`).

Two rules always hold, and both have tests: aggregation produces a candidate,
never a release; and a release gate whose metric is missing blocks the release.
The controller also refuses a reused round id, a result from a site the plan
did not select, a second aggregate or release decision for a round, and, with
the HTTP engine, any posted result. Only a GMA whose engine collected the
results itself records `metrics_source: rounds`, the one value an approval
accepts.

### Extending the framework from an application

An application contributes through `dagents/common/extensions`, never by patching
the framework. An extension supplies feature contracts, data classifications,
condition packs (with their label field and approved purposes), model kinds
(how the LMA trains and evaluates a model and how the GMA checks and combines
its updates), site sources (how an LMA started from settings reads a site's
records), named pipeline steps and named model adapters. Registration is
explicit, and two extensions registering the same id is an error, because
otherwise a guard decision would depend on import order. The LMA and GMA load
extensions named in `LMA_EXTENSIONS` and `GMA_EXTENSIONS`.

`apps/healthcare-demo/backend/app/extension.py` is the reference: one
declarative file that imports no framework internals. If a new application needs
something that does not fit this interface, improve the interface instead of
working around it.

### Scope

Product-specific code belongs in Dagents only if several consumers can reuse it.
The two demos show the boundary:

- NL2SQL: the app owns its UI and SQL generation; Dagents owns validation,
  planning, service checks and workload compilation. See
  `services/nl2sql-demo/backend/app/services/dagents_orchestrator.py` for how an
  app calls the services.
- Healthcare: the app owns its clinical feature contract, the dataset extract,
  the per-hospital stores, the scoring rule, the logistic regression model
  (contributed as a model kind, including how an update is checked and
  combined), the study sequence, the FHIR mapping and the intended-use
  statement. It calls the LMA and GMA APIs through
  `dagents/common/infrastructure/agent_client.py`. Dagents owns governance,
  federation (the LMA and GMA APIs, job dispatch, the update store, the model
  registry and approval), role scoping, the running profile, the chart renderer
  and everything else generic. See `apps/healthcare-demo/backend/app/extension.py`
  for the extension and `apps/healthcare-demo/backend/app/services/agents.py`
  for how the app builds or connects to the agents.

Nothing in `dagents/` knows what a stroke is, and nothing in the healthcare app
implements a quorum rule. Apply that test to a new capability: if it would need a
domain word in `dagents/`, it belongs in the application.

## Writing style

All text in this repository uses easy, professional English: the site, the demo
UIs, READMEs and docs, the learning guide, code comments, commit messages and
pull request text.

- Short sentences and plain words. Define a term the first time you use it.
- Facts and numbers instead of adjectives. Check each claim against the code or
  a measured result.
- Descriptive headings, and lists or tables for steps and comparisons.
- State a limitation once, plainly, where it applies.
- No slogans ("A framework, not a product backend"), aphorisms, dramatic
  metaphors, or words such as "honest", "crucially" and "simply".

The `writing` skill has the full rules, with examples.

## Current state

The agents deliver messages in memory, and broker-backed messaging is deferred.
An LMA can serve one site (its records, profile, Guard probe, federated jobs and
job log), and a GMA can dispatch rounds to the LMAs over HTTP, keep model
versions and handle approvals. They call each other through
`dagents/common/infrastructure/agent_client.py`, in one process through
`httpx.ASGITransport` or over HTTP, with bearer tokens of fixed scopes
(`agent_auth.py`). With a database, an LMA keeps its audit chain and job log in
Postgres, and the GMA keeps its model registry and audit chain there and,
with `GMA_ROUND_TABLE`, `GMA_ENROLMENT_TABLE` and the opt-in
`GMA_UPDATE_TABLE`, its rounds, enrolments and pending updates
(`dagents/gma/infrastructure/postgres_rounds.py`), all on one kept connection.
Such a GMA recovers once after a restart, as the planner decides, and the
study goes on. A round cut during its dispatch is closed and runs again under
a new id. Without those settings, the round state stays in memory. The
rendered Kubernetes manifest still uses a rolling update for the GMA, and the
healthcare demo sets none of the round table settings. The getting-started example's `step5_agents_over_http.py` runs a
round through `agent_client`, and so does the healthcare demo: it calls one
LMA per hospital and a GMA only
through their APIs, in its own process on the published demo and as separate
processes in network mode. Every successful answer of the demo whose body is
a JSON object lists the agent calls behind it (`framework_calls`); errors, the
hospital list and the SVG charts do not.

Open work is in `TODO.md`. The main item is live Kubernetes validation on
Minikube, which is blocked on local Docker Desktop disk space (see `CHANGES.md`).
`generate_manifests_local.py` produces `dagents-workloads.yaml` without starting
the whole stack.

## Repository skills

`.claude/skills/` holds five repository skills. They are binding where they say
**never**:

- `backend`: the layer rule, where each kind of decision may live, the
  governance and federation rules, and the test loop for each layer.
- `frontend`: what a UI may own, the design system, the framework site and its
  guide, and why a typecheck and a bundle are not enough (and exit 2 is not a pass).
- `ci`: the pipeline's shape, the false greens it defends against, and the rules
  for changing a workflow.
- `writing`: plain-language rules for every text in the repository.
- `docs-sync`: which docs each code area feeds, so a code change updates them
  in the same commit; the commands that regenerate the generated docs; the
  tests that catch drift; what only reading can check; and the docs-impact
  check in CI.

They are first-party repository skills; no third-party marketplace plugin is
installed.

## Reference docs

- `docs/learn/` — the learning guide: federated learning, governance, the
  architecture, the APIs, and running Dagents yourself
- `docs/guide/` — the guide to building an application on Dagents, step by step,
  with the worked example in `examples/getting-started/`
- `AGENTS.md` — the long contributor guide, with the most detail on boundaries
- `docs/reference/service-inventory.md` — every endpoint the Java and Python services expose
- `.claude/skills/{backend,frontend,ci,writing,docs-sync}/SKILL.md` — the rules for each area
- `docs/agents/lma-gma-architecture.md` — agent responsibilities and run flow
- `docs/architecture/ocaml-adoption-plan.md` — why OCaml, where it is used, what stays out
- `bindings/ocaml/README.md` — module map, CLI commands, contract examples
- `docs/demo/app-architecture-walkthrough.md` and `functional-modules-walkthrough.md`
- `docs/presentation/dagents-project-presentation.pptx` — the project deck
- `docs/presentation/healthcare-case-study/` — the stroke case study and the federated use
  case, including the GRAILS sections the governance layer implements
- `apps/healthcare-demo/README.md` — the demo app, its boundary table, and its limits
