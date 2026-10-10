---
name: backend
description: Strict rules for changing Dagents backend code — the OCaml planners in bindings/ocaml, the Python agents in agents/, and the FastAPI and Spring Boot services in services/. Use before adding or changing an endpoint, a planner rule, a governance or federation decision, a model family, a source adapter, or anything under agents/, services/ or bindings/ocaml. States where each kind of decision is allowed to live and the test-first loop that must pass before the change is finished.
---

# Dagents backend

These rules are binding. A change that breaks a **never** rule is wrong even if
it works, and even if the request seemed to ask for it. Say so, and propose the
placement these rules allow. Comments, docstrings and commit messages follow the
`writing` skill.

## 1. Where code lives

Each language does the work it suits best:

| Layer | Owns | Never owns |
|---|---|---|
| **OCaml** `bindings/ocaml/` | Pure planning: validate, compile, route, render | Database connections, training loops, API servers |
| **Python** `agents/`, `services/` | FastAPI services, model training and inference, source I/O, runtime state | Deterministic planning rules |
| **Spring Boot** `services/spring-services/` | Orchestration APIs, policy entry points, external integration | Machine learning |

The two agents:

- **LMA** (`agents/lma/`): one per data source (a tenant, database, service,
  event stream or environment). It profiles data, splits work, runs local
  models, publishes summaries, and enforces governance at three of the four
  guard boundaries.
- **GMA** (`agents/gma/`): registers LMAs, combines their outputs, runs
  aggregate models, coordinates dispatch, and runs federated rounds and release
  decisions.

Python calls OCaml through the `dagentsc` command-line program, sending JSON on
standard input. **Never** link the languages directly (FFI). Always go through
`agents/common/infrastructure/dagents_runner.py`, which converts between
snake_case and camelCase and runs the subprocess. The binary comes from
`DAGENTSC_BIN`, or from `dagentsc` on PATH.

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
| `lib/manifest_compiler` | Typed workload spec → Kubernetes YAML |
| `lib/governance_compiler` | GRAILS: the protection each field of a request needs |
| `lib/federation_compiler` | Round manifests, eligibility, quorum, aggregation readiness, release gates |

## 2. Hard rules

### Governance

- **The Ethical Guard fails closed.** If the planner cannot be reached, the
  Guard denies the request and records the denial. **Never** add a fallback that
  permits a request when the planner fails.
- Deciding happens in `bindings/ocaml/lib/governance_compiler`. Sensitivity,
  trust, granularity and strategy are algebraic data types, and
  `select_strategy` matches every combination of the three inputs. Adding a
  level must fail to compile until every combination has a strategy.
- Enforcing happens in `agents/common/application/ethical_guard.py`: apply the
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
  aggregation readiness and release gates.
  `agents/common/application/federation.py` holds state and side effects and
  calls the planner for the rest.
- **Aggregation produces a candidate, never a release.** A release needs its
  gates to pass and a person to approve it.
- **A release gate whose metric is missing blocks the release.** It never passes
  by default.
- The federated protocol belongs to a dedicated runtime behind
  `FederationEngine`. The in-process engine is a simulator for tests and demos.
  **Never** turn it into a federated optimizer.

### Agent layout

`agents/lma/` and `agents/gma/` use the same layout, and a new agent must too:

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
The test: **if a capability needs a domain word in `agents/`, it belongs in the
consumer**, through `agents/common/extensions`. Nothing in `agents/` knows what
a stroke is, and nothing in `apps/healthcare-demo/` implements a quorum rule.

A consumer contributes feature contracts, data classifications, condition packs,
named pipeline steps and named model adapters through
`agents/common/extensions`. It never patches the framework. Registration is
explicit, and two extensions registering the same id is an error, because
otherwise a guard decision would depend on import order.
`apps/healthcare-demo/backend/app/extension.py` is the reference: about eighty
declarative lines that import no framework internals. If a new consumer's needs
do not fit through this interface, improve the interface instead of working
around it.

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

Tests import as `agents.common...`, so run them from the repository root, with
the gitignored `.venv/` rather than system Python:

```bash
.venv/bin/python -m unittest discover -s agents/tests -t .
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
- No test may need the network. Pin fixtures; never download a model or a
  dataset inside a test.

## 4. Before you call a backend change done

```bash
# 1. planners
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test && cd -

# 2. agents and control plane, with the planner available
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  .venv/bin/python -m unittest discover -s agents/tests -t .

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
