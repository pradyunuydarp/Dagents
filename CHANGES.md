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
