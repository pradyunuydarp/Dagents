# Presentation Talk Track

Target: 25 minutes total, with 20 minutes of presentation/demo and 5 minutes of Q&A.

## 0-3 min: Problem And Motivation

Dagents is a reusable framework for ML automation and data engineering. The system has local agents, global agents, model services, pipeline services, and workload generation.

The programming languages question is: where does functional programming help in this architecture?

Answer: in the deterministic planner layer. OCaml handles typed validation and plan generation, while Python and Java handle runtime APIs, model training, and integration.

## 3-6 min: Full App Architecture

Show `docs/demo/app-architecture-walkthrough.md`.

Explain:

- LMA runs source-scoped local work.
- GMA coordinates aggregate/fleet-wide work.
- Pipeline service registers and executes JSON workflows.
- Model service trains and evaluates models.
- Core service exposes topology and workload plans.
- OCaml `dagentsc` is used by services as a planner boundary.

## 6-9 min: Full Stack Runtime Demo

Run:

```bash
bash docs/demo/run_full_stack_demo.sh
```

If Docker is slow, show the script and saved outputs under `docs/demo/expected/`.

Explain that this proves the project is an app stack, not only isolated OCaml code.

## 9-16 min: Functional Programming Demo

Run:

```bash
bash docs/demo/run_functional_layer_demo.sh
```

Narrate each step:

1. Source validation rejects bad source specs before runtime I/O.
2. Extraction planning normalizes Postgres config into a source-independent plan.
3. Schema validation compares data against a contract.
4. Quality evaluation returns severity-aware blocking status.
5. Transform planning predicts output schema.
6. Transform application shows deterministic record transformation.
7. Pipeline planning orders a DAG.
8. Cycle rejection shows safety.
9. Model routing shows policy as a pure function.
10. Manifest rendering turns typed workload specs into Kubernetes YAML.

## 16-19 min: Integration Story

Show `agents/common/infrastructure/dagents_runner.py`.

Explain:

- Services call `dagentsc` through JSON.
- This avoids FFI complexity.
- The OCaml layer remains pure and testable.
- Python services can fall back where needed.

## 19-20 min: Programming Languages Takeaway

Summarize:

- Algebraic data types encode valid domain states.
- Pattern matching makes cases explicit.
- Pure functions make outputs deterministic.
- Process boundaries make polyglot integration practical.
- The result is a safer planner layer for a real ML/data-engineering app.

## Q&A Prep

### Is this a programming-language compiler?

No. “Compiler” here means domain-specific planner/translator. The modules translate Dagents source, pipeline, model, and workload specs into execution plans.

### Why not write all of this in Python?

Python is still used for APIs and ML. OCaml is used where static types and pattern matching reduce risk in planning logic.

### Why not use direct FFI?

Subprocess JSON keeps failure isolation simple, avoids embedding complexity, and makes contracts explicit.

### What is functional about it?

The core logic is expressed as pure transformations over immutable typed values: source specs become extraction plans, pipelines become ordered DAGs, and workload specs become manifests.

### What is the limitation?

The OCaml layer intentionally does not do runtime I/O or heavy ML. It plans and validates; services execute.
