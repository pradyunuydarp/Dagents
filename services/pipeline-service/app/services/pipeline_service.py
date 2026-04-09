"""Service facade for registering and executing reusable pipelines."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time
from typing import Protocol

from agents.common.infrastructure.sources import DefaultSourceResolver
from app.models import PipelineCatalogResponse, PipelineDefinition, PipelineRunRequest, PipelineRunResponse
from app.pipeline.engine import PipelineExecutionEngine


class PipelineDefinitionRepository(Protocol):
    def save(self, definition: PipelineDefinition) -> PipelineDefinition:
        """Persist one pipeline definition."""

    def get(self, pipeline_id: str) -> PipelineDefinition | None:
        """Load one pipeline definition."""

    def list(self) -> list[PipelineDefinition]:
        """List registered pipelines."""


class PipelineRunRepository(Protocol):
    def save(self, run: PipelineRunResponse) -> PipelineRunResponse:
        """Persist one pipeline run."""

    def update(self, run: PipelineRunResponse) -> PipelineRunResponse:
        """Update one pipeline run."""

    def get(self, run_id: str) -> PipelineRunResponse | None:
        """Load one pipeline run."""

    def list_recent(self, limit: int = 20) -> list[PipelineRunResponse]:
        """List recent pipeline runs."""


class InMemoryPipelineDefinitionRepository:
    def __init__(self) -> None:
        self._definitions: dict[str, PipelineDefinition] = {}

    def save(self, definition: PipelineDefinition) -> PipelineDefinition:
        self._definitions[definition.pipeline_id] = definition
        return definition

    def get(self, pipeline_id: str) -> PipelineDefinition | None:
        return self._definitions.get(pipeline_id)

    def list(self) -> list[PipelineDefinition]:
        return [self._definitions[key] for key in sorted(self._definitions)]


class InMemoryPipelineRunRepository:
    def __init__(self) -> None:
        self._runs: dict[str, PipelineRunResponse] = {}
        self._ordered_ids: list[str] = []

    def save(self, run: PipelineRunResponse) -> PipelineRunResponse:
        self._runs[run.run_id] = run
        if run.run_id in self._ordered_ids:
            self._ordered_ids.remove(run.run_id)
        self._ordered_ids.append(run.run_id)
        return run

    def update(self, run: PipelineRunResponse) -> PipelineRunResponse:
        self._runs[run.run_id] = run
        if run.run_id not in self._ordered_ids:
            self._ordered_ids.append(run.run_id)
        return run

    def get(self, run_id: str) -> PipelineRunResponse | None:
        return self._runs.get(run_id)

    def list_recent(self, limit: int = 20) -> list[PipelineRunResponse]:
        ordered = [self._runs[run_id] for run_id in self._ordered_ids]
        return list(reversed(ordered[-limit:]))


class PipelineService:
    def __init__(
        self,
        definitions: PipelineDefinitionRepository,
        runs: PipelineRunRepository,
        engine: PipelineExecutionEngine,
        source_resolver: DefaultSourceResolver | None = None,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._definitions = definitions
        self._runs = runs
        self._engine = engine
        self._source_resolver = source_resolver or DefaultSourceResolver()
        self._executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="dagents-pipelines")

    def list_pipelines(self) -> PipelineCatalogResponse:
        return PipelineCatalogResponse(pipelines=self._definitions.list())

    def register_pipeline(self, definition: PipelineDefinition) -> PipelineDefinition:
        self._engine.validate(definition)
        stored = definition.model_copy(update={"registered_at": int(time.time())})
        return self._definitions.save(stored)

    def run_pipeline(self, pipeline_id: str, request: PipelineRunRequest) -> PipelineRunResponse:
        definition = self._definitions.get(pipeline_id)
        if definition is None:
            raise ValueError(f"Unknown pipeline: {pipeline_id}")
        run = self._engine.execute(definition, request.payload, source_resolver=self._source_resolver)
        stored = run.model_copy(update={"run_id": f"{pipeline_id}-run-{int(time.time() * 1000)}"})
        return self._runs.save(stored)

    def submit_pipeline_run(self, request: PipelineRunRequest) -> PipelineRunResponse:
        pipeline_id = request.pipeline_id
        if not pipeline_id:
            raise ValueError("pipeline_id is required")
        definition = self._definitions.get(pipeline_id)
        if definition is None:
            raise ValueError(f"Unknown pipeline: {pipeline_id}")

        run_id = f"{pipeline_id}-run-{int(time.time() * 1000)}"
        queued = PipelineRunResponse(
            run_id=run_id,
            pipeline_id=pipeline_id,
            status="queued",
            started_at=int(time.time()),
            completed_at=0,
            final_payload=request.payload,
            step_results=[],
        )
        self._runs.save(queued)
        self._executor.submit(self._run_async, definition, request, run_id)
        return queued

    def _run_async(self, definition: PipelineDefinition, request: PipelineRunRequest, run_id: str) -> None:
        running = self._runs.get(run_id)
        if running is None:
            return
        self._runs.update(running.model_copy(update={"status": "running", "started_at": int(time.time())}))
        try:
            completed = self._engine.execute(definition, request.payload, source_resolver=self._source_resolver)
            stored = completed.model_copy(update={"run_id": run_id})
            self._runs.update(stored)
        except Exception as exc:  # pragma: no cover - defensive path
            failed = self._runs.get(run_id)
            if failed is None:
                return
            self._runs.update(
                failed.model_copy(update={"status": "failed", "completed_at": int(time.time()), "error": str(exc)})
            )

    def get_run(self, run_id: str) -> PipelineRunResponse | None:
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 20) -> list[PipelineRunResponse]:
        return self._runs.list_recent(limit=limit)


pipeline_service = PipelineService(
    definitions=InMemoryPipelineDefinitionRepository(),
    runs=InMemoryPipelineRunRepository(),
    engine=PipelineExecutionEngine(),
    source_resolver=DefaultSourceResolver(),
)
