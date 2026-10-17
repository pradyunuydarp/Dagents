# Dagents

Dagents is a framework for running analytics and machine learning across many
data sources without moving the data. Each data source runs a local agent, a
global agent combines their results, and typed planners decide what each request
may see and whether a new model may be released.

It started as shared code from products such as Watchdog, and is meant to be
reused by systems such as Datalytics.

**Status: alpha.** Dagents is in early development. Every version before
1.0.0 is a pre-release, and the Python APIs, settings, routes and package
layout can change from one release to the next.

## Install

The Python framework is released to PyPI as
[`dagents`](https://pypi.org/project/dagents/), starting with 0.1.0a1. It needs
Python 3.11 or later.

```bash
pip install dagents                # the Ethical Guard, federated rounds, extensions and the planner runner
pip install "dagents[lma,gma]"     # adds FastAPI, uvicorn and httpx, to serve an LMA or a GMA
```

The other extras are `postgres` (the Postgres audit chain, job log, model
registry, lease and source adapter), `charts` (the SVG chart renderer), `compose`
(PyYAML, for the appliers that start a service from a Compose file) and `all`.
While every release is a pre-release, `pip install dagents` installs the
newest one. After 1.0.0, a pre-release needs `pip install --pre dagents` or an
exact version.

The wheels for Linux x86_64, Linux aarch64 and macOS arm64 include the
planner, `dagentsc`. On other systems, such as Windows and Intel Macs, pip
builds the package from its source distribution, which has no planner. There
the Ethical Guard denies every request until `DAGENTSC_BIN` names a planner
binary or `dagentsc` is on `PATH`. Each
[GitHub Release](https://github.com/pradyunuydarp/Dagents/releases) has the
planner for the three platforms, and
[`bindings/ocaml/`](bindings/ocaml/README.md) shows how to build it.
`python -m dagents` prints the installed version and the planner it found, and
exits with status 1 when it finds none.

`dagents-lma` and `dagents-gma` start an agent, by default on `127.0.0.1:8010`
and `127.0.0.1:8020`. An installed package reads its settings only from the
process environment, never from an env file; the [LMA](dagents/lma/README.md)
and [GMA](dagents/gma/README.md) READMEs list them. The services in
`services/`, the Spring services and the demos are not in the package.

The guide's [Install the package](docs/guide/00-install.md) page shows each
step with its output, including how to run the worked example on the package.

## Links

| | |
|---|---|
| **Framework site** | <https://pradyunuydarp.github.io/Dagents/>: the learning guide, the guide to building an application, the API reference and links to the demos |
| **Stroke triage demo** | <https://pradyunuydarp.github.io/Dagents/healthcare-demo/>: governance, federated training and data entry across three hospitals |
| **NL2SQL demo** | <https://pradyunuydarp.github.io/Dagents/nl2sql-demo/>: an ordinary app that uses the framework |

The stroke triage demo uses real, de-identified records from the
[International Stroke Trial](https://doi.org/10.7488/ds/104) (IST-1, 1991-1996):
1,014 patients from three hospitals in the United Kingdom, Italy and Switzerland.
Each hospital keeps its records in its own store, and the page has a view for
each hospital, with a form to enter a new record. Each hospital is served by a
Dagents LMA and the study by a Dagents GMA, and the demo calls them only through
their HTTP APIs; the page lists the agent calls behind each answer. The published page
calls the demo's deployed API, which runs the agents in its own process. The
agents make every governance and federation decision with the OCaml planner in
its image, and the page shows the data source the API reports.
The API sleeps when idle, so the first request takes about 20 seconds, and the
page shows a banner while it waits. If, when the site is built, the API does not
answer, is not reading Postgres, has a hospital store without records, or
refuses requests from the site, the page replays a recorded run instead and says
so.

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

The slide decks and reports we used to teach the framework are on the site's
[Resources page](https://pradyunuydarp.github.io/Dagents/#/resources), as
PowerPoint files and PDFs. The list is
[`docs/presentation/resources.json`](docs/presentation/resources.json).

## Build an application on Dagents

The [guide to building an application](docs/guide/README.md) shows, step by
step, how to use Dagents in your own code: install the package or clone the
repository, choose one of three ways to build on it, describe your data, give
each source its own store, govern every read, run a federated round, call the
framework services, deploy and test. Read it on the
[framework site](https://pradyunuydarp.github.io/Dagents/#/guide) or in
[`docs/guide/`](docs/guide/README.md). Its code comes from a small application
in [`examples/getting-started/`](examples/getting-started/README.md), and a
test checks that the two match.

## What is in the repository

| Folder | Contents |
|---|---|
| `dagents/` | The Python framework, packaged as `dagents` (`pyproject.toml`): the LMA (one per data source) and GMA (coordinator) services |
| `dagents/common/extensions/` | The interface applications use to extend the framework |
| `bindings/ocaml/` | The planners: validation, DAG planning, model routing, Kubernetes manifests and Docker Compose files, governance, federated rounds |
| `services/core-service/` | Service catalog, topology, workload compilation, Kubernetes manifests and Docker Compose files |
| `services/pipeline-service/` | Pipeline registry, validation and runs |
| `services/model-service/` | Model training, checks and jobs |
| `services/spring-services/` | Spring Boot versions of the control and core APIs |
| `contracts/grpc/dagents/agents/v1/` | Shared protobuf contract for the agents |
| `apps/healthcare-demo/` | Stroke triage demo: governance and federated learning |
| `services/nl2sql-demo/` | NL2SQL demo: validation, planning and service checks |
| `examples/getting-started/` | The worked example of the guide to building an application: two stores check a rule together |
| `site/` | The framework site |
| `design/` | The design system shared by the three frontends |
| `docs/` | The learning guide, the guide to building an application, architecture notes and the endpoint list |
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

Applications use both through `dagents/common/extensions`, by registering feature
contracts, data classifications, condition packs, model kinds, site sources,
pipeline steps and model adapters, and call the LMA and GMA through their APIs.
[`apps/healthcare-demo/`](apps/healthcare-demo/README.md) is a worked example.

## Run it

The demos are not in the package. They run from a clone of the repository:

```bash
git clone https://github.com/pradyunuydarp/Dagents.git
cd Dagents
```

The stroke triage demo needs OCaml with opam, Node.js 22 with npm, and a Python
virtual environment in `.venv` with the demo's requirements.
[Step 5 of the learning guide](docs/learn/05-hands-on.md) shows how to set up
each one. Then build the planner and run the demo locally (no Docker or
database needed):

```bash
cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe && cd -
export DAGENTSC_BIN=$PWD/bindings/ocaml/_build/default/bin/dagentsc.exe
apps/healthcare-demo/scripts/run_frontend_demo.sh
```

Step 5 of the learning guide also covers the tests and exercises.

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

The stroke triage demo's records come from the International Stroke Trial
database (version 2), made available under the
[ODC Attribution License](https://opendatacommons.org/licenses/by/1-0/); see
[`apps/healthcare-demo/data/ist/NOTICE.md`](apps/healthcare-demo/data/ist/NOTICE.md).
The demo's models are not clinically validated, and it is not a medical device.
