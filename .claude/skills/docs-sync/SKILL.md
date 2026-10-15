---
name: docs-sync
description: Rules for keeping the docs that the framework site publishes in step with the code — the API reference, the guide to building an application and its worked example, the learning guide, the Resources page, the home page's status list and demo cards, the service inventory, the demo READMEs, README.md, CLAUDE.md, AGENTS.md and the planner README. Use before changing any code under agents/, services/, bindings/ocaml/, apps/, contracts/, scripts/, examples/ or env/, the compose file, a workflow, or a deck in docs/presentation/, and before calling such a change done. Also use when a docs check fails (check_docs_impact.py, test_api_reference, test_api_examples, test_service_inventory, test_getting_started_example, test_site_resources).
---

# Keeping the docs in step with the code

These rules are binding where they say **never**. Text you write follows the
`writing` skill. The `backend`, `frontend` and `ci` skills point here.

## 1. The rule

**A code change and the docs it affects land in the same commit.** In a pull
request, they land in the same pull request. **A change is not done while the
site describes the old behaviour.**

The Pages workflow (`.github/workflows/pages.yml`) publishes the framework site
on every push to `main`. From that push on, a page that describes the old code
is wrong for every reader.

A doc is affected when it states something the change makes false: a path, a
field, a default, a status code, a command, a port, an output, a number, a
limit, a side effect or a diagram. Removed behaviour counts: a page that
describes it is affected.

## 2. What each code area feeds

The area names are the ones in `AREAS` in `scripts/check_docs_impact.py`
(section 6). "API reference" means the module page in `docs/reference/api/`,
its captured examples in `docs/reference/api/examples/`, and
`docs/reference/api-reference.json`. "Inventory" means
`docs/reference/service-inventory.{json,md}`.

| Area | Code | Docs it feeds |
|---|---|---|
| `planners` | `bindings/ocaml/` | `bindings/ocaml/README.md` (module map, commands, contract examples); API reference `dagentsc.md` and `examples/dagentsc.*`; the guide's tables of strategies (`docs/guide/04-govern-every-read.md`), exclusion reasons and release decisions (`docs/guide/06-federated-rounds.md`), and its output blocks; learning guide pages 02 and 03; `docs/architecture/`; `docs/demo/expected/`; the home page's `STEPS` and `STATUS` (`site/src/pages/Home.tsx`) |
| `agents` | `agents/` | API reference `agents.md`, `examples/lma.*`, `examples/gma.*`; inventory; guide pages 02 to 06, including the blocks that quote `agents/common/application/federation.py` and `federation_worker.py`; learning guide pages 02 to 04; `docs/agents/`; `agents/lma/README.md`, `agents/gma/README.md`; the home page's `STEPS`, `PARTS` and `STATUS` (`site/src/pages/Home.tsx`) |
| `framework-services` | `services/core-service/`, `services/pipeline-service/`, `services/model-service/` | API reference `framework-services.md` and those services' examples; inventory; guide pages 07 and 08; learning guide page 04; each service's README |
| `spring-services` | `services/spring-services/` | API reference `spring-services.md` and `examples/spring-*`; inventory |
| `nl2sql-demo` | `services/nl2sql-demo/` | `services/nl2sql-demo/README.md`; API reference `demo-apps.md` and `examples/nl2sql-demo-backend.*`; inventory; guide page 07 (quotes `dagents_orchestrator.py`); learning guide page 03; `docs/demo/`; its card in `DEMOS` (`site/src/config.ts`) |
| `healthcare-demo` | `apps/healthcare-demo/` | `apps/healthcare-demo/README.md` and `apps/healthcare-demo/docs/deployment.md`; API reference `demo-apps.md` and `examples/healthcare-demo-backend.*`; inventory; guide page 03 (quotes `site_store.py` and migration `0002`); learning guide pages 02 to 05 (walkthrough 1 in page 04, the run steps in page 05); its card in `DEMOS` (`site/src/config.ts`) and the home page's `STATUS` |
| `contracts` | `contracts/grpc/` | `docs/agents/lma-gma-architecture.md`; the repository map in `README.md`, `CLAUDE.md` and `AGENTS.md` |
| `scripts` | `scripts/` | the commands in `CLAUDE.md`; the skill that names the script; `docs/reference/api/README.md`; guide page 09; learning guide page 05; `docs/demo/README.md`; the files the script generates |
| `example` | `examples/` | every block in `docs/guide/` with a `source` or `output` marker; `examples/getting-started/README.md` |
| `config` | `env/`, `docker-compose.yml`, `.github/workflows/` | the port table and the CI section of `CLAUDE.md`; the `ci` skill; `docs/guide/01-setup.md`, `08-deploy.md` and `09-test-your-integration.md`; `docs/learn/05-hands-on.md`; `apps/healthcare-demo/docs/deployment.md` |
| `decks` | `docs/presentation/**/*.pptx` | the Resources page (`#/resources`): the deck's entry in `docs/presentation/resources.json` (its description, audience and note) and its PDF and preview in `docs/presentation/exports/` |

Every area also feeds `README.md`, `CLAUDE.md` and `AGENTS.md` when the change
touches a command, a port, a path, an environment variable, a convention or the
repository map.

The site has two pages of its own that state facts about the code, and the
check counts them as docs of the areas above:

- `site/src/pages/Home.tsx`: `STEPS` (how Dagents works), `PARTS` (what each
  part does) and `STATUS` (the "Current state" list, such as "The framework
  agents keep their state in memory"). A change that makes one of these false
  updates it.
- `site/src/config.ts`: `DEMOS`, the card for each demo (its data, what to try
  first and where it gets its answers).

Two kinds of change have docs but no area, so the check does not see them:

| You changed | Update |
|---|---|
| A page's file name or a heading | every link to it, and `ORDER` in `site/src/learn.ts` or `site/src/guide.ts`, or `MODULES` in `site/src/api/modules.ts` |
| A Dockerfile | `docs/guide/08-deploy.md`, `apps/healthcare-demo/docs/deployment.md` (the Dockerfile's own area also asks for one of its docs) |

## 3. Generated docs and how to regenerate them

Some docs are written by a script from the code. **Never edit a generated file
by hand.** Change its source, run its generator and commit both.

| Generated file | Source | Generator |
|---|---|---|
| `docs/reference/service-inventory.{json,md}` | FastAPI routing tables, Spring controllers | `scripts/service_inventory.py --write` |
| `docs/reference/api-reference.json` | each app's OpenAPI document, `Field(description=...)`, docstrings, Spring records, `bindings/ocaml/bin/dagentsc.ml`, the `api-object` blocks | `scripts/api_reference.py --write` |
| `docs/reference/api/examples/<service>.json` | `docs/reference/api/examples/<service>.requests.json`, replayed against the real app | `scripts/capture_api_examples.py` (needs the planner) |
| the `facts` of each entry in `docs/presentation/resources.json`, and `docs/presentation/exports/` | the decks | `scripts/export_presentations.py --write` (needs LibreOffice Impress, PyMuPDF and Pillow) |
| each frontend's `public/social-preview.png` and icons, and `design/social/rendered.json` | the cards and the mark in `design/social/`, the design system, the fonts' versions, the addresses in `env/.env.published` | `node scripts/render_social_cards.mjs` (needs `cd site && npm install` and Chromium); look at every changed PNG |

Docs written by people, and what keeps them in step with the code:

| Doc | Kept in step by |
|---|---|
| The module pages in `docs/reference/api/*.md` | placing every endpoint in one interface; prose rules for a `complete` interface (`docs/reference/api/README.md`) |
| The code blocks in `docs/guide/` | `<!-- source: PATH -->` and `<!-- output: PATH -->` markers (section 5) |
| The descriptions in `docs/presentation/resources.json` | nothing; read them |

Run from the repository root. The planner path is the dune build, or the
binary CI downloads:

```bash
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe

# An endpoint, a model, a field description or a route docstring changed
.venv/bin/python scripts/service_inventory.py --write
.venv/bin/python scripts/api_reference.py --write
.venv/bin/python scripts/capture_api_examples.py --service <service>   # or no flag for all
.venv/bin/python scripts/capture_api_examples.py --check               # replay; write nothing

# The Spring examples start spring-control-service from its jar
mvn --batch-mode -f services/spring-services/pom.xml -pl spring-control-service -am package -DskipTests

# Code the guide or its example shows changed
PYTHONPATH=. .venv/bin/python examples/getting-started/step4_federated_round.py   # paste the output
.venv/bin/python -m unittest tests.test_getting_started_example -v

# A deck changed, was added or was removed
.venv/bin/python scripts/export_presentations.py --write
.venv/bin/python scripts/export_presentations.py --check

# Every page on the site
cd site && npm run build && npm run check && cd -
```

After `export_presentations.py --write`, open the new PDF and look at every
changed slide. The conversion substitutes fonts, and a broken page would be
published unseen. Then update that deck's `description` and `note` in
`resources.json` if what the deck covers changed, and commit the exports with
the JSON.

## 4. The tests that catch drift

| Test | Fails when | Does not catch |
|---|---|---|
| `tests/test_service_inventory.py` | the inventory differs from the routes; a framework service has an unversioned path; an LMA/GMA alias pair is missing or answers differently; Spring stops mirroring Python | prose about an endpoint |
| `tests/test_api_reference.py` | `api-reference.json` differs from a fresh run; an endpoint is placed in no interface or in two; a listed endpoint, file or class does not exist; a `complete` interface lacks prose, an example or a field description | whether the prose is true |
| `tests/test_api_examples.py` | a stored example no longer matches its request file or the schemas; with the planner, a replayed request answers differently | endpoints with no example; errors described only in prose |
| `tests/test_getting_started_example.py` | a `source` block differs from its file; a Python block in the guide has no marker; an `output` block differs from what the script prints with the planner; an example file is never shown; a word of the example's domain appears in `agents/` | prose next to a block; commands and outputs the test cannot run (curl calls, Postgres tests) |
| `tests/test_site_resources.py` | a deck, PDF or preview changed after `--write`; a PDF's page count differs from the deck's slides; a deck or note under `docs/presentation/` is neither listed nor excluded with a reason; a published file contains the API host | a description that no longer matches the deck |
| `tests/test_publishing_guards.py` | the deployed API's host appears outside `env/` and `.github/`; the README does not link a published site | anything else in the docs |
| `tests/test_social_previews.py` | a link preview tag is missing; a card or icon is stale, the wrong size or over 300 KB; a title, description or alt text has a digit or a number word; the stroke card's hospitals, or the hospital count in its texts, differ from `SITES`; a Pages check in `scripts/check_link_previews.py` stops failing on a broken input | whether a card's wording is still true |
| `tests/test_docs_impact.py` | the docs-impact check, its area map or its CI job is broken (section 6) | — |
| `site/check.mjs` (`npm run check`) | a Mermaid diagram does not draw, an image does not load, a link or anchor between pages is broken, raw Markdown shows, an endpoint is missing from the API page that places it, a Resources download is missing | text that renders and is wrong |

The example and replay tests skip without `dagentsc`. **A run that skipped them
has not checked the guide's outputs or the API examples.** Report the skip
count.

## 5. The guide and its example

The guide in `docs/guide/` teaches with the application in
`examples/getting-started/`. `tests/test_getting_started_example.py` holds them
together:

- A block with `<!-- source: PATH -->` on the line above it must appear, line
  for line, in that file. **Every Python block in the guide needs a marker.**
  Blocks may quote framework or demo files too: `federation.py`,
  `federation_worker.py`, the stroke demo's `site_store.py` and migration, and
  NL2SQL's `dagents_orchestrator.py`. Changing a quoted part of those files
  fails the test until the guide is updated. That failure is intended.
- A block with `<!-- output: PATH -->` above it must equal what that script
  prints with the real planner.

When the test fails, read which block and file it names. For a `source` block,
copy the lines again from the file. For an `output` block, run the script and
paste what it prints. Then read the prose around the block: if a number or a
decision changed, the sentence next to it is now wrong too.

To add to the guide: add the page as `docs/guide/NN-name.md` and list it in
`ORDER` in `site/src/guide.ts`. Show only code that exists in a file, with a
marker. If the code does not exist yet, add it to the example and run it first.
Each step has what to do, the code, the command, the expected output and what
can go wrong.

## 6. The docs-impact check

`scripts/check_docs_impact.py` runs in the `docs-impact` job of
`.github/workflows/ci.yml`, on pull requests and on pushes to `main`. For each
area in section 2, it fails when the change touches the area's code and none of
the area's docs. Markdown files, tests, `__pycache__`, `node_modules` and
`package-lock.json` inside a code folder are not code.

The change is the pull request (base to head) or the push (`before` to
`after`). When `before` is missing, as after a force push, the script checks
the head commit alone and says so.

When the change needs no docs, say why in a commit message, on a line of its
own, and name the areas the line covers in brackets:

```text
Docs-Impact: none [agents] (internal rename; no output or page mentions it)
Docs-Impact: none [planners, agents] (rename of private helpers only)
```

The line exempts only the areas it names, so a line written for one change
cannot hide another area's change later in the same pull request. The area
names are the ones in section 2, and the failure message prints the line to
use. The reason needs at least three words. A line without brackets, with an
unknown area, or with a reason such as `n/a`, `ok` or `reason` is refused and
listed in the output. The reason is printed in the job log for the reviewer.
Use it for a change no page describes, such as a refactor, a fixed typo in an
error message no page quotes, or a dependency bump. Never use it to defer docs
that are affected.

Run it locally before you push:

```bash
.venv/bin/python scripts/check_docs_impact.py --base origin/main
```

**What it cannot check.** It proves that a decision was made, not that the
right page changed. A typo fix in any of the area's docs satisfies it. The
tests in section 4 and a reader do the rest.

**Why it exists, and its cost.** A rule that no check enforces stops being
followed (the `ci` skill, failure c). Since the repository skills were added
(`8825aea`), 14 commits on `main` touched a mapped area. Checked one at a time,
7 of them fail. One published the site and both demos to Pages, and their docs
followed two commits later (`2ab946b`). The other six would have needed a docs
change or a `Docs-Impact` line. So expect about half of code changes to need
one or the other. One line with a reason costs less than a page that is wrong
on the live site.

When you move a folder or a doc, update `AREAS`: `tests/test_docs_impact.py`
fails when a pattern matches no file, and when an area is missing from the
table in section 2.

## 7. What only reading can check

No test reads the meaning of a sentence. After the generators and tests pass,
search the docs for every name you changed and read every hit:

```bash
grep -rn "OLD_NAME" docs/ README.md CLAUDE.md AGENTS.md examples/ \
  apps/*/README.md apps/*/docs/ services/*/README.md agents/*/README.md bindings/ocaml/README.md
```

Then check, against the code:

- **Descriptions.** The first paragraph of each endpoint in
  `docs/reference/api/*.md`: what it does, what it stores, which planner
  command it runs, which Guard boundary it passes. The **Errors** list: every
  status the handler can return.
- **Side effects and limits.** The "Limits" and "What it stores" sections of
  the module pages, the "What can go wrong" tables in the guide, and the limits
  in the demo READMEs. A fixed limit is removed from every page that states it.
- **Diagrams.** Mermaid diagrams in `docs/learn/`, `docs/guide/` and
  `docs/reference/api/` show calls the code makes. A diagram can draw and be
  wrong. Some PlantUML images in `docs/presentation/puml/` show steps the code
  does not have, such as an "escalate" Guard outcome. Use an image only where
  it matches the code.
- **Numbers and outputs in prose.** JSON outputs in `docs/learn/04-apis.md`,
  the commands and outputs the guide copies from real runs (curl calls,
  Postgres tests), and the AUC and gate numbers in `docs/demo/README.md` and
  the healthcare README. No test runs these; run the command again and paste
  the new output.
- **Recorded fixtures.** `docs/demo/expected/` holds recorded planner and
  service responses that no test compares. Record the affected ones again.
- **Deck descriptions.** The `description` and `note` of each entry in
  `resources.json`.
- **Rule tables.** The governance strategy table and the round rules in the
  guide, the learning guide and `docs/reference/api/dagentsc.md` restate
  planner rules. Check them against the OCaml module after a planner change.

## 8. Never

- **Never edit a generated file by hand** (section 3). Change the source and run
  the generator.
- **Never invent an example or an output.** API examples come from
  `capture_api_examples.py`; guide outputs come from running the script; other
  outputs are copied from a run you watched. If you cannot run it, say so in
  your report and leave the old output in place rather than writing a new one.
- **Never leave a page describing removed behaviour.** Delete or rewrite the
  section in the same change.
- **Never merge with a stale doc because "a follow-up will fix it".** The site
  publishes on the push. If the docs cannot be updated now, the code change is
  not ready.
- **Never use `Docs-Impact: none [...]` for a change that affects a page.**
- **Never weaken a docs test or the area map to make a change pass.** Fix the
  docs.
- **Never write the deployed API's address in a doc.** It lives in
  `env/.env.published` only.

## 9. Before you call the change done

```bash
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
.venv/bin/python scripts/service_inventory.py --check
.venv/bin/python scripts/api_reference.py --check
.venv/bin/python scripts/export_presentations.py --check
.venv/bin/python scripts/check_docs_impact.py --base origin/main
.venv/bin/python -m unittest discover -s tests -t . -v      # reference, examples, guide, resources, guards
cd site && npm run build && npm run check && cd -           # every page, diagram, link and download
```

Report what ran and the number of skipped tests, and list the docs you changed
next to the code they describe.
