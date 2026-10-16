"""API and orchestration models for the Dagents NL2SQL demo."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from dagents.common.domain.base import DagentsModel


class HealthResponse(DagentsModel):
    """The NL2SQL backend's liveness answer."""

    status: str = Field(description="Always `ok` when the process answers.")
    service: str = Field(description="The app's name, from `NL2SQL_APP_NAME`.")
    environment: str = Field(description="The deployment environment, from `NL2SQL_APP_ENV`.")
    transport: str = Field(description="Always `http`: the app reaches the framework services over HTTP.")


class SchemaColumn(DagentsModel):
    """One column of a table the question is about."""

    name: str = Field(description="The column's name. Characters other than letters, digits and `_` become `_` in the DDL.")
    dtype: str = Field(
        default="TEXT",
        description="The column's SQL type, such as `TEXT` or `NUMBER`. It is written in upper case into the DDL.",
    )
    description: str | None = Field(
        default=None, description="A note about the column. It is not sent to the model or the planner."
    )


class SchemaTable(DagentsModel):
    """One table the question is about."""

    name: str = Field(description="The table's name, cleaned the same way as a column name.")
    columns: list[SchemaColumn] = Field(default_factory=list, description="The table's columns, in order.")


class SamplePrompt(DagentsModel):
    """A sample question with the schema it is about."""

    sample_id: str = Field(description="The sample's id, such as `orders-by-tenant`.")
    title: str = Field(description="A short title for the sample.")
    question: str = Field(description="The question in plain English.")
    tables: list[SchemaTable] = Field(description="The tables the question is about.")


class ModelDescriptor(DagentsModel):
    """A SQL model the backend can use."""

    model_id: str = Field(
        description="The id to send as `model_id`: `heuristic_demo`, or one made from a model zip's file name."
    )
    label: str = Field(description="A name to show.")
    artifact_path: str | None = Field(
        default=None, description="The zip file the model is loaded from. Null for the heuristic fallback."
    )
    adapter_kind: Literal["codeqwen_lora", "codet5_seq2seq", "heuristic"] = Field(
        description=(
            "How the model runs: `codet5_seq2seq` (a CodeT5+ or T5 checkpoint), `codeqwen_lora` (a LoRA adapter "
            "on CodeQwen1.5-7B-Chat) or `heuristic` (rules, no model)."
        )
    )
    prompt_format: str = Field(description="How the question and the schema are written into the model's prompt.")
    available: bool = Field(default=True, description="Always true in this version.")
    notes: str = Field(default="", description="What the model needs to run.")


class DagentsTraceStep(DagentsModel):
    """One framework step the backend ran for a question."""

    name: str = Field(description="What the step did, such as `Dagents pipeline DAG planning`.")
    status: Literal["ok", "warning", "error", "skipped"] = Field(
        description="`ok` when the step answered, `warning` when it raised an error. The backend sends no other value."
    )
    detail: str = Field(description="`completed`, or the error's message.")
    payload: dict[str, Any] | list[Any] | None = Field(
        default=None, description="What the planner command or the service answered. Null when the step failed."
    )


class NL2SQLRequest(DagentsModel):
    """A question to turn into SQL, with the schema it is about."""

    question: str = Field(description="The question in plain English.")
    tables: list[SchemaTable] = Field(description="The tables the SQL may use. They become the DDL in the prompt.")
    model_id: str | None = Field(
        default=None,
        description=(
            "A `model_id` from `GET /api/v1/models`. Without it, `NL2SQL_DEFAULT_MODEL_ID`. An id that is not "
            "listed selects the first listed model."
        ),
    )
    max_new_tokens: int = Field(
        default=128, ge=16, le=512, description="Most tokens a model may generate. The heuristic ignores it."
    )
    use_dagents_services: bool = Field(
        default=True,
        description=(
            "Whether to run the framework steps and return them in `dagents_trace`. With `false`, the trace is "
            "empty and no planner command or service call is made."
        ),
    )


class NL2SQLResponse(DagentsModel):
    """The SQL for a question, the prompt that produced it, and the framework steps."""

    sql: str = Field(description="The generated SQL. It is not run against any database.")
    model_id: str = Field(description="The model that was selected.")
    model_label: str = Field(description="The selected model's label.")
    prompt: str = Field(description="The prompt the model received, with the question and the DDL.")
    schema_ddl: str = Field(description="`CREATE TABLE` statements built from `tables`.")
    adapter_kind: str = Field(description="How the selected model runs, as in the model list.")
    used_fallback: bool = Field(
        default=False,
        description="True when the heuristic wrote the SQL: it was selected, or the selected model could not be loaded.",
    )
    fallback_detail: str | None = Field(default=None, description="Why the heuristic wrote the SQL, when it did.")
    dagents_trace: list[DagentsTraceStep] = Field(
        default_factory=list, description="The framework steps, in the order they ran."
    )


class DagentsServiceStatus(DagentsModel):
    """Whether one framework service answered its health check."""

    name: str = Field(description="The service: `core-service`, `pipeline-service`, `model-service`, `lma` or `gma`.")
    url: str = Field(description="The health address the backend called, from the `*_PUBLIC_URL` settings.")
    status: Literal["ok", "unavailable"] = Field(description="`ok` when the health check answered with a 2xx status.")
    detail: str = Field(description="`reachable`, or the error the call raised.")


class DagentsBackendStatus(DagentsModel):
    """Which framework services the NL2SQL backend can reach."""

    services: list[DagentsServiceStatus] = Field(description="One entry per service, always in the same order.")
