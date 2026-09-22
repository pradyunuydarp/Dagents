"""One simulated hospital, wired to the framework as a federated site.

Each hospital owns a trust boundary. Its records are generated locally, scored
locally, and never handed to the consortium: what leaves is a metric bundle and
a bounded contribution norm, and only after the Ethical Guard has approved it.

The framework supplies the sequence and the guard placement. This module
supplies the two things the framework cannot know: where a site's records come
from (:class:`StrokeDataSource`) and what "train or evaluate" means for stroke
triage (:class:`StrokeLocalRunner`).
"""

from __future__ import annotations

from typing import Any

from agents.common.application.federation_worker import (
    LocalDataSource,
    LocalFederatedWorker,
    LocalRunOutcome,
)
from agents.common.application.governance_service import GovernanceService
from agents.common.domain.federation import FederatedJob, SiteRegistration

from app.domain import stroke_rule
from app.domain.conditions import (
    STROKE_APPROVED_PURPOSES,
    STROKE_CLASSIFICATION,
    STROKE_FEATURE_CONTRACT,
    STROKE_FEATURE_CONTRACT_VERSION,
)
from app.domain.synthetic import HospitalProfile, generate_cohort


class StrokeDataSource:
    """A hospital's local suspected-stroke cohort.

    The cohort is materialized once and kept here, inside the site. Nothing in
    this class hands records to anything outside the hospital's worker; the
    consortium never calls it.
    """

    def __init__(self, profile: HospitalProfile, cohort_size: int) -> None:
        self._profile = profile
        self._records = generate_cohort(profile, cohort_size)

    @property
    def records_held(self) -> list[dict[str, Any]]:
        """The site's own view of its records, for the site's own UI."""
        return self._records

    def records(self, job: FederatedJob) -> list[dict[str, Any]]:
        """Return the records this job may consider.

        The outcome label is stripped for anything but an evaluation task: a
        training round has no business reading the confirmed outcome it is
        supposed to be learning to anticipate.
        """
        if job.task == "evaluate":
            return [dict(record) for record in self._records]
        return [
            {key: value for key, value in record.items() if key != "confirmed_stroke"}
            for record in self._records
        ]

    def cohort_size(self, job: FederatedJob) -> int:
        """How many subjects the cohort holds, without materializing it."""
        return len(self._records)


class StrokeLocalRunner:
    """What a hospital computes for a round.

    Profiling counts and measures completeness. Evaluation scores the rule
    against the site's recorded outcomes. Training, in this demo, evaluates and
    reports the magnitude of the update it would contribute: there is no model
    to fit, and inventing one would obscure the governance path rather than
    demonstrate it.
    """

    def __init__(self, site_id: str) -> None:
        self._site_id = site_id

    def run(self, job: FederatedJob, records: list[dict[str, Any]]) -> LocalRunOutcome:
        if job.task == "profile":
            return LocalRunOutcome(metrics=self._profile_metrics(records), examples=len(records))

        metrics = stroke_rule.evaluate_against_labels(records)
        if not metrics:
            # No labels means nothing to evaluate. Reporting completeness is
            # honest; reporting an AUC computed from nothing would not be.
            metrics = self._profile_metrics(records)

        # A stand-in for the norm of the update this site would contribute,
        # derived from how far its local metrics sit from the seed model's.
        # A real site reports the norm of an actual gradient or weight delta.
        norm = abs(metrics.get("auc", 0.5) - 0.5) * 4.0
        return LocalRunOutcome(
            metrics=metrics,
            examples=len(records),
            update_norm=norm,
            evidence_pointer=f"site-local://{self._site_id}/rounds/{job.round_id}/evidence",
        )

    @staticmethod
    def _profile_metrics(records: list[dict[str, Any]]) -> dict[str, float]:
        """Data-readiness measures, which are what an analytics round is for."""
        if not records:
            return {"record_count": 0.0, "completeness": 0.0}
        required = STROKE_FEATURE_CONTRACT.required_fields()
        present = sum(
            1 for record in records if all(field in record and record[field] is not None for field in required)
        )
        return {
            "record_count": float(len(records)),
            "completeness": present / len(records),
            "label_coverage": sum(1 for r in records if r.get("confirmed_stroke") is not None) / len(records),
        }


class Hospital:
    """A hospital's local Dagents deployment, as this demo simulates it.

    Composes the framework's :class:`LocalFederatedWorker` with a governance
    service carrying the stroke classification, so all three site-side guard
    boundaries are active: before read, before train, and before send.
    """

    def __init__(self, profile: HospitalProfile, cohort_size: int = 400) -> None:
        self.profile = profile
        self.site_id = profile.site_id
        self.display_name = profile.display_name
        self.data_source = StrokeDataSource(profile, cohort_size)
        self.governance = GovernanceService()
        self.governance.register_classification(STROKE_CLASSIFICATION)
        self._worker: LocalFederatedWorker | None = None

    def worker(self, expected_digest: str | None = None) -> LocalFederatedWorker:
        """Build this hospital's federated worker.

        ``expected_digest`` is the manifest digest the hospital computed for
        itself. Passing it is what makes verification meaningful: without it a
        site is only checking a job against the job's own claim.
        """
        self._worker = LocalFederatedWorker(
            site_id=self.site_id,
            governance=self.governance,
            data_source=self.data_source,
            runner=StrokeLocalRunner(self.site_id),
            classification_id=STROKE_CLASSIFICATION.classification_id,
            feature_contract_version=STROKE_FEATURE_CONTRACT_VERSION,
            approved_fields=STROKE_FEATURE_CONTRACT.field_names(),
            approved_purposes=STROKE_APPROVED_PURPOSES,
            expected_digest=expected_digest,
        )
        return self._worker

    def registration(self, conditions: list[str] | None = None) -> SiteRegistration:
        """How this hospital enrols in a study."""
        return SiteRegistration(
            site_id=self.site_id,
            capabilities=["local_profiling", "local_evaluation", "local_training", "telemetry"],
            feature_contract_version=STROKE_FEATURE_CONTRACT_VERSION,
            approved_conditions=conditions or ["suspected_stroke"],
            policy_version="demo-hospital-policy-v1",
            cohort_size=len(self.data_source.records_held),
            enrolled=True,
        )

    def local_worklist(self, limit: int = 25) -> list[dict[str, Any]]:
        """The site's own ranked worklist.

        This is the clinician-facing view, and it stays inside the hospital.
        Nothing here crosses a boundary, which is why it is not guarded: the
        Guard governs egress and cross-boundary reads, not a site reading its
        own records for its own clinicians.
        """
        return stroke_rule.rank_worklist(self.data_source.records_held)[:limit]

    def local_metrics(self) -> dict[str, float]:
        """The site's own evaluation of the rule against its own outcomes."""
        return stroke_rule.evaluate_against_labels(self.data_source.records_held)

    def threshold_sweep(self) -> list[dict[str, float]]:
        """How sensitivity and alert burden trade off at this site."""
        return stroke_rule.sweep_alert_thresholds(self.data_source.records_held)

    def audit_trail(self, limit: int = 50) -> dict[str, Any]:
        """This site's guard decisions and whether its audit chain verifies."""
        return {
            "site_id": self.site_id,
            "chain_intact": self.governance.audit_chain_intact(),
            "records": [record.model_dump(mode="json") for record in self.governance.recent_audit(limit)],
        }
