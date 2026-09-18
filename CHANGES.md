# Changes & Insights

## Insights from AGENTS.md Evaluation
1. **LMA & GMA Paradigm**:
    - The repository strictly defines an architectural boundary between Local Monitoring Agents (LMA), which handle source-level data and models, and Global Monitoring Agents (GMA), which handle cross-tenant aggregate data and models.

2. **Core Service & Manifest Generation**:
    - The `core-service` functions as the Kubernetes manifest generator, runtime topology provider, and orchestrator façade. Instead of statically maintaining YAML/Helm charts, the framework generates parameterized manifests on demand for dynamic deployment of `gma`/`lma`/`model-service`/`pipeline-service`.

3. **OCaml Direction**:
    - Looking long-term, the project plans to introduce OCaml bounds primarily for strongly typed compilers and planners (e.g., manifest generation, datasets/pipelines schemas), relying less on Python/Spring for purely declarative processing and rule compilation.

4. **Component Decoupling**:
    - The layered design cleanly segregates `domain/`, `application/`, `adapters/`, and `infrastructure/` per agent, encouraging independent orchestration interfaces yet making the deployment process (`docker-compose` locally vs `Minikube`) somewhat complex to manage manually.

## Progress Notes
- Added this file as part of evaluating the framework specifications.
- Proceeded with the `Live Kubernetes Validation With Minikube` task from `TODO.md`.

## Execution Findings & Roadblocks
- Successfully extracted the required `dagents-workloads.yaml` directly from the `core-service` logic via a custom local Python script (`generate_manifests_local.py`), effectively negating the need to run the entire backend via `docker-compose` just to grab the manifests. I also fixed the programmatic generator correctly injecting environment variables, preventing the pods from getting trapped in `CrashLoopBackOff` in Kubernetes.
- **OCaml Subprocess Wrapper Added**: Added a `dagents_runner.py` to route JSON payloads between Python schemas and the OCaml binary natively. `ManifestService.compile()` and `PipelineEngine.validate()` now seamlessly call `run_dagentsc()` dynamically.
- **Multi-Stage Dockerfiles added**: Enabled container deployment of OCaml by modifying both `core-service` and `pipeline-service` Dockerfiles with `ocaml/opam:debian-ocaml-5.0` multi-stage builders to build `dagentsc.exe`.
- **Infrastructure Issue flagged**: Found a local network / proxy Docker Desktop limitation preventing downloading images over `minikube` locally. I attempted to build all 7 multi-stage Docker images (`lma`, `gma`, `model-service`, Spring layers, etc.), but the Docker VM disk is fully constrained (`no space left on device`). Testing the entire system inside the live containerized environment is physically blocked by the MacOS Docker Desktop 64GB virtual hard disk limit overbooking. I have done `docker system prune` heavily but the framework's internal multi-stage builds consistently exceed the allocated virtual disk space.

## Governance, Federation, and the Healthcare Demo

Added the two framework capabilities the healthcare case study specifies, plus the extension
point a consumer app needs to use them, plus the demo app that does.

### What went into the framework

- `bindings/ocaml/lib/governance_compiler` — the GRAILS Ethical-Restriction Rails. Know-Your-User
  scoring and an exhaustive sensitivity x trust x granularity match producing a filtering strategy,
  a decision, GRAILS' Filtering Score, and the obligations an enforcing guard must discharge.
- `bindings/ocaml/lib/federation_compiler` — round manifests, site eligibility, quorum,
  aggregation readiness, and release gates. Pure and replayable, because a round has to be
  explicable during an audit months later.
- `agents/common/application/ethical_guard.py` — the enforcing half, with a digest-chained audit
  log. It fails closed.
- `agents/common/application/federation.py` and `federation_worker.py` — the GMA-side round
  lifecycle and the LMA-side worker, with an adapter seam (`FederationEngine`) for a specialist
  federated runtime and an in-process simulator behind it.
- `agents/common/extensions/` — how a consumer contributes feature contracts, data
  classifications, condition packs, pipeline steps, and model adapters without forking anything.
- New dual-form routes on both agents, following the existing legacy + `/api/v1` convention.

### The GRAILS extension this project contributes back

The published GRAILS granularity set is `{cell, row, column, table}`. Every member is data that can
be pointed at and read. A federated round sends none of them; it sends a model update, which is not
a row but still carries patient signal. Treating the update as a fifth granularity is what lets one
planner govern both a row request and a federated egress, and the invariant that it never resolves
to "allow unchanged" is enforced by test rather than asserted in a comment.

### Findings worth recording

- **A property test caught a real ordering bug.** The filtering score weighted `AddNoise` as less
  protective than `ClipContribution`, which made the score claim that trusting a requester more
  could protect the data more. Noise bounds and obscures where clipping only bounds, so the weights
  were corrected. The monotonicity test that found it is now permanent.
- **The JSON codec did not compile against every yojson build.** `json_codec.ml` relied on
  inference that unified against an open polymorphic variant and failed where `Yojson.Safe.t`
  includes `Tuple` and `Variant`. Parameter types are now pinned explicitly.
- **The Python suite was silently skipping its governance tests.** They only ran when `DAGENTSC_BIN`
  was exported, so the suite could report green having proved nothing. It now finds the dune build,
  and the runner tests assert against the resolved binary rather than the literal name.
- **Two bugs in the federated worker surfaced from its own tests.** The before-train check asked at
  table granularity without supplying the site's cohort, which the Rails correctly refused; and the
  worker had no notion of its own approved field list, so a round could ask for more than a site had
  agreed to. A job's field list is now intersected against the site's.
- **The demo needed a cross-site validation round.** Reading candidate metrics off the training
  round measured nothing, because a training round strips the outcome label by design. The candidate
  is now validated at each hospital, which is also what the case study describes.

### Environment note for future work

This work was done in a container without opam. The OCaml layer was built with system OCaml 4.14
plus `libyojson-ocaml-dev`, and dune 3.21 built from source, because `dune-project` requires a lang
version newer than the packaged 3.14. The normal `opam exec -- dune` path in CLAUDE.md is unchanged;
this is only relevant if a CI image hits the same gap.

