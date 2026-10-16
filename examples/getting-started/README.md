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
PYTHONPATH=. .venv/bin/python examples/getting-started/step5_agents_over_http.py
```

Steps 1 to 4 call framework classes in the script's own process. Step 5 runs
the same round through the Dagents agents: one LMA per store and one GMA,
called through their HTTP APIs. The agents run inside the script's process
(`AgentNetwork.in_process`), so it needs no server and no free port.

## What is where

| File | What it does | Dagents part it uses |
|---|---|---|
| `shop/contract.py` | The fields, their sensitivity, the label field, the purposes and the release gates | `FeatureContract`, `DataClassification`, `ConditionPack`, `ReleaseGate` |
| `shop/extension.py` | Registers all of that once, with the rule as a model kind | `DagentsExtension`, `ExtensionRegistry`, `ModelKind` |
| `shop/store.py` | One in-memory store per shop, with a running profile | `RunningProfile` |
| `shop/rule.py` | The rule being checked, its metrics, and `RuleKind`, which lets an LMA run it | Application code, and the `ModelKind` an LMA calls |
| `shop/store_site.py` | One shop's Guard and federated worker (the LMA role) | `GovernanceService`, `LocalFederatedWorker` |
| `shop/coordinator.py` | Plans the round and decides on release (the GMA role) | `FederatedRoundController`, `InProcessFederationEngine` |
| `shop/agents.py` | Builds one LMA per store and one GMA, each with its own tokens, and the source an LMA reads each store's orders from | `create_app` of the LMA and the GMA, `AgentNetwork`, `SiteRecordSource` |
| `step1_register.py` ... `step5_agents_over_http.py` | One script for each guide step that runs code: `step1` for guide Step 2, `step2` for Step 4, `step3` for Step 5, and `step4` and `step5` for Step 6 | |

Nothing in this folder is imported by the framework, and nothing in `dagents/`
knows what an order is.
