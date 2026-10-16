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
| **Python** (`agents/`, `services/`) | FastAPI services, model training and inference, source I/O, runtime state | Deterministic planning rules |
| **Spring Boot** (`services/spring-services/`) | Orchestration APIs, policy entry points, external integration | Machine learning |

Decisions go in the OCaml planners. Anything with side effects goes in Python or
Java.

Services call OCaml by running `dagentsc` with JSON on standard input, never
through FFI. A separate process isolates failures, lets each side be upgraded on
its own, and keeps the Kubernetes deployment simple.

## Repository map

```text
agents/
  common/        shared domain contracts + dagents_runner (the dagentsc bridge) +
                 compose_apply (starts one service of a rendered Compose file)
  lma/           Local Monitoring Agent service
  gma/           Global Monitoring Agent service
  tests/         agent + control-plane + runner tests
bindings/ocaml/  the planners (dune workspace)
  lib/common_ir           shared typed IR + JSON codecs
  lib/dataset_compiler    source validation, profiling, schema contracts, quality, transforms
  lib/pipeline_compiler   DAG validation + topological ordering
  lib/model_router        dataset profile + task -> model family + packaging mode
  lib/manifest_compiler   typed workload spec -> Kubernetes YAML or a Docker Compose file
  lib/governance_compiler GRAILS ethical restrictions: what protection a request needs
  lib/federation_compiler federated round manifests, eligibility, quorum, release gates
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
                      README, tests and compose file; a split run can add a fourth hospital
                      while it runs; scripts/extract_repo.sh turns it into its own repository
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

Tests import as `agents.common...`, so run them from the repository root.
FastAPI and the other dependencies are in the gitignored `.venv/`, not system
Python:

```bash
.venv/bin/python -m unittest discover -s agents/tests -t .
```

If `.venv/` is missing (a fresh clone), create it and install the requirements
you need. Each service has its own `requirements.txt`; there is no root-level
one.

Each service has its own suite under `services/<name>/tests/`. The NL2SQL suite
adds its backend to the path itself, so it also runs from the root.

`tests/` at the root holds checks that span more than one service, such as the
endpoint inventory and the API conventions. They need every Python service
importable, while `agents/tests` needs only the agent requirements:

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
export DAGENTS_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres          # agents/tests/test_postgres_roles.py
```

Without them these tests skip with a message that starts `database test
skipped`. The in-memory store runs the same assertions without a database, so
both store implementations are tested.

### CI

`.github/workflows/ci.yml` runs the same commands, one job per layer: `planners`
(builds OCaml, runs `dune test`, and uploads `dagentsc` for the other jobs),
`agents`, `framework-services` (core-service twice, with and without
`DAGENTSC_BIN`), `contracts`, `spring`, `demo-apps`, `frontends`,
`docs-impact`, and a `ci` gate job that is the single required check.

`docs-impact` runs `scripts/check_docs_impact.py` on pull requests and pushes
to `main`. It fails when a change touches a code area (`agents/`, `services/`,
`bindings/ocaml/`, `apps/`, `contracts/`, `scripts/`, `examples/`, `env/`, the
compose file, a workflow, or a deck) and none of the docs that area feeds,
unless a commit message has a line `Docs-Impact: none [area] (reason)` that
names the area. Run it before pushing:

```bash
.venv/bin/python scripts/check_docs_impact.py --base origin/main
```

Two guard scripts catch runs that look green but are not:

```bash
python -m unittest discover -s agents/tests -t . -v 2>&1 | tee suite.log
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

The jobs also require classes that need no database, so that a class that
stops running fails the job:

| Job | Classes, besides the governance, federation and chart classes |
|---|---|
| `agents` | `test_site_addresses.SiteAttachTests` and `SiteAddressRouteTests`; `test_compose_apply.LocalProcessApplierTests` and `DockerComposeApplierTests`, which need PyYAML (the job installs the app's range) |
| `demo-apps`, and the same list in `healthcare-data.yml` | `test_split_processes.SplitProcessTests`; `test_deployment_flow.DeploymentFlowTests`; `test_deployment_spec.ComposeRequestTests`; `test_split_deploy.SplitDeployTests`; `test_compose_override.ComposeOverrideTests` |

The four deploy classes in `demo-apps` test adding a hospital while the demo
runs. `ComposeOverrideTests` skips without the docker CLI and runs only
`docker compose config`; CI starts no container. The other three skip without
the planner.

The `frontends` job runs the healthcare smoke test twice: against one process,
and with `run_frontend_demo.sh --deploy --check`, which runs the split layout,
adds Hospital 290 after every other check, and must print
`ok  deploy: hospital pl-290 joined`.

Docker and Minikube are not in CI; see **Current state**. The suites set
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
schema and has the planner in its image. Its address is written only in
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
refuses a recording whose guard controls change nothing, or that lacks a
request the UI sends at the exact path and query string it sends it. Recordings
are gitignored; the workflow makes them.

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

### Full stack

```bash
docker compose --env-file env/.env.compose up --build
```

### Demos

```bash
bash docs/demo/run_functional_layer_demo.sh   # OCaml planner output
bash docs/demo/run_full_stack_demo.sh         # whole stack via compose
bash docs/demo/run_demo_quick.sh              # fallback: build + tests + a few planner calls
services/nl2sql-demo/scripts/run_local_demo.sh  # NL2SQL app, no Docker
```

`docs/demo/inputs/` holds request payloads and `docs/demo/expected/` the recorded
responses. Use them as fixtures when changing planner or service output.

The healthcare demo has its own scripts, which need no Docker and no database:

```bash
apps/healthcare-demo/scripts/run_frontend_demo.sh          # API + UI + a guided tour
apps/healthcare-demo/scripts/run_frontend_demo.sh --check  # start, smoke-test the UI, exit
apps/healthcare-demo/scripts/run_pilot.sh                  # no UI: one pilot, printed
apps/healthcare-demo/scripts/run_local_demo.sh             # the API alone on :8080
apps/healthcare-demo/scripts/run_split_demo.sh             # the API as five processes (below)
apps/healthcare-demo/scripts/run_frontend_demo.sh --split  # the UI in front of those five
apps/healthcare-demo/scripts/run_split_demo.sh --deploy    # the five, core-service, and the deploy action on
apps/healthcare-demo/scripts/run_frontend_demo.sh --deploy # the UI in front of those, with "Add Hospital 290"
apps/healthcare-demo/scripts/run_frontend_demo.sh --deploy --check  # the smoke test, which also adds it
```

The split run starts the Dagents GMA (`agents.gma.main`, :8020, with
`GMA_FEDERATION_SITES`), one LMA per hospital (`HEALTHCARE_DEMO_ROLE=lma`,
:8082 to :8084) and the API (`HEALTHCARE_DEMO_ROLE=app`, :8080), which forwards
each hospital's routes to that hospital's LMA
(`apps/healthcare-demo/backend/app/api/forward.py`).
`apps/healthcare-demo/docker-compose.yml` runs the same layout in containers.
The hospitals call the API's model registry and update store on a second port
(`HEALTHCARE_DEMO_INTERNAL_PORT`, 8081); the public port answers 404 for those
routes. The calls between the processes have no authentication. So the split
run binds the five processes to 127.0.0.1, and the compose file publishes only
the API's public port and the UI to other machines. The UI's dev server
(`run_frontend_demo.sh`, and the compose UI) listens on every interface and
passes `/api` to the public port.
`apps/healthcare-demo/tests/test_split_processes.py` starts the five processes
and runs a pilot through them; CI requires it to run.

**Adding a hospital while the demo runs.** A split run started with
`--deploy` can add a fourth hospital, Hospital 290 in Poland (`pl-290`, 263
real IST records from `data/ist/ist_joining_hospitals.csv`), from the
consortium page or with `POST /api/v1/deployments`. The app asks core-service
(`POST /api/v1/manifests/compose`, :8040) for the new LMA's Compose file and
never writes Compose itself. A framework applier
(`agents/common/infrastructure/compose_apply.py`) starts that one service as a
local process on 127.0.0.1:8085. The GMA accepts the LMA's address
(`GMA_FEDERATION_JOINABLE_SITES`), enrols the site, and its planner decides
whether the next round selects it. A failure in a later step stops the service
this attempt started and withdraws the enrolment. Rules that keep it off the
public demo:

- `HEALTHCARE_DEMO_DEPLOYER` is `off` by default; `process` or `compose`
  turns it on. The app refuses to start with it on in any role but `app`, or
  when `RENDER` is set, so the hosted demo never starts a process or a
  container. The Pages replay shows the panel with a note and no button.
- The `POST` is accepted only without an `Origin` header (curl, tests) or from
  a page on 127.0.0.1, localhost or ::1. Every port that can reach it listens
  on 127.0.0.1 only; with `--deploy` the UI's dev server does too.
- A pilot and a deployment hold one flight, so a request for one while the
  other runs gets 409.

`apps/healthcare-demo/docker-compose.deploy.yml` turns the action on for the
container layout (`HEALTHCARE_DEMO_DEPLOYER=compose`): the new hospital starts
as a container in its own Compose project. **It mounts the Docker socket into
the backend, which gives the backend control of Docker on that machine. Never
use it on a shared or public host.** Run it from `apps/healthcare-demo` with
`DOCKER_GID=$(stat -c %g /var/run/docker.sock) docker compose -f
docker-compose.yml -f docker-compose.deploy.yml up --build`. CI checks it with
`docker compose config` only; `apps/healthcare-demo/docs/deployment.md` has
the check to run by hand and the settings.

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
connecting banner while the app's own 503 shows at once, and fails on any
console error. With `--expect-join pl-290`, which `run_frontend_demo.sh
--deploy --check` passes, it then presses "Add Hospital 290", checks that
every step is done, that the hospital's page draws its charts and that a pilot
includes it. Without that option it checks that the Add a hospital panel shows
a reason and no button. A typecheck
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
| healthcare-demo internal listener (split run, 127.0.0.1) | 8081 |
| healthcare-demo hospital LMAs (split run) | 8082, 8083, 8084 |
| healthcare-demo joining hospital LMA (split run with `--deploy`) | 8085 |
| healthcare-demo frontend | 5174 |
| framework site (dev) | 5175 |

Configuration comes from `env/.env.shared` plus a file per service. Never
hardcode URLs or ports. Each service has a `*_PUBLIC_URL` (host) and a
`*_INTERNAL_URL` (compose network).

## Conventions

### Docs change with the code

A code change and the docs it affects land in the same commit, and a change is
not done while the framework site describes the old behaviour. The site
publishes on every push to `main`. The `docs-sync` skill maps each code area to
the docs it feeds (the API reference, the guide and its example, the learning
guide, the Resources page, the inventory and the READMEs), with the command
that regenerates each generated doc. Never edit a generated doc by hand.

### Agent layout

`agents/lma/` and `agents/gma/` use the same layout, and new agents should too:

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

Use `agents/common/infrastructure/dagents_runner.py`. It converts keys between
snake_case and camelCase and runs the subprocess. The binary comes from
`DAGENTSC_BIN`, or from `dagentsc` on PATH (containers install it there; local
runs point at the dune build).

An image whose service calls the planner builds it in an `ocaml/opam` stage and
copies it to `/usr/local/bin/dagentsc`. Copy that stage from
`services/core-service/Dockerfile`. `tests/test_planner_in_images.py` fails if
an image lacks it.

### Starting a service the planner rendered

Rendering a Compose file is a planner decision; starting the service is a side
effect. So an application asks core-service (`POST /api/v1/manifests/compose`)
for the file, never writes Compose itself, and hands the exact `compose_yaml`
text to an applier in `agents/common/infrastructure/compose_apply.py`:

- `LocalProcessApplier` (`process`) runs the service's `command` as a local
  process in its own process group, with its `environment`. It starts only a
  program on its allowlist, and refuses a key it cannot apply (such as a
  non-empty `depends_on`) and variable interpolation. It needs no Docker, so
  tests and CI use it.
- `DockerComposeApplier` (`compose`) refuses a service key the manifest
  compiler never renders (such as `privileged`, `pid` or `volumes`) and a
  network other than `{}` or `external: true`. It then checks the file with
  `docker compose config` and runs `docker compose up --detach --no-build
  --pull never --no-deps --wait` for the one service, in its own project.
  Every command goes through one runner, so tests replace it with a fake.

Both accept a file with exactly one service. PyYAML is imported only when a
file is parsed. The caller checks the service's health itself, and stops what
it started when a later step fails.

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
charts, that mark identity (which hospital), never a decision. A series
without one of those slots, such as a fourth hospital that joins while the
demo runs, is drawn in neutral ink (`--ds-ink-2`, `series_slot=None` in
`agents/common/infrastructure/charts.py`); never reuse another hospital's
colour for it. Code blocks
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
  `agents/common/application/ethical_guard.py`. The Guard applies the plan,
  drops every field the request did not ask for, and writes an audit record that
  includes the digest of the previous record.
- **Policy** is configuration: a `DataClassification` contributed by an
  extension or registered at runtime. Changing a field's sensitivity changes no
  code path.

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
  (`agents/common/infrastructure/postgres_roles.py`, `acting_as`). Never use a
  session-level `SET ROLE`; a pooled connection would keep it.
- **The coordinator never reads site rows.** It sees what sites report: counts,
  rates the site's planner permits (at least the minimum cohort), round
  results, and each audit chain's state and length, never its records. The
  coordinator role has no grant on any site schema.
- **Training labels never leave the site.** A site trains on the Guard's
  `before_read` payload, in its own process, and returns a parameter update.
- **The update vector never enters `SiteResult`.** The worker sends it through
  the `before_send` Guard, which clips it, and hands the clipped vector to an
  `UpdateChannel`. `SiteResult` carries only `update_digest` and `update_norm`,
  because results are shown to the coordinator.
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
eligibility, quorum, aggregation readiness and release gates.
`agents/common/application/federation.py` holds state and side effects and calls
the planner for the rest. Do not write a planning rule again in Python.

The federated protocol belongs to a dedicated runtime behind `FederationEngine`.
The in-process engine is a simulator for tests and demos; do not turn it into a
federated optimizer. `HttpFederationEngine`
(`agents/common/infrastructure/federation_http.py`) is the same simulator with
each site reached over HTTP. The GMA uses it when `GMA_FEDERATION_SITES` lists
the sites as `site_id=url` entries.

`GMA_FEDERATION_JOINABLE_SITES`, in the same format, lists the sites the GMA
may reach from run time on. `PUT /api/v1/federation/sites/{site_id}/address`
attaches a listed site only at the exact listed address. Any other site or
address gets 403, and a site that `GMA_FEDERATION_SITES` lists with another
address gets 409. An address alone never enrols a site: enrolment stays
`PUT /api/v1/federation/sites/{site_id}`, and the planner still decides
eligibility. A joined address lives in memory, so a restarted GMA forgets it
and the caller sends it again. There is no route that removes an address.
Each GMA reads its own list.

Two rules always hold, and both have tests: aggregation produces a candidate,
never a release; and a release gate whose metric is missing blocks the release.

### Extending the framework from an application

An application contributes through `agents/common/extensions`, never by patching
the framework. An extension supplies feature contracts, data classifications,
condition packs, named pipeline steps and named model adapters. Registration is
explicit, and two extensions registering the same id is an error, because
otherwise a guard decision would depend on import order.

`apps/healthcare-demo/backend/app/extension.py` is the reference: about a
hundred declarative lines that import no framework internals. If a new application needs
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
  the per-hospital stores, the scoring rule, the logistic regression model and
  its parameter-averaging runtime, the FHIR mapping and the intended-use
  statement. Dagents owns governance, federation, role scoping
  (`postgres_roles.py`), the running profile, the chart renderer and everything
  else generic. See `apps/healthcare-demo/backend/app/extension.py` for the
  extension. When a hospital joins, the app owns which hospital, the workload
  spec it sends and the order of the steps; Dagents renders the Compose file
  (core-service), starts the service (`compose_apply.py`), accepts the address
  and enrols the site (the GMA), and decides eligibility (the planner).

Nothing in `agents/` knows what a stroke is, and nothing in the healthcare app
implements a quorum rule. Apply that test to a new capability: if it would need a
domain word in `agents/`, it belongs in the application.

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

The agents deliver messages in memory. Health and control endpoints come first;
messaging and storage sit behind interfaces, and broker-backed infrastructure is
deferred.

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
