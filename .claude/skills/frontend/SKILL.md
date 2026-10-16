---
name: frontend
description: Strict rules for the Dagents frontends — the framework site in site/, the healthcare demo UI in apps/healthcare-demo/frontend and the NL2SQL demo UI in services/nl2sql-demo/frontend. Use before changing a component, a page, the guide renderer, styling, the API calls a UI makes, or anything under a frontend/ or site/ directory. States what a UI may own, the design system rules, and the browser checks that must pass before the change is finished.
---

# Dagents frontend

These rules are binding. A change that breaks a **never** rule is wrong even if
the page renders. Text on every page follows the `writing` skill. Which docs a
code change must update, and how to regenerate them, is in the `docs-sync` skill.

## 1. The three frontends

| App | Path | Port | Owns | Shows |
|---|---|---|---|---|
| Framework site | `site/` | 5175 | Home page, the learning guide, the guide to building an application, the API reference, the Resources page | How Dagents works and how to use it |
| Healthcare demo | `apps/healthcare-demo/frontend/` | 5174 | The consortium view (guard controls, pilot, consortium charts), one view per hospital, data entry | Governance, federated training and per-site storage work, and their cost can be measured |
| NL2SQL demo | `services/nl2sql-demo/frontend/` | 5173 | Prompt UI, schema editor, SQL display | An app can use the framework's validation, planning and checks |

All three are React 19 + Vite 7 + TypeScript, built with `tsc -b && vite build`.
The healthcare demo and the site use `playwright-core` for browser checks.

`apps/healthcare-demo/` can be extracted into its own repository with
`scripts/extract_repo.sh`. So its frontend must **never** import from `agents/`,
`services/` or anything else outside `apps/healthcare-demo/`.

## 2. The design system

`design/dagents-design-system.css` is the single source.
`scripts/sync_design_system.py --write` copies it into each frontend, together
with the link preview plugin `design/social/social-meta.mjs` (section 3b), and
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
- Charts have three series tokens, `--ds-series-1` to `--ds-series-3`, used only
  inside charts. They mark identity (which hospital), never a decision. A
  hospital keeps its slot in every chart.
- Code blocks have syntax tokens, `--ds-syntax-*`, used only inside code
  blocks. They are GitHub's Primer light and dark syntax colours, and each
  has at least 4.5:1 contrast against `--ds-inset`. On the site,
  `site/src/highlight.ts` highlights every code block (guide pages, API
  samples and responses) with highlight.js, which registers only the
  languages the site shows. Plain text and unknown languages stay plain.
- Every code block on the site has a copy icon button in its top right
  corner (`site/src/codeCopy.ts`). Guide pages write it as HTML and API
  blocks as React, with the same markup, and one document click handler
  copies the `<pre>` inside the nearest `data-copy-scope` element.
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
- **The guide to building an application is written once, in
  `docs/guide/*.md`** (`#/guide`, page order in `site/src/guide.ts`). Both
  guides use one page layout, `site/src/pages/DocPages.tsx`. Its code blocks
  are copied from `examples/getting-started/`, and the `docs-sync` skill says
  how to keep them in step.
- Links between guide pages use relative `.md` paths. The renderer turns them
  into site routes and turns other repository paths into GitHub links. Heading
  ids match GitHub's, so anchors work in both places.
- Diagrams are Mermaid code blocks. Images under `docs/presentation/puml/` are
  bundled with the site.
- The API reference (`#/api`, `site/src/api/`) reads
  `docs/reference/api-reference.json`, the module pages in `docs/reference/api/`
  and the captured examples in `docs/reference/api/examples/`. The JSON loads
  only on API pages. Never copy these files into `site/`; the format is in
  `docs/reference/api/README.md`.
- **One search box, one matcher.** The API reference and both guides use
  `site/src/SearchBox.tsx` with the fuzzy matcher in `site/src/fuzzy.ts`, so
  word starts and typos find a result everywhere. The API index is
  `site/src/api/search.ts`; the guides' index (`site/src/docSearch.ts`) is
  built in the browser from the bundled Markdown, one entry per heading. A
  matched word is shown with weight and an underline, never a colour.
- The Resources page (`#/resources`) reads `docs/presentation/resources.json`.
  `site/resources-plugin.ts` copies each listed deck, PDF and preview into
  `dist/resources/` at build time. **Never copy a deck or a PDF into `site/`.**
  After a deck changes, run `scripts/export_presentations.py --write`, look at
  the new PDF and commit it; `tests/test_site_resources.py` fails until then.

## 3b. Link previews and icons

A link to one of the three sites, shared on LinkedIn, WhatsApp, Slack or X,
shows that site's card: the 1200 x 630 PNG named by the page's `og:image` tag.
Browser tabs show the logo mark.

- **The sources are in `design/social/`:** one HTML file per card, `card.css`,
  the mark in its light, dark and small sizes, and `favicon.svg`, which switches
  to the dark mark with a `prefers-color-scheme` query. The folder's README lists
  every output and which ones are copies. Every colour is a design system token.
- **Render, look, commit.** `scripts/render_social_cards.mjs` renders each card
  in Chromium with the fonts from the `@fontsource` packages (devDependencies
  of `site/`) and writes `social-preview.png`, `favicon.svg`, `favicon-32.png`,
  `apple-touch-icon.png` and `icon-512.png` into each frontend's `public/`. It
  blocks every request outside the repository, so it never uses the network.
  The PNGs are committed, so publishing needs no browser and no fonts.

  ```bash
  cd site && npm install && cd -
  node scripts/render_social_cards.mjs      # renders what changed; --force renders everything
  ```

  Look at every changed PNG with an image viewer before you commit it with
  `design/social/rendered.json`. **Never edit an output or a copy by hand.**
- **A card shows structure and purpose only.** **Never** put a count, score,
  date or result on a card, in a description or in alt text, because those
  change. The stroke triage card names the hospitals in `SITES` and the NL2SQL
  card shows a bundled sample question; the test checks both.
- **Never write a published address in `index.html`.** Crawlers ignore a
  relative `og:image`, so each page writes `%PUBLIC_SITE_URL%` and
  `social-meta.mjs` replaces it at build time with `PUBLIC_SITE_URL`. The Pages
  workflow reads that from `env/.env.published`. Without the variable the
  build uses the base path, which works locally but gives crawlers nothing. The
  plugin refuses an address that is not https, carries a query string, or does
  not match the base path.
- Each `index.html` has the description, the `og:` and `twitter:` tags, the
  icon links, the manifest link and a `theme-color` for each theme. Pages live
  in the URL hash, so every page of a site shares its root page's preview.
- The healthcare demo keeps its own copies of the icons and the plugin in
  `apps/healthcare-demo/frontend/`, so it stays extractable.
- **Previews are cached.** LinkedIn and WhatsApp keep the preview they fetched
  first. After a change, paste the address into the
  [LinkedIn Post Inspector](https://www.linkedin.com/post-inspector/) to refresh
  LinkedIn's copy. WhatsApp has no such tool.

`tests/test_social_previews.py` fails when a page lacks a tag, when an output
is stale or the wrong size, when a card is over 300 KB (WhatsApp drops larger
images), or when a title, description or alt text holds a number, as a digit
or a word. The stroke demo's texts may give its hospital count, and the test
ties that count to `SITES`. `site/check.mjs` reads the built home page's HTML
and loads its card and icons. The Pages workflow runs
`scripts/check_link_previews.py` on each built page before the upload, and
after the deploy it requests each published `og:image` and fails on anything
but a 200 image response.

## 4. Published demos replay a real run or call a real API

`src/api.ts` has three modes, and the page says which one it is in:

| Build | Mode |
|---|---|
| local dev | live, same origin, through the Vite proxy |
| `VITE_DAGENTS_STATIC=1` | replays `public/recording.json` |
| `VITE_HEALTHCARE_API_BASE` set | live, against the deployed API, which keeps each hospital's records in its own Postgres schema |

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
  (`/api/v1/framework/status`, `cohort_source`: `postgres` or `in-memory`),
  never one inferred from the configured URL.
- **Never show the API's address**, a hosting vendor's name or cold-start
  internals on the page. The address is in `env/.env.published` only, and
  `tests/test_publishing_guards.py` fails if the API's host appears in any other
  tracked file outside `.github/`. Error messages are plain and name no host.
- **Show the wait for a cold start.** The hosted API stops after 15 minutes
  without traffic, so the first request usually starts it (about 20 seconds).
  The API puts an `X-Dagents-App` header on every response it sends itself,
  errors included (404, 413, 422, 429, its own 503), and exposes it through
  CORS. The platform's answers during a cold start do not have it. `liveFetch`
  retries only requests that never reached the app: a network failure, or a
  502, 503 or 504 **without** `X-Dagents-App`. It reports the wait through
  `onApiWaking`, so the page can show a plain "connecting to the demo server"
  banner. A response with the header is returned on the first attempt, so the
  app's own 503 ("the hospital records cannot be reached") shows at once.
  Retrying an application error would hide a real failure. The smoke test
  checks both cases.
- **Data entry is disabled in a replay**, with a plain explanation. The
  recording holds no answers to new entries or approvals, and a replay must not
  invent one.
- **A replay never offers to add a hospital.** Adding one starts a process on
  the machine that runs the demo. The capture records `GET /api/v1/deployments`
  from one process with `HEALTHCARE_DEMO_DEPLOYER=off`, and
  `check_demo_recordings.py` refuses a recording whose answer is available or
  lists an attempt.
- **Never record without the planner.** The Guard fails closed, so a recording
  made without `dagentsc` holds only denials. `capture_demo_recordings.py`
  refuses to run without it, and `check_demo_recordings.py` refuses a recording
  whose controls change nothing.
- The page names the commit it was recorded from. Keep that visible.
- If you change what the UI requests, record again. Otherwise the published
  demo answers "not recorded" where it used to work. A hospital-page GET path
  is defined once, in `capture_demo_recordings.hospital_view_paths`, and
  `tests/test_publishing_guards.py` fails if the UI sends a GET path the
  recording checker does not require. Change a query string in both places.

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

### The healthcare demo's two kinds of view

- **The consortium view is the coordinator's view.** It shows the numbers each
  hospital reports (counts, and outcome rates only above the minimum cohort),
  the Guard probe, the pilot and charts of released aggregates. **Never** show a
  patient row on it.
- **A hospital view is that hospital's staff view.** It shows where the
  hospital's records are stored, its running statistics, its worklist, its
  entered records with the action to record an outcome, its own charts, its
  audit log and what it sent in each round. One hospital's view never shows
  another hospital's rows.
- Hospital ids come from `/api/v1/overview`. **Never** hardcode a site id.
- **The data-entry form comes from the backend**
  (`/api/v1/hospitals/{site_id}/entry-form`). The browser renders the fields,
  their options and ranges, and shows the backend's validation errors next to
  each field. It does not restate a range or an option list. Each submission
  sends a new `Idempotency-Key`. The form tells people not to enter real patient
  information.
- **The panel to add a hospital shows what the backend reports.**
  `DeployPanel` reads `GET /api/v1/deployments` and offers the button
  (`data-action="deploy"`) only when the answer says `available`, outside a
  replay. Otherwise it shows the backend's `reason`, or the replay's note, and
  no button; a 404 means the server does not offer the action. The button
  sits under the table, never in a table cell, so the table's scroll box
  cannot hide it on a 375 px screen. When a request ends, focus moves to the
  attempt's heading (or back to the button after an error), and a live region
  that stays mounted says how it ended. A hospital in the consortium whose
  LMA has stopped (`service` is `stopped` or `not_started`) gets a button to
  start it again, and while the overview fails the app shows the panel alone
  for that case, because the overview cannot load until the LMA runs. The steps
  (`data-step`, `data-status`) show their status as words, never as colours:
  a step is not a permit, narrow or deny decision. The page never decides
  whether a hospital can join or whether the next round selects it; it shows
  the planner's answer. After a hospital joins, it fetches the overview and
  the status again, so the header and the table list the new hospital.
- **Charts are drawn by the backend** as SVG and fetched through the transport,
  so a cold start and a replay work for them too. Fetch them again when the
  theme changes. Every chart has its table view next to it, and the page never
  draws a chart in the browser from raw records. The IST licence notice stays on
  every page built from the records.

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

# The same against the split run with --deploy: the test presses
# "Add Hospital 290" (smoke.mjs --expect-join pl-290).
apps/healthcare-demo/scripts/run_frontend_demo.sh --deploy --check

# Or against a running stack:
cd apps/healthcare-demo/frontend && npm install && npm run build && npm run smoke

# Framework site: every guide page, diagram, image and internal link.
cd site && npm install && npm run build && npm run check
```

The healthcare `smoke.mjs` drives the real UI against a live backend. It checks
that **each of the guard's three controls changes the `systolic_bp`
strategy**, checks that the panel to add a hospital shows the backend's
reason and no button, runs a full pilot and checks that a decision is shown, opens a
hospital view and checks that its charts draw, checks that the backend's
validation errors appear on an empty form, submits a data entry and checks that
the record appears and the count rises, switches to the dark theme and checks
that the charts are drawn again, checks that a 503 without `X-Dagents-App` shows the connecting
banner while the app's own 503 shows at once, and fails on any console error. It finds controls by their labels ("Requester verified",
"Cohort size") and buttons by name ("Ask the guard", "Run the pilot"); rename
them only together with the test. When you add a control, add an assertion for
it. The test must not assume which release decision the pilot reaches.

With `--expect-join <site_id>` the smoke test expects the button instead of
the reason. It presses it, checks that the button is disabled while the app
works, that every step is done or skipped and that the Compose file names
the hospital's LMA, that the overview lists one more hospital, that the new
hospital's page draws its charts, and that the next pilot's rounds include
it. CI greps its `ok  deploy: hospital pl-290 joined` line, so keep the
line's text, including the two spaces after `ok`.

The site's `check.mjs` serves `dist/`, visits every guide page, and fails if a
Mermaid diagram does not draw, an image does not load, an internal link or
anchor is missing, raw Markdown shows on a page, or the console has an error.
Mermaid syntax errors only appear in a browser, so the build cannot catch them.
On the Resources page it fetches every download and preview and checks each
file's first bytes (`PK` for a .pptx, `%PDF` for a PDF), because the check's
server answers a missing file with the index page and status 200.
On the API reference it opens every module page and fails unless each endpoint
in the inventory, and each dagentsc command, appears once in the interface
`docs/reference/api/` places it in; it also follows every link on those pages,
switches the code tabs, runs the search (with a typo) and checks the width
at 375 px. On Learn it searches both guides with a typo and opens a result. It
fails when a success response shows only "A JSON object." with no field table,
and when a code sample reads an SVG, a `304` or a refused command as JSON.
Every page of both guides must fit a 375 px screen. It reads the home page's
HTML as a link preview crawler does, without running the script, and fails
when a link preview tag is missing, the tags do not name `social-preview.png`
under the build's address, or the card (a 1200 x 630 PNG under 300 KB), the
favicons, the touch icon or a manifest icon does not load.

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
