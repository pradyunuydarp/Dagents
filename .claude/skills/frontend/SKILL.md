---
name: frontend
description: Strict rules for the Dagents frontends — the framework site in site/, the healthcare demo UI in apps/healthcare-demo/frontend and the NL2SQL demo UI in services/nl2sql-demo/frontend. Use before changing a component, a page, the guide renderer, styling, the API calls a UI makes, or anything under a frontend/ or site/ directory. States what a UI may own, the design system rules, and the browser checks that must pass before the change is finished.
---

# Dagents frontend

These rules are binding. A change that breaks a **never** rule is wrong even if
the page renders. Text on every page follows the `writing` skill.

## 1. The three frontends

| App | Path | Port | Owns | Shows |
|---|---|---|---|---|
| Framework site | `site/` | 5175 | Home page, the learning guide, the API reference | How Dagents works and how to use it |
| Healthcare demo | `apps/healthcare-demo/frontend/` | 5174 | Stroke triage UI, the guard controls, the pilot view | Governance and federation work, and their cost can be measured |
| NL2SQL demo | `services/nl2sql-demo/frontend/` | 5173 | Prompt UI, schema editor, SQL display | An app can use the framework's validation, planning and checks |

All three are React 19 + Vite 7 + TypeScript, built with `tsc -b && vite build`.
The healthcare demo and the site use `playwright-core` for browser checks.

`apps/healthcare-demo/` can be extracted into its own repository with
`scripts/extract_repo.sh`. So its frontend must **never** import from `agents/`,
`services/` or anything else outside `apps/healthcare-demo/`.

## 2. The design system

`design/dagents-design-system.css` is the single source.
`scripts/sync_design_system.py --write` copies it into each frontend, and
`tests/test_publishing_guards.py` fails if a copy differs.

- **Never edit a copy.** Edit the source and run the sync. The copies exist
  because the healthcare demo must not import across the tree.
- **Never use a literal colour.** Every colour comes from a `--ds-*` token, and
  every token is defined on bare `:root` before a dark block redefines it. A
  token defined only in a dark block leaves one theme's text on the other
  theme's background.
- **Colour carries meaning only:** the decision states (permit, narrow, deny)
  and one blue for links and focus. There is no accent colour, because a
  decorative colour could be mistaken for a decision. Use type, weight and thin
  rules for hierarchy.
- **Never show state by colour alone.** A chip includes its word, so the page
  still reads in greyscale.
- Mermaid diagrams on the site take their colours from the tokens
  (`site/src/Mermaid.tsx`), so they stay grey and follow light and dark mode.

## 3. The framework site

- Pages live in the URL hash (`#/learn/04-apis`), because GitHub Pages has no
  server-side routing. A second `#` selects a heading:
  `#/learn/04-apis#plan-a-federated-round`.
- **The guide is written once, in `docs/learn/*.md`.** The site bundles those
  files, so the guide reads the same on GitHub and on the site. Edit the
  Markdown, never a copy in `site/`. The page order is `ORDER` in
  `site/src/learn.ts`.
- Links between guide pages use relative `.md` paths. The renderer turns them
  into site routes and turns other repository paths into GitHub links. Heading
  ids match GitHub's, so anchors work in both places.
- Diagrams are Mermaid code blocks. Images under `docs/presentation/puml/` are
  bundled with the site.
- The API reference reads `docs/reference/service-inventory.json`. Never copy
  that file into `site/`.

## 4. Published demos replay a real run or call a real API

`src/api.ts` has three modes, and the page says which one it is in:

| Build | Mode |
|---|---|
| local dev | live, same origin, through the Vite proxy |
| `VITE_DAGENTS_STATIC=1` | replays `public/recording.json` |
| `VITE_HEALTHCARE_API_BASE` set | live, against the deployed API, which reads the encounter store |

The third mode is the healthcare demo in production. NL2SQL has no deployed
backend and replays.

- **Never fake a response, and never use the replay as a fallback.** A request
  the recording does not hold raises `NotRecordedError`, and the UI says so. An
  invented permit would misrepresent the framework.
- **A live build never falls back to the recording.** `isRecorded` is false
  whenever an API is configured, even when that API is unreachable. A recording
  from another commit shown as live would misreport the data and the framework.
  The page reports the outage instead.
- **The page prints the data source the backend reports**
  (`/api/v1/framework/status`), never one inferred from the configured URL. An
  API that generates its cohorts in process must show `synthetic`.
- **Show the wait for a cold start.** The hosted API stops after 15 minutes
  without traffic, so the first request usually starts it (about 20 seconds).
  `liveFetch` retries only requests that never reached the app (502, 503, 504,
  or a network failure), reports the wait through `onApiWaking` so the page can
  show a banner, and returns an application error on the first attempt.
  Retrying an application error would hide a real failure.
- **Never record without the planner.** The Guard fails closed, so a recording
  made without `dagentsc` holds only denials. `capture_demo_recordings.py`
  refuses to run without it, and `check_demo_recordings.py` refuses a recording
  whose controls change nothing.
- The page names the commit it was recorded from. Keep that visible.
- If you change what the UI requests, record again. Otherwise the published
  demo answers "not recorded" where it used to work.

## 5. Hard rules

### The boundary

- The app owns its UI, its wording, its domain language and its layout. **The
  framework owns every decision the UI displays.**
- **Never compute a framework decision in the browser.** If a panel needs a
  protection strategy, the eligible sites or a gate result, it asks the backend,
  which asks the planner. A threshold, quorum rule or sensitivity level written
  again in TypeScript is a second copy of a planner rule, and it will drift.
- A UI may hold presentation state (the open tab, an expanded row, a draft). It
  must not hold the only copy of anything the backend owns.

### Configuration

- **Never hardcode a backend URL or port in a component.** Vite proxies `/api`
  to the backend, and the target comes from the environment:

  ```ts
  // apps/healthcare-demo/frontend/vite.config.ts
  server: { proxy: { "/api": process.env.HEALTHCARE_DEMO_API_URL ?? "http://127.0.0.1:8080" } }
  ```

  Components call relative `/api/v1/...` paths. A new backend means extending
  the proxy config and the env file.
- Site addresses are built from `import.meta.env.BASE_URL` (`site/src/config.ts`),
  so the site works at the root and under `/Dagents/`. Never hardcode the base path.
- Use the ports in the table above. Do not invent a new one.

### Dependencies

- Keep the dependency list short. The demos must start on a machine with no GPU,
  no database and no Docker. A new runtime dependency needs a reason the
  existing ones cannot meet.
- `playwright-core` is a devDependency only.
- Large libraries load only where needed: the site imports Mermaid lazily, on
  pages with a diagram.
- Never commit `node_modules/`, a `dist/` bundle, or a lockfile you did not
  regenerate with the matching `npm install`.

### Showing refusals and failures

The demos show the framework refusing things. A denial, a blocked gate or an
unreachable planner is a normal UI state: show it clearly, with its reason.

- Without `dagentsc`, the Ethical Guard denies every request. Every panel
  showing a denial is then the correct result. **Never** add a UI fallback that
  hides the denial or creates a permissive result.
- Show why: the strategy, the gate, or the missing metric. A bare "something
  went wrong" hides the information the demo exists to show.

## 6. The test loop

**A typecheck and a bundle only prove that the app compiles.** A browser check
proves it works. Write or extend the browser assertion for the behaviour you are
changing, watch it fail, then make it pass.

```bash
# Healthcare demo: starts the API and the UI, runs the smoke test, exits.
# Needs no Docker and no database.
apps/healthcare-demo/scripts/run_frontend_demo.sh --check

# Or against a running stack:
cd apps/healthcare-demo/frontend && npm install && npm run build && npm run smoke

# Framework site: every guide page, diagram, image and internal link.
cd site && npm install && npm run build && npm run check
```

The healthcare `smoke.mjs` drives the real UI against a live backend. It checks
that **each of the guard's three controls changes the strategy**, runs a full
pilot, and fails on any console error. It finds controls by their labels
("Requester verified", "Cohort size") and buttons by name ("Ask the guard",
"Run the pilot"); rename them only together with the test. When you add a
control, add an assertion for it.

The site's `check.mjs` serves `dist/`, visits every guide page, and fails if a
Mermaid diagram does not draw, an image does not load, an internal link or
anchor is missing, raw Markdown shows on a page, or the console has an error.
Mermaid syntax errors only appear in a browser, so the build cannot catch them.

Both scripts use the same exit codes:

| Exit | Meaning |
|---|---|
| 0 | Passed |
| 1 | Failed |
| 2 | `SKIP`: no Chromium or no `playwright-core` |

**Exit 2 is not a pass.** Report it as "skipped".

Web fonts load from Google Fonts, and some sandboxes block that host. The fonts
are optional (the tokens list fallbacks), so both checks ignore failed font
requests. Every other failed request or console error fails the check.

The NL2SQL demo has no browser test yet:

```bash
services/nl2sql-demo/scripts/run_local_demo.sh   # the app, no Docker
cd services/nl2sql-demo/frontend && npm install && npm run build
```

If you change behaviour there that a bundle cannot prove, add the assertion.

### Both demos need `dagentsc`

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe
```

`dune` is not on PATH, so always use `opam exec --`. The demo scripts check for
the binary, because without it every governed panel shows a denial.

## 7. Before you call a frontend change done

```bash
cd apps/healthcare-demo/frontend && npm run build && npm run smoke   # or the site/NL2SQL equivalent
python scripts/sync_design_system.py --check                         # no copy differs
python -m unittest discover -s tests -t .                            # publishing guards
```

Report the browser check result, including a skip. If the change touched an API
shape, run the backend suite that owns it and refresh both generated files:

```bash
.venv/bin/python scripts/service_inventory.py --check
.venv/bin/python scripts/capture_demo_recordings.py --demo all   # needs dagentsc built
```

The build does not notice a request the recording lacks; only a new recording does.

## 8. When a design is handed to you

A design may arrive as a spec, a mockup or an artifact. It decides layout,
hierarchy, colour and wording. It does not move a framework decision into the
browser, hardcode a URL, or hide a denial. If it requires one of those, name the
rule it breaks and offer the closest version that keeps the rule.
