"""What an LMA started for one site serves about that site.

The site's records come from a :class:`SiteRecordSource`, read as the site's
role. The service keeps the records it read, and a running profile built from
them, until the source's version changes. It asks the source for its version
at most once per ``max_staleness_ms``, or at once when a caller needs current
data (a refresh, a registration, a federated job).

What the service reports leaves the site's records at the site:

- :meth:`SiteService.info` and :meth:`SiteService.summary`: counts, the
  source's kind and location (never a host or a connection string), and the
  label rate only when the site's planner permits a ``before_send`` table
  request for the label at the labelled count;
- :meth:`SiteService.probe`: what the Guard would release, never the rows;
- :meth:`SiteService.audit_summary`: the audit chain's state and length.

The profile, the local evaluation and the records themselves are for the
site's own staff and the site's own jobs.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import logging
import math
import re
import threading
import time
from typing import Any, Callable, Mapping, Protocol
from uuid import uuid4

from dagents.common.application.ethical_guard import unusable_restrictions
from dagents.common.application.governance_service import GovernanceService
from dagents.common.application.ports import SiteRecordSource
from dagents.common.application.running_profile import RunningProfile
from dagents.common.domain.errors import ModelVerificationError
from dagents.common.domain.federation import ModelVersion, SiteRegistration
from dagents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest
from dagents.common.domain.site import (
    AuditSummary,
    LabelRateStatus,
    LocalEvaluation,
    ModelInUse,
    ProbeRequest,
    ProbeResponse,
    SiteInfo,
    SiteSourceInfo,
    SiteSummary,
    SourceState,
)
from dagents.common.extensions import ExtensionError, ExtensionRegistry, ModelKind
from dagents.lma.application.errors import GmaUnavailable, LabelFieldMissing, NoReleasedModel
from dagents.lma.domain.site_config import SiteConfig

logger = logging.getLogger(__name__)

#: Location keys and values that could say where a database is. A source must
#: never report them; the service leaves them out if one does.
_HIDDEN_LOCATION_KEY = re.compile(r"pass|secret|token|dsn|url|uri|host|user|credential", re.IGNORECASE)
_HIDDEN_LOCATION_VALUE = re.compile(r"://|@")


def has_label(record: Mapping[str, Any], label_field: str | None) -> bool:
    """Whether a record carries a label the model kinds can learn: a yes/no value or a finite number."""
    if label_field is None:
        return False
    value = record.get(label_field)
    if isinstance(value, bool):
        return True
    return isinstance(value, (int, float)) and math.isfinite(value)


class ReleasedModelSource(Protocol):
    """Where the site reads the released model: the GMA, through its HTTP API."""

    def released_model(self, known_digest: str | None) -> ModelVersion | None:
        """The released version with its parameters, or ``None`` when its digest is still ``known_digest``.

        Raises :class:`GmaUnavailable`, :class:`NoReleasedModel` or
        :class:`GmaRefused`.
        """


@dataclass(frozen=True)
class _View:
    """The records read at one source version, and what the service derived from them."""

    version: str
    records: list[dict[str, Any]]
    profile: dict[str, Any]
    labelled: int
    positives: int
    negatives: int


class SiteService:
    """One site's records, profile, label-rate decision, Guard probe and model in use.

    Params:
    - `site`: the site's configuration.
    - `registry`: the extensions; the condition pack, feature contract,
      classification and model kinds come from it.
    - `governance`: the site's Guard, with the site's audit chain. It should
      run with strict classifications.
    - `source`: where the site's records come from.
    - `models`: where the released model comes from. ``None`` makes the model
      routes answer 503.
    - `max_staleness_ms`: how long the records read are used before the
      source's version is checked again.
    - `clock`: monotonic seconds; tests replace it.

    Raises ``ExtensionError`` when the registry lacks the condition pack, its
    feature contract or its classification, so a misconfigured LMA fails at
    startup.
    """

    def __init__(
        self,
        site: SiteConfig,
        *,
        registry: ExtensionRegistry,
        governance: GovernanceService,
        source: SiteRecordSource,
        models: ReleasedModelSource | None = None,
        max_staleness_ms: int = 30_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.site = site
        self.registry = registry
        self.governance = governance
        self.source = source
        self._models = models
        self._max_staleness_s = max(int(max_staleness_ms), 0) / 1000.0
        self._clock = clock

        self.pack = registry.condition_pack(site.condition_id)
        self.contract = registry.feature_contract(self.pack.feature_contract_id)
        self.classification = governance.get_classification(self.pack.classification_id)
        self.label_field = self.pack.label_field
        self.approved_purposes = list(self.pack.approved_purposes)
        self.approved_fields = self.contract.field_names()
        self.study_id = site.study_id or site.condition_id
        self.reporting_purpose = site.reporting_purpose or (
            self.approved_purposes[0] if self.approved_purposes else site.condition_id
        )
        self.profile_fields = list(site.profile_fields) or self.contract.field_names()

        self._lock = threading.Lock()
        self._view: _View | None = None
        self._checked_at = 0.0
        self._rate_lock = threading.Lock()
        self._rate_decisions: dict[int, tuple[LabelRateStatus, str | None]] = {}
        self._released_lock = threading.Lock()
        self._released: ModelVersion | None = None
        self._location_warned = False

    # --- Identity --------------------------------------------------------------

    @property
    def site_id(self) -> str:
        return self.site.site_id

    @property
    def condition_id(self) -> str:
        return self.site.condition_id

    @property
    def feature_contract_version(self) -> str:
        return self.contract.version

    @property
    def minimum_cohort(self) -> int:
        return int(self.classification.minimum_cohort)

    # --- The records and the profile -------------------------------------------

    def _current(self, *, check_now: bool = False) -> _View:
        """The records at the source's current version, read again only when the version changed."""
        with self._lock:
            now = self._clock()
            due = self._view is None or check_now or now - self._checked_at >= self._max_staleness_s
            if due:
                version = str(self.source.version())
                if self._view is None or version != self._view.version:
                    self._view = self._read(version)
                self._checked_at = now
            return self._view

    def _read(self, version: str) -> _View:
        records = self.source.records()
        profile = RunningProfile(self.profile_fields)
        profile.add_many(records)
        label = self.label_field
        labelled = sum(1 for record in records if has_label(record, label))
        positives = sum(1 for record in records if label is not None and record.get(label) is True)
        negatives = sum(1 for record in records if label is not None and record.get(label) is False)
        return _View(version, records, profile.snapshot(), labelled, positives, negatives)

    def job_records(self) -> list[dict[str, Any]]:
        """Copies of the site's records at the source's current version, for a federated job."""
        return [dict(record) for record in self._current(check_now=True).records]

    def refresh(self) -> SourceState:
        """Ask the source for its version now and read the records again if it changed."""
        view = self._current(check_now=True)
        return SourceState(record_count=len(view.records), source_version=view.version)

    def profile(self) -> dict[str, Any]:
        """The running profile of the site's records, as ``RunningProfile.snapshot()``. Local only."""
        return copy.deepcopy(self._current().profile)

    # --- What the site reports ---------------------------------------------------

    def _location(self) -> dict[str, str]:
        location = {}
        for key, value in dict(self.source.location() or {}).items():
            key, value = str(key), str(value)
            if _HIDDEN_LOCATION_KEY.search(key) or _HIDDEN_LOCATION_VALUE.search(value):
                if not self._location_warned:
                    self._location_warned = True
                    logger.warning(
                        "site %s: the source reported a location entry that may name a host; it is not shown",
                        self.site_id,
                    )
                continue
            location[key] = value
        return location

    def info(self) -> SiteInfo:
        """Who the site is and what its LMA serves. Never a connection string."""
        view = self._current()
        return SiteInfo(
            site_id=self.site_id,
            condition_id=self.condition_id,
            classification_id=self.classification.classification_id,
            feature_contract_version=self.feature_contract_version,
            label_field=self.label_field,
            source=SiteSourceInfo(
                kind=str(getattr(self.source, "kind", None) or "unknown"),
                location=self._location(),
                role=getattr(self.source, "role", None),
            ),
            record_count=len(view.records),
            source_version=view.version,
            capabilities=list(self.site.capabilities),
            policy_version=self.site.policy_version,
            minimum_cohort=self.minimum_cohort,
        )

    def _reporting_requester(self) -> Requester:
        """The coordinator, as the site's Guard sees it when the site reports its summary.

        The coordinator is trusted through the study, not through a person:
        the verified attributes are the site's enrolment, the feature contract
        it shares and the study's privacy profile.
        """
        return Requester(
            requester_id=self.study_id,
            requester_kind="coordinator",
            affiliation=self.study_id,
            stated_purpose=self.reporting_purpose,
            compliance_history=1.0,
            attributes=[
                KyuAttribute(attribute_id="site_registration", verified=True),
                KyuAttribute(attribute_id="feature_contract_version", verified=True),
                KyuAttribute(attribute_id="privacy_profile", verified=True),
            ],
        )

    def label_rate_status(self, labelled: int) -> tuple[LabelRateStatus, str | None]:
        """Whether the label rate over ``labelled`` records may leave the site, and the planner's reason.

        The site's planner decides, on a ``before_send`` table request for the
        label at that cohort size. :meth:`GovernanceService.plan` records
        nothing, so page views do not grow the audit chain. A decision is kept
        per count. ``withheld`` (the planner could not be asked, or its plan
        names a strategy the Guard cannot apply) is not kept, so the next call
        asks again: the Guard fails closed.
        """
        if self.label_field is None or labelled <= 0:
            return "no_labels", None
        with self._rate_lock:
            known = self._rate_decisions.get(labelled)
        if known is not None:
            return known
        request = RestrictionRequest(
            request_id=f"summary:{self.site_id}:label_rate",
            boundary="before_send",
            requester=self._reporting_requester(),
            classification=self.classification,
            requested_fields=[self.label_field],
            granularity="table",
            cohort_size=labelled,
            declared_purpose=self.reporting_purpose,
            approved_purposes=self.approved_purposes,
        )
        try:
            plan = self.governance.plan(request)
        except Exception:  # noqa: BLE001 - any planner failure withholds the rate
            logger.warning("site %s: the planner could not decide the label rate, so it is withheld", self.site_id)
            return "withheld", None
        if unusable_restrictions(plan):
            # The Guard would deny a request with this plan, so the rate does not leave on it.
            logger.warning(
                "site %s: the planner's plan for the label rate names a strategy the Guard cannot apply, "
                "so the rate is withheld",
                self.site_id,
            )
            return "withheld", None
        decision: tuple[LabelRateStatus, str | None]
        if plan.decision != "deny":
            decision = ("released", None)
        elif any("minimum_cohort" in violation for violation in plan.violations):
            decision = ("below_minimum_cohort", "; ".join(plan.violations))
        else:
            decision = ("denied", "; ".join(plan.violations) or "the planner denied the request")
        with self._rate_lock:
            self._rate_decisions[labelled] = decision
        return decision

    def summary(self) -> SiteSummary:
        """Counts, and the label rate only when the site's planner permits it."""
        view = self._current()
        status, reason = self.label_rate_status(view.labelled)
        boolean = view.positives + view.negatives
        rate = view.positives / boolean if status == "released" and boolean else None
        return SiteSummary(
            site_id=self.site_id,
            record_count=len(view.records),
            labelled_count=view.labelled,
            label_rate=rate,
            label_rate_status=status,
            minimum_cohort=self.minimum_cohort,
            planner_reason=reason,
        )

    def profile_metrics(self, records: list[dict[str, Any]]) -> dict[str, float]:
        """What a profile job reports: counts and completeness from the Guard's payload, label coverage and rate from the site.

        ``label_rate`` is reported only when :meth:`label_rate_status` is
        ``released``.
        """
        if not records:
            return {"record_count": 0.0, "completeness": 0.0}
        required = self.contract.required_fields()
        complete = sum(1 for record in records if all(record.get(name) is not None for name in required))
        metrics = {"record_count": float(len(records)), "completeness": complete / len(records)}
        view = self._current()
        if view.records and self.label_field is not None:
            metrics["label_coverage"] = view.labelled / len(view.records)
        boolean = view.positives + view.negatives
        if boolean and self.label_rate_status(view.labelled)[0] == "released":
            metrics["label_rate"] = view.positives / boolean
        return metrics

    def registration(self) -> SiteRegistration:
        """How the site enrols in a study, with its current record count."""
        view = self._current(check_now=True)
        return SiteRegistration(
            site_id=self.site_id,
            capabilities=list(self.site.capabilities),
            feature_contract_version=self.feature_contract_version,
            approved_conditions=[self.condition_id],
            policy_version=self.site.policy_version,
            cohort_size=len(view.records),
            enrolled=True,
        )

    def audit_summary(self) -> AuditSummary:
        """Whether the site's audit chain verifies, and how many decisions it holds. Never its records."""
        return AuditSummary(
            chain_intact=self.governance.audit_chain_intact(),
            record_count=self.governance.audit_record_count(),
        )

    # --- The Guard probe -----------------------------------------------------------

    def probe(self, request: ProbeRequest) -> ProbeResponse:
        """Run one request through the site's Guard over the site's first records, and report what it would release.

        The LMA adds the site's classification and approved purposes. When the
        probe names no cohort size, the number of records read is used. The
        decision is recorded in the site's audit chain. The response holds the
        released count and field names, never the rows.
        """
        view = self._current()
        rows = [dict(record) for record in view.records[: request.record_limit]]
        fields = list(request.requested_fields) if request.requested_fields is not None else self.contract.field_names()
        restriction = RestrictionRequest(
            request_id=f"probe:{self.site_id}:{uuid4().hex}",
            boundary=request.boundary,
            requester=request.requester,
            classification=self.classification,
            requested_fields=fields,
            granularity=request.granularity,
            cohort_size=request.cohort_size if request.cohort_size is not None else len(rows),
            declared_purpose=request.declared_purpose,
            approved_purposes=self.approved_purposes,
        )
        decision = self.governance.enforce(restriction, rows)
        return ProbeResponse(
            request_id=decision.request_id,
            boundary=decision.boundary,
            permitted=decision.permitted,
            plan=decision.plan,
            released_count=len(decision.payload),
            released_fields=list(dict.fromkeys(name for row in decision.payload for name in row)),
            withheld_fields=list(decision.withheld_fields),
            transformed_fields=list(decision.transformed_fields),
            audit_id=decision.audit_id,
            message=decision.message,
        )

    # --- The model in use ------------------------------------------------------------

    def model_kind(self, kind: str) -> ModelKind:
        """The registered model kind, or :class:`ModelVerificationError` when this site cannot run it."""
        try:
            return self.registry.model_kind(kind)
        except ExtensionError:
            raise ModelVerificationError(f"model kind {kind} is not one this site can run") from None

    def _verified(self, model: ModelVersion) -> tuple[ModelVersion, ModelKind]:
        kind = self.model_kind(model.kind)
        if model.params is None:
            raise ModelVerificationError(f"the GMA sent {model.version} without its parameters")
        if kind.parameter_digest(model.params) != model.digest:
            raise ModelVerificationError(f"the parameters of {model.version} do not match its digest")
        return model, kind

    def model_in_use(self) -> ModelInUse:
        """The released model, read from the GMA and checked by its model kind.

        The service keeps the last verified version and sends its digest, so
        the GMA answers 304 when the released model has not changed. No lock
        is held while the GMA is called.
        """
        model, _ = self._released_model()
        return ModelInUse(
            version=model.version,
            kind=model.kind,
            digest=model.digest,
            status=model.status,
            params=model.params or {},
            parent_version=model.parent_version,
            created_at=model.created_at,
            release_decision=model.release_decision,
        )

    def _released_model(self) -> tuple[ModelVersion, ModelKind]:
        if self._models is None:
            raise GmaUnavailable("this LMA has no GMA to read the released model from")
        with self._released_lock:
            known = self._released
        fetched = self._models.released_model(known.digest if known is not None else None)
        model = fetched if fetched is not None else known
        if model is None:
            raise NoReleasedModel("the GMA has no released model")
        verified, kind = self._verified(model)
        with self._released_lock:
            self._released = verified
        return verified, kind

    def evaluate_model(self) -> LocalEvaluation:
        """The released model measured on the site's own labelled records. The records stay at the site."""
        if self.label_field is None:
            raise LabelFieldMissing(f"condition pack {self.condition_id} names no label field")
        model, kind = self._released_model()
        labelled = [dict(record) for record in self._current().records if has_label(record, self.label_field)]
        metrics = kind.evaluate(model.params or {}, labelled, self.label_field)
        return LocalEvaluation(
            version=model.version,
            labelled_count=len(labelled),
            metrics={str(name): float(value) for name, value in metrics.items()},
        )
