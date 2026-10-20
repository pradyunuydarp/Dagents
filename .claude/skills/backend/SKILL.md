---
name: backend
description: Strict rules for changing Dagents backend code — the OCaml planners in bindings/ocaml, the Python agents in dagents/, and the FastAPI and Spring Boot services in services/. Use before adding or changing an endpoint, a planner rule, a governance or federation decision, a model family, a source adapter, or anything under dagents/, services/ or bindings/ocaml. States where each kind of decision is allowed to live and the test-first loop that must pass before the change is finished.
---

# Dagents backend

These rules are binding. A change that breaks a **never** rule is wrong even if
it works, and even if the request seemed to ask for it. Say so, and propose the
placement these rules allow. Comments, docstrings and commit messages follow the
`writing` skill. A backend change updates the docs it affects in the same
commit; the `docs-sync` skill says which docs and how to regenerate them.

## 1. Where code lives

Each language does the work it suits best:

| Layer | Owns | Never owns |
|---|---|---|
| **OCaml** `bindings/ocaml/` | Pure planning: validate, compile, route, render | Database connections, training loops, API servers |
| **Python** `dagents/`, `services/` | FastAPI services, model training and inference, source I/O, runtime state | Deterministic planning rules |
| **Spring Boot** `services/spring-services/` | Orchestration APIs, policy entry points, external integration | Machine learning |

The two agents:

- **LMA** (`dagents/lma/`): one per data source (a tenant, database, service,
  event stream or environment). It profiles data, splits work, runs local
  models, publishes summaries, and enforces governance at three of the four
  guard boundaries.
- **GMA** (`dagents/gma/`): registers LMAs, combines their outputs, runs
  aggregate models, coordinates dispatch, and runs federated rounds and release
  decisions.

Python calls OCaml through the `dagentsc` command-line program, sending JSON on
standard input. **Never** link the languages directly (FFI). Always go through
`dagents/common/infrastructure/dagents_runner.py`, which converts between
snake_case and camelCase and runs the subprocess. It looks for the binary in
`DAGENTSC_BIN`, then in the planner a platform wheel bundles at
`dagents/_bin/dagentsc`, then `dagentsc` on PATH. When none is found it raises
`PlannerUnavailableError`, and the Guard denies. **Never** add a lookup that
falls through from a configured `DAGENTSC_BIN` that does not exist to another
planner. An agent built without a planner it can start logs one warning
(`warn_if_no_planner`), because it still answers `/health`.

**An installed package reads no env file.** `dagents/common/env.py` reads
`env/.env.*` only next to a source tree's `dagents/` folder, never from
`site-packages`, where another distribution could install an `env/` folder.

**Every image whose service calls the planner must contain it.** Build it in an
`ocaml/opam` stage and copy it to `/usr/local/bin/dagentsc`, as
`services/core-service/Dockerfile` does. This includes any service that uses the
Ethical Guard or the federated round controller. The suites set `DAGENTSC_BIN`,
so they pass without it; `tests/test_planner_in_images.py` reads the
Dockerfiles and fails instead.

### Where a decision is allowed to live

Ask one question of every new rule: **does it always give the same answer for
the same inputs?**

- Yes: it is a planner rule. It goes in an OCaml compiler module, as an
  algebraic data type with exhaustive matching, and Python calls it.
- No, because it needs real data, a clock, a socket, a random seed or an audit
  log: it is runtime code. It goes in Python, and it calls the planner for the
  deterministic part.

**Never write a planner rule again in Python** to avoid calling `dagentsc`. Two
copies of one rule drift apart; this repo has had that bug (see §5).

### The compiler modules

| Module | Decides |
|---|---|
| `lib/common_ir` | The shared types and their JSON format. Every new type starts here. |
| `lib/dataset_compiler` | Source validation, profiling, schema contracts, quality rules, transforms |
| `lib/pipeline_compiler` | Whether a pipeline is a valid DAG, and the order of its steps |
| `lib/model_router` | Dataset profile + task → model family + packaging mode |
| `lib/manifest_compiler` | Typed workload spec → Kubernetes YAML or a Docker Compose file |
| `lib/governance_compiler` | GRAILS: the protection each field of a request needs |
| `lib/federation_compiler` | Round manifests, eligibility, quorum, aggregation readiness, release gates, every change of a model version's status, whether a round that a restart left unfinished is kept or closed |

## 2. Hard rules

### Governance

- **The Ethical Guard fails closed.** If the planner cannot be reached, the
  Guard denies the request and records the denial. **Never** add a fallback that
  permits a request when the planner fails.
- Deciding happens in `bindings/ocaml/lib/governance_compiler`. Sensitivity,
  trust, granularity and strategy are algebraic data types, and
  `select_strategy` matches every combination of the three inputs. Adding a
  level must fail to compile until every combination has a strategy.
- Enforcing happens in `dagents/common/application/ethical_guard.py`: apply the
  plan, drop every field the request did not ask for, and write an audit record
  that includes the digest of the previous record.
- Policy is configuration: a `DataClassification`, contributed by an extension
  or registered at runtime. Changing a field's sensitivity must not change any
  code path.
- A site's local runner receives the **guarded** payload, not its raw records,
  even though that data never leaves the site. This costs accuracy (the
  healthcare demo measures it). A deployment that finds the cost too high
  changes the field's classification, not this code path.

### Federation

- `bindings/ocaml/lib/federation_compiler` decides eligibility, quorum,
  aggregation readiness, release gates, every change of a model version's
  status (`federation model lifecycle`, including the stored-decision rule for
  a retried release evaluation) and whether a round that a restart left
  unfinished is kept or closed (`federation round recover`).
  `dagents/common/application/federation.py` holds state and side effects and
  calls the planner for the rest.
- **Aggregation produces a candidate, never a release.** A release needs its
  gates to pass and a person to approve it.
- **A release gate whose metric is missing blocks the release.** It never passes
  by default.
- The federated protocol belongs to a dedicated runtime behind
  `FederationEngine`. The in-process engine is a simulator for tests and demos.
  **Never** turn it into a federated optimizer.

### Site data

The healthcare demo shows how a site's data is kept. Each hospital has a store of
its own: a Postgres schema owned by a `NOLOGIN` role with the same name, or an
in-memory store when no database is configured. These rules apply to any
consumer that stores site data:

- **Every read or write of a site's data goes through that site's store, acting
  as that site's role.** Use `acting_as` from
  `dagents/common/infrastructure/postgres_roles.py`: one transaction that starts
  with `SET LOCAL ROLE` and checks `current_user`. **Never** use a session-level
  `SET ROLE`, and never bind a role name as a query parameter; it is validated
  and quoted as an identifier.
- **The coordinator never reads site rows.** It sees what sites report (counts,
  rates only when the site's planner permits a table request at that cohort,
  and each audit chain's state and length) and round results. Its role has no
  grant on any site schema, and a test checks that its reads fail.
- **Training labels never leave the site.** A site's LMA trains on the
  Guard's `before_read` payload, inside the LMA, and returns a parameter
  update.
- **The update vector never enters `SiteResult`.** `LocalFederatedWorker` sends
  it through the `before_send` Guard, which clips it, and hands the clipped
  vector to an `UpdateChannel`. The LMA's channel, `HttpUpdateChannel`, sends it
  to the GMA's write-only update store; no route reads it back. `SiteResult`
  carries only `update_digest` and `update_norm`, because results are shown to
  the coordinator. An update with no channel is refused.
- **Each site's LMA reads only its own store, and the GMA reads none.** The
  healthcare demo hands each LMA a site source over that hospital's store, and
  the LMA keeps its audit chain and job log on the store's connection, as the
  hospital's role. The GMA holds only the coordinator's connection, and its
  source routes are off (`GMA_ALLOW_SOURCE_READS=0`).
  `apps/healthcare-demo/tests/test_agents_wiring.py` checks both.
- **No app lock or store connection is held across an agent call.** In
  process, an agent can use the same store the app writes. Finish the write,
  release the lock, then call the agent. `apps/healthcare-demo/tests/test_concurrency.py`
  fails if a lock is held during a call.
- **Entries are typed.** A data-entry endpoint accepts enums, numeric ranges and
  yes/no fields, built from the field domains the app declares. It accepts no
  free text and no identifiers, and it refuses unknown keys.
- **Two store implementations share one test suite.** The in-memory and Postgres
  stores run the same assertions (see §3).
- On Supabase the API connects with one credential that holds
  `pg_read_all_data`, so the isolation binds the code paths, not the credential.
  A real deployment gives each site's LMA its own login and database; the store
  reads `HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL` for that.

### Agent layout

`dagents/lma/` and `dagents/gma/` use the same layout, and a new agent must too:

```text
domain/          typed models, no I/O
application/     use cases
adapters/        code for specific technologies
infrastructure/  messaging, storage, concrete implementations
config.py di.py main.py    wiring
```

`domain/` stays pure: no imports that open a socket, read a file or read the
clock. New infrastructure goes behind an interface, so in-memory delivery can be
replaced by a message broker and a database without restructuring.

### Endpoints

- The LMA and GMA have short paths (`/health`, `/datasets/profile`,
  `/models/run`) **and** versioned paths (`/api/v1/health`,
  `/api/v1/datasets:profile`, `/api/v1/model-jobs`). This is deliberate. When
  you add an endpoint to either agent, add both forms and make the versioned one
  call the same handler.
- The framework services (`core`, `pipeline`, `model`) use only `/api/v1/...`.
- **Never hardcode a URL or a port.** Configuration comes from `env/.env.shared`
  plus a file per service, with separate `*_PUBLIC_URL` (host) and
  `*_INTERNAL_URL` (compose network) values.
- Every endpoint change must be reflected in the service inventory:

  ```bash
  .venv/bin/python scripts/service_inventory.py --write
  ```

  Commit the regenerated `docs/reference/service-inventory.json` and `.md`. The
  inventory test fails if they are out of date.

### Scope

Product-specific code belongs in Dagents only if several consumers can reuse it.
The test: **if a capability needs a domain word in `dagents/`, it belongs in the
consumer**, through `dagents/common/extensions`. Nothing in `dagents/` knows what
a stroke is, and nothing in `apps/healthcare-demo/` implements a quorum rule.

A consumer contributes feature contracts, data classifications, condition packs,
model kinds, site sources, named pipeline steps and named model adapters through
`dagents/common/extensions`. It never patches the framework. Registration is
explicit, and two extensions registering the same id is an error, because
otherwise a guard decision would depend on import order.
`apps/healthcare-demo/backend/app/extension.py` is the reference: one
declarative file that imports no framework internals. If a new consumer's needs
do not fit through this interface, improve the interface instead of working
around it.

The healthcare demo shows the split. The app owns its clinical feature
contract, the dataset extract, the per-hospital stores, the scoring rule, the
logistic regression model (contributed as a model kind, including how an update
is checked and combined), the study sequence, the FHIR mapping and the
intended-use statement. It calls the LMA and GMA APIs through
`dagents/common/infrastructure/agent_client.py`; `app/services/agents.py` is the
only module that builds or connects to the agents. Dagents owns governance,
federation (the LMA and GMA APIs, job dispatch, the update store, the model
registry and approval), role scoping, the running profile, the chart renderer
and everything else generic. **Never** import the Guard, the round controller,
the worker, the running profile or `dagents_runner` into the app: it reaches
them only through the agents' APIs. `apps/healthcare-demo/tests/test_boundary.py`
lists what each app file may import.

Adding a hospital while the demo runs keeps the same split. core-service's
OCaml manifest compiler renders the new LMA's Compose file; an applier from
`dagents/common/infrastructure/compose_apply.py` starts it and makes no
decision; the GMA accepts only the address `GMA_JOINABLE_SITE_ENDPOINTS` lists
for the site; the GMA's planner decides whether the next round selects it.
The app (`app/services/deployment.py`) only orders the steps and undoes them
on a failure. **Never** let it start a process when `RENDER` is set, when the
agents run in process, or from a page on another host: `check_deployer`
refuses those settings at start, and the POST refuses a foreign `Origin`.

## 3. The test loop

Write the failing test first, watch it fail for the reason you expect, then make
it pass.

### Adding to the OCaml layer, in this order

1. The type, in `common_ir`.
2. Its JSON parsing.
3. The compiler module.
4. The tests, in `bindings/ocaml/test/test_compilers.ml`.

Model choices as algebraic data types, never as string comparisons; exhaustive
matching is the reason this layer is in OCaml. Return reports and plans instead
of raising exceptions, except for requests that cannot produce a meaningful
plan. Test at the compiler boundary: invalid inputs, the shape of the output,
and deterministic ordering.

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test
```

`dune` is not on PATH, so always use `opam exec --`. The binary is written to
`bindings/ocaml/_build/default/bin/dagentsc.exe`. Most OCaml tests need no
Docker, database or GPU, so this is the fastest test loop in the repo.

### Python

Tests import as `dagents.common...`, so run them from the repository root, with
the gitignored `.venv/` rather than system Python:

```bash
.venv/bin/python -m unittest discover -s dagents/tests -t .
```

Each service has its own suite under `services/<name>/tests/`. The healthcare
demo has its own:

```bash
cd apps/healthcare-demo && PYTHONPATH=../..:backend ../../.venv/bin/python -m unittest discover -s tests -t .
```

### Rules for meaningful tests

- **Governance and federation tests run against the real `dagentsc` binary**,
  never a stub. A stub would only prove that the code calls something.
- **Check the skip count.** These tests find the dune build themselves and skip,
  with a message, when it is missing. A run that skipped them has not tested
  governance. Build the binary and run again.
- **Run the service suites with and without `DAGENTSC_BIN`.** `core-service`
  renders manifests with the OCaml compiler when the binary is available and
  with a Python fallback when it is not. Containers have `dagentsc` on PATH, so
  the OCaml path is the one deployed:

  ```bash
  DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
    PYTHONPATH=.:services/core-service .venv/bin/python \
    -m unittest discover -s services/core-service/tests -t services/core-service/tests
  ```

- **Run the same assertions against every implementation of a behaviour.** The
  two manifest renderers drifted apart because the suite only tested the
  fallback. If two implementations cannot be kept in step, delete one.
- **The Postgres tests need a real server.** The store, isolation and role tests
  create a scratch database on a PostgreSQL 16 server, add Supabase's roles,
  apply the migrations as a non-superuser and drop everything afterwards. Point
  them at an admin login, or they skip with `database test skipped`:

  ```bash
  export HEALTHCARE_DEMO_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres  # healthcare suite
  export DAGENTS_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres          # dagents/tests/test_postgres_roles.py
  ```
- No test may need the network. Pin fixtures; never download a model or a
  dataset inside a test.

## 4. Before you call a backend change done

```bash
# 1. planners
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test && cd -

# 2. agents and control plane, with the planner available
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  .venv/bin/python -m unittest discover -s dagents/tests -t .

# 3. the service suite you changed, both ways (see §3)

# 4. the inventory, if you changed a route
.venv/bin/python scripts/service_inventory.py --check
```

Report what ran, including the number of skipped tests. "Tests pass" with the
governance tests skipped is not a pass.

## 5. Mistakes this repo has already made

- Two implementations of one rule drifted apart, because only one was tested.
- A suite was green because its most important tests had skipped.
- A convention was written down but not checked, so it stopped being followed.
- An enforcement layer permitted requests when its planner was unreachable.
- The LMA and GMA images had no planner. Every test passed, and in the compose
  stack the Guard denied every request.
