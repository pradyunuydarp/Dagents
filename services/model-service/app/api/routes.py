"""API routes for the model service."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import TypeAdapter

from agents.common.domain.models import SourceSpec
from agents.common.domain.sources import SourceValidationResult
from app.core.config import Settings, settings
from app.models import (
    ClassificationCheckResponse,
    DatasetCatalogResponse,
    ErrorResponse,
    ForecastingCheckResponse,
    HealthResponse,
    MLCheckRequest,
    ModelInventoryResponse,
    ModelJobCatalogResponse,
    ModelJobResponse,
    RegressionCheckResponse,
    TrainRequest,
    TrainResponse,
    UnsupportedModelErrorResponse,
)
from app.services.training_service import ModelTrainingService, training_service

router = APIRouter(prefix="/api/v1", tags=["model-service"])


def get_settings() -> Settings:
    """Dependency hook that exposes the process-wide model-service settings."""
    return settings


def get_training_service() -> ModelTrainingService:
    """Dependency hook that exposes the shared training service instance."""
    return training_service


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


def _unsupported(task: str) -> dict[str, Any]:
    return {
        "model": UnsupportedModelErrorResponse,
        "description": f"`model_family` is not implemented for {task} in this deployment, or is only planned.",
    }


def _not_found(what: str) -> dict[str, Any]:
    return {"model": ErrorResponse, "description": what}


_JOB_ID = Path(description="The `job_id` that `POST /api/v1/model-jobs` returned.")
_SOURCE_ID = Path(description="The `source_id` the source was registered with.")
_LIMIT = Query(
    description=(
        "The most jobs to return, newest first. There is no bound check: 0 returns every job, and a negative "
        "number leaves out that many of the oldest jobs."
    )
)
_DATA_FAILED = (
    "The records could not be read or used: `dataset_name` is not a built-in benchmark or cannot be downloaded, "
    "`dataset.source_id` names no registered source, or `label_field` is not in the records."
)
_SOURCE_SPEC_SCHEMA = TypeAdapter(SourceSpec).json_schema(ref_template="#/components/schemas/{model}")
_SOURCE_SPEC_SCHEMA.pop("$defs", None)


@router.get("/health", response_model=HealthResponse)
def health(runtime_settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Return service health and runtime metadata for health checks."""
    return HealthResponse(**runtime_settings.as_health_payload())


@router.get("/datasets", response_model=DatasetCatalogResponse)
def datasets(service: ModelTrainingService = Depends(get_training_service)) -> DatasetCatalogResponse:
    """List built-in benchmark datasets available to the model service.

    The list is fixed in the code. Nothing is downloaded to answer it.
    """
    return DatasetCatalogResponse(datasets=service.list_datasets())


@router.get("/model-families", response_model=ModelInventoryResponse)
def model_families(
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelInventoryResponse:
    """Describe the model families this deployment can execute, and with what.

    Params:
    - `service`: injected training façade, which owns the inventory.

    What it does:
    - Returns the provider-agnostic model inventory: which `(family, task)` pairs
      are implemented, which provider backs each, what an optional one needs
      installed, and which families the OCaml router may select that this
      runtime cannot yet run.

    Returns:
    - `ModelInventoryResponse`.
    """
    return ModelInventoryResponse(**service.describe_model_inventory())


@router.post(
    "/train",
    response_model=TrainResponse,
    responses={
        400: _unsupported("anomaly detection"),
        500: _server_error(
            _DATA_FAILED + " Also when the records have too few anomalies to split, or `tuning_strategy` or "
            "`target_metric` is not a known name."
        ),
    },
)
def train(
    request: TrainRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> TrainResponse:
    """Execute synchronous anomaly-model training.

    Params:
    - `request`: training payload selecting dataset, model family, and tuning options.
    - `service`: injected training façade.

    What it does:
    - Delegates directly to the training service.

    Returns:
    - `TrainResponse` with artifact and metric metadata.
    """
    return service.train(request)


@router.post(
    "/checks/classification",
    response_model=ClassificationCheckResponse,
    responses={
        400: _unsupported("classification"),
        500: _server_error(_DATA_FAILED + " Also when the label has fewer than two classes, or more than two."),
    },
)
def classification_check(
    request: MLCheckRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> ClassificationCheckResponse:
    """Run a one-shot classification evaluation against the supplied dataset.

    Nothing is stored and no model file is written.
    """
    return service.classification_check(request)


@router.post(
    "/checks/regression",
    response_model=RegressionCheckResponse,
    responses={400: _unsupported("regression"), 500: _server_error(_DATA_FAILED)},
)
def regression_check(
    request: MLCheckRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> RegressionCheckResponse:
    """Run a one-shot regression evaluation against the supplied dataset.

    Nothing is stored and no model file is written.
    """
    return service.regression_check(request)


@router.post(
    "/checks/forecasting",
    response_model=ForecastingCheckResponse,
    responses={
        400: _unsupported("forecasting"),
        500: _server_error(_DATA_FAILED + " Also when there are not more records than `sequence_length`."),
    },
)
def forecasting_check(
    request: MLCheckRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> ForecastingCheckResponse:
    """Run a one-shot forecasting evaluation, typically with GRU or LSTM.

    Nothing is stored and no model file is written.
    """
    return service.forecasting_check(request)


@router.post("/model-jobs", response_model=ModelJobResponse, status_code=202)
def submit_model_job(
    request: TrainRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelJobResponse:
    """Submit a background training job and return the queued job record.

    Training runs on a background thread, as `POST /api/v1/train` would. A
    failure, such as an unsupported model family, marks the job `failed`.
    """
    return service.submit_job(request)


@router.get(
    "/model-jobs/{job_id}",
    response_model=ModelJobResponse,
    responses={404: _not_found("No job is stored under this id.")},
)
def get_model_job(
    job_id: Annotated[str, _JOB_ID],
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelJobResponse:
    """Fetch one previously submitted model job by `job_id`."""
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
    return job


@router.get("/model-jobs", response_model=ModelJobCatalogResponse)
def list_model_jobs(
    limit: Annotated[int, _LIMIT] = 20,
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelJobCatalogResponse:
    """List recent model jobs in reverse chronological order."""
    return ModelJobCatalogResponse(jobs=service.list_jobs(limit=limit))


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
def register_source(payload: dict[str, Any], service: ModelTrainingService = Depends(get_training_service)):
    """Validate and register a reusable source definition for model-service use.

    A source with the same `source_id` is replaced. No records are read.
    """
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@router.get(
    "/sources",
    responses={200: {"model": list[SourceSpec], "description": "Every stored source, sorted by `source_id`."}},
)
def list_sources(service: ModelTrainingService = Depends(get_training_service)):
    """List all source definitions registered with the model service."""
    return service.list_sources()


@router.get(
    "/sources/{source_id}",
    responses={
        200: {"model": SourceSpec, "description": "The stored source definition."},
        404: _not_found("No source is stored under this id."),
    },
)
def get_source(source_id: Annotated[str, _SOURCE_ID], service: ModelTrainingService = Depends(get_training_service)):
    """Fetch one stored source definition by id."""
    source = service.get_source(source_id)
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
def validate_source(source_id: Annotated[str, _SOURCE_ID], service: ModelTrainingService = Depends(get_training_service)):
    """Run shared source validation for one registered source id.

    No records are read and no connection is opened.
    """
    try:
        return service.validate_source(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
