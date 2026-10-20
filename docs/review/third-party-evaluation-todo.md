# Third-party evaluation: improvement TODO

This is a prioritized list of work that would make Dagents more useful to
people outside the project. It comes from an outside review of the repository
at commit `57ab963`, written by Claude (an AI assistant) at the request of the
repository owner. The reviewer did not build the code. This is not a security
audit.

`TODO.md` in the repository root lists work the project already plans. This
file does not repeat those items. Where this review agrees with one, the item
says so and adds acceptance criteria.

## How to read this file

Each item has a priority and a rough effort.

| Priority | Meaning |
|---|---|
| P0 | Blocks outside use or a main claim of the project. Do first. |
| P1 | Raises trust or lowers adoption cost a lot. Do next. |
| P2 | Useful. Do when the P0 and P1 items are in progress. |

| Effort | Meaning |
|---|---|
| S | A day or two |
| M | About a week |
| L | More than a week |

Every item has **Finding** (what was observed, with a file or command where
possible), **Work** (what to do) and **Done when** (a check someone else can
run).

## What this review covered and did not cover

Covered:

- The README, `CLAUDE.md`, the learning guide, the guide to building an
  application, the architecture notes and `TODO.md`.
- The repository layout, the CI workflows, the packaging files and the tracked
  files in the repository root.
- Specific code: `dagents/common/infrastructure/dagents_runner.py`,
  `dagents/common/application/ethical_guard.py`, the sizes of the OCaml
  modules and test file, and a search of the agents and framework services for
  authentication code.
- Published material on other federated learning and federated analysis
  tools, listed under References.

Not covered:

- No test suite was run. The review environment had neither the OCaml
  toolchain nor the Python dependencies.
- The demos and the published sites were not exercised in a browser.
- No security testing was done, and the OCaml planner code was not read line
  by line.
- The prior-art search was short. Other projects that overlap with Dagents may
  exist.

Treat each finding as a claim to confirm against the code before acting on it.

## Summary

| ID | Item | Priority | Effort |
|---|---|---|---|
| A1 | Publish an installable Python package | P0 | M |
| A2 | Ship the planner as a prebuilt binary | P0 | M |
| A3 | Define the core and mark the rest optional | P1 | S |
| A4 | Remove or justify the duplicate implementations | P1 | M |
| A5 | Publish planner conformance vectors | P2 | M |
| B1 | Ship one real federation runtime adapter | P0 | L |
| B2 | Keep round state across restarts | P1 | M |
| B3 | Add privacy accounting and secure aggregation | P1 | L |
| C1 | Write a threat model | P0 | M |
| C2 | Authenticate and authorize service calls | P0 | L |
| C3 | Sign manifests, bind site identity and anchor the audit chain | P1 | M |
| C4 | Harden the planner call | P1 | S |
| D1 | Publish a comparison with existing tools | P0 | S |
| D2 | Measure the cost of governance | P1 | M |
| D3 | Run a cold-start test of the guide | P1 | S |
| E1 | Add property tests for the planner rules | P1 | M |
| E2 | Add Python and OCaml lint, format and type checks to CI | P1 | S |
| E3 | Manage dependencies | P2 | S |
| E4 | Report test coverage | P2 | S |
| F1 | Clean the repository root | P1 | S |
| F2 | Remove absolute paths from the docs | P1 | S |
| F3 | Add project policy files | P2 | S |
| F4 | Make a first release | P1 | S |
| F5 | Make the site readable without JavaScript | P2 | M |
| G1 | Extend the Python-versus-OCaml decision record | P1 | M |
| G2 | Map the Guard boundaries to published controls | P2 | M |

## A. Adoption

### A1. Publish an installable Python package (P0, M)

**Finding.** The repository has no root `pyproject.toml`. The only one is
`apps/healthcare-demo/pyproject.toml`. Each service has its own
`requirements.txt`. The guide says Dagents is not published as a package and
tells readers to clone the repository and put its root on `PYTHONPATH`.

**Work.**

- Choose the smallest useful import surface. The Guard, the federation
  controller, the extension registry, the domain models and the planner bridge
  are the candidates (`dagents/common/`).
- Add a root `pyproject.toml` that builds that surface as a package with a
  version and a declared Python range.
- Publish to TestPyPI first, then PyPI.
- Change `examples/getting-started/` and the guide to import from the
  installed package.

**Done when.** In a clean virtual environment, `pip install` of the package
followed by the steps of `examples/getting-started/` gives the output shown in
`docs/guide/`. No `PYTHONPATH` setting is needed.

**Status.** Published to PyPI as `dagents` 0.1.0a1, an alpha. The package
`agents` was renamed to `dagents`, because another project owns `agents` on
PyPI. A root `pyproject.toml` builds the whole `dagents/` package (without its
tests), and `.github/workflows/release.yml` publishes each tagged release to
TestPyPI and then PyPI. `scripts/smoke_installed_package.py` checks the "done
when" condition on every pull request (CI's `packaging` job) and in every
release. The guide's install page (`docs/guide/00-install.md`) installs the
package and runs the worked example on it without `PYTHONPATH`. Step 1 keeps
the checkout route.

### A2. Ship the planner as a prebuilt binary (P0, M)

**Finding.** Running the Guard needs the `dagentsc` binary. The README tells
readers to build it with `opam` and `dune`. CI already builds the binary and
shares it between jobs, so the build is automated for the project but not for
users. Without the binary the Guard denies every request, as designed.

**Work.**

- Build `dagentsc` for Linux (x86-64 and arm64) and macOS (arm64 and x86-64)
  in a release workflow, and attach each file and its SHA-256 checksum to a
  GitHub release.
- Add a platform-specific Python wheel that contains the binary, or a command
  that downloads and verifies it, so `pip install` gives a working Guard.
- Keep the source build as the documented route for contributors.

**Done when.** On a machine with no OCaml installed, a fresh install runs the
Guard tests in `dagents/tests/test_governance.py` and none of them skip.

**Status.** Published with 0.1.0a1. The release workflow builds `dagentsc`
for Linux x86-64, Linux arm64 and macOS arm64, bundles it in a platform wheel
at `dagents/_bin/dagentsc`, and attaches each binary and `SHA256SUMS` to the
GitHub release. The runner finds the bundled binary after `DAGENTSC_BIN` and
before PATH. macOS x86-64 is not built yet, so that platform installs from the
sdist without a planner. The
Guard tests are in `dagents/tests`, which the wheel does not ship; the smoke
test checks Guard decisions from the installed package instead.

### A3. Define the core and mark the rest optional (P1, S)

**Finding.** The README lists the LMA and GMA agents, three framework services,
Spring Boot versions of two of them, an NL2SQL demo, a healthcare demo, a
Kubernetes manifest compiler and a site. The guide offers three ways to build
on the framework. Path A (call the services over HTTP) gives no Guard on the
reader's data, and the guide says so. A new reader cannot tell from the README
which part is the reason to use Dagents.

**Work.**

- Write one paragraph that names the core: governed reads and sends at each
  site, federated round control, and release gates.
- Mark the pipeline, model and manifest services, the Spring services and the
  NL2SQL demo as optional in the README repository table.
- Decide whether the optional parts stay in this repository, move to a
  `contrib/` folder or move to their own repositories.

**Done when.** The first screen of the README states one purpose and a table
marks each folder as core or optional.

### A4. Remove or justify the duplicate implementations (P1, M)

**Finding.** Two kinds of duplication exist.

- `services/spring-services/` provides Spring Boot versions of the control and
  core APIs. A test (`tests/test_service_inventory.py`) keeps them in step with
  the Python services.
- `core-service` renders manifests with the OCaml compiler when the binary is
  available and with a Python fallback otherwise (`CLAUDE.md`). The two once
  drifted apart unnoticed.

`CLAUDE.md` already gives the rule: behaviour with two implementations needs
the same assertions run against both, and if they cannot be kept in step, one
should be deleted.

**Work.**

- List who uses each Spring service. If nobody does, archive it. If a JVM
  consumer needs it, generate both from one API description.
- Decide whether the Python manifest fallback is still needed. If it is, add a
  CI check that renders the same inputs with both and compares the YAML.

**Done when.** Each duplicate is either removed or covered by a test that
fails when the two outputs differ.

### A5. Publish planner conformance vectors (P2, M)

**Finding.** The planners are reached through JSON on standard input and
output. The API reference for `dagentsc` is generated from the code. There is
no language-neutral set of test cases that another implementation could run.

**Work.** Export the inputs and expected outputs of the governance and
federation planner tests as JSON files under a `conformance/` folder, with a
script that runs them against any command that follows the `dagentsc`
interface.

**Done when.** The script passes against `dagentsc` in CI, and a second
implementation (even a small Python one) can be checked with the same files.
This also supports item G1.

## B. Federation runtime

### B1. Ship one real federation runtime adapter (P0, L)

**Finding.** The in-process engine is a simulator. The healthcare demo uses its
own parameter-averaging code that stands in for a runtime such as Flower. Until
a real runtime runs behind `FederationEngine`
(`dagents/common/application/federation.py`), the claim that Dagents governs
existing federated learning runtimes is untested. `TODO.md` lists an adapter
for NVIDIA FLARE.

**Work.**

- Start with Flower, which has a small simulation mode, and keep FLARE as the
  second adapter.
- Write contract tests for `FederationEngine` that both the in-process engine
  and the adapter must pass.
- Run the stroke demo's eight rounds through the adapter and compare the
  results with the in-process run.
- Document how each Guard boundary (`before_train`, `before_read`,
  `before_send`, `before_release`) maps to the runtime's client and server
  hooks.

**Done when.** The demo runs with a setting that selects the adapter, the
contract tests pass for both engines, and guide step 6 describes the adapter.

### B2. Keep round state across restarts (P1, M)

**Finding.** The LMA and GMA keep state in memory. A restart loses site
enrolments, rounds and their audit logs (the guide's limits section).
`TODO.md` lists durable round state.

**Work.** Define a store interface for rounds, contributions and the release
audit chain. Implement it for PostgreSQL, reusing the role-scoped access in
`dagents/common/infrastructure/postgres_roles.py`.

**Done when.** A test stops the GMA in the middle of a round, restarts it and
finishes the round. The release decision matches an uninterrupted run, and the
audit chain verifies.

### B3. Add privacy accounting and secure aggregation (P1, L)

**Finding.** `TODO.md` lists both. The `add_noise` strategy in
`dagents/common/application/ethical_guard.py` says in its documentation that it
is not a calibrated differential-privacy mechanism, because that needs a
privacy accountant. The strategy name can still suggest more than it does.

**Work.**

- Use an existing accounting library instead of writing one. Record the
  privacy budget per site and per round, and block a contribution that would
  exceed it.
- Rename `add_noise`, or label it in every output, until it is backed by an
  accountant.
- Pick a secure aggregation protocol from the runtime in B1 rather than
  building one in the Guard.

**Done when.** A test shows a site's contribution blocked after the budget is
used up, and the audit record shows the budget spent.

## C. Security and trust

### C1. Write a threat model (P0, M)

**Finding.** The docs describe what each Guard boundary checks. They do not
say what an attacker could still do. A project that sells governance needs a
document that states the adversaries and the limits.

**Work.** Add `docs/architecture/threat-model.md` with:

- The assets: site records, model updates, audit logs, the planner binary and
  release decisions.
- The adversaries: a curious coordinator, a malicious site, a malicious
  requester and a replaced planner binary.
- For each Guard boundary, what it stops and what it leaves open.
- A list of attacks to analyze, because the Guard does not obviously cover
  them: repeated requests that difference small cohorts, inference of records
  from clipped model updates, and leakage through counts and rates that are
  allowed to cross the boundary.
- A short "not covered" list.

**Done when.** The document exists, each open item links to an issue, and the
README links to it.

### C2. Authenticate and authorize service calls (P0, L)

**Finding.** A text search of the Python code in `dagents/`, `services/` and
`apps/` (tests excluded) for authorization headers, API keys, bearer tokens,
JWT, TLS and mTLS found only database role settings. The one request header
the healthcare demo reads is an `Idempotency-Key`, which is not a credential.
The Spring control service's `pom.xml` lists the web, actuator, validation and
test starters, with no security starter. The NL2SQL demo backend allows every
origin (`services/nl2sql-demo/backend/app/main.py`). That demo only replays
recordings, but the agents are meant to run at separate organizations. A text
search does not prove that no check exists, for example in a proxy in front of
the services, so confirm this against a running deployment.

**Work.**

- Give each site a credential tied to its site id, and check it on every call
  between the GMA and an LMA, in both directions.
- Use mutual TLS or short-lived signed tokens.
- Reject a result whose site id does not match the credential that sent it.
- Document the settings for a deployment, and keep the demo's open settings
  out of the deployment defaults.

**Done when.** A request without a credential returns 401. A test shows that a
credential for site A cannot submit a result for site B.

### C3. Sign manifests, bind site identity and anchor the audit chain (P1, M)

**Finding.** `TODO.md` lists signing manifests and moving the audit log to
append-only storage. The round digest detects drift but proves no origin. The
audit chain shows tampering in a test but does not prevent it.

**Work.**

- Have the coordinator sign each round manifest. Have each LMA verify the
  signature before it accepts a job. Document how keys are distributed.
- Sign release artifacts (the planner binary and the package) with a
  transparency-log tool such as Sigstore.
- Publish the head digest of each audit chain to a place the site does not
  control, on a schedule, so that rewriting history is detectable.

**Done when.** A test shows an LMA refusing a manifest with a bad signature,
and a second test shows a verifier detecting a rewritten audit chain from the
published head.

### C4. Harden the planner call (P1, S)

**Finding.** `dagents/common/infrastructure/dagents_runner.py` runs `dagentsc`
with `subprocess.run` and no `timeout`. A planner that hangs blocks the caller
with no limit. The runner already maps a missing binary, a non-zero exit and
invalid JSON to `PlannerUnavailableError`, and
`test_unreachable_planner_denies_rather_than_permits` covers one case. I found
no planner version or binary hash among the fields of the audit record.

**Work.**

- Add a timeout and a cap on output size, and treat both as a planner failure
  that the Guard answers with a denial.
- Check which of these cases have a test and add the missing ones: binary
  missing, non-zero exit, timeout, truncated output, invalid JSON, oversized
  input and an unknown strategy name.
- Record the planner version and the SHA-256 of the binary in every audit
  record. Check the hash against a pinned value at start-up when the operator
  sets one.

**Done when.** Each failure case produces a denial and an audit record in a
test, and an audit record names the planner that decided.

**Status.** Done. `dagents_runner.py` runs `dagentsc` with a timeout
(`DAGENTSC_TIMEOUT_SECONDS`, default 10 seconds) and caps on its input and
output (`DAGENTSC_MAX_INPUT_BYTES` and `DAGENTSC_MAX_OUTPUT_BYTES`, 16 MiB
each). The defaults are far above the slowest (50 ms) and largest (11 KB in,
15 KB out) of about 8,100 planner calls measured on 19 October 2026 across
the repository's suites, demos and examples. The runner kills a planner that
runs or prints past a limit, with every process it started, and an LMA or GMA
refuses to start with a limit that is zero, negative or not a number.
`dagents/tests/test_planner_failures.py` shows a denial and an audit record
for each case in the list, and for a pinned hash that differs. An unknown
strategy now denies the whole request; before, the Guard withheld that field
and applied the rest of the plan. A plan the Guard fails to apply to the
records is a denial too; before, `enforce` raised and wrote no record. A
planner answer that nests more than 200 levels deep, or a restriction plan
that fails validation, is a planner failure too, on every Python version the
package supports. Every audit record names its planner:
`planner_version` is the `dagents` version, because the binary prints none,
and `planner_sha256` is the binary's SHA-256. `DAGENTSC_SHA256` pins the hash.
The LMA and GMA compare it when they are built and log a warning on a
mismatch, and the runner compares it before each call and starts no other
binary. Audit chains stored before this change still verify, in memory and in
Postgres; the tests check a chain written by the code at `905ed28`.

## D. Evidence that the framework works

### D1. Publish a comparison with existing tools (P0, S)

**Finding.** Federated learning runtimes (Flower, NVIDIA FLARE, OpenFL, FATE,
Substra) and federated analysis tools with disclosure control (DataSHIELD,
vantage6) are established. Published comparisons place NVIDIA FLARE for
hospital-scale production, Flower for research and fast iteration, and Substra
for compliance-focused work (see References). A reader needs to know where
Dagents sits among them. The learning guide names Flower and NVIDIA FLARE as
runtimes that can plug in and links tutorials for them. It does not compare
Dagents with other tools. The learning guide, the application guide and the
README do not mention Substra, DataSHIELD, vantage6, OpenFL or FATE.

**Work.** Add a page under `docs/learn/` and list it in `ORDER` in
`site/src/learn.ts`. It holds a table of capabilities, one column per tool.
Suggested rows: where policy is checked (read, send, release), audit
log, training runtime included, language, deployment model and maturity. Cite
a source or a file for every cell, and date the page. Say where Dagents is
behind as well as where it differs. Check the authorization features of FLARE
and Substra first, because they may overlap with the Guard.

**Done when.** Every cell has a source, and the page names a case where each
other tool is the better choice.

### D2. Measure the cost of governance (P1, M)

**Finding.** Two costs are described but not published as numbers. The first is
model quality. The Guard trains on the guarded payload instead of raw records,
which `CLAUDE.md` says costs some accuracy that the healthcare demo measures.
The second is latency. Every governance decision starts a `dagentsc` process
and passes JSON through it.

**Work.**

- Add a script that reports AUC for four cases on the stroke data: centralized
  training, each site alone, federated with the Guard off and federated with
  the Guard on, across the Guard strategies.
- Measure planner latency per call (p50 and p99) and the request rate one agent
  can sustain.
- If the latency is a problem, add a long-running planner mode that avoids the
  process start.

**Done when.** A script reproduces a table in the docs from a clean checkout,
and the table states the hardware used.

### D3. Run a cold-start test of the guide (P1, S)

**Finding.** The guide's code is checked against the example by tests. No test
shows that a new reader can follow it.

**Work.** Ask one or two developers who have not worked on the project to build
the getting-started application from the guide alone. Record the time taken
and each place they got stuck. Fix those places in the guide.

**Done when.** A developer with no prior contact completes the guide in a time
you set in advance, and the notes from the sessions are in the repository.

## E. Test and build quality

### E1. Add property tests for the planner rules (P1, M)

**Finding.** The OCaml tests are 31 functions in one file,
`bindings/ocaml/test/test_compilers.ml` (1,219 lines). They check chosen
examples. I found no property-based testing library in the OCaml or Python
dependencies. The core rules in the docs are statements about all inputs, so
property tests fit them.

**Work.**

- Add QCheck for OCaml and Hypothesis for Python.
- Write properties for the rules in the docs: a more sensitive field never gets
  weaker protection; lower trust never gets weaker protection; a larger
  request never gets weaker protection; the same input always gives the same
  plan; aggregation never gives a release; a missing gate metric always blocks
  a release.
- Add a round-trip property for the key conversion in `dagents_runner.py`,
  which depends on a list of data keys that pass through unchanged.

**Done when.** CI runs the properties on every pull request, and each rule in
the guide's strategy and round tables names the property that covers it.

### E2. Add Python and OCaml lint, format and type checks to CI (P1, S)

**Finding.** The checks differ by language.

| Language | What CI checks today | Gap |
|---|---|---|
| TypeScript | The `frontends` job runs `npm run build` for the site and both demo frontends. Each build script runs `tsc -b` before `vite build`, so types are checked. | No linter or formatter: the three `package.json` files list no ESLint, Prettier or Biome. |
| Python | I found no linter, formatter or type checker in `.github/workflows/` or in `apps/healthcare-demo/pyproject.toml`. | All three. |
| OCaml | The `planners` job builds and tests. There is no `.ocamlformat` file. | Format check. |

**Work.** Add a `lint` job:

- Ruff for Python, and a type checker (mypy or pyright) on `dagents/common/`.
- `ocamlformat --check` for OCaml.
- Optionally, a linter and formatter for the TypeScript code. Do not add a
  second type check there.

Fix or baseline the existing findings, then make the job required in the `ci`
gate.

**Done when.** The `ci` gate job depends on `lint`, and a deliberate style
error and a deliberate type error in a test branch each fail it.

### E3. Manage dependencies (P2, S)

**Finding.** The Python requirements files mix exact pins (for example FastAPI
and Uvicorn) with ranges (for example Pydantic). The three frontends have a
`package-lock.json`. I found no lock file for the Python services or the OCaml
planner, no automated update service and no vulnerability scan in
`.github/workflows/`.

**Work.** Add Dependabot or Renovate for pip, npm, Maven, GitHub Actions and
Docker. Generate lock files with hashes for the deployable Python services. Add
`pip-audit` and an SBOM step to the release workflow. Commit an `opam` lock
file for the planner.

**Done when.** A new vulnerable dependency in a test branch fails a CI job.

### E4. Report test coverage (P2, S)

**Finding.** The CI guards check that tests ran and did not skip. They do not
show how much code the tests reach.

**Work.** Add `coverage.py` for the agents and `bisect_ppx` for the planners.
Report the numbers on pull requests. Set a minimum for the governance and
federation modules only.

**Done when.** A pull request shows coverage for those modules, and a drop
below the minimum fails the job.

## F. Repository and documentation hygiene

### F1. Clean the repository root (P1, S)

**Finding.** These files sit in the root or `notebooks/` and are tracked:

| File | Size | Note |
|---|---|---|
| `predictions.json` | 420 KB | Output file; one notebook writes a file with this name |
| `predictions (1).json` | 33 KB | A second copy with a browser-download suffix |
| `nlsql-debug.md` | | A debug note |
| `Project Report_Dagents project.docx` | 3.2 MB | A report in a binary format |
| `dagents-project-presentation.pdf` | 865 KB | Listed under `excluded` in `docs/presentation/resources.json` as an earlier version of the deck |
| `notebooks/googleflant5base_(1).ipynb`, `notebooks/notebookb925e4cb38 (1).ipynb` | | Download-style names |

**Work.**

- Move evaluation outputs to a folder with a README that says how they were
  produced, or delete them if they can be regenerated.
- Attach the report and the old deck to a release, or move them to
  `docs/reports/archive/`.
- Rename the notebooks.
- If the old PDF is removed, remove its `excluded` entry in `resources.json`
  and run `tests/test_site_resources.py`.

**Done when.** The root holds only the README, license, contributor files,
configuration and top-level folders.

### F2. Remove absolute paths from the docs (P1, S)

**Finding.** Two Markdown files link to absolute paths under a macOS user
home folder, which work only on the author's computer:

- `docs/architecture/ocaml-adoption-plan.md` (the three images and three source
  links, lines 30 to 42, do not load for other readers).
- `docs/integrations/datalytics-backend-guide.md` (lines 140 to 154).

**Work.** Replace each with a relative link. The diagrams are in
`docs/diagrams/puml/`. Add a test that fails when a tracked Markdown file
contains an absolute path to a user home folder or a Windows drive. Write the
test's patterns so that this file, which describes the problem, does not match.

**Done when.** The test passes, and `npm run check` in `site/` finds no broken
image or link in these pages.

### F3. Add project policy files (P2, S)

**Finding.** The repository has no `SECURITY.md`, `CONTRIBUTING.md`,
`CODE_OF_CONDUCT.md`, issue templates or pull request template. `AGENTS.md`
is a contributor guide, but GitHub does not surface it as one.

**Work.** Add a `SECURITY.md` with a private reporting route and a response
time. Add a short `CONTRIBUTING.md` that points to `AGENTS.md`. Add issue and
pull request templates, with the docs-impact line as a checklist item.

**Done when.** GitHub's community profile for the repository shows these files.

### F4. Make a first release (P1, S)

**Finding.** The GitHub page lists no releases. Users cannot pin a version, and
`CHANGES.md` holds insights, progress notes and findings. It is not a list of
changes by version.

**Work.** After A1 and A2, tag `v0.1.0`, publish the package and the binaries,
and keep a change log that lists user-visible changes per version. State the
stability promise for the Guard and planner JSON contracts.

**Done when.** A release page lists the package, the binaries, their checksums
and the changes.

**Status.** Partly done. `v0.1.0a1` is the first release, a pre-release. Its
GitHub Release lists the sdist, the three platform wheels, the three planner
binaries and `SHA256SUMS`, and the package is on PyPI. The release notes link
to PyPI, describe the files, and then give GitHub's generated list of merged
pull requests. `dagents/lma/CHANGELOG.md` and `dagents/gma/CHANGELOG.md` list
user-visible changes to the agents, but only under "Unreleased", with no
section for 0.1.0a1. There is no change log by version for the whole package
yet. The README says that the Python APIs, settings, routes and package layout
can change before 1.0.0. It does not say what stability the Guard and planner
JSON contracts have.

### F5. Make the site readable without JavaScript (P2, M)

**Finding.** A fetch of the site root returned the title and meta tags but no
page text. The pages are drawn in the browser and live in the URL hash, so
every page shares one address. Search engines and tools that do not run
JavaScript see no content.

**Work.** Pre-render the home page and the guide pages to static HTML at build
time, and give each page its own path instead of a hash. Keep the link-preview
checks in `scripts/check_link_previews.py` working.

**Done when.** `curl` on the site root and on one guide page returns the page
headings and text.

## G. Positioning

### G1. Extend the Python-versus-OCaml decision record (P1, M)

**Finding.** `docs/architecture/python-vs-ocaml-planning-comparison.md` is 36
lines and covers pipeline validation, dataset planning and manifest rendering.
It does not cover the governance and federation planners, which are the
reason for the OCaml layer. It does not mention typed Python (exhaustiveness
checking with a type checker) or policy engines such as OPA or Cedar. The
governance planner is 232 lines and the federation planner is 284 lines, so
a side-by-side build is practical.

**Work.**

- Implement the strategy selection of `governance_compiler` in typed Python and
  in one policy language.
- Compare what each catches before runtime when a new sensitivity level or
  strategy is added, and what each costs to build, ship and run (toolchain,
  binary, process start, contributor skills).
- Write the result as a decision record that states the decision and the
  conditions that would change it.

**Done when.** The record includes working code for each alternative and
measured numbers, and the existing comparison links to it.

### G2. Map the Guard boundaries to published controls (P2, M)

**Finding.** DataSHIELD describes its protections against the Five Safes
framework. Dagents describes its boundaries in its own terms. Users in
regulated settings ask which controls a tool helps with.

**Work.** Add a table that maps each Guard boundary and audit feature to the
controls it supports in the Five Safes framework and in the access-control and
audit parts of the regulations your target users face. State that the table
describes support for controls and is not a compliance claim, and have a
person with regulatory expertise review it.

**Done when.** The table is published and reviewed, and each row points to the
code or test that implements the control.

## Suggested order

| Phase | Items | Result |
|---|---|---|
| First month | A1, A2, C1, D1, F1, F2, E2 | An outside developer can install and run the Guard, and can see the limits |
| Months two and three | B1, C2, C4, A3, F4, D3, E1 | A real runtime behind the Guard, authenticated calls, a first release |
| After that | B2, B3, C3, D2, A4, G1, E3, E4, F3, F5, A5, G2 | Production gaps closed and the design choices measured |

## References

- Gupta, R., et al. *Benchmarking Federated Learning Frameworks for Medical
  Imaging Deployment: A Comparative Study of NVIDIA FLARE, Flower, and Owkin
  Substra.* arXiv:2511.00037. <https://arxiv.org/abs/2511.00037v1>
- Avraam, D., et al. *DataSHIELD: mitigating disclosure risk in a multi-site
  federated analysis platform.* Bioinformatics Advances, 2025.
  <https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11968321/>
