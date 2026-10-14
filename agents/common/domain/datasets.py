"""Dataset and model execution contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from agents.common.domain.base import DagentsModel
from agents.common.domain.sources import DatasetInput

# Descriptions shared by the profile and model-run requests.
_SCOPE_KIND = (
    "`source` for data from one source, `assimilated` for data combined from several. The agent decides: "
    "the LMA always records `source` and the GMA `assimilated`, whatever the request says."
)
_DATASET = (
    "Where the records come from: records in the request, a source sent with the request, or the id of a "
    "registered source. When it is set, `records` is ignored."
)
_RECORDS = "Records sent directly. The older way to send data, used only when `dataset` is not set."
_FEATURE_FIELDS = "The fields to profile. Empty means every field of the first record except `label_field`."
_LABEL_FIELD = "The field that holds the label, for supervised tasks. It is left out of the inferred feature fields."


class DatasetProfileRequest(DagentsModel):
    """A request to profile a set of records."""

    scope_id: str = Field(
        description="A name for the data, such as the source or tenant it belongs to. It is copied into the profile."
    )
    scope_kind: Literal["source", "assimilated"] = Field(default="source", description=_SCOPE_KIND)
    extraction_strategy: Literal["tabular", "time_series", "text", "hybrid"] = Field(
        default="tabular",
        description=(
            "How the data is read. It changes the suggested models: `time_series` suggests gru, lstm and "
            "autoencoder, and `text` suggests naive_bayes, gru and transformer."
        ),
    )
    dataset: DatasetInput | None = Field(default=None, description=_DATASET)
    records: list[dict[str, Any]] = Field(default_factory=list, description=_RECORDS)
    feature_fields: list[str] = Field(default_factory=list, description=_FEATURE_FIELDS)
    label_field: str | None = Field(
        default=None,
        description=_LABEL_FIELD + " When it is set, the profile suggests classification models.",
    )
    batch_size: int = Field(
        default=1000,
        ge=1,
        description="Records per partition: `partition_count` is the record count divided by this, rounded up. At least 1.",
    )


class DatasetProfile(DagentsModel):
    """What a profile found in a set of records: their count, their fields and the models that suit them."""

    scope_id: str = Field(description="Copied from the request.")
    scope_kind: Literal["source", "assimilated"] = Field(
        description="`source` when an LMA made the profile, `assimilated` when the GMA did."
    )
    extraction_strategy: str = Field(
        description="The request's extraction strategy. For a model run, the one inferred from the records."
    )
    record_count: int = Field(description="How many records were read.")
    feature_fields: list[str] = Field(
        default_factory=list,
        description="The fields profiled: the request's `feature_fields`, or every field of the first record except the label.",
    )
    label_field: str | None = Field(default=None, description="Copied from the request.")
    numeric_fields: list[str] = Field(
        default_factory=list,
        description="Feature fields whose first non-null value is a number. A boolean does not count as a number.",
    )
    categorical_fields: list[str] = Field(
        default_factory=list,
        description="Every other feature field, including a field that is null in every record.",
    )
    partition_count: int = Field(
        default=0, description="`record_count` divided by `batch_size`, rounded up. 0 when there are no records."
    )
    suggested_models: list[str] = Field(
        default_factory=list,
        description=(
            "Two or three model families, chosen by fixed rules from the extraction strategy, whether there "
            "is a label, and whether any feature field is numeric."
        ),
    )


class ModelExecutionRequest(DagentsModel):
    """A request to run a model over a set of records and record the run."""

    scope_id: str = Field(description="A name for the data. It is part of the run id and of the artifact path.")
    scope_kind: Literal["source", "assimilated"] = Field(default="source", description=_SCOPE_KIND)
    task_type: Literal["anomaly_detection", "classification", "forecasting", "embedding", "regression"] = Field(
        default="anomaly_detection",
        description="The task. It is copied into the run record; `forecasting` makes the profile use `time_series`.",
    )
    model_family: Literal[
        "autoencoder",
        "variational_autoencoder",
        "gru",
        "lstm",
        "naive_bayes",
        "transformer",
        "random_forest",
        "xgboost",
        "linear",
        "custom",
    ] = Field(
        default="autoencoder",
        description=(
            "The model family to record. It sets the base of the estimated `quality_score` and the artifact's "
            "file name. No model is trained."
        ),
    )
    dataset: DatasetInput | None = Field(default=None, description=_DATASET)
    records: list[dict[str, Any]] = Field(default_factory=list, description=_RECORDS)
    feature_fields: list[str] = Field(default_factory=list, description=_FEATURE_FIELDS)
    label_field: str | None = Field(default=None, description=_LABEL_FIELD)
    hyperparameters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Settings for the run. Only `batch_size` is read (default 1000): it sets `batch_count` and the "
            "profile's partitions."
        ),
    )
    artifact_prefix: str = Field(default="artifacts", description="The start of `artifact_uri`. No file is written.")
    model_version: str = Field(default="v1", description="The version written into `artifact_uri`.")


class ModelRunRecord(DagentsModel):
    """One recorded model run."""

    run_id: str = Field(
        description=(
            "`<role>-<scope_id>-<start time in seconds>`, for example `lma-hospital-a-1760000000`. Two runs "
            "for one scope in the same second get the same id."
        )
    )
    agent_role: Literal["LMA", "GMA", "SERVICE"] = Field(
        description="Who ran it: `LMA`, `GMA`, or `SERVICE` for a step of the pipeline service's engine."
    )
    scope_id: str = Field(description="Copied from the request.")
    scope_kind: Literal["source", "assimilated"] = Field(description="`source` from an LMA, `assimilated` from the GMA.")
    task_type: str = Field(description="Copied from the request.")
    model_family: str = Field(description="Copied from the request.")
    record_count: int = Field(description="How many records the run read.")
    feature_count: int = Field(description="How many feature fields the profile found.")
    batch_count: int = Field(
        description="`record_count` divided by the `batch_size` hyperparameter, rounded up. 0 when there are no records."
    )
    status: Literal["completed", "failed"] = Field(
        default="completed",
        description="`completed`. A run that raises an error is not recorded, so the agents never store `failed`.",
    )
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Estimates, not measurements. `quality_score` is a fixed base per model family, plus "
            "0.15 x min(1, max(0.1, records / 10,000)), plus 0.04 on the GMA, at most 0.99. "
            "`throughput_rows_per_batch` is the record count divided by the partition count, rounded up. "
            "`feature_coverage` is the number of feature fields divided by that number plus the number of "
            "categorical fields."
        ),
    )
    artifact_uri: str = Field(
        description="`<artifact_prefix>/<role>/<scope_id>/<model_family>-<model_version>.json`. A name only; no file is written."
    )
    started_at: int = Field(description="When the run started, in Unix seconds.")
    completed_at: int = Field(description="When the run ended, in Unix seconds.")


class ModelExecutionResponse(DagentsModel):
    """The result of a model run: the profile of its records and the run record."""

    dataset_profile: DatasetProfile = Field(description="The profile of the records the run read.")
    run: ModelRunRecord = Field(description="The run record. The agent also keeps it in its run history.")
