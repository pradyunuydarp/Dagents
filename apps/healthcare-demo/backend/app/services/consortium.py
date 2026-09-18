"""The consortium: a governed federated pilot across three hospitals.

This is the app's top-level orchestration, and it is thin by design. Every
decision that matters — which sites are eligible, whether quorum holds, whether
contributions may be aggregated, whether a candidate may be released — belongs
to the framework, and is delegated to it. What this module contributes is the
study: which hospitals, which condition, which gates, and what the resulting
evidence should look like on screen.

The pilot follows the recommended order. Analytics first, so the consortium can
prove every site produces the same measures before anything is trained.
Evaluation next. Training only after both. A round that skips ahead is a round
nobody can interpret.
"""

from __future__ import annotations

import time
from typing import Any

from agents.common.application.federation import (
    FederatedRoundController,
    InProcessFederationEngine,
)
from agents.common.application.governance_service import GovernanceService
from agents.common.domain.federation import (
    AggregationMethod,
    RoundManifest,
    RoundPhase,
)
from agents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest

from app.domain.conditions import (
    STROKE_APPROVED_PURPOSES,
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT,
    STROKE_FEATURE_CONTRACT_VERSION,
    STROKE_RELEASE_GATES,
)
from app.domain.synthetic import DEFAULT_HOSPITALS, HospitalProfile
from app.services.hospital import Hospital

#: The study this demo runs.
STUDY_ID = "stroke-triage-pilot"

#: The model version the consortium starts from.
#:
#: A seed is created centrally from public, consented, or synthetic data, never
#: by copying hospital records into the coordinator. Here it is the rule itself,
#: which is why there is no artifact to sign.
SEED_MODEL_VERSION = "stroke-rule-seed-v1"

#: The baseline a candidate has to beat, standing in for the current release.
BASELINE_METRICS = {"auc": 0.78, "sensitivity": 0.60, "subgroup_auc_gap": 0.09, "alerts_per_1000": 300.0}


class Consortium:
    """Runs governed federated rounds across the demo's hospitals."""

    def __init__(
        self,
        profiles: list[HospitalProfile] | None = None,
        cohort_size: int = 400,
        secure_aggregation_threshold: int = 3,
    ) -> None:
        self.hospitals: dict[str, Hospital] = {
            profile.site_id: Hospital(profile, cohort_size) for profile in (profiles or DEFAULT_HOSPITALS)
        }
        self._engine = InProcessFederationEngine()
        self.controller = FederatedRoundController(engine=self._engine)
        self.governance = GovernanceService()
        self.governance.register_classification(STROKE_CLASSIFICATION)
        self._secure_threshold = secure_aggregation_threshold
        for hospital in self.hospitals.values():
            self.controller.register_site(hospital.registration())

    def manifest(self, round_id: str, phase: RoundPhase = "analytics") -> RoundManifest:
        """Build the round contract every site verifies before accepting.

        Secure aggregation is the default profile, so the coordinator cannot
        resolve any single hospital's contribution. Its threshold raises the
        participation floor, which is what makes that guarantee real rather
        than declared.
        """
        return RoundManifest(
            round_id=round_id,
            study_id=STUDY_ID,
            condition_id=STROKE_CONDITION.condition_id,
            phase=phase,
            model_version=SEED_MODEL_VERSION,
            model_artifact_digest="sha256:demo-seed-rule",
            training_code_digest="sha256:demo-stroke-rule-v1",
            feature_contract_version=STROKE_FEATURE_CONTRACT_VERSION,
            privacy_profile="stroke-consortium-profile-v1",
            aggregation=AggregationMethod(kind="secure_aggregation", threshold=self._secure_threshold),
            minimum_participants=2,
            minimum_cohort_per_site=20,
            required_capabilities=["local_evaluation"],
            stop_conditions=["schema_failure", "quorum_not_met", "unsafe_metric"],
            invited_sites=sorted(self.hospitals),
        )

    def _attach_workers(self, manifest: RoundManifest) -> str:
        """Give every hospital the digest it independently verifies against."""
        digest = self.controller.digest(manifest)
        for hospital in self.hospitals.values():
            self._engine.register_worker(hospital.worker(expected_digest=digest))
        return digest

    def run_round(self, round_id: str, phase: RoundPhase = "analytics") -> dict[str, Any]:
        """Plan, dispatch, and collect one round, reporting what happened."""
        manifest = self.manifest(round_id, phase)
        digest = self._attach_workers(manifest)
        record = self.controller.dispatch_round(manifest)
        readiness = None
        if record.plan.quorum_met:
            readiness = self.controller.evaluate_readiness(round_id)
        return {
            "round_id": round_id,
            "phase": phase,
            "digest": digest,
            "plan": record.plan.model_dump(mode="json"),
            "results": [result.model_dump(mode="json") for result in record.results],
            "readiness": readiness.model_dump(mode="json") if readiness else None,
            "status": record.status,
        }

    def run_pilot(self, round_prefix: str | None = None) -> dict[str, Any]:
        """Run the full pilot: analytics, evaluation, then a governed release.

        Returns everything a reviewer needs to check the run without trusting
        the summary: the per-round plans and exclusions, the contributions that
        were accepted and rejected, the candidate, the gate-by-gate verdict, and
        every site's audit chain.
        """
        prefix = round_prefix or f"round-{int(time.time())}"

        # 1. Analytics first: prove every site can produce the same measures
        #    before anything is trained on anything.
        analytics = self.run_round(f"{prefix}-analytics", "analytics")

        # 2. Baseline evaluation: how the seed performs at each site today.
        evaluation = self.run_round(f"{prefix}-evaluation", "evaluation")

        # 3. Training, which produces contributions but reads no outcome labels.
        training_id = f"{prefix}-training"
        training = self.run_round(training_id, "training")

        candidate = None
        validation = None
        decision = None
        if training["readiness"] and training["readiness"]["aggregation_permitted"]:
            candidate_model = self.controller.aggregate(training_id)

            # 4. Cross-site validation. A candidate's metrics come from testing
            #    it at each hospital, not from the round that produced it: a
            #    training round has no labels, so reading metrics off it would
            #    report nothing and call it evidence.
            validation_id = f"{prefix}-candidate-validation"
            validation = self.run_round(validation_id, "evaluation")
            if validation["readiness"] and validation["readiness"]["aggregation_permitted"]:
                validated = self.controller.aggregate(validation_id)
                candidate_model = candidate_model.model_copy(update={"metrics": validated.metrics})

            candidate = candidate_model.model_dump(mode="json")

            # 5. Release evaluation, itself a governed act.
            release_check = self.release_guard_decision(training_id, candidate_model.candidate_version)
            decision = {
                "guard": release_check,
                "gates": self.controller.evaluate_release(
                    training_id,
                    STROKE_RELEASE_GATES,
                    candidate_model,
                    baseline_metrics=BASELINE_METRICS,
                    rollback_version=SEED_MODEL_VERSION,
                ).model_dump(mode="json"),
            }

        return {
            "study_id": STUDY_ID,
            "condition": STROKE_CONDITION.model_dump(mode="json"),
            "engine": self.controller.engine_name,
            "rounds": {
                "analytics": analytics,
                "evaluation": evaluation,
                "training": training,
                "candidate_validation": validation,
            },
            "candidate": candidate,
            "release": decision,
            "baseline_metrics": BASELINE_METRICS,
            "site_audits": {
                site_id: hospital.audit_trail() for site_id, hospital in sorted(self.hospitals.items())
            },
            "coordinator_audit": {
                "chain_intact": self.governance.audit_chain_intact(),
                "records": [r.model_dump(mode="json") for r in self.governance.recent_audit()],
            },
            "notice": (
                "All patient data in this run is synthetic. The scoring rule is a transparent "
                "demonstration, not a validated triage model, and no output here is clinical advice."
            ),
        }

    def release_guard_decision(self, round_id: str, candidate_version: str) -> dict[str, Any]:
        """Run the Guard at the release boundary before the gates are evaluated.

        Evaluating a release is itself a governed act. Running the Guard first
        means the decision to consider a candidate carries an audit record, not
        just the decision that comes out of it.
        """
        requester = Requester(
            requester_id="stroke-consortium-release-committee",
            requester_kind="human",
            affiliation="stroke-triage-consortium",
            stated_purpose="stroke_triage_research",
            compliance_history=0.95,
            attributes=[
                KyuAttribute(attribute_id="verified_committee_identity", verified=True),
                KyuAttribute(attribute_id="signed_study_protocol", verified=True),
                KyuAttribute(attribute_id="governance_training", verified=True),
            ],
        )
        decision = self.governance.enforce(
            RestrictionRequest(
                request_id=f"{round_id}:release:{candidate_version}",
                boundary="before_release",
                requester=requester,
                classification=STROKE_CLASSIFICATION,
                requested_fields=["model_update"],
                granularity="model_update",
                cohort_size=sum(len(h.data_source.records_held) for h in self.hospitals.values()),
                declared_purpose="stroke_triage_research",
                approved_purposes=STROKE_APPROVED_PURPOSES,
            ),
            [{"model_update": 1.0}],
            correlation_id=round_id,
        )
        return decision.model_dump(mode="json")

    def overview(self) -> dict[str, Any]:
        """A consortium-level summary for the app's landing view."""
        return {
            "study_id": STUDY_ID,
            "condition_id": STROKE_CONDITION.condition_id,
            "feature_contract": {
                "contract_id": STROKE_FEATURE_CONTRACT.contract_id,
                "version": STROKE_FEATURE_CONTRACT.version,
                "required_fields": STROKE_FEATURE_CONTRACT.required_fields(),
            },
            "engine": self.controller.engine_name,
            "secure_aggregation_threshold": self._secure_threshold,
            "hospitals": [
                {
                    "site_id": hospital.site_id,
                    "display_name": hospital.display_name,
                    "cohort_size": len(hospital.data_source.records_held),
                    "local_metrics": hospital.local_metrics(),
                }
                for hospital in sorted(self.hospitals.values(), key=lambda h: h.site_id)
            ],
            "release_gates": [gate.model_dump(mode="json") for gate in STROKE_RELEASE_GATES],
            "baseline_metrics": BASELINE_METRICS,
        }
