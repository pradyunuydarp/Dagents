"""One hospital's local Dagents deployment: the site, or LMA, of this demo.

A hospital reads and writes its records only through its own store
(:mod:`app.services.site_store`): its own schema and role in Postgres, or its
own in-memory store in a local run. Everything here runs at the hospital.

What the hospital shows its own staff, such as the worklist, the running
statistics and the local charts, comes from its own records and never leaves the
hospital. What it reports to the coordinator is limited to:

- :meth:`Hospital.summary`: counts, and an outcome rate only when the
  hospital's Guard planner permits it for the number of labelled records;
- :meth:`Hospital.registration`: what the site can do and how many records it
  holds;
- the results of the federated jobs it accepts. The Ethical Guard checks each
  job before it reads data, and again before anything leaves.

The hospital keeps a cached view of its records, cleared on every write, and a
:class:`RunningProfile` of per-field statistics. The profile is built from the
store on first use and then updated one record at a time as records are
entered, instead of being recomputed.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Callable, Mapping

from agents.common.application.federation_worker import LocalFederatedWorker, LocalRunOutcome, UpdateChannel
from agents.common.application.governance_service import GovernanceService
from agents.common.application.running_profile import RunningProfile
from agents.common.domain.federation import FederatedJob, JobAcceptance, RoundManifest, SiteRegistration, SiteResult
from agents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest

from app.domain import stroke_model, stroke_rule
from app.domain.conditions import (
    STROKE_APPROVED_PURPOSES,
    STROKE_CLASSIFICATION,
    STROKE_FEATURE_CONTRACT,
    STROKE_FEATURE_CONTRACT_VERSION,
    STROKE_FIELD_DOMAINS,
    STROKE_LABEL_FIELD,
    STROKE_STUDY_ID,
)
from app.domain.dataset import AGE_BANDS, Site
from app.services.runtime import ModelRegistry, ModelVerificationError, RegisteredModel, code_digest, update_digest
from app.services.site_store import CoordinatorStore, SiteStore

LABEL = STROKE_LABEL_FIELD

#: Fields the running profile tracks: provenance, the exact age (which stays at
#: the site) and every contract field.
PROFILE_FIELDS: tuple[str, ...] = ("origin", "age", *STROKE_FEATURE_CONTRACT.field_names())

#: What a hospital can do in a round. The manifest names the capability each
#: phase needs.
CAPABILITIES = ["local_profiling", "local_evaluation", "local_training", "telemetry"]

#: Most records one hospital accepts from the demo's data-entry form. The public
#: demo has no accounts, so this keeps one visitor from filling a store.
DEFAULT_MAX_ENTERED = 300

#: Findings the presentation chart shows, in display order.
PRESENTATION_FIELDS: tuple[str, ...] = (
    "face_weakness",
    "arm_weakness",
    "leg_weakness",
    "speech_difficulty",
    "visual_field_loss",
    "atrial_fibrillation",
)


#: Why a hospital refuses a job whose digest is not the one it computed.
DIGEST_MISMATCH = "job digest does not match the manifest this site computed"


class EntryLimitReached(RuntimeError):
    """The hospital already holds the most entered records the demo accepts."""


def _check_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive whole number, got {limit!r}")
    return limit


def _refused_result(job: FederatedJob, site_id: str, reason: str) -> SiteResult:
    return SiteResult(
        round_id=job.round_id,
        site_id=site_id,
        job_digest=job.manifest_digest,
        participation="rejected",
        code_verified=False,
        privacy_checks_passed=False,
        contributed_examples=0,
        metrics={},
        local_evidence_pointer=f"site-local://{site_id}/{job.round_id}#refused:{reason}",
    )


class SiteDataSource:
    """The records a federated job at this hospital may consider.

    The outcome label is kept for training and evaluation, which need it, and
    stays at the site: the job returns metrics and a bounded update, never rows.
    The label is removed for profiling, so the profiling code never reads an
    individual outcome.
    """

    def __init__(self, hospital: "Hospital") -> None:
        self._hospital = hospital

    def records(self, job: FederatedJob) -> list[dict[str, Any]]:
        rows = self._hospital._local_records()
        if job.task == "profile":
            return [{key: value for key, value in row.items() if key != LABEL} for row in rows]
        return [dict(row) for row in rows]

    def cohort_size(self, job: FederatedJob) -> int:
        return len(self._hospital._local_records())


class SiteRunner:
    """What a hospital computes for each task, on the Guard's payload.

    - ``profile``: record count and completeness from the payload, and label
      coverage and outcome rate from the hospital's running profile. The rate
      is reported only when :meth:`Hospital.rate_may_leave` permits it.
    - ``evaluate``: the model the manifest names, measured with
      :func:`stroke_rule.evaluate_against_labels`, plus its log loss.
    - ``train``: :func:`stroke_model.train_local` from the model the manifest
      names. The update is the change in parameters.

    The model is loaded from the registry and its digest checked against the
    manifest before it is used.
    """

    def __init__(self, hospital: "Hospital", registry: ModelRegistry) -> None:
        self._hospital = hospital
        self._registry = registry

    def run(self, job: FederatedJob, records: list[dict[str, Any]]) -> LocalRunOutcome:
        pointer = f"site-local://{self._hospital.site_id}/rounds/{job.round_id}/evidence"
        if job.task == "profile":
            return LocalRunOutcome(
                metrics=self._profile_metrics(records), examples=len(records), evidence_pointer=pointer
            )

        model = self._registry.load(job.manifest.model_version, job.manifest.model_artifact_digest)
        labelled = [record for record in records if isinstance(record.get(LABEL), bool)]
        if job.task == "evaluate":
            metrics = stroke_rule.evaluate_against_labels(labelled, score_fn=model.score)
            loss = _score_log_loss(model, labelled)
            if loss is not None:
                metrics["log_loss"] = loss
            return LocalRunOutcome(metrics=metrics, examples=len(labelled), evidence_pointer=pointer)

        if model.kind != stroke_model.MODEL_KIND:
            raise ModelVerificationError(f"{model.version} is a {model.kind}, which cannot be trained")
        if not labelled:
            return LocalRunOutcome(metrics={"record_count": 0.0}, examples=0, evidence_pointer=pointer)
        local = stroke_model.train_local(model.params, labelled)
        metrics = {
            "start_loss": stroke_model.log_loss(model.params, labelled),
            "train_loss": stroke_model.log_loss(local, labelled),
            "record_count": float(len(labelled)),
        }
        return LocalRunOutcome(
            metrics=metrics,
            examples=len(labelled),
            evidence_pointer=pointer,
            update=stroke_model.delta(local, model.params),
        )

    def _profile_metrics(self, records: list[dict[str, Any]]) -> dict[str, float]:
        if not records:
            return {"record_count": 0.0, "completeness": 0.0}
        required = STROKE_FEATURE_CONTRACT.required_fields()
        complete = sum(1 for record in records if all(record.get(name) is not None for name in required))
        metrics = {"record_count": float(len(records)), "completeness": complete / len(records)}
        positives, negatives = self._hospital._label_counts()
        total = self._hospital._profile_record_count()
        if total:
            metrics["label_coverage"] = (positives + negatives) / total
        if self._hospital.rate_may_leave(positives + negatives):
            metrics["outcome_rate"] = positives / (positives + negatives)
        return metrics


def _score_log_loss(model: RegisteredModel, labelled: list[dict[str, Any]]) -> float | None:
    """Mean log loss of a model's scores over labelled records it could score."""
    total = 0.0
    count = 0
    for record in labelled:
        score = model.score(record)
        if score is None:
            continue
        p = min(max(score, 1e-12), 1.0 - 1e-12)
        total -= math.log(p) if record[LABEL] else math.log(1.0 - p)
        count += 1
    return total / count if count else None


class HospitalWorker:
    """The hospital's side of a round, around the framework's worker.

    Before it accepts a job, the hospital checks the model the manifest names:
    it must be in the registry, its parameters must match the manifest's
    digest, and the manifest's code digest must be the code this hospital runs
    for that kind of model. After each job it records what it sent in the
    coordinator store's site contributions, as itself.

    ``expected_digest`` is the manifest digest the hospital computed. The
    framework's worker checks it when a job is offered. :meth:`execute` checks
    it again, because a hospital in its own process receives the offer and the
    execution as two separate requests.
    """

    def __init__(
        self, hospital: "Hospital", inner: LocalFederatedWorker, expected_digest: str | None = None
    ) -> None:
        self.site_id = hospital.site_id
        self._hospital = hospital
        self._inner = inner
        self._expected_digest = expected_digest

    def offer(self, job: FederatedJob) -> JobAcceptance:
        reason = self._hospital.verify_manifest(job.manifest)
        if reason is not None:
            acceptance = JobAcceptance(
                round_id=job.round_id,
                site_id=self.site_id,
                accepted=False,
                reason=reason,
                verified_digest=False,
                feature_contract_version=self._hospital.feature_contract_version,
            )
        else:
            acceptance = self._inner.offer(job)
        if not acceptance.accepted:
            self._hospital.record_contribution(
                job.manifest.phase,
                _refused_result(job, self.site_id, acceptance.reason),
                reason=acceptance.reason,
            )
        return acceptance

    def execute(self, job: FederatedJob) -> SiteResult:
        if self._expected_digest and job.manifest_digest != self._expected_digest:
            result = _refused_result(job, self.site_id, DIGEST_MISMATCH)
            self._hospital.record_contribution(job.manifest.phase, result, reason=DIGEST_MISMATCH)
            return result
        try:
            result = self._inner.execute(job)
        except ModelVerificationError as error:
            result = _refused_result(job, self.site_id, str(error))
        # A refused job names its reason in the evidence pointer; the hospital
        # keeps it with the contribution so its staff can see why.
        pointer = result.local_evidence_pointer or ""
        reason = pointer.split("#refused:", 1)[1] if "#refused:" in pointer else None
        self._hospital.record_contribution(job.manifest.phase, result, reason=reason)
        return result


class Hospital:
    """One hospital: its store, its Guard, its running profile and its worker.

    Params:
    - `site`: the hospital's identity, schema and role.
    - `store_factory`: builds the hospital's store on first use, so building a
      ``Hospital`` reads nothing.
    - `registry`: where the hospital loads the models a round names.
    - `contributions`: builds the store the hospital writes its round
      contributions to, acting as itself. In Postgres it uses the hospital's
      own connection string.
    - `max_entered`: most records the data-entry form may add.
    - `feature_contract_version`: the contract this hospital produces.
    - `series_slot`: the hospital's chart colour slot (1-3), the same on its
      own charts and on the consortium charts. ``None`` when the consortium has
      more hospitals than colours.
    """

    def __init__(
        self,
        site: Site,
        *,
        store_factory: Callable[[], SiteStore],
        registry: ModelRegistry,
        contributions: Callable[[], CoordinatorStore],
        max_entered: int = DEFAULT_MAX_ENTERED,
        feature_contract_version: str = STROKE_FEATURE_CONTRACT_VERSION,
        series_slot: int | None = None,
    ) -> None:
        self.site = site
        self.series_slot = series_slot
        self.site_id = site.site_id
        self.display_name = site.display_name
        self.country = site.country
        self.feature_contract_version = feature_contract_version
        self.minimum_cohort = STROKE_CLASSIFICATION.minimum_cohort
        self.max_entered = max_entered
        self._store_factory = store_factory
        self._contributions_factory = contributions
        self._registry = registry
        self._store: SiteStore | None = None
        self._contribution_store: CoordinatorStore | None = None
        self._governance: GovernanceService | None = None
        self._lock = threading.RLock()
        self._records: list[dict[str, Any]] | None = None
        self._profile: RunningProfile | None = None
        self._profile_ids: set[str] = set()
        self._rate_decisions: dict[int, str] = {}

    # --- Stores, built on first use -------------------------------------------

    @property
    def store(self) -> SiteStore:
        """The hospital's own store."""
        with self._lock:
            if self._store is None:
                self._store = self._store_factory()
            return self._store

    @property
    def contribution_store(self) -> CoordinatorStore:
        with self._lock:
            if self._contribution_store is None:
                self._contribution_store = self._contributions_factory()
            return self._contribution_store

    @property
    def governance(self) -> GovernanceService:
        """The hospital's Guard. Its audit chain is the store's audit sink."""
        with self._lock:
            if self._governance is None:
                governance = GovernanceService(audit=self.store.audit_sink())
                governance.register_classification(STROKE_CLASSIFICATION)
                self._governance = governance
            return self._governance

    def close(self) -> None:
        """Release the database connections of the stores built so far.

        The cached records and the profile stay. A later operation opens a new
        connection.
        """
        with self._lock:
            for store in (self._store, self._contribution_store):
                if store is not None:
                    store.close()

    # --- The cached records view and the running profile -------------------------

    def _local_records(self) -> list[dict[str, Any]]:
        """Every record, read from the store once and kept until the next write.

        The list is shared. Callers copy a record before changing it.
        """
        with self._lock:
            if self._records is None:
                self._records = self.store.records()
            return self._records

    def _invalidate(self) -> None:
        with self._lock:
            self._records = None

    def _ensure_profile(self, expected_total: int | None = None) -> RunningProfile:
        """Build the profile from the store on first use.

        If the store holds a different number of records than the profile has
        seen, for example because another process wrote to it, the cache is
        cleared and the profile built again.
        """
        with self._lock:
            comparable = expected_total is not None and self._profile is not None
            if comparable and self._profile.record_count != expected_total:
                self._profile = None
                self._records = None
            if self._profile is None:
                rows = self._local_records()
                profile = RunningProfile(PROFILE_FIELDS)
                profile.add_many(rows)
                self._profile = profile
                self._profile_ids = {row["encounter_id"] for row in rows}
            return self._profile

    def _label_counts(self) -> tuple[int, int]:
        """``(poor outcome, no poor outcome)`` among labelled records."""
        snapshot = self._ensure_profile().snapshot()
        booleans = snapshot["fields"].get(LABEL, {}).get("booleans") or {}
        return int(booleans.get("true", 0)), int(booleans.get("false", 0))

    def _profile_record_count(self) -> int:
        return self._ensure_profile().record_count

    def _relabel(self, old: bool | None, new: bool) -> None:
        """Move one record's outcome in the profile from ``old`` to ``new``.

        The profile has no remove operation, so the change is made on its
        snapshot, which restores exactly.
        """
        with self._lock:
            if self._profile is None:
                return
            snapshot = self._profile.snapshot()
            stats = snapshot["fields"][LABEL]
            counts = {"false": 0, "true": 0, **(stats.get("booleans") or {})}
            if old is not None:
                counts["true" if old else "false"] -= 1
            counts["true" if new else "false"] += 1
            stats["booleans"] = counts if counts["true"] + counts["false"] else {}
            stats["present"] = stats["present"] + (0 if old is not None else 1)
            stats["missing"] = snapshot["record_count"] - stats["present"]
            self._profile = RunningProfile.from_snapshot(snapshot)

    def profile_snapshot(self) -> dict[str, Any]:
        """The running profile of the hospital's records. Local only."""
        with self._lock:
            return self._ensure_profile().snapshot()

    # --- What the hospital reports ------------------------------------------------

    def counts(self) -> dict[str, int]:
        """``{"total", "entered", "labelled"}``, from the store."""
        return self.store.count()

    def record_count(self) -> int:
        """How many records the store holds now.

        If that differs from what the cached records and profile have seen, for
        example because the store was seeded by another process after this one
        read it empty, the cache is cleared so the next read sees the change.
        """
        total = self.counts()["total"]
        with self._lock:
            self._ensure_profile(expected_total=total)
        return total

    def _reporting_requester(self) -> Requester:
        """The coordinator, as this hospital's Guard sees it when the hospital reports a summary.

        The coordinator is trusted through the study, not through a person. The
        verified attributes are the hospital's enrolment, the feature contract
        it shares and the study's privacy profile.
        """
        return Requester(
            requester_id=STROKE_STUDY_ID,
            requester_kind="coordinator",
            affiliation=STROKE_STUDY_ID,
            stated_purpose="stroke_triage_research",
            compliance_history=1.0,
            attributes=[
                KyuAttribute(attribute_id="site_registration", verified=True),
                KyuAttribute(attribute_id="feature_contract_version", verified=True),
                KyuAttribute(attribute_id="privacy_profile", verified=True),
            ],
        )

    def rate_may_leave(self, labelled: int) -> bool:
        """Whether the outcome rate over ``labelled`` records may leave this hospital.

        ``True`` only when :meth:`outcome_rate_status` is ``released``. It is
        ``False`` when the planner denies the request or cannot be asked, so
        the Guard fails closed.
        """
        return self.outcome_rate_status(labelled) == "released"

    def outcome_rate_status(self, labelled: int) -> str:
        """Why the outcome rate over ``labelled`` records may or may not leave this hospital.

        - ``released``: the planner permits it.
        - ``no_outcomes``: no record has an outcome, so there is no rate.
        - ``below_minimum_cohort``: the planner denies it, and its reason is
          the minimum cohort.
        - ``denied``: the planner denies it for another reason.
        - ``withheld``: the planner cannot be asked, so the Guard withholds it.

        The hospital asks its Guard's planner about a ``before_send`` table
        request for the outcome at that cohort size. The planner's cohort rule
        decides, and the status repeats the planner's reason; this method does
        not compare the count with the minimum itself.

        :meth:`GovernanceService.plan` records nothing, so page views do not
        grow the audit chain. A status is kept per count; ``withheld`` is not,
        so the next call asks again.
        """
        if labelled <= 0:
            return "no_outcomes"
        with self._lock:
            known = self._rate_decisions.get(labelled)
        if known is not None:
            return known
        request = RestrictionRequest(
            request_id=f"summary:{self.site_id}:outcome_rate",
            boundary="before_send",
            requester=self._reporting_requester(),
            classification=STROKE_CLASSIFICATION,
            requested_fields=[LABEL],
            granularity="table",
            cohort_size=labelled,
            declared_purpose="stroke_triage_research",
            approved_purposes=STROKE_APPROVED_PURPOSES,
        )
        try:
            plan = self.governance.plan(request)
        except Exception:  # noqa: BLE001 - any planner failure withholds the rate
            return "withheld"
        if plan.decision != "deny":
            status = "released"
        elif any("minimum_cohort" in violation for violation in plan.violations):
            status = "below_minimum_cohort"
        else:
            status = "denied"
        with self._lock:
            self._rate_decisions[labelled] = status
        return status

    def summary(self) -> dict[str, Any]:
        """What the hospital reports to the coordinator about its records.

        Counts, and the share of labelled records with a poor outcome only when
        the planner permits it (otherwise ``None``). ``outcome_rate_status``
        says why (see :meth:`outcome_rate_status`), so a page shows the reason
        instead of guessing it.
        """
        counts = self.counts()
        with self._lock:
            self._ensure_profile(expected_total=counts["total"])
            positives, negatives = self._label_counts()
        labelled = positives + negatives
        status = self.outcome_rate_status(labelled)
        return {
            "site_id": self.site_id,
            "display_name": self.display_name,
            "country": self.country,
            "record_count": counts["total"],
            "entered_count": counts["entered"],
            "labelled_count": counts["labelled"],
            "outcome_rate": positives / labelled if status == "released" else None,
            "outcome_rate_status": status,
            "minimum_cohort": self.minimum_cohort,
            "store": {"kind": self.store.kind, **self.store.location},
        }

    def registration(self, conditions: list[str] | None = None) -> SiteRegistration:
        """How this hospital enrols in a study, with its current record count."""
        return SiteRegistration(
            site_id=self.site_id,
            capabilities=list(CAPABILITIES),
            feature_contract_version=self.feature_contract_version,
            approved_conditions=conditions or ["suspected_stroke"],
            policy_version="demo-hospital-policy-v1",
            cohort_size=self.counts()["total"],
            enrolled=True,
        )

    def verify_manifest(self, manifest: RoundManifest) -> str | None:
        """Why this hospital refuses a round's model, or ``None`` if it verifies."""
        try:
            model = self._registry.load(manifest.model_version, manifest.model_artifact_digest)
            expected_code = code_digest(model.kind)
        except ModelVerificationError as error:
            return str(error)
        if manifest.training_code_digest != expected_code:
            return f"the round's code digest is not the code this site runs for a {model.kind}"
        return None

    def worker(self, expected_digest: str | None = None, update_channel: UpdateChannel | None = None) -> HospitalWorker:
        """Build this hospital's worker for one round.

        ``expected_digest`` is the manifest digest the hospital computed for
        itself. ``update_channel`` is where a training job's clipped update
        goes; without one the framework's worker refuses to send an update.
        """
        inner = LocalFederatedWorker(
            site_id=self.site_id,
            governance=self.governance,
            data_source=SiteDataSource(self),
            runner=SiteRunner(self, self._registry),
            classification_id=STROKE_CLASSIFICATION.classification_id,
            feature_contract_version=self.feature_contract_version,
            approved_fields=STROKE_FEATURE_CONTRACT.field_names(),
            approved_purposes=STROKE_APPROVED_PURPOSES,
            expected_digest=expected_digest,
            update_channel=update_channel,
            # The runtime digests updates with the model's parameter digest, so
            # the hospital uses the same rule to check what the runtime stored.
            update_digest=update_digest,
        )
        return HospitalWorker(self, inner, expected_digest=expected_digest)

    def record_contribution(self, phase: str, result: SiteResult, reason: str | None = None) -> dict[str, Any]:
        """Record what this hospital sent in a round, acting as the hospital."""
        payload: dict[str, Any] = {
            "participation": result.participation,
            "contributed_examples": result.contributed_examples,
            "update_norm": result.update_norm,
            "update_digest": result.update_digest,
            "metrics": dict(result.metrics),
            "job_digest": result.job_digest,
        }
        if reason:
            payload["reason"] = reason
        return self.contribution_store.add_contribution(self.site, result.round_id, phase, payload)

    def contributions(self, limit: int = 20) -> list[dict[str, Any]]:
        """What this hospital sent in recent rounds, newest first, read as the hospital."""
        _check_limit(limit)
        rows = self.contribution_store.site_contributions(self.site)
        rows = rows[-limit:][::-1]
        return [
            {
                "round_id": row["round_id"],
                "phase": row["phase"],
                "participation": row["payload"].get("participation"),
                "contributed_examples": row["payload"].get("contributed_examples"),
                "update_norm": row["payload"].get("update_norm"),
                "metrics": row["payload"].get("metrics", {}),
                "reason": row["payload"].get("reason"),
                "submitted_at": row["submitted_at"],
            }
            for row in rows
        ]

    # --- What the hospital's own staff see -----------------------------------------

    def model_in_use(self) -> RegisteredModel:
        return self._registry.model_in_use()

    def local_metrics(self, model: RegisteredModel | None = None) -> dict[str, Any]:
        """The model in use, measured on this hospital's own labelled records."""
        model = model or self.model_in_use()
        rows = self._local_records()
        labelled = [row for row in rows if isinstance(row.get(LABEL), bool)]
        return {
            "model_version": model.version,
            "labelled_records": len(labelled),
            "metrics": stroke_rule.evaluate_against_labels(labelled, score_fn=model.score),
        }

    def dashboard(self, contribution_limit: int = 20) -> dict[str, Any]:
        """Everything the hospital view shows about this hospital."""
        model = self.model_in_use()
        summary = self.summary()
        return {
            "site_id": self.site_id,
            "display_name": self.display_name,
            "country": self.country,
            "store": summary["store"],
            "counts": {
                "total": summary["record_count"],
                "entered": summary["entered_count"],
                "labelled": summary["labelled_count"],
            },
            "outcome_rate": summary["outcome_rate"],
            "outcome_rate_status": summary["outcome_rate_status"],
            "minimum_cohort": self.minimum_cohort,
            "max_entered": self.max_entered,
            "profile": self.profile_snapshot(),
            "local_metrics": self.local_metrics(model),
            "current_model": model.summary(),
            "contributions": self.contributions(contribution_limit),
        }

    def worklist(self, limit: int = 12) -> list[dict[str, Any]]:
        """Records ranked by the model in use, highest risk first.

        Nothing is removed. Records the model cannot score go to the end.
        """
        _check_limit(limit)
        model = self.model_in_use()
        scored = [{**row, "assessment": model.assess(row)} for row in self._local_records()]
        scored.sort(key=lambda item: (not item["assessment"]["suppressed"], item["assessment"]["score"]), reverse=True)
        return scored[:limit]

    def local_records(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Copies of this hospital's first records, for code that runs at the hospital.

        The planner trace uses it. Nothing it returns is sent to the coordinator.
        """
        rows = self._local_records()
        if limit is not None:
            rows = rows[: _check_limit(limit)]
        return [dict(row) for row in rows]

    def threshold_sweep(self) -> list[dict[str, float]]:
        """Sensitivity, specificity and alerts at a range of thresholds, for the model in use."""
        model = self.model_in_use()
        return stroke_rule.sweep_alert_thresholds(self._local_records(), score_fn=model.score)

    def entered(self, limit: int = 20) -> list[dict[str, Any]]:
        """Records entered in the demo, newest first, each with its assessment."""
        model = self.model_in_use()
        return [{**row, "assessment": model.assess(row)} for row in self.store.list_entered(_check_limit(limit))]

    def enter(self, fields: Mapping[str, Any], client_request_id: str | None = None) -> dict[str, Any]:
        """Store a new record, as this hospital, and update the running profile.

        Params:
        - `fields`: ``age`` and contract fields. ``None`` means missing. The
          store refuses any other key and checks every value.
        - `client_request_id`: the idempotency key. Repeating it with the same
          fields returns the first record and changes nothing.

        Returns ``{record, assessment, counts, profile}``.

        Raises the store's ``RecordRejected`` or ``IdempotencyKeyReused``, and
        :class:`EntryLimitReached` when the hospital already holds
        :attr:`max_entered` entered records. A retry of an earlier entry is
        refused too once the limit is reached.
        """
        with self._lock:
            if self.counts()["entered"] >= self.max_entered:
                raise EntryLimitReached(
                    f"{self.display_name} already holds {self.max_entered} entered records, "
                    "the most this demo accepts"
                )
            self._ensure_profile()
            record = self.store.append(fields, client_request_id)
            if record["encounter_id"] not in self._profile_ids and self._profile is not None:
                self._profile.add({key: record[key] for key in record if key in PROFILE_FIELDS})
                self._profile_ids.add(record["encounter_id"])
            self._invalidate()
            counts = self.counts()
            profile = self.profile_snapshot()
        return {
            "record": record,
            "assessment": self.model_in_use().assess(record),
            "counts": counts,
            "profile": profile,
        }

    def set_outcome(self, encounter_id: str, poor_outcome_6m: bool) -> dict[str, Any]:
        """Record the six-month outcome of an entered record.

        Returns ``{record, counts, profile}``. Raises the store's
        ``UnknownEncounterError`` or ``RecordRejected``.
        """
        with self._lock:
            self._ensure_profile()
            previous = next(
                (row.get(LABEL) for row in self._local_records() if row["encounter_id"] == encounter_id), None
            )
            record = self.store.set_outcome(encounter_id, poor_outcome_6m)
            if encounter_id in self._profile_ids:
                self._relabel(previous, poor_outcome_6m)
            self._invalidate()
            counts = self.counts()
            profile = self.profile_snapshot()
        return {"record": record, "counts": counts, "profile": profile}

    def audit_summary(self) -> dict[str, Any]:
        """Whether this hospital's audit chain verifies, and how many decisions it holds.

        This is what the hospital reports to the coordinator about its audit
        chain. The records themselves stay at the hospital (see
        :meth:`audit_trail`).
        """
        governance = self.governance
        return {
            "site_id": self.site_id,
            "chain_intact": governance.audit_chain_intact(),
            "record_count": governance.audit_record_count(),
        }

    def audit_trail(self, limit: int = 50) -> dict[str, Any]:
        """This hospital's Guard decisions and whether its audit chain verifies. Local only."""
        return {
            "site_id": self.site_id,
            "chain_intact": self.governance.audit_chain_intact(),
            "records": [record.model_dump(mode="json") for record in self.governance.recent_audit(limit)],
        }

    def guard_probe(self, restriction: RestrictionRequest, record_limit: int = 5) -> dict[str, Any]:
        """Run one request through this hospital's Guard over its first records.

        When the request names no cohort size, the number of records passed in
        is used. The decision is recorded in the hospital's audit chain.

        The probe is asked from the coordinator's page, so the hospital reports
        what its Guard would release, never the records: ``released_count``
        and ``released_fields`` (the field names in the released records, in
        the order they first appear), with ``withheld_fields``,
        ``transformed_fields``, the plan and the audit id.
        """
        rows = [dict(row) for row in self._local_records()[: max(int(record_limit), 0)]]
        if restriction.cohort_size is None:
            restriction = restriction.model_copy(update={"cohort_size": len(rows)})
        decision = self.governance.enforce(restriction, rows)
        return {
            "request_id": decision.request_id,
            "boundary": decision.boundary,
            "permitted": decision.permitted,
            "plan": decision.plan.model_dump(mode="json"),
            "released_count": len(decision.payload),
            "released_fields": list(dict.fromkeys(name for row in decision.payload for name in row)),
            "withheld_fields": list(decision.withheld_fields),
            "transformed_fields": list(decision.transformed_fields),
            "audit_id": decision.audit_id,
            "message": decision.message,
        }

    # --- Data for the hospital's charts ---------------------------------------------

    def outcome_by_age_band(self) -> dict[str, Any]:
        """Labelled records and poor outcomes per age band, youngest first."""
        rows = self._local_records()
        labelled = [0] * len(AGE_BANDS)
        poor = [0] * len(AGE_BANDS)
        unlabelled = [0] * len(AGE_BANDS)
        for row in rows:
            band = row.get("age_band")
            if band not in AGE_BANDS:
                continue
            index = AGE_BANDS.index(band)
            if isinstance(row.get(LABEL), bool):
                labelled[index] += 1
                poor[index] += 1 if row[LABEL] else 0
            else:
                unlabelled[index] += 1
        return {
            "categories": list(AGE_BANDS),
            "labelled": labelled,
            "poor_outcome": poor,
            "outcome_rate": [p / n if n else None for p, n in zip(poor, labelled, strict=True)],
            "unlabelled": unlabelled,
        }

    def presentation_shares(self) -> dict[str, Any]:
        """For each finding, how many records have it among those that recorded it."""
        rows = self._local_records()
        found = [sum(1 for row in rows if row.get(name) is True) for name in PRESENTATION_FIELDS]
        recorded = [sum(1 for row in rows if isinstance(row.get(name), bool)) for name in PRESENTATION_FIELDS]
        return {
            "fields": list(PRESENTATION_FIELDS),
            "labels": [STROKE_FIELD_DOMAINS[name].label for name in PRESENTATION_FIELDS],
            "with_finding": found,
            "recorded": recorded,
            "share": [f / r if r else None for f, r in zip(found, recorded, strict=True)],
        }

    def systolic_bp_histogram(self, bin_width: int = 20) -> dict[str, Any]:
        """Systolic blood pressure counts in bins over the contract's range, 60-300 mmHg."""
        low, high = 60, 300
        if bin_width <= 0 or (high - low) % bin_width:
            raise ValueError(f"bin_width must divide {high - low}")
        edges = list(range(low, high + 1, bin_width))
        counts = [0] * (len(edges) - 1)
        missing = 0
        for row in self._local_records():
            value = row.get("systolic_bp")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                missing += 1
                continue
            index = min(int((value - low) // bin_width), len(counts) - 1)
            if 0 <= index:
                counts[index] += 1
        return {"bin_edges": edges, "counts": counts, "missing": missing, "unit": "mmHg"}

    def risk_score_distribution(self, bins: int = 10) -> dict[str, Any]:
        """Scores of the model in use, in equal bins from 0 to 1, by recorded outcome."""
        model = self.model_in_use()
        edges = [round(index / bins, 4) for index in range(bins + 1)]
        series = {"poor outcome": [0] * bins, "no poor outcome": [0] * bins, "outcome not recorded": [0] * bins}
        not_scored = 0
        for row in self._local_records():
            score = model.score(row)
            if score is None:
                not_scored += 1
                continue
            index = min(int(score * bins), bins - 1)
            label = row.get(LABEL)
            key = "poor outcome" if label is True else "no poor outcome" if label is False else "outcome not recorded"
            series[key][index] += 1
        return {
            "model_version": model.version,
            "bin_edges": edges,
            "series": series,
            "alert_threshold": stroke_rule.ALERT_THRESHOLD,
            "not_scored": not_scored,
        }
