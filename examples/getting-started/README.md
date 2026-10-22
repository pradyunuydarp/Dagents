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

With the `dagents` package installed from PyPI in `.venv` instead, leave out
`PYTHONPATH=.` and `DAGENTSC_BIN`: Python then imports the installed package,
whose wheels for Linux x86_64, Linux aarch64 and macOS arm64 include the
planner. Steps 1 to 4 need `pip install dagents`, and step 5 needs
`pip install "dagents[lma,gma]"`
([Install the package](../../docs/guide/00-install.md#5-run-the-worked-example-on-the-package)).

```bash
.venv/bin/python examples/getting-started/step1_register.py
```

Steps 1 to 4 call framework classes in the script's own process. Step 5 runs
the same round through the Dagents agents: one LMA per store and one GMA,
called through their HTTP APIs. The agents run inside the script's process
(`AgentNetwork.in_process`), so it needs no server and no free port.

Step 5 can also reach agents that run as separate processes. Start the GMA and
one LMA per store with uvicorn from their settings, then run step 5 with
`SHOP_GMA_URL`, `SHOP_LMA_URLS`, `SHOP_ADMIN_TOKEN` and `SHOP_STORE_TOKENS`
set. It prints the same round. The commands are in
[Run the agents as separate processes](../../docs/guide/06-federated-rounds.md#run-the-agents-as-separate-processes).
Run step 5 once each time you start the agents, because the GMA refuses a
round id it has stored.

`tests/test_getting_started_example.py` starts the agents with the same
settings, on free ports, and checks what step 5 prints. A dry run of the
guide's commands, with stand-ins that record each process, checks that the
commands pass each port and token to the right setting. No test starts agents
on the guide's fixed ports, or runs the `dagents-gma` and `dagents-lma`
commands.

## What is where

| File | What it does | Dagents part it uses |
|---|---|---|
| `shop/contract.py` | The fields, their sensitivity, the label field, the purposes and the release gates | `FeatureContract`, `DataClassification`, `ConditionPack`, `ReleaseGate` |
| `shop/extension.py` | Registers all of that once, with the rule as a model kind and the orders as a site source | `DagentsExtension`, `ExtensionRegistry`, `ModelKind`, `SiteSourceFactory` |
| `shop/store.py` | One in-memory store per shop, with a running profile | `RunningProfile` |
| `shop/rule.py` | The rule being checked, its metrics, and `RuleKind`, which lets an LMA run it | Application code, and the `ModelKind` an LMA calls |
| `shop/store_site.py` | One shop's Guard and federated worker (the LMA role) | `GovernanceService`, `LocalFederatedWorker` |
| `shop/coordinator.py` | Plans the round and decides on release (the GMA role) | `FederatedRoundController`, `InProcessFederationEngine` |
| `shop/site_source.py` | The source an LMA reads a store's orders from, and the `shop_orders` factory that builds it for an LMA started from its settings | `SiteRecordSource`, `LMA_SITE_SOURCE` |
| `shop/agents.py` | Builds one LMA per store and one GMA, each with its own tokens, or reaches agents that run as separate processes | `create_app` of the LMA and the GMA, `AgentNetwork.in_process`, `AgentNetwork.over_http` |
| `step1_register.py` ... `step5_agents_over_http.py` | One script for each guide step that runs code: `step1` for guide Step 2, `step2` for Step 4, `step3` for Step 5, and `step4` and `step5` for Step 6 | |

Nothing in this folder is imported by the framework, and nothing in `dagents/`
knows what an order is.
