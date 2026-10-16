"""Contracts for one site's LMA: what it says about itself and its records.

An LMA started for a site (a tenant, database, service or other data source)
serves these through its site routes. The shapes hold counts, rates the site's
planner permits, model versions and audit chain state. None of them holds a
record, a connection string or a credential.

Nothing here is specific to one domain. ``label_field`` is whichever field a
condition pack names as the outcome to learn.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field

from agents.common.domain.base import DagentsModel
from agents.common.domain.federation import ModelReleaseDecision, ModelStatus, RoundPhase, SiteParticipation
from agents.common.domain.governance import (
    Granularity,
    GuardBoundary,
    Requester,
    RestrictionPlan,
)

#: Whether the label rate may leave the site, and if not, why.
#:
#: - ``released``: the site's planner permits a table request for the label at
#:   the labelled count.
#: - ``no_labels``: no record has a label, so there is no rate.
#: - ``below_minimum_cohort``: the planner denies it because too few records
#:   are labelled.
#: - ``denied``: the planner denies it for another reason.
#: - ``withheld``: the planner could not be asked, so the rate is withheld.
LabelRateStatus = Literal["released", "no_labels", "below_minimum_cohort", "denied", "withheld"]


class AuditSummary(DagentsModel):
    """The state of an agent's Guard audit chain, without its records."""

    chain_intact: bool = Field(description="Whether the audit chain's digests verify.")
    record_count: int = Field(ge=0, description="How many Guard decisions the chain holds. At least 0.")


class SiteSourceInfo(DagentsModel):
    """Where a site's records come from, named without a connection string."""

    kind: str = Field(description="The kind of source, such as `memory` or `postgres`.")
    location: dict[str, str] = Field(
        default_factory=dict,
        description="Names that place the records, such as a schema. Never a host, a password or a connection string.",
    )
    role: str | None = Field(
        default=None, description="The database role every read runs as. Null for a source with no roles."
    )


class SiteInfo(DagentsModel):
    """Who a site is and what its LMA serves."""

    site_id: str = Field(description="The site's id in the study.")
    condition_id: str = Field(description="The condition pack the site serves.")
    classification_id: str = Field(description="The data classification the site's Guard plans against.")
    feature_contract_version: str = Field(description="The version of the feature contract the site's records follow.")
    label_field: str | None = Field(
        default=None, description="The field that holds the outcome to learn, from the condition pack. Null when the pack names none."
    )
    source: SiteSourceInfo = Field(description="Where the site's records come from.")
    record_count: int = Field(ge=0, description="How many records the source holds. At least 0.")
    source_version: str = Field(
        description="A value that changes whenever the source's records change. The LMA rebuilds its profile when it changes."
    )
    capabilities: list[str] = Field(
        default_factory=list, description="What the site can do in a round, such as `evaluate` or `train`."
    )
    policy_version: str = Field(description="The version of the site's local policy.")
    minimum_cohort: int = Field(
        ge=0, description="The fewest records a group result may describe, from the classification. At least 0."
    )


class SourceState(DagentsModel):
    """The source's record count and version, after the LMA has read them again."""

    record_count: int = Field(ge=0, description="How many records the source holds. At least 0.")
    source_version: str = Field(description="The source's current version.")


class SiteSummary(DagentsModel):
    """What a site reports to the coordinator about its records: counts and a permitted rate."""

    site_id: str = Field(description="The site's id.")
    record_count: int = Field(ge=0, description="How many records the site holds. At least 0.")
    labelled_count: int = Field(ge=0, description="How many records carry a label. At least 0.")
    label_rate: float | None = Field(
        default=None,
        description="The share of labelled records whose label is true. Null unless `label_rate_status` is `released`.",
    )
    label_rate_status: LabelRateStatus = Field(
        description=(
            "`released`, `no_labels`, `below_minimum_cohort`, `denied`, or `withheld` when the planner could "
            "not be asked."
        )
    )
    minimum_cohort: int = Field(ge=0, description="The classification's minimum cohort. At least 0.")
    planner_reason: str | None = Field(
        default=None,
        description="The planner's reason when it denied the rate, such as its violation. Null otherwise.",
    )


class ProbeRequest(DagentsModel):
    """A question for the site's Guard, run over the site's first records.

    The LMA adds the site's classification and approved purposes itself, so a
    body that names its own is refused.
    """

    model_config = ConfigDict(extra="forbid")

    boundary: GuardBoundary = Field(description="Where the Guard stands: `before_read`, `before_train`, `before_send` or `before_release`.")
    granularity: Granularity = Field(description="How much is asked for: `cell`, `row`, `column`, `table` or `model_update`.")
    requested_fields: list[str] | None = Field(
        default=None, description="The fields asked for. Null asks for every field of the site's feature contract."
    )
    cohort_size: int | None = Field(
        default=None,
        ge=0,
        description="How many subjects the result describes. Null uses the number of records the probe reads. At least 0.",
    )
    record_limit: int = Field(
        default=5, ge=0, le=20, description="How many of the site's first records the Guard is applied to. 0 to 20."
    )
    requester: Requester = Field(description="Who asks. The planner computes the requester's trust from it.")
    declared_purpose: str | None = Field(
        default=None, description="Why the data is wanted. It must be one of the condition pack's approved purposes."
    )


class ProbeResponse(DagentsModel):
    """What the site's Guard would release for a probe. Never the records themselves."""

    request_id: str = Field(description="The id the LMA gave the request. It is in the audit record.")
    boundary: GuardBoundary = Field(description="Copied from the probe.")
    permitted: bool = Field(description="Whether the Guard permitted the request.")
    plan: RestrictionPlan = Field(description="The plan the Guard applied.")
    released_count: int = Field(ge=0, description="How many rows the Guard would release. At least 0.")
    released_fields: list[str] = Field(
        default_factory=list, description="The field names in the released rows, in the order they first appear."
    )
    withheld_fields: list[str] = Field(default_factory=list, description="The requested fields the Guard removed.")
    transformed_fields: list[str] = Field(
        default_factory=list, description="The requested fields the Guard returned with changed values."
    )
    audit_id: str = Field(description="The id of the record written to the site's audit chain.")
    message: str = Field(default="", description="The decision in words.")


class ModelInUse(DagentsModel):
    """The released model, as the site's LMA reads it from the GMA."""

    version: str = Field(description="The released version.")
    kind: str = Field(description="The model kind that runs it.")
    digest: str = Field(description="The digest of the parameters, checked by the model kind.")
    status: ModelStatus = Field(description="The version's status in the GMA's registry; `released` when it is in use.")
    params: dict[str, Any] = Field(description="The parameters, in the form the model kind reads.")
    parent_version: str | None = Field(
        default=None,
        description="The version the training round that made it started from, as the GMA's registry records it. Null for a registered version.",
    )
    created_at: str | None = Field(
        default=None, description="When the GMA's registry stored the version, in ISO 8601. Null when the GMA sent none."
    )
    release_decision: ModelReleaseDecision | None = Field(
        default=None, description="The release decision the GMA stored with the version. Null for a version released without one."
    )


class LocalEvaluation(DagentsModel):
    """The released model measured on the site's own labelled records. The records stay at the site."""

    version: str = Field(description="The version that was measured.")
    labelled_count: int = Field(ge=0, description="How many labelled records the model was measured on. At least 0.")
    metrics: dict[str, float] = Field(default_factory=dict, description="The model kind's metrics, by name.")


class JobLogEntry(DagentsModel):
    """One federated job, as the site's LMA recorded it in its job log."""

    round_id: str = Field(description="The round the job belonged to.")
    phase: RoundPhase = Field(description="The round's phase: `analytics`, `evaluation` or `training`.")
    participation: SiteParticipation = Field(description="What the site returned: `completed`, `rejected`, `failed` and so on.")
    contributed_examples: int = Field(default=0, ge=0, description="How many examples the site used. At least 0.")
    update_norm: float | None = Field(
        default=None, description="The L2 norm of the clipped update the site sent. Null when it sent none."
    )
    update_digest: str | None = Field(default=None, description="The digest of the update the site sent. Null when it sent none.")
    job_digest: str = Field(default="", description="The round digest the job carried.")
    metrics: dict[str, float] = Field(default_factory=dict, description="The metrics the site returned.")
    reason: str | None = Field(default=None, description="Why the site refused the job. Null for a completed job.")
    submitted_at: str | None = Field(
        default=None, description="When the entry was written, in ISO 8601. Set by the job log."
    )


class SiteDirectoryEntry(DagentsModel):
    """One site, as the GMA's site directory reports it from the site's LMA."""

    site_id: str = Field(description="The site's id.")
    reachable: bool = Field(description="Whether the site's LMA answered.")
    source_kind: str | None = Field(
        default=None, description="The kind of source the LMA reports, such as `postgres`. Null when it did not answer."
    )
    summary: SiteSummary | None = Field(default=None, description="The site's summary. Null when the LMA did not answer.")
    audit: AuditSummary | None = Field(
        default=None, description="The site's audit chain state, when asked for. Null otherwise, or when the LMA did not answer."
    )
