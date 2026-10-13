# Step 5: Run it yourself

## What you need

- Python 3.11 or newer
- Node.js 22 and npm, for the demo's web interface
- OCaml 5.1 with opam, for the planner. See [Learn OCaml](https://ocaml.org/docs) for installation.

No database, GPU or Docker is needed for anything on this page.

## 1. Get the code

```bash
git clone https://github.com/pradyunuydarp/Dagents.git
cd Dagents
```

## 2. Build the planner

```bash
opam install dune yojson
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && cd -
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
```

`DAGENTSC_BIN` tells the Python services where the planner is. Without it, the
Guard cannot reach the planner and denies every request.

## 3. Call the planner directly

The services talk to the planner by sending it JSON. You can do the same:

```bash
$DAGENTSC_BIN governance restrict \
  --input docs/learn/examples/restriction-request.planner.json
```

This file is the request from Step 4 with camelCase keys, which is the format the
planner reads. The Python services convert keys for you. The output gives one
strategy per field, with the reason:

```json
{"field": "diagnosis", "sensitivity": "high", "strategy": "generalize:1",
 "reason": "sensitivity=high trust=high granularity=row -> generalize:1"}
```

Run `$DAGENTSC_BIN --help` to see the other commands.

## 4. Set up Python

```bash
python3 -m venv .venv
.venv/bin/pip install -r agents/lma/requirements.txt -r agents/gma/requirements.txt \
  -r apps/healthcare-demo/requirements.txt httpx
```

## 5. Run the stroke triage demo

```bash
apps/healthcare-demo/scripts/run_frontend_demo.sh
```

This starts the API on port 8080 and the web interface on port 5174, then prints
what to try. Open <http://localhost:5174>. Locally each hospital keeps its
International Stroke Trial records in memory, loaded from
`apps/healthcare-demo/data/ist/`, so the demo needs no database. Records you
enter are lost when you stop it. Press Ctrl-C to stop it.

## 6. Run the tests

```bash
# Framework agents and governance
.venv/bin/python -m unittest discover -s agents/tests -t .

# The stroke demo
cd apps/healthcare-demo && PYTHONPATH=../..:backend ../../.venv/bin/python -m unittest discover -s tests -t . && cd -

# The planners
cd bindings/ocaml && opam exec -- dune test && cd -
```

The governance and federation tests use the real planner. They look for it in
`DAGENTSC_BIN`, then in the dune build folder, and are skipped if it is missing.
The test summary shows how many were skipped.

The stroke demo's Postgres tests (the per-hospital stores and their isolation)
also skip unless you point them at a PostgreSQL 16 server with an admin login.
Each test creates and drops its own scratch database:

```bash
export HEALTHCARE_DEMO_TEST_DATABASE_URL=postgresql://admin:pw@127.0.0.1:5432/postgres
export DAGENTS_TEST_DATABASE_URL=$HEALTHCARE_DEMO_TEST_DATABASE_URL
```

## Exercises

### 1. Change a field's sensitivity

In [`apps/healthcare-demo/backend/app/domain/conditions.py`](../../apps/healthcare-demo/backend/app/domain/conditions.py),
find `STROKE_CLASSIFICATION` and change `"systolic_bp": "high"` to `"medium"`.
Restart the demo and run the Ethical Guard probe with a verified requester at row
granularity.

Expected: `systolic_bp` changes from `generalize:1` to `allow_full`, because the
rule for medium sensitivity, high trust and row granularity is `allow_full`. For
an unverified requester it changes from `redact` to `generalize:1`. No code
changed, only the classification.

### 2. Loosen a release gate

In the same file, find `STROKE_RELEASE_GATES` and change the margin of
`improves_on_current_release` from `0.02` to `0.005`. Run the pilot again with
`apps/healthcare-demo/scripts/run_pilot.sh`.

Expected: the gate passes (the candidate's AUC of 0.786 is above 0.777 + 0.005),
and so does every other blocking gate. The decision is still not `release`: it
is `another_round`. The advisory `alert_burden` gate still fails (853 alerts per
1,000 against a limit of 750). An advisory gate cannot block a release, but the
planner releases a candidate only when every gate passes, so it asks for another
round. The hospitals keep using `stroke-outcome-rule-v1`, and an attempt to
approve the candidate returns `409`.

Now also raise the `alert_burden` limit from `750.0` to `900.0` and run the pilot
again. Every gate passes and the decision is `release`. The candidate is still
not in use: approve it with `POST /api/v1/models/{version}:approve` (or the
approve button in the UI). After that, the worklists rank patients with the
logistic regression, and each assessment lists its three largest terms as the
basis.

Undo both changes afterwards.

### 3. Make a site ineligible

Follow the last part of [Step 4, walkthrough 2](04-apis.md#plan-a-federated-round):
give `ch-245` the old feature contract `stroke-triage-features-v2` and plan a
round. The site is excluded, and the round does not have enough sites to start
(`quorum_not_met`).

### 4. See exhaustive matching

The `granularity` type is declared in
[`dagents_common_ir.ml`](../../bindings/ocaml/lib/common_ir/dagents_common_ir.ml) and in its
interface file `dagents_common_ir.mli`, in the same folder. Add a new case to both,
for example `| DatasetGrain`. Run `opam exec -- dune build` in `bindings/ocaml`.

Expected: the build stops with "this pattern-matching is not exhaustive" in three
places: the JSON conversion in `common_ir`, the rule table `select_strategy`, and
the cohort check `cohort_sensitive_granularity` in the governance compiler. A new
granularity cannot be used until every rule says what to do with it. Undo the
change afterwards.

## Where to go next

- Read [`AGENTS.md`](../../AGENTS.md) for the full design and contributor rules.
- Browse the endpoints in [`docs/reference/service-inventory.md`](../reference/service-inventory.md).
- Read the stroke demo's [README](../../apps/healthcare-demo/README.md) for its design and limits.
- Build your own federated system with the [Flower tutorial](https://flower.ai/docs/framework/tutorial-get-started-with-flower-pytorch.html) or the [TensorFlow Federated tutorials](https://www.tensorflow.org/federated/tutorials/tutorials_overview).

Back to the [start of the guide](README.md).
