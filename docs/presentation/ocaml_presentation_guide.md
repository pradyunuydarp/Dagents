# Dagents OCaml Functional Modules: Presentation Guide

This guide summarizes the `bindings/ocaml` directory in the Dagents project to help you prepare for your presentation. 

## 1. High-Level Concept: "The Functional Kernel"
The OCaml layer in Dagents is designed as a **deterministic functional kernel**. 
* **What it does:** It handles the complex logic of compiling, validating, and planning ML workloads, data pipelines, and Kubernetes deployments.
* **What it DOESN'T do:** It is **not** a long-running service. It doesn't handle database I/O, actual model training, or orchestration.
* **Why OCaml?** OCaml's strong type system and algebraic data types make it perfect for building reliable compilers and planners where all states and contracts must be explicitly defined.

## 2. Integration Model
Instead of messy direct bindings (like FFI), other services (Python, Spring Boot, etc.) interact with the OCaml layer via explicit boundaries. They call the OCaml CLI tool (`dagentsc`) and exchange data using standardized JSON contracts.

## 3. Module Breakdown (`bindings/ocaml/lib/`)
The logic is divided into specific compiler modules:

### `common_ir`
* **The Shared Contract Layer.** Defines the Intermediate Representation (IR).
* Contains the types and JSON codecs for everything exchanged between modules (dataset profiles, schema contracts, extraction plans, Kubernetes specs).

### `dataset_compiler`
* **The Data Engineering Kernel.**
* **Responsibilities:** Infers schemas from records, evaluates data quality rules, and compiles extraction/transformation plans. It plans data operations without actually executing the database I/O.

### `pipeline_compiler`
* **The Workflow Planner.**
* **Responsibilities:** Validates workflow DAGs (Directed Acyclic Graphs), rejects cycles, orders steps topologically, and assigns execution targets.

### `model_router`
* **The Decision Engine.**
* **Responsibilities:** Maps a dataset profile and task type to a specific ML model and deployment strategy (e.g., inline service call vs. Kubernetes job).

### `manifest_compiler`
* **The Kubernetes Translator.**
* **Responsibilities:** Takes typed workload specifications and deterministically renders the final Kubernetes YAML (Deployments, Jobs, Services, etc.).

## 4. The CLI Interface (`bin/dagentsc.ml`)
`dagentsc` is the command-line interface that exposes these functional modules. Services interact with it using JSON via stdin/stdout. 
* *Example:* `dagentsc manifest compile --input workload.json --output json`
* *Example:* `dagentsc pipeline compile --input pipeline.json --output json`

## Key Takeaways for your Presentation:
1. **Separation of Concerns:** By isolating complex planning and validation in a strongly-typed functional language (OCaml) and keeping execution in Python/Spring Boot, the system is both robust and flexible.
2. **Deterministic Planning:** Given the same inputs, the OCaml modules will always produce the same execution plans or Kubernetes manifests.
3. **Compile-Then-Execute:** The overarching pattern is to first compile and validate the plan (OCaml), and then pass it to the runtime adapters to execute it.
