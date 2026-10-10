# Learn Dagents

This guide explains the ideas behind Dagents and how to use it. Read the steps in
order. Each step has short background, diagrams, links to videos and papers, and
something to try.

| Step | Topic | What you will learn | Time |
|---|---|---|---|
| 1 | [Federated learning](01-federated-learning.md) | How models are trained without moving data | 1–2 h |
| 2 | [Privacy and data governance](02-privacy-and-governance.md) | How to decide what data a request may see | 1–2 h |
| 3 | [How Dagents is built](03-architecture.md) | The agents, the services and the planners | 1 h |
| 4 | [Using the APIs](04-apis.md) | Calling the governance and federation endpoints | 1 h |
| 5 | [Run it yourself](05-hands-on.md) | Running the demo and the tests on your machine | 1–2 h |

[References](references.md) lists every paper, video and tutorial used in these guides.

## Before you start

You need:

- Basic Python.
- A general idea of how a machine learning model is trained and evaluated.

You do not need to know OCaml. Step 3 explains why part of Dagents is written in it.

## Glossary

| Term | Meaning |
|---|---|
| Site | One place that holds data, such as a hospital. In Dagents a site runs a local agent (LMA). |
| Coordinator | The service that organizes a federated round. In Dagents this is the global agent (GMA). |
| Round | One cycle of work: the coordinator sends a task, sites compute locally, and sites send back results. |
| Aggregation | Combining the results from several sites into one, for example by averaging model weights. |
| Candidate | The model produced by aggregation. It is not used until it passes the release gates. |
| Release gate | A check a candidate must pass before release, such as a minimum accuracy or a fairness limit. |
| Classification | A list of fields and how sensitive each one is (low, medium or high). |
| Granularity | How much data a request asks for: a cell, a row, a column, a table, or a model update. |
| Guard | The component that applies a governance decision to real data and records it in an audit log. |
| Planner | A pure function, written in OCaml, that makes a decision from typed inputs. Dagents calls it through `dagentsc`. |

## Try the live demos

- [Stroke triage demo](https://pradyunuydarp.github.io/Dagents/healthcare-demo/): governance and federated rounds across three simulated hospitals.
- [NL2SQL demo](https://pradyunuydarp.github.io/Dagents/nl2sql-demo/): an app that uses the framework's validation and planning.

All patient data in the demos is synthetic.
