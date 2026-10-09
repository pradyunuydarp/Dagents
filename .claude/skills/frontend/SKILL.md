---
name: frontend
description: Strict rules for changing Dagents frontend code — the React + Vite + TypeScript UIs at apps/healthcare-demo/frontend and services/nl2sql-demo/frontend, and the framework site at site/, plus the shared design system in design/ and the recorded static mode the published demos replay. Use before editing any .tsx, .ts, .css, package.json, vite.config.ts or smoke.mjs under either frontend, or when adding a panel, control, chart or API call to a demo UI. States what a demo frontend is allowed to own, and why a typecheck and a bundle are not evidence that the UI works.
---

# Dagents frontend

This skill is binding, not advisory. Where it says **never**, a change that does it is
wrong even if it renders.

## 1. What a Dagents frontend is for

Dagents is a **reusable framework**, not a product. Its frontends exist to prove a
consumer app can really consume the framework, and to make the framework's decisions
visible — which strategy the governance planner chose, which sites a federated round
selected, what a release gate blocked on.

There are two, and they are separate apps with separate package trees:

| App | Path | Port | Owns | Proves |
|---|---|---|---|---|
| Healthcare demo | `apps/healthcare-demo/frontend/` | 5174 | Stroke-triage UI, the guard's three levers, the pilot view | That governance and federation are real and have a measurable cost |
| NL2SQL demo | `services/nl2sql-demo/frontend/` | 5173 | Prompt UI, schema editor, SQL display | That an app can consume the framework's validation, planning and checks |

Both are React 19 + Vite 7 + TypeScript, built with `tsc -b && vite build`. The
healthcare demo additionally has `playwright-core` and a real browser smoke test.

`apps/healthcare-demo/` is extractable into a standalone repository via
`scripts/extract_repo.sh` — so **never** import from `agents/`, `services/` or anything
else outside `apps/healthcare-demo/` in its frontend, and never add a dependency on
another app's code.

## 2. The design system

`design/dagents-design-system.css` is the single source. `scripts/sync_design_system.py --write`
generates a copy into each frontend, and `tests/test_publishing_guards.py` fails on drift.

- **Never edit a copy.** Edit the source and re-run the sync. The copies exist only because
  `apps/healthcare-demo/` must stay extractable and cannot import across the tree.
- **Never introduce a literal colour.** Every colour comes from a `--ds-*` token, and every token
  is defined on bare `:root` before any dark block redefines it. A colour defined only inside a
  dark block leaves one theme's text on the other theme's background.
- **Colour carries meaning only.** Decision states — permit, narrow, deny — plus one functional
  blue for links and focus. There is no accent colour to spend. In a framework whose argument is
  that a decision is computed by a typed planner, a decorative accent would make a styled panel
  indistinguishable from a refused request. Hierarchy comes from type, weight and hairline rules.
- State is never carried by colour alone: a chip spells its own word, and a severity stripe
  repeats it, so the page survives being read in greyscale.

## 3. The published demos replay a real run — or call a real one

`src/api.ts` has three modes and the page says which one it is in:

| Build | Mode |
|---|---|
| local dev | live, same origin, through the Vite proxy |
| `VITE_DAGENTS_STATIC=1` | replays `public/recording.json` |
| `VITE_HEALTHCARE_API_BASE` set | live against the deployed API, which reads the encounter store |

The third is the healthcare demo in production. NL2SQL has no deployed backend and replays.

- **Never fake a response, and never widen the replay into a fallback.** A request the capture
  does not hold raises `NotRecordedError` and the UI says so. A governance demo that invented a
  permit would demonstrate the opposite of the framework's point — this is the same rule as
  "never hide a denial", applied to the transport.
- **A live build never falls back to the recording.** `isRecorded` is false the moment an API is
  configured, and it stays false when that API is unreachable. Serving a capture from another
  commit as though it were live would misreport the data *and* the framework, and the reader
  would have no way to tell. The page reports the outage instead.
- **The page prints the provenance the backend reported**, read from
  `/api/v1/framework/status`, never inferred from the fact that a URL was configured. A build
  pointed at an API that is generating cohorts in process must say `synthetic`.
- **A cold start is waited out visibly.** The API runs on a tier that stops the container when
  idle, so the first request is usually the one that wakes it. `liveFetch` retries only the
  statuses that mean the request never reached the app (502/503/504, and a network-level
  failure), announces the wait through `onApiWaking` so a banner can say what is happening, and
  returns an application error on the first attempt — retrying that would hide a real failure
  behind ninety seconds of patience.
- **Never capture without the planner.** The Guard fails closed, so an unplanned capture is
  nothing but denials: it looks fine and proves nothing. `capture_demo_recordings.py` refuses,
  and `check_demo_recordings.py` refuses a capture whose levers change nothing.
- The page names the commit it was captured from. Keep that visible.
- Changing what the UI requests means re-capturing. A new request shape the capture does not
  cover publishes as "not captured", not as a working demo.

## 4. Hard rules

### The boundary

- The app owns its UI, its wording, its clinical or SQL domain language, and its
  presentation choices. **The framework owns every decision the UI displays.**
- **Never compute a framework decision in the browser.** If a panel needs to know which
  protection strategy applies, which sites are eligible, or whether a gate passes, it
  asks the backend, which asks the planner. A threshold, a quorum rule, or a
  sensitivity level reimplemented in TypeScript is a second implementation of a planner
  rule, and it will drift. Fetch it.
- A UI may hold presentation state (which tab, which row is expanded, a draft input). It
  must not hold the authoritative copy of anything the backend owns.

### Configuration

- **Never hardcode a backend URL or port in a component.** Vite proxies `/api` to the
  backend, and the target is env-driven:

  ```ts
  // apps/healthcare-demo/frontend/vite.config.ts
  server: { proxy: { "/api": process.env.HEALTHCARE_DEMO_API_URL ?? "http://127.0.0.1:8080" } }
  ```

  Components call relative `/api/v1/...` paths. Adding a new backend means extending the
  proxy config and the env file, not embedding a host.
- Ports are the ones in the table above, and they are env-driven by design. Do not
  invent a new one.

### Dependencies

- Keep the dependency list short and justified. These are demos that must start on a
  machine with no GPU, no database and no Docker. A new runtime dependency needs a
  reason that the existing ones cannot serve.
- `playwright-core` stays a devDependency of the healthcare frontend only.
- Never commit `node_modules/`, a `dist/` bundle, or a lockfile you did not regenerate
  with the matching `npm install`.

### Failure display

The demos exist partly to show the framework refusing things. A denial, a blocked gate,
or an unreachable planner is a **first-class UI state**, not an error toast to be
swallowed.

- Without `dagentsc` built, the Ethical Guard denies every request. That is correct
  behaviour, and every panel showing a denial is the correct rendering of it. Do not add
  a UI-side fallback that hides the denial or synthesises a permissive result — that
  would lie about the framework's most important property.
- Show *why*: the strategy, the gate, the missing metric. A bare "something went wrong"
  throws away the thing the demo is demonstrating.

## 5. The TDD loop

**A typecheck and a bundle prove the app compiles and nothing about whether it works.**
This is written on the smoke test itself, and it is the rule for this layer.

So the loop is: write or extend the smoke assertion for the behaviour you are about to
change, watch it fail against the running app, then make it pass.

```bash
# Healthcare demo — the full loop, no Docker and no database needed
apps/healthcare-demo/scripts/run_frontend_demo.sh --check   # start, smoke-test the UI, exit

# Or, against an already-running stack:
cd apps/healthcare-demo/frontend && npm install && npm run build && npm run smoke
```

`smoke.mjs` drives the real UI against a live backend: it asserts that **each of the
guard's three levers changes the strategy**, runs a full pilot, and fails on any console
error. Extend it when you add a control that carries an argument; a control nobody
asserts on is a control that will silently stop working.

Its exit codes matter:

| Exit | Meaning |
|---|---|
| 0 | Passed |
| 1 | Failed |
| 2 | `SKIP` — no Chromium or no `playwright-core` |

Exit 2 is **not** a pass. A machine without a browser must report "skipped", and a CI
job or a report that treats 2 as success is a false green. Say "skipped" when it skips.

For the NL2SQL demo:

```bash
services/nl2sql-demo/scripts/run_local_demo.sh   # app, no Docker
cd services/nl2sql-demo/frontend && npm install && npm run build
```

It has no browser smoke test yet. If you change behaviour there that a bundle cannot
prove, add the assertion rather than relying on the typecheck.

### Both demos need `dagentsc`

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe
```

`dune` is not on PATH — always `opam exec --`. The demo scripts check for the binary and
say so, because without it every governed panel shows a denial.

## 6. Before you call a frontend change done

```bash
cd apps/healthcare-demo/frontend && npm run build && npm run smoke   # or the NL2SQL equivalent
python scripts/sync_design_system.py --check                         # no copy drifted
python -m unittest discover -s tests -t .                            # publishing guards
```

Report the smoke result honestly, including a skip. Then, if the change touched an API
shape, re-run the backend suite that owns it — a frontend change that needed a new field
is a backend change too, and both generated artifacts have to be refreshed:

```bash
.venv/bin/python scripts/service_inventory.py --check
.venv/bin/python scripts/capture_demo_recordings.py --demo all   # needs dagentsc built
```

A change to what the UI requests and no re-capture means the published demo answers "not
captured" where it used to work. The build will not tell you; only the capture will.

## 7. When the design is handed to you

A visual design for a demo frontend may arrive as a spec, mockup or artifact. Implement
it within these rules: the design decides layout, hierarchy, colour and wording; it does
not get to move a framework decision into the browser, hardcode a URL, or hide a
denial. If a design requires one of those, say which rule it hits and offer the nearest
implementation that holds.
