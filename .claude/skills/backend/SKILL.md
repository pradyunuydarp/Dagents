---
name: backend
description: Strict rules for changing Dagents backend code — the OCaml planners in bindings/ocaml, the Python agents in agents/, and the FastAPI and Spring Boot services in services/. Use before adding or changing an endpoint, a planner rule, a governance or federation decision, a model family, a source adapter, or anything under agents/, services/ or bindings/ocaml. States where each kind of decision is allowed to live and the test-first loop that must pass before the change is finished.
---

# Dagents backend

This skill is binding, not advisory. Where it says **never**, a change that does it is
wrong even if it works, and even if the user's request seemed to ask for it. Say so and
propose the placement this skill allows instead.

## 1. The architecture, in one rule

**Polyglot by layer. Each language owns what it is best at.** Dagents is
planner-first where planning matters, runtime-first where side effects matter — not
OCaml-first, not Python-first.

| Layer | Owns | Never owns |
|---|---|---|
| **OCaml** `bindings/ocaml/` | Pure planning: validate, compile, route, render | DB sockets, training loops, API servers |
| **Python** `agents/`, `services/` | FastAPI surfaces, ML training/inference, source I/O, runtime state | Deterministic planning rules |
| **Spring Boot** `services/spring-services/` | Orchestration APIs, policy entrypoints, external integration | ML execution |

The two agent roles:

- **LMA** (`agents/lma/`) — one per source boundary (tenant, database, service, event
  stream, environment). Profiles data, partitions work, runs source-level models,
  publishes summaries, enforces governance at three of the four guard boundaries.
- **GMA** (`agents/gma/`) — registers LMAs, assimilates their outputs, runs aggregate
  models, coordinates dispatch, owns federated round control and release governance.

Python reaches OCaml through a **JSON subprocess boundary** (`dagentsc`), never FFI,
and always through `agents/common/infrastructure/dagents_runner.py` — it owns the
snake_case ↔ camelCase conversion and the subprocess plumbing. The binary resolves from
`DAGENTSC_BIN`, else `dagentsc` on PATH.

### Where a decision is allowed to live

Ask one question of every new rule: **is it deterministic?**

- Deterministic (given the same inputs, always the same answer) → it is a planner rule.
  It belongs in an OCaml compiler module, modelled as an algebraic data type with
  exhaustive matching, and Python calls out to it.
- Needs real data, a clock, a socket, a random seed, or an audit sink → it is runtime.
  It belongs in Python, and it delegates the deterministic part.

**Never re-derive a planner rule in Python** because shelling out to `dagentsc` felt
inconvenient. Two implementations of one rule is the specific failure this repo has
already been bitten by; see §5.

### The compiler modules and what they decide

| Module | Decides |
|---|---|
| `lib/common_ir` | The shared typed IR and its JSON codecs. Every new type starts here. |
| `lib/dataset_compiler` | Source validation, profiling, schema contracts, quality, transforms |
| `lib/pipeline_compiler` | DAG validation and topological ordering |
| `lib/model_router` | dataset profile + task → model family + packaging mode |
| `lib/manifest_compiler` | typed workload spec → Kubernetes YAML |
| `lib/governance_compiler` | GRAILS Ethical-Restriction Rails: what protection a request needs |
| `lib/federation_compiler` | Federated round manifests, eligibility, quorum, release gates |

## 2. Hard rules

### Governance

- **The Ethical Guard fails closed.** If the planner cannot be reached, the request is
  denied and the denial is recorded. **Never** add a fallback that permits on planner
  failure. An enforcement layer whose absence grants access is not one.
- Deciding lives in `bindings/ocaml/lib/governance_compiler`; sensitivity, trust,
  granularity and strategy are ADTs and `select_strategy` matches the full triple
  exhaustively. Adding a level must fail to compile until every combination is handled.
- Enforcing lives in `agents/common/application/ethical_guard.py`: apply the plan,
  project away what the request did not ask for, write a digest-chained audit record.
- Policy is configuration, not code — a `DataClassification`, contributed by an
  extension or registered at runtime. Changing a field's sensitivity must change no
  code path.
- A site's local runner is handed the **guarded** payload, not its raw records, even
  though that data never leaves the site. The lever for a deployment that finds the
  accuracy cost too high is the field's classification, not this code path.

### Federation

- `bindings/ocaml/lib/federation_compiler` owns eligibility, quorum, aggregation
  readiness and release gates. `agents/common/application/federation.py` owns state and
  side effects and delegates the rest.
- **Never let aggregation imply release.** Aggregation produces a candidate; a release
  needs its gates passed and a human to approve.
- **A release gate whose metric is absent blocks.** It never passes by default.
- The federated protocol belongs to a specialist runtime behind `FederationEngine`. The
  in-process engine is a simulator for tests and demos — **never** grow it into a
  federated optimizer.

### Agent layering

Both `agents/lma/` and `agents/gma/` use the same shape, and a new agent must too:

```text
domain/          pure typed models, no I/O
application/     orchestration and use-cases
adapters/        technology-facing boundaries
infrastructure/  messaging, persistence, concrete implementations
config.py di.py main.py    composition root
```

`domain/` stays pure — no imports that open a socket, read a file, or read the clock.
New infrastructure goes behind a replaceable interface so the repo can move from
in-memory delivery to broker-backed and persisted deployments without a structural
rewrite.

### Endpoints

- LMA and GMA expose legacy short paths (`/health`, `/datasets/profile`, `/models/run`)
  **and** versioned equivalents (`/api/v1/health`, `/api/v1/datasets:profile`,
  `/api/v1/model-jobs`). This is deliberate. When you add an endpoint to either agent,
  add both forms, and make the versioned one call the legacy handler so they cannot
  drift.
- The framework services (`core`, `pipeline`, `model`) are uniformly `/api/v1/...`.
- **Never hardcode a URL or a port.** Config comes from `env/.env.shared` plus a
  per-service file, with distinct `*_PUBLIC_URL` (host) and `*_INTERNAL_URL` (compose
  network) values.
- Every endpoint you add or change must be reflected in the service inventory:

  ```bash
  .venv/bin/python scripts/service_inventory.py --write
  ```

  Then commit the regenerated `docs/reference/service-inventory.json` and `.md`. The
  inventory test fails on drift, so this is not optional.

### Scope

Product-specific logic does not belong in Dagents unless it is genuinely reusable
across consumers. The test: **if a capability would need a domain word in `agents/`, it
belongs in the consumer**, reached through `agents/common/extensions`. Nothing in
`agents/` knows what a stroke is; nothing in `apps/healthcare-demo/` re-implements a
quorum rule.

A consumer contributes feature contracts, data classifications, condition packs, named
pipeline steps and named model adapters through `agents/common/extensions` — never by
patching the framework. Registration is explicit and id conflicts are errors, because
two extensions claiming one id would make a guard decision depend on import order.
`apps/healthcare-demo/backend/app/extension.py` is the reference: ~80 declarative
lines, importing no framework internal. If something will not fit through that
interface, that is a signal about the interface, not a reason to reach around it.

## 3. The TDD loop

Test first. Not "write the code, then add a test that passes" — write the assertion
that fails for the reason you are about to fix, watch it fail, then make it pass.

### Adding to the OCaml layer, in this order

1. The type goes in `common_ir` **first**.
2. Then its JSON parsing.
3. Then the compiler module.
4. Then the tests in `bindings/ocaml/test/test_compilers.ml`.

Model choices as algebraic data types, never string conditionals — exhaustive matching
is the entire reason this layer is in OCaml. Return reports and plans rather than
raising; reserve exceptions for compile requests that cannot produce a meaningful plan.
Test at the compiler boundary: invalid inputs, normalized output contracts,
deterministic ordering.

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test
```

`dune` is not on PATH — always go through `opam exec --`. The binary lands at
`bindings/ocaml/_build/default/bin/dagentsc.exe`. Most OCaml tests need no Docker,
database or GPU because the modules are pure planners, so this is the fastest feedback
loop in the repo. Prefer it.

### Python

Tests import as `agents.common...`, so run from the repository root. FastAPI and friends
live in the gitignored `.venv/`, not system Python:

```bash
.venv/bin/python -m unittest discover -s agents/tests -t .
```

Per-service suites live under `services/<name>/tests/`. The healthcare demo has its own:

```bash
cd apps/healthcare-demo && PYTHONPATH=../..:backend ../../.venv/bin/python -m unittest discover -s tests -t .
```

### Rules that make the suite mean something

- **Governance and federation tests run against the real `dagentsc` binary**, never a
  stub. Stubbing the planner would prove the code calls something, not that the
  governance holds.
- **Check the skip count.** Those tests find the dune build automatically and skip with
  a message when it is missing. A suite that silently skips its governance tests is
  green without having proved anything. Build the binary, then re-run.
- **Run the service suites both with and without `DAGENTSC_BIN`.** `core-service`
  compiles manifests through the OCaml compiler when the binary is reachable and
  through a Python fallback when it is not; containers put `dagentsc` on PATH, so the
  OCaml path is the deployed one:

  ```bash
  DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
    PYTHONPATH=.:services/core-service .venv/bin/python \
    -m unittest discover -s services/core-service/tests -t services/core-service/tests
  ```

- **Any behaviour with two implementations needs the same assertions run against both.**
  The two manifest renderers had silently diverged and nobody noticed, because the suite
  only ever exercised the fallback. If two implementations cannot be kept in step,
  delete one.
- A test that needs the network is a test that will be skipped. Pin fixtures, never
  fetch a model or dataset inside a test.

## 4. Before you call a backend change done

```bash
# 1. planners
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && opam exec -- dune test && cd -

# 2. agents + control plane, with the planner reachable
DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe \
  .venv/bin/python -m unittest discover -s agents/tests -t .

# 3. the service suite you touched, both ways (see §3)

# 4. the inventory, if you touched any route
.venv/bin/python scripts/service_inventory.py --check
```

Report what actually ran, including the skip count. "Tests pass" with the governance
tests skipped is a false report.

## 5. The failure modes this repo has already hit

Recognise these; do not repeat them.

- Two implementations of one rule diverging silently, because only one was under test.
- A test suite green because its most important tests skipped.
- A convention documented in prose and enforced nowhere, so it decayed.
- An enforcement layer that permitted when its planner was unreachable.
