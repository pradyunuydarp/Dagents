"""API routes for the pipeline service."""

from collections.abc import Iterator
from contextlib import contextmanager
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import TypeAdapter

from agents.common.domain.models import SourceSpec
from agents.common.domain.sources import SourceValidationResult
from agents.common.infrastructure.dagents_runner import PlannerRejectedError, PlannerUnavailableError
from app.core.config import Settings, settings
from app.models import (
    ErrorResponse,
    HealthResponse,
    PipelineCatalogResponse,
    PipelineDefinition,
    PipelineRunCatalogResponse,
    PipelineRunRequest,
    PipelineRunResponse,
    PipelineRunSubmission,
)
from app.pipeline.engine import PipelineStepError
from app.services.pipeline_service import PipelineNotFoundError, PipelineService, pipeline_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["pipeline-service"])

#: The 503 body. The planner's own error can hold a file path, so it goes to
#: the log, not to the caller.
PLANNER_UNAVAILABLE_DETAIL = (
    "The pipeline planner is not available, so the pipeline cannot be checked or run. Try again later."
)


@contextmanager
def _pipeline_errors() -> Iterator[None]:
    """Turn the service's errors into HTTP responses.

    - 404: no pipeline with that id.
    - 422: the planner rejected the graph, or a step failed on its config or payload.
    - 503: the planner cannot be run. The service has no other way to check or
      order a pipeline, so it refuses the request.
    """
    try:
        yield
    except PipelineNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (PlannerRejectedError, PipelineStepError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except PlannerUnavailableError as error:
        logger.warning("Pipeline planner unavailable: %s", error)
        raise HTTPException(status_code=503, detail=PLANNER_UNAVAILABLE_DETAIL) from error


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


def _unprocessable(description: str) -> dict[str, Any]:
    """A 422 is either FastAPI's list of field errors or a sentence from this service."""
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/ErrorResponse"},
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                    ]
                }
            }
        },
    }


_PIPELINE_ID = Path(description="The `pipeline_id` the pipeline was registered with.")
_RUN_ID = Path(description="The `run_id` a run request returned.")
_SOURCE_ID = Path(description="The `source_id` the source was registered with.")
_LIMIT = Query(
    description=(
        "The most runs to return, newest first. There is no bound check: 0 returns every run, and a negative "
        "number leaves out that many of the oldest runs."
    )
)
_GRAPH_REJECTED = _unprocessable(
    "The body does not match the schema (`detail` is a list of field errors), or the planner rejected the graph: "
    "two steps share a `step_id`, a step depends on a step that does not exist, or the steps depend on each other "
    "in a cycle (`detail` is the planner's sentence). Nothing is stored."
)
_STEP_FAILED = _unprocessable(
    "The body does not match the schema (`detail` is a list of field errors), or a step raised a `ValueError`, "
    "`TypeError` or `LookupError` because of its `config` or the payload: for example a sum with no `field`, a "
    "source that is not registered, or a condition that compares a number with a string (`detail` names the step "
    "and the reason). Nothing is stored."
)
_STEP_CRASHED = _server_error("A step raised any other error, which is a fault in the step's code. Nothing is stored.")
_PLANNER_UNAVAILABLE = _not_found(
    "The planner (`dagentsc`) cannot be run, or its answer has the wrong shape. The service has no other way to "
    "check or order a pipeline, so it refuses the request and stores nothing. Try again later."
)
_SOURCE_SPEC_SCHEMA = TypeAdapter(SourceSpec).json_schema(ref_template="#/components/schemas/{model}")
_SOURCE_SPEC_SCHEMA.pop("$defs", None)


@router.get("/health", response_model=HealthResponse)
def health(runtime_settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Report that the pipeline service is running.

    It reads only the service's settings, so it answers whenever the process is up.
    """
    return HealthResponse(**runtime_settings.as_health_payload())


@router.get("/pipelines", response_model=PipelineCatalogResponse)
def list_pipelines(service: PipelineService = Depends(get_pipeline_service)) -> PipelineCatalogResponse:
    """List every stored pipeline, sorted by `pipeline_id`."""
    return service.list_pipelines()


@router.get("/pipeline-definitions", response_model=PipelineCatalogResponse)
def list_pipeline_definitions(service: PipelineService = Depends(get_pipeline_service)) -> PipelineCatalogResponse:
    """List every stored pipeline. The same handler as `GET /api/v1/pipelines`."""
    return list_pipelines(service)


@router.post(
    "/pipelines",
    response_model=PipelineDefinition,
    responses={422: _GRAPH_REJECTED, 503: _PLANNER_UNAVAILABLE},
)
def register_pipeline(
    definition: PipelineDefinition,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    """Check a pipeline's graph with the planner and store it.

    `dagentsc pipeline compile` decides whether the graph is valid. A pipeline
    with the same `pipeline_id` is replaced.
    """
    with _pipeline_errors():
        return service.register_pipeline(definition)


@router.post(
    "/pipeline-definitions",
    response_model=PipelineDefinition,
    responses={422: _GRAPH_REJECTED, 503: _PLANNER_UNAVAILABLE},
)
def register_pipeline_definition(
    definition: PipelineDefinition,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    """Check a pipeline's graph with the planner and store it. The same handler as `POST /api/v1/pipelines`."""
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
    """Fetch one stored pipeline by `pipeline_id`."""
    definition = service._definitions.get(pipeline_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown pipeline: {pipeline_id}")
    return definition


@router.post(
    "/pipeline-definitions/{pipeline_id}:validate",
    response_model=PipelineDefinition,
    responses={
        404: _not_found("No pipeline is stored under this id."),
        422: _GRAPH_REJECTED,
        503: _PLANNER_UNAVAILABLE,
    },
)
def validate_pipeline_definition(
    pipeline_id: Annotated[str, _PIPELINE_ID],
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    """Check a stored pipeline's graph with the planner again and return the pipeline.

    It runs the same check as registering, `dagentsc pipeline compile`, and
    stores nothing.
    """
    with _pipeline_errors():
        return service.validate_pipeline(pipeline_id)


@router.post(
    "/pipelines/{pipeline_id}/runs",
    response_model=PipelineRunResponse,
    responses={
        404: _not_found("No pipeline is stored under this id."),
        422: _STEP_FAILED,
        500: _STEP_CRASHED,
        503: _PLANNER_UNAVAILABLE,
    },
)
def run_pipeline(
    pipeline_id: Annotated[str, _PIPELINE_ID],
    request: PipelineRunRequest,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineRunResponse:
    """Run a stored pipeline on a payload and wait for it to finish.

    The steps run in the order `dagentsc pipeline compile` returns. The
    finished run is stored, so `GET /api/v1/runs` lists it.
    """
    with _pipeline_errors():
        return service.run_pipeline(pipeline_id, request)


@router.post(
    "/pipeline-runs",
    response_model=PipelineRunResponse,
    status_code=202,
    responses={
        404: _not_found("No pipeline is stored under `pipeline_id`."),
        422: _GRAPH_REJECTED,
        503: _PLANNER_UNAVAILABLE,
    },
)
def submit_pipeline_run(
    request: PipelineRunSubmission,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineRunResponse:
    """Queue a run of a stored pipeline and answer at once.

    The planner orders the steps before the run is queued, so a planner that
    cannot be run fails the request, not the run. The run executes on a
    background thread; a step that fails there marks the run `failed` with the
    step's error. Read its state with `GET /api/v1/pipeline-runs/{run_id}`.
    """
    with _pipeline_errors():
        return service.submit_pipeline_run(request)


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
        200: {"model": SourceSpec, "description": "The stored source definition, with its defaults filled in."},
        500: _server_error("The body is not a valid source definition. The handler validates it itself."),
    },
    # The handler takes a plain dict and validates it with TypeAdapter, so
    # FastAPI cannot see the body's shape. This states it for the schema only.
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": _SOURCE_SPEC_SCHEMA}}}},
)
def register_source(payload: dict[str, Any], service: PipelineService = Depends(get_pipeline_service)):
    """Store a source definition in the pipeline service's catalog.

    A source with the same `source_id` is replaced. No records are read.
    """
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service._source_resolver.register(source)


@router.get(
    "/sources",
    responses={200: {"model": list[SourceSpec], "description": "Every stored source, sorted by `source_id`."}},
)
def list_sources(service: PipelineService = Depends(get_pipeline_service)):
    """List every stored source definition, sorted by `source_id`."""
    return service._source_resolver.list()


@router.get(
    "/sources/{source_id}",
    responses={
        200: {"model": SourceSpec, "description": "The stored source definition."},
        404: _not_found("No source is stored under this id."),
    },
)
def get_source(source_id: Annotated[str, _SOURCE_ID], service: PipelineService = Depends(get_pipeline_service)):
    """Fetch one stored source definition by `source_id`."""
    source = service._source_resolver.get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
    return source


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
