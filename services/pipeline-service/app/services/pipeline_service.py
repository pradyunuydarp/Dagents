"""Service facade for registering and executing reusable pipelines."""

from __future__ import annotations

import time
from typing import Protocol

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
        self._runs: list[PipelineRunResponse] = []

    def save(self, run: PipelineRunResponse) -> PipelineRunResponse:
        self._runs.append(run)
        return run

    def get(self, run_id: str) -> PipelineRunResponse | None:
        for run in self._runs:
            if run.run_id == run_id:
                return run
        return None

    def list_recent(self, limit: int = 20) -> list[PipelineRunResponse]:
        return list(reversed(self._runs[-limit:]))


class PipelineService:
    def __init__(
        self,
        definitions: PipelineDefinitionRepository,
        runs: PipelineRunRepository,
        engine: PipelineExecutionEngine,
    ) -> None:
        self._definitions = definitions
        self._runs = runs
        self._engine = engine

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
        run = self._engine.execute(definition, request.payload)
        stored = run.model_copy(update={"run_id": f"{pipeline_id}-run-{int(time.time() * 1000)}"})
        return self._runs.save(stored)

    def get_run(self, run_id: str) -> PipelineRunResponse | None:
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 20) -> list[PipelineRunResponse]:
        return self._runs.list_recent(limit=limit)


pipeline_service = PipelineService(
    definitions=InMemoryPipelineDefinitionRepository(),
    runs=InMemoryPipelineRunRepository(),
    engine=PipelineExecutionEngine(),
)
