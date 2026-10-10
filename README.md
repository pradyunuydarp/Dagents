# Dagents

Dagents is a framework for running analytics and machine learning across many
data sources without moving the data. Each data source runs a local agent, a
global agent combines their results, and typed planners decide what each request
may see and whether a new model may be released.

It started as shared code from products such as Watchdog, and is meant to be
reused by systems such as Datalytics.

## Links

| | |
|---|---|
| **Framework site** | <https://pradyunuydarp.github.io/Dagents/>: the learning guide, the API reference and links to the demos |
| **Stroke triage demo** | <https://pradyunuydarp.github.io/Dagents/healthcare-demo/>: governance and federated rounds across three simulated hospitals |
| **NL2SQL demo** | <https://pradyunuydarp.github.io/Dagents/nl2sql-demo/>: an ordinary app that uses the framework |
| **Healthcare demo API** | <https://dagents-healthcare-api.onrender.com/api/v1/health>: the backend the stroke demo calls |

The stroke triage demo calls the deployed API. That API reads its patient records
from Postgres through the framework's source adapter, and makes every governance
and federation decision with the OCaml planner in its image. The page shows the
data source the API reports. All records are synthetic. The API sleeps when
idle, so the first request takes about 20 seconds, and the page shows a banner
while it waits.

The NL2SQL demo has no deployed backend. It replays responses recorded from the
real services and names the commit they came from. A question that was not
recorded gets no answer.

[`apps/healthcare-demo/docs/deployment.md`](apps/healthcare-demo/docs/deployment.md)
explains how the deployed parts fit together and where each credential is kept.

## Learn

The learning guide explains federated learning, data governance, the Dagents
architecture and the APIs, with diagrams, videos, papers and exercises. Read it
on the [framework site](https://pradyunuydarp.github.io/Dagents/#/learn) or in
[`docs/learn/`](docs/learn/README.md).

1. [Federated learning](docs/learn/01-federated-learning.md)
2. [Privacy and data governance](docs/learn/02-privacy-and-governance.md)
3. [How Dagents is built](docs/learn/03-architecture.md)
4. [Using the APIs](docs/learn/04-apis.md)
5. [Run it yourself](docs/learn/05-hands-on.md)

## What is in the repository

| Folder | Contents |
|---|---|
| `agents/` | The LMA (one per data source) and GMA (coordinator) services |
| `agents/common/extensions/` | The interface applications use to extend the framework |
| `bindings/ocaml/` | The planners: validation, DAG planning, model routing, Kubernetes manifests, governance, federated rounds |
| `services/core-service/` | Service catalog, topology, workload compilation, Kubernetes manifests |
| `services/pipeline-service/` | Pipeline registry, validation and runs |
| `services/model-service/` | Model training, checks and jobs |
| `services/spring-services/` | Spring Boot versions of the control and core APIs |
| `contracts/grpc/dagents/agents/v1/` | Shared protobuf contract for the agents |
| `apps/healthcare-demo/` | Stroke triage demo: governance and federated learning |
| `services/nl2sql-demo/` | NL2SQL demo: validation, planning and service checks |
| `site/` | The framework site |
| `design/` | The design system shared by the three frontends |
| `docs/` | The learning guide, architecture notes and the endpoint list |
| `env/` | Configuration for each service (no secrets) |

## Governance and federated rounds

These are the two parts an application would otherwise build itself:

- **Governance (GRAILS).** A planner decides the protection each field of a
  request needs, from three inputs: how sensitive the field is, how much the
  requester is trusted, and how much data is requested. The Ethical Guard
  applies the decision and writes it to an audit log. The planner is OCaml, so
  adding a sensitivity level or a strategy does not compile until every
  combination is handled. If the planner cannot be reached, the Guard denies the
  request.
- **Federated rounds.** Round manifests with a digest, site eligibility, quorum,
  aggregation readiness and release gates. Dagents decides who may take part and
  whether a result may be released. The training protocol itself belongs to a
  dedicated federated learning runtime behind an adapter. Aggregation produces a
  candidate model, never a release.

Applications use both through `agents/common/extensions`, by registering feature
contracts, data classifications, condition packs, pipeline steps and model
adapters. [`apps/healthcare-demo/`](apps/healthcare-demo/README.md) is a worked
example.

## Run it

Build the planner, then run the stroke triage demo locally (no Docker or
database needed):

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && cd -
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
apps/healthcare-demo/scripts/run_frontend_demo.sh
```

[Step 5 of the guide](docs/learn/05-hands-on.md) covers setup, tests and
exercises.

To run the whole stack in containers, with the committed configuration:

```bash
docker compose --env-file env/.env.compose up --build
```

Each service reads `env/.env.shared` plus its own file under `env/`.

## Further reading

- [`AGENTS.md`](AGENTS.md): the contributor guide
- [`docs/reference/service-inventory.md`](docs/reference/service-inventory.md): every endpoint, generated from the code
- [`docs/architecture/ocaml-adoption-plan.md`](docs/architecture/ocaml-adoption-plan.md): why the planners are in OCaml
- [`docs/integrations/datalytics-backend-guide.md`](docs/integrations/datalytics-backend-guide.md): moving the Datalytics backend onto Dagents
- `docs/diagrams/puml/` and `docs/presentation/puml/`: PlantUML sources and rendered diagrams

The stroke triage demo uses synthetic data only. It is not clinically validated
and not a medical device.
