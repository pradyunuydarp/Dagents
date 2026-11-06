"""API routes for the pipeline service."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import TypeAdapter

from dagents.common.domain.models import SourceSpec
from dagents.common.domain.sources import SourceValidationResult
from dagents.common.infrastructure.sources import REDACTED, SHOWN_CONNECTION_OPTIONS, redact_source
from app.core.config import Settings, settings
from app.models import (
    ErrorResponse,
    HealthResponse,
    PipelineCatalogResponse,
    PipelineDefinition,
    PipelineRunCatalogResponse,
    PipelineRunRequest,
    PipelineRunResponse,
    PipelineStep,
)
from app.services.pipeline_service import PipelineService, pipeline_service

router = APIRouter(prefix="/api/v1", tags=["pipeline-service"])


def get_settings() -> Settings:
    return settings


def get_pipeline_service() -> PipelineService:
    return pipeline_service


# --------------------------------------------------------------------------- #
# Documentation only. These shapes describe the endpoints in the OpenAPI
# document; they change no validation and no response.
# --------------------------------------------------------------------------- #


def _server_error(when: str) -> dict[str, Any]:
    """An unhandled exception: Starlette answers 500 with a plain-text body."""
    return {
        "description": when,
        "content": {"text/plain": {"schema": {"type": "string"}, "example": "Internal Server Error"}},
    }


def _not_found(what: str) -> dict[str, Any]:
    return {"model": ErrorResponse, "description": what}


_PIPELINE_ID = Path(description="The `pipeline_id` the pipeline was registered with.")
_RUN_ID = Path(description="The `run_id` a run request returned.")
_SOURCE_ID = Path(description="The `source_id` the source was registered with.")
_LIMIT = Query(
    description=(
        "The most runs to return, newest first. There is no bound check: 0 returns every run, and a negative "
        "number leaves out that many of the oldest runs."
    )
)
_INVALID_GRAPH = _server_error(
    "The graph is not valid: two steps share a `step_id`, a step depends on a step that does not exist, or the "
    "steps depend on each other in a cycle."
)
_STEP_FAILED = _server_error(
    "A step raised an error other than a `ValueError`, for example a `filter_items` condition that compares a "
    "number with a string. Nothing is stored."
)
_SOURCE_SPEC_SCHEMA = TypeAdapter(SourceSpec).json_schema(ref_template="#/components/schemas/{model}")
_SOURCE_SPEC_SCHEMA.pop("$defs", None)


def _redact_step(step: PipelineStep) -> PipelineStep:
    """Return `step` with the connection options of its inline source redacted.

    A `profile_dataset` or `run_model_job` step can carry its own source in
    `config.dataset_input.source` instead of naming a registered one. Its
    `connection_ref.options` are shown the way `redact_source` shows a
    registered source's: each value becomes `[redacted]`, except the keys in
    `SHOWN_CONNECTION_OPTIONS`. A step's config is checked only when the step
    runs, so this reads the JSON as sent and does not validate it.
    """
    dataset_input = step.config.get("dataset_input")
    source = dataset_input.get("source") if isinstance(dataset_input, dict) else None
    connection_ref = source.get("connection_ref") if isinstance(source, dict) else None
    options = connection_ref.get("options") if isinstance(connection_ref, dict) else None
    if not isinstance(options, dict) or not options:
        return step
    shown = {key: value if key in SHOWN_CONNECTION_OPTIONS else REDACTED for key, value in options.items()}
    redacted_source = {**source, "connection_ref": {**connection_ref, "options": shown}}
    config = {**step.config, "dataset_input": {**dataset_input, "source": redacted_source}}
    return step.model_copy(update={"config": config})


def _redact_definition(definition: PipelineDefinition) -> PipelineDefinition:
    """Return a copy of `definition` that is safe to send back, or the same one when nothing is hidden.

    The stored definition is not changed, so a run still connects with the real values.
    """
    steps = [_redact_step(step) for step in definition.steps]
    if all(new is old for new, old in zip(steps, definition.steps)):
        return definition
    return definition.model_copy(update={"steps": steps})


@router.get("/health", response_model=HealthResponse)
def health(runtime_settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Report that the pipeline service is running.

    It reads only the service's settings, so it answers whenever the process is up.
    """
    return HealthResponse(**runtime_settings.as_health_payload())


@router.get("/pipelines", response_model=PipelineCatalogResponse)
def list_pipelines(service: PipelineService = Depends(get_pipeline_service)) -> PipelineCatalogResponse:
    """List every stored pipeline, sorted by `pipeline_id`.

    A step's inline source (`config.dataset_input.source`) is shown with its
    connection options redacted.
    """
    return PipelineCatalogResponse(pipelines=[_redact_definition(item) for item in service.list_pipelines().pipelines])


@router.get("/pipeline-definitions", response_model=PipelineCatalogResponse)
def list_pipeline_definitions(service: PipelineService = Depends(get_pipeline_service)) -> PipelineCatalogResponse:
    """List every stored pipeline. The same handler as `GET /api/v1/pipelines`."""
    return list_pipelines(service)


@router.post("/pipelines", response_model=PipelineDefinition, responses={500: _INVALID_GRAPH})
def register_pipeline(
    definition: PipelineDefinition,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    """Check a pipeline's graph and store it.

    The graph is sent to `dagentsc pipeline compile` and then ordered in Python.
    A pipeline with the same `pipeline_id` is replaced. The answer shows a
    step's inline source (`config.dataset_input.source`) with its connection
    options redacted; the stored pipeline keeps the real values.
    """
    return _redact_definition(service.register_pipeline(definition))


@router.post("/pipeline-definitions", response_model=PipelineDefinition, responses={500: _INVALID_GRAPH})
def register_pipeline_definition(
    definition: PipelineDefinition,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    """Check a pipeline's graph and store it. The same handler as `POST /api/v1/pipelines`."""
    return register_pipeline(definition, service)


@router.get(
    "/pipeline-definitions/{pipeline_id}",
    response_model=PipelineDefinition,
    responses={404: _not_found("No pipeline is stored under this id.")},
)
def get_pipeline_definition(
    pipeline_id: Annotated[str, _PIPELINE_ID],
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    """Fetch one stored pipeline by `pipeline_id`.

    A step's inline source (`config.dataset_input.source`) is shown with its
    connection options redacted.
    """
    definition = service._definitions.get(pipeline_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown pipeline: {pipeline_id}")
    return _redact_definition(definition)


@router.post(
    "/pipeline-definitions/{pipeline_id}:validate",
    response_model=PipelineDefinition,
    responses={
        404: _not_found("No pipeline is stored under this id."),
        500: _INVALID_GRAPH,
    },
)
def validate_pipeline_definition(
    pipeline_id: Annotated[str, _PIPELINE_ID],
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    """Check a stored pipeline's graph again and return the pipeline.

    It runs the same checks as registering, including `dagentsc pipeline compile`,
    and stores nothing. A step's inline source is shown with its connection
    options redacted.
    """
    definition = service._definitions.get(pipeline_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown pipeline: {pipeline_id}")
    service._engine.validate(definition)
    return _redact_definition(definition)


@router.post(
    "/pipelines/{pipeline_id}/runs",
    response_model=PipelineRunResponse,
    responses={
        404: _not_found(
            "No pipeline is stored under this id, or a step raised a `ValueError`: for example its `config` is "
            "not valid, or it names a source that is not registered. Nothing is stored."
        ),
        500: _STEP_FAILED,
    },
)
def run_pipeline(
    pipeline_id: Annotated[str, _PIPELINE_ID],
    request: PipelineRunRequest,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineRunResponse:
    """Run a stored pipeline on a payload and wait for it to finish.

    The finished run is stored, so `GET /api/v1/runs` lists it.
    """
    try:
        return service.run_pipeline(pipeline_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/pipeline-runs",
    response_model=PipelineRunResponse,
    status_code=202,
    responses={404: _not_found("`pipeline_id` is missing, or no pipeline is stored under it.")},
)
def submit_pipeline_run(
    request: PipelineRunRequest,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineRunResponse:
    """Queue a run of a stored pipeline and answer at once.

    The run executes on a background thread. Read its state with
    `GET /api/v1/pipeline-runs/{run_id}`.
    """
    try:
        return service.submit_pipeline_run(request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs", response_model=PipelineRunCatalogResponse)
def list_runs(
    limit: Annotated[int, _LIMIT] = 20,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineRunCatalogResponse:
    """List the stored runs, newest first."""
    return PipelineRunCatalogResponse(runs=service.list_runs(limit=limit))


@router.get("/pipeline-runs", response_model=PipelineRunCatalogResponse)
def list_pipeline_runs(
    limit: Annotated[int, _LIMIT] = 20,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineRunCatalogResponse:
    """List the stored runs, newest first. The same handler as `GET /api/v1/runs`."""
    return list_runs(limit, service)


@router.get(
    "/runs/{run_id}",
    response_model=PipelineRunResponse,
    responses={404: _not_found("No run is stored under this id.")},
)
def get_run(run_id: Annotated[str, _RUN_ID], service: PipelineService = Depends(get_pipeline_service)) -> PipelineRunResponse:
    """Fetch one stored run by `run_id`, in its current state."""
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    return run


@router.get(
    "/pipeline-runs/{run_id}",
    response_model=PipelineRunResponse,
    responses={404: _not_found("No run is stored under this id.")},
)
def get_pipeline_run(
    run_id: Annotated[str, _RUN_ID], service: PipelineService = Depends(get_pipeline_service)
) -> PipelineRunResponse:
    """Fetch one stored run by `run_id`. The same handler as `GET /api/v1/runs/{run_id}`."""
    return get_run(run_id, service)


@router.post(
    "/sources",
    responses={
        200: {
            "model": SourceSpec,
            "description": "The stored source definition, with its defaults filled in and its connection options redacted.",
        },
        500: _server_error("The body is not a valid source definition. The handler validates it itself."),
    },
    # The handler takes a plain dict and validates it with TypeAdapter, so
    # FastAPI cannot see the body's shape. This states it for the schema only.
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": _SOURCE_SPEC_SCHEMA}}}},
)
def register_source(payload: dict[str, Any], service: PipelineService = Depends(get_pipeline_service)):
    """Store a source definition in the pipeline service's catalog.

    A source with the same `source_id` is replaced. No records are read. The
    answer shows each connection option's value as `[redacted]`, except
    `role`, `sslmode` and `connect_timeout`, so a password is never sent back.
    """
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return redact_source(service._source_resolver.register(source))


@router.get(
    "/sources",
    responses={
        200: {
            "model": list[SourceSpec],
            "description": "Every stored source, sorted by `source_id`, with its connection options redacted.",
        }
    },
)
def list_sources(service: PipelineService = Depends(get_pipeline_service)):
    """List every stored source definition, sorted by `source_id`, with its connection options redacted."""
    return [redact_source(source) for source in service._source_resolver.list()]


@router.get(
    "/sources/{source_id}",
    responses={
        200: {"model": SourceSpec, "description": "The stored source definition, with its connection options redacted."},
        404: _not_found("No source is stored under this id."),
    },
)
def get_source(source_id: Annotated[str, _SOURCE_ID], service: PipelineService = Depends(get_pipeline_service)):
    """Fetch one stored source definition by `source_id`, with its connection options redacted."""
    source = service._source_resolver.get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
    return redact_source(source)


@router.post(
    "/sources/{source_id}:validate",
    responses={
        200: {"model": SourceValidationResult},
        404: _not_found("No source is stored under this id."),
    },
)
def validate_source(source_id: Annotated[str, _SOURCE_ID], service: PipelineService = Depends(get_pipeline_service)):
    """Run the source adapter's checks on a stored source definition.

    No records are read and no connection is opened.
    """
    try:
        return service._source_resolver.validate(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
