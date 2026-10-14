"""Pydantic API models for the pipeline service.

The field descriptions appear in the service's OpenAPI document (`/docs`) and
in the API reference on the framework site, which is generated from it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.common.domain.models import DatasetInput


class HealthResponse(BaseModel):
    status: str = Field(description="Always `ok` when the service answers.")
    service: str = Field(description="The service name, from `APP_NAME`. The default is `dagents-pipeline-service`.")
    environment: str = Field(description="The deployment environment, from `APP_ENV`. The default is `development`.")
    transport: str = Field(description="How the service is reached. Always `http`.")


class ErrorResponse(BaseModel):
    detail: str = Field(description="What went wrong, in one sentence.")


class PipelineCondition(BaseModel):
    """One test a `filter_items` step applies to each item."""

    field: str = Field(
        description=(
            "A dotted path into the item, such as `amount` or `customer.country`. An item without this path "
            "fails the test."
        )
    )
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in"] = Field(
        description=(
            "How the item's value is compared with `value`: equal, not equal, greater than, greater or equal, "
            "less than, less or equal, `value` is inside the item's value (`contains`), or the item's value is "
            "inside `value` (`in`)."
        )
    )
    value: Any = Field(description="The value to compare with. For `in`, a list or a string.")


class AggregationSpec(BaseModel):
    """One number a `summarize_items` step computes over the items."""

    operation: Literal["count", "sum", "avg", "min", "max"] = Field(
        description=(
            "`count` counts the items. The others use the numeric values at `field`; items without a number "
            "there are skipped, and with no numbers at all the result is `0.0`."
        )
    )
    field: str | None = Field(
        default=None, description="A dotted path into each item. Required for every operation except `count`."
    )
    alias: str | None = Field(
        default=None,
        description="The key the result is stored under. Empty means `field`, or `operation` when `field` is empty.",
    )


class PipelineStep(BaseModel):
    """One named step of a pipeline."""

    step_id: str = Field(description="The step's name. It must be unique within the pipeline.")
    kind: Literal[
        "enrich_context",
        "filter_items",
        "summarize_items",
        "project_fields",
        "profile_dataset",
        "run_model_job",
    ] = Field(
        description=(
            "What the step does. Each kind reads its own keys from `config`; the API reference lists them."
        )
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="The `step_id`s that must run before this step. Each must name a step of the same pipeline.",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Settings for the step's kind, such as `items_field` or `conditions`. Unknown keys are ignored.",
    )


class PipelineDefinition(BaseModel):
    """A pipeline: a graph of named steps, run in dependency order."""

    pipeline_id: str = Field(
        description="The pipeline's name. Registering a pipeline with an id that exists replaces the stored one."
    )
    description: str = Field(default="", description="Free text for people. The service does not read it.")
    steps: list[PipelineStep] = Field(
        default_factory=list,
        description=(
            "The steps. A step runs after the steps it depends on; steps that do not depend on each other run "
            "in the order listed."
        ),
    )
    registered_at: int | None = Field(
        default=None,
        description=(
            "When the pipeline was registered, in seconds since 1970. The service sets it and replaces any value "
            "you send."
        ),
    )


class PipelineCatalogResponse(BaseModel):
    pipelines: list[PipelineDefinition] = Field(description="Every stored pipeline, sorted by `pipeline_id`.")


class StepRunResult(BaseModel):
    """What one step did during a run."""

    step_id: str = Field(description="The step's `step_id`.")
    kind: str = Field(description="The step's `kind`.")
    status: str = Field(
        description="Always `completed`. A step that fails stops the run, so it gets no result."
    )
    output: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "A short summary of what the step did, such as `input_count` and `output_count` for `filter_items`. "
            "The step's real output is written into the run's payload."
        ),
    )
    started_at: int = Field(description="When the step started, in seconds since 1970.")
    completed_at: int = Field(description="When the step finished, in seconds since 1970.")


class PipelineRunRequest(BaseModel):
    """A request to run a stored pipeline on a payload."""

    pipeline_id: str | None = Field(
        default=None,
        description=(
            "The pipeline to run. Required by `POST /api/v1/pipeline-runs`. "
            "`POST /api/v1/pipelines/{pipeline_id}/runs` ignores it and uses the id in the path."
        ),
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The JSON object the steps read and change, for example `{\"items\": [...]}`. The steps work on a "
            "copy of it."
        ),
    )


class PipelineRunResponse(BaseModel):
    """One run of a pipeline, finished or still in progress."""

    run_id: str = Field(
        description=(
            "`<pipeline_id>-run-<milliseconds since 1970>`, made from the clock when the run is stored. Two runs "
            "of one pipeline in the same millisecond get the same id."
        )
    )
    pipeline_id: str = Field(description="The pipeline that ran.")
    status: str = Field(
        description=(
            "`completed` for a run that has finished. A background run is `queued`, then `running`, then "
            "`completed` or `failed`."
        )
    )
    started_at: int = Field(description="When the run started, in seconds since 1970.")
    completed_at: int = Field(description="When the run finished, in seconds since 1970. `0` until a background run ends.")
    final_payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The payload after every step ran. For a queued or running run, the payload as it was sent."
        ),
    )
    step_results: list[StepRunResult] = Field(
        default_factory=list, description="One entry per step, in the order the steps ran. Empty until the run ends."
    )
    error: str | None = Field(
        default=None, description="Why a background run failed. Null otherwise."
    )


class PipelineRunCatalogResponse(BaseModel):
    runs: list[PipelineRunResponse] = Field(description="The stored runs, newest first.")


class SourceRegistrationRequest(BaseModel):
    source: DatasetInput | None = None
