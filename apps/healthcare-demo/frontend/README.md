# Stroke triage demo: the frontend and its browser checks

This folder holds the demo's UI (Vite, React and TypeScript, in `src/`) and the
scripts that drive it in a real browser. A typecheck and a bundle only show
that the app compiles. These scripts show that the pages work.

| Script | What it drives | Where it runs |
|---|---|---|
| `smoke.mjs` | The live UI against a running backend: the Guard's controls, a full pilot, the hospital pages, data entry, both themes, the connecting banner | the `frontends` and `deploy-smoke` jobs of `ci.yml`, through `../scripts/run_frontend_demo.sh --check` |
| `replay-smoke.mjs` | The replay build: the page GitHub Pages publishes when the Pages workflow cannot use the deployed API | the `frontends` job of `ci.yml`, and the build job of `pages.yml` before the upload |
| `serve-dist.mjs` | Serves a built copy the way Pages does. `replay-smoke.mjs` uses it, and you can run it on its own to look at a build | |

Both checks use the same exit codes:

| Exit | Meaning |
|---|---|
| 0 | Every check passed |
| 1 | A check failed. The lines that start with `FAIL` say which |
| 2 | `SKIP`: no Chromium or no `playwright-core` was found |

Exit 2 is not a pass. CI fails a step that ends with it.

## The replay check

A replay build answers every request from `recording.json`, which
`scripts/capture_demo_recordings.py` records from the real backend with the
OCaml planner. The page finds each answer by its method, its path and, for a
POST, its exact body. It calls no backend.

`replay-smoke.mjs` serves the build with `serve-dist.mjs` under the base path
Pages uses (`/Dagents/healthcare-demo/`). The server answers a missing file
with 404, as Pages does, and closes every `/api/` request without an answer.
In Chromium, the check then:

1. reads the recording from the build and checks the banner: it says the page
   is a replay and names the recording's commit and date;
2. checks that the hospitals table, the header and "Who does what" show the
   recorded overview and status;
3. asks the Guard every combination the page offers: each hospital, both
   values of "Requester verified", each granularity in the list and each
   cohort size the recording holds. It changes one control between two asks,
   so a page that sends an old value of a control fails. Each answer must come
   from the chosen hospital's Guard, as the note under the decision and the
   "Dagents API calls" table name it. It must show the recorded decision,
   `systolic_bp` strategy and count of released records, and none may say
   "Not recorded";
4. asks for a cohort size the recording lacks and checks the "Not recorded"
   note, which lists the sizes it holds;
5. checks each hospital page and the consortium charts before the pilot, runs
   the pilot, and checks every release gate, the decision, the dispatch rows
   of every round and the audit chains. When the recorded gates allow a
   release, the approve button must be turned off with its reason. When they
   do not, there must be no approve panel;
6. checks that after the pilot, the consortium charts, each hospital's
   dashboard and its audit log show the answers recorded after it;
7. compares every chart, on the consortium page and on each hospital page,
   with the recorded SVG in the light theme, then in the dark theme;
8. checks that the data-entry form and the panel to add a hospital are turned
   off and say why, and that each hospital page lists the entered records the
   recording holds and says the replay cannot store an outcome;
9. fails on any API request, any request to another origin except the
   optional web fonts, and any console error or failed request.

When a check stops the run, the drive prints the first console errors and
requests it collected until then.

A run took 13 to 31 seconds on a machine with 4 CPUs that other jobs shared.

### Run it locally

From the repository root, with the planner built (`dune` is not on the PATH,
so use `opam exec --`):

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && cd -
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe

# Record, and check the recording. The capture writes public/recording.json.
.venv/bin/python scripts/capture_demo_recordings.py --demo healthcare   # --port 8090 if 8080 is busy
.venv/bin/python scripts/check_demo_recordings.py healthcare

# Build a replay copy outside dist/, then drive it.
cd apps/healthcare-demo/frontend
npm ci
DEMO_BASE_PATH=/Dagents/healthcare-demo/ VITE_DAGENTS_STATIC=1 VITE_HEALTHCARE_API_BASE= \
  npx vite build --outDir /tmp/healthcare-replay --emptyOutDir
node replay-smoke.mjs /tmp/healthcare-replay --base /Dagents/healthcare-demo/
```

The check finds Chromium through `CHROMIUM_PATH` first, then in a few common
places. To look at the same build yourself:

```bash
node serve-dist.mjs /tmp/healthcare-replay --port 11500
```

Then open <http://127.0.0.1:11500/Dagents/healthcare-demo/>. When you stop the
server with Ctrl-C, it lists every API request the page sent.

### When it fails

- **The banner check fails with "no banner".** The page did not load. The
  lines after `Stopped` usually show a 404 for a script or a stylesheet. The
  usual cause is a build whose `DEMO_BASE_PATH` differs from `--base`. When
  the first check fails instead, the build has no `recording.json`: record
  again before you build.
- **The Guard answers "Not recorded".** The page sends a request the recording
  does not hold. A key added to or removed from the probe body is the usual
  cause. Change `probe_body` in `scripts/capture_demo_recordings.py` together
  with `src/components/GuardProbe.tsx`, then record again.
  `tests/test_replay_wiring.py` compares the two bodies, and the pilot's,
  without a browser.
- **The Guard answers from another hospital, or with another request's
  answer.** The page sent an old value of a control, or picked the wrong
  recorded answer. Two causes are a control missing from the dependency list
  of the `useCallback` in `src/components/GuardProbe.tsx`, and a change to how
  `keyFor` in `src/api.ts` matches a body. The failure line names the control
  the drive changed before that ask.
- **A page shows an error banner.** The UI sends a GET the recording does not
  hold, for example after a change to a query string. The hospital-page paths
  are defined once, in `capture_demo_recordings.hospital_view_paths`.
- **A chart is not the recorded SVG.** The page asked for the wrong theme, or
  drew the chart from somewhere other than the recording.
- **The page sent an API request.** Something fetches from the API without the
  transport in `src/api.ts`, for example an `<img>` that points at a chart's
  address. A replay must not call any backend.

The check cannot see the approve button when the recorded gates reject the
candidate. It sees outcome buttons only when the recording holds an entered
record without an outcome, and then requires each button to be turned off.
The capture enters no records, so the drive checks the replay note above
those buttons, and its detail line says that there was no button to check.
