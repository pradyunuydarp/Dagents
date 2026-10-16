"""Agent control-plane contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from dagents.common.domain.base import DagentsModel


class AgentIdentity(DagentsModel):
    """Who an agent is: its id, its workspace and its kind."""

    agent_id: str = Field(description="The agent's id. The GMA keys its registry by it.")
    workspace_id: str = Field(description="The workspace, or tenant, the agent belongs to.")
    name: str = Field(description="A readable name for the agent.")
    agent_type: str = Field(default="LMA", description="`LMA` or `GMA`.")


class RegisterRequest(DagentsModel):
    """An agent's registration with the GMA."""

    agent: AgentIdentity = Field(description="Who the agent is. The GMA stores the agent under `agent.agent_id`.")
    scope: dict[str, str] = Field(
        default_factory=dict, description="Labels for what the agent covers, such as a tenant or a source. Stored."
    )
    version: str = Field(description="The agent's software version. Stored.")
    capabilities: list[str] = Field(
        default_factory=list, description="What the agent can do. Stored, and copied into the response."
    )


class RegisterResponse(DagentsModel):
    """The GMA's answer to a registration."""

    accepted: bool = Field(description="Always true: the GMA accepts every registration.")
    deployment_plan_id: str | None = Field(
        default=None,
        description="The `plan_token` of the desired deployment stored for this agent. Null when there is none.",
    )
    capabilities: list[str] = Field(default_factory=list, description="Copied from the request.")


class HeartbeatRequest(DagentsModel):
    """A sign of life from an agent, with its state."""

    agent: AgentIdentity = Field(
        description="The agent sending the heartbeat. An agent that never registered is added, with version `unknown`."
    )
    status: str = Field(
        description=(
            "The agent's state in its own words, such as `ACTIVE`. Stored as the agent's `status`. The overview "
            "counts an agent as active when this is `ACTIVE` or `SYNC_REQUIRED`."
        )
    )
    timestamp: int = Field(description="When the agent sent it, in Unix seconds. Stored as `last_seen_at`; not checked.")


class HeartbeatResponse(DagentsModel):
    """The GMA's answer to a heartbeat."""

    ack: bool = Field(description="Always true.")
    desired_state: str = Field(
        description=(
            "`SYNC_REQUIRED` when the agent's record has a desired bundle that differs from the bundle it last "
            "reported, otherwise `ACTIVE`."
        )
    )


class TelemetryPoint(DagentsModel):
    """One measured value."""

    key: str = Field(description="The metric's name, such as `local_checks_total`.")
    value: float = Field(description="The measured value.")
    labels: dict[str, str] = Field(
        default_factory=dict, description="Labels that say what the value is about, such as `bundle_id`."
    )
    observed_at: int = Field(description="When the value was measured, in Unix seconds.")


class TelemetryEnvelope(DagentsModel):
    """The telemetry one run or one agent sends: who sent it, the values, and pointers to the evidence."""

    agent: AgentIdentity = Field(description="The agent that sent the telemetry.")
    metrics: list[TelemetryPoint] = Field(default_factory=list, description="The measured values.")
    pointers: dict[str, str] = Field(
        default_factory=dict,
        description="References to evidence kept elsewhere, such as the run's scope and the bundle's address.",
    )


class TelemetryAck(DagentsModel):
    """The GMA's receipt for one telemetry envelope."""

    ack: bool = Field(description="Always true.")
    ingestion_id: str = Field(
        description="`telemetry-<n>`, where n counts the envelopes this GMA has received since it started, from 1."
    )


class DeploymentSyncRequest(DagentsModel):
    """An agent's report of the bundle it runs now."""

    agent: AgentIdentity = Field(
        description="The agent reporting. An agent that never registered is added, with version `unknown`."
    )
    bundle_id: str = Field(description="The bundle the agent runs now. Stored as `current_bundle_id`.")
    bundle_version: str = Field(description="The version the agent runs now. Stored as `current_bundle_version`.")


class DeploymentSyncResponse(DagentsModel):
    """Whether the agent runs the bundle the GMA wants it to run."""

    up_to_date: bool = Field(
        description="True when the agent runs the desired bundle and version, or when no deployment is planned for it."
    )
    plan_token: str = Field(description="The desired deployment's `plan_token`. Empty when none is planned.")
    config_digest: str = Field(description="The desired deployment's `config_digest`. Empty when none is planned.")
    desired_bundle_id: str | None = Field(default=None, description="The bundle to run. Null when none is planned.")
    desired_bundle_version: str | None = Field(
        default=None, description="The version to run. Null when none is planned."
    )
    desired_bundle_uri: str | None = Field(
        default=None, description="Where the bundle's files are. Null when none is planned."
    )


class DeployBundleCommand(DagentsModel):
    """A monitoring bundle for an LMA to store, so that later runs can name it."""

    agent: AgentIdentity = Field(description="The agent the bundle is for. The LMA accepts any value and does not store it.")
    bundle_id: str = Field(description="The bundle's id.")
    bundle_version: str = Field(
        description=(
            "The bundle's version. A bundle is stored under its id and version together; deploying the same "
            "pair again replaces it."
        )
    )
    bundle_uri: str = Field(default="", description="Where the bundle's files are. Stored, not fetched.")
    signature: bytes = Field(default=b"", description="The bundle's signature, as text. Stored, not checked in this version.")


class TriggerRunCommand(DagentsModel):
    agent: AgentIdentity
    correlation_id: str
    bundle_id: str
    bundle_version: str
    scope: dict[str, str] = Field(default_factory=dict)


class CommandStatus(DagentsModel):
    """The answer to a command: whether it was carried out, and what was done."""

    accepted: bool = Field(description="True when the command was carried out.")
    message: str = Field(description="What was done, in words.")


class RunRequest(DagentsModel):
    """A request to run a deployed monitoring bundle once."""

    correlation_id: str = Field(
        description=(
            "An id for this run, chosen by the caller. The run is stored as `run-<correlation_id>`; a second "
            "run with the same id is stored as another entry, not in place of the first."
        )
    )
    bundle_id: str = Field(description="The id of the bundle to run.")
    bundle_version: str = Field(
        description=(
            "The version of the bundle to run. The run goes ahead when no such bundle is deployed; its "
            "`bundle_uri` pointer is then empty."
        )
    )
    scope: dict[str, str] = Field(
        default_factory=dict,
        description="Labels for what the run covers, such as a table or a tenant. `local_checks_total` counts them, with a minimum of 1.",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Extra labels. `workspace_id` sets the workspace of the agent named in the telemetry, and "
            "`metadata_fields_total` counts the entries."
        ),
    )


class BundleRecord(DagentsModel):
    """A monitoring bundle the LMA has stored."""

    bundle_id: str = Field(description="The bundle's id.")
    bundle_version: str = Field(description="The bundle's version.")
    bundle_uri: str = Field(default="", description="Where the bundle's files are, as deployed.")
    signature: bytes = Field(default=b"", description="The signature sent with the bundle, as text.")
    deployed_at: int = Field(description="When the bundle was last deployed, in Unix seconds.")
    config_digest: str = Field(
        default="", description="SHA-256, in hex, of `<bundle_id>:<bundle_version>:<bundle_uri>`."
    )


class RunRecord(DagentsModel):
    """One recorded monitoring run."""

    run_id: str = Field(description="`run-` followed by the correlation id.")
    correlation_id: str = Field(description="Copied from the request.")
    bundle_id: str = Field(description="Copied from the request.")
    bundle_version: str = Field(description="Copied from the request.")
    scope: dict[str, str] = Field(default_factory=dict, description="Copied from the request.")
    status: Literal["COMPLETED", "FAILED"] = Field(
        default="COMPLETED",
        description="`COMPLETED`. The in-memory runner cannot fail, so this version never records `FAILED`.",
    )
    started_at: int = Field(description="When the run started, in Unix seconds.")
    completed_at: int = Field(description="When the run ended, in Unix seconds.")
    metrics_emitted: int = Field(description="How many telemetry values the run published.")
    pointers: dict[str, str] = Field(
        default_factory=dict,
        description="The telemetry's pointers: `scope` (the scope as text), `bundle_uri` and `correlation_id`.",
    )


class RunExecutionResponse(DagentsModel):
    """The result of a monitoring run: the stored run record and the telemetry it published."""

    run: RunRecord = Field(description="The run record, also listed by `GET /runs`.")
    telemetry: TelemetryEnvelope = Field(description="The telemetry the run published, also listed by `GET /telemetry`.")


class DesiredDeploymentRequest(DagentsModel):
    """The bundle the GMA should want one agent to run."""

    agent_id: str = Field(description="The agent the deployment is for.")
    bundle_id: str = Field(description="The bundle to run.")
    bundle_version: str = Field(description="The version to run.")
    bundle_uri: str = Field(default="", description="Where the bundle's files are. Stored, not fetched.")
    config: dict[str, str] = Field(default_factory=dict, description="Settings for the bundle. Stored, and part of the digest.")


class DeploymentPlan(DagentsModel):
    """A stored desired deployment for one agent."""

    agent_id: str = Field(description="Copied from the request.")
    bundle_id: str = Field(description="Copied from the request.")
    bundle_version: str = Field(description="Copied from the request.")
    bundle_uri: str = Field(default="", description="Copied from the request.")
    config: dict[str, str] = Field(default_factory=dict, description="Copied from the request.")
    plan_token: str = Field(description="`plan-` and the first 12 hex digits of `config_digest`.")
    config_digest: str = Field(
        description=(
            "SHA-256, in hex, of `<agent_id>:<bundle_id>:<bundle_version>:<bundle_uri>:<config>`, with the config "
            "written in Python dict notation. The same request gives the same digest."
        )
    )
    created_at: int = Field(description="When the plan was stored, in Unix seconds.")


class RunDispatchRequest(DagentsModel):
    """A request to record a run for an agent."""

    agent_id: str = Field(description="The agent that should run it. It must be registered, or the dispatch is refused.")
    correlation_id: str = Field(description="An id for the run, chosen by the caller.")
    bundle_id: str = Field(description="The bundle to run.")
    bundle_version: str = Field(description="The version to run.")
    scope: dict[str, str] = Field(default_factory=dict, description="Labels for what the run covers, such as a table.")


class DispatchedRun(DagentsModel):
    """A run dispatch the GMA has recorded."""

    agent_id: str = Field(description="Copied from the request.")
    correlation_id: str = Field(description="Copied from the request.")
    bundle_id: str = Field(description="Copied from the request.")
    bundle_version: str = Field(description="Copied from the request.")
    scope: dict[str, str] = Field(default_factory=dict, description="Copied from the request.")
    dispatched_at: int = Field(description="When the dispatch was recorded, in Unix seconds.")
    status: str = Field(description="Always `QUEUED`. Nothing in this version runs a dispatch or changes its status.")


class AgentSnapshot(DagentsModel):
    """What the GMA knows about one agent."""

    agent: AgentIdentity = Field(description="The identity from the agent's last registration or report.")
    scope: dict[str, str] = Field(
        default_factory=dict, description="From the last registration. Empty when the agent never registered."
    )
    version: str = Field(description="From the last registration. `unknown` when the agent never registered.")
    capabilities: list[str] = Field(
        default_factory=list, description="From the last registration. Empty when the agent never registered."
    )
    status: str = Field(default="REGISTERED", description="`REGISTERED` until the first heartbeat, then the last heartbeat's status.")
    last_seen_at: int | None = Field(
        default=None, description="The `timestamp` of the last heartbeat. Null before the first."
    )
    last_heartbeat_status: str | None = Field(
        default=None, description="The status of the last heartbeat. Null before the first."
    )
    last_ingestion_id: str | None = Field(
        default=None,
        description="The `ingestion_id` of the last telemetry envelope received while the agent was known. Null before one.",
    )
    desired_bundle_id: str | None = Field(
        default=None, description="From the last desired deployment stored while the agent was known. Null before one."
    )
    desired_bundle_version: str | None = Field(
        default=None, description="From the last desired deployment stored while the agent was known. Null before one."
    )
    current_bundle_id: str | None = Field(
        default=None, description="From the agent's last deployment sync. Null before one."
    )
    current_bundle_version: str | None = Field(
        default=None, description="From the agent's last deployment sync. Null before one."
    )


class TelemetrySummary(DagentsModel):
    """The telemetry one agent has sent, added up."""

    agent_id: str = Field(description="The agent that sent the envelopes.")
    envelopes: int = Field(description="How many envelopes it sent.")
    metric_points: int = Field(description="How many values those envelopes held.")
    latest_observed_at: int | None = Field(
        default=None, description="The latest `observed_at` among the values. Null when the envelopes held none."
    )
    metric_totals: dict[str, float] = Field(
        default_factory=dict, description="Each metric key, with the sum of its values."
    )


class FleetOverview(DagentsModel):
    """Counts of what the GMA holds."""

    total_agents: int = Field(description="Agents in the registry.")
    active_agents: int = Field(description="Agents whose status is `ACTIVE` or `SYNC_REQUIRED`.")
    telemetry_events: int = Field(description="Telemetry envelopes received, counted up to 100,000.")
    pending_deployments: int = Field(
        description="Agents whose desired bundle differs from the bundle they last reported."
    )
    dispatched_runs: int = Field(
        description="Run dispatches plus aggregate model runs, each counted up to 100,000."
    )


class StepOutput(DagentsModel):
    step_id: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)
