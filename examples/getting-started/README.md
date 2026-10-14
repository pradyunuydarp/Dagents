# Getting started: two stores check a returns rule together

A small application built on Dagents. Two stores, `store-north` and
`store-south`, each keep their own orders. Together they measure how well a
rule finds orders that are likely to be returned, without sending an order to
each other or to the coordinator. The orders in `data/` are made up.

The [guide](../../docs/guide/README.md) explains every file here, step by step.
Its code blocks are copied from these files, and
`tests/test_getting_started_example.py` fails if a copy differs or if a script's
output no longer matches the guide.

## Run it

From the repository root, with the planner built and the virtual environment
set up ([setup](../../docs/guide/01-setup.md)):

```bash
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
PYTHONPATH=. .venv/bin/python examples/getting-started/step1_register.py
PYTHONPATH=. .venv/bin/python examples/getting-started/step2_guard_check.py
PYTHONPATH=. .venv/bin/python examples/getting-started/step3_profile.py
PYTHONPATH=. .venv/bin/python examples/getting-started/step4_federated_round.py
```

## What is where

| File | What it does | Dagents part it uses |
|---|---|---|
| `shop/contract.py` | The fields, their sensitivity, the purposes and the release gates | `FeatureContract`, `DataClassification`, `ConditionPack`, `ReleaseGate` |
| `shop/extension.py` | Registers all of that once | `DagentsExtension`, `ExtensionRegistry` |
| `shop/store.py` | One in-memory store per shop, with a running profile | `RunningProfile` |
| `shop/rule.py` | The rule being checked and its metrics | Application code only |
| `shop/store_site.py` | One shop's Guard and federated worker (the LMA role) | `GovernanceService`, `LocalFederatedWorker` |
| `shop/coordinator.py` | Plans the round and decides on release (the GMA role) | `FederatedRoundController`, `InProcessFederationEngine` |
| `step1_register.py` ... `step4_federated_round.py` | One script for each guide step that runs code: `step1` for guide Step 2, `step2` for Step 4, `step3` for Step 5 and `step4` for Step 6 | |

Nothing in this folder is imported by the framework, and nothing in `agents/`
knows what an order is.
