"""The coordinator's side: plan a round, collect results, decide on release.

This is the role the GMA (global agent) plays. It sees what the stores report
(counts, metrics, digests) and never reads an order.
"""

from __future__ import annotations

from dagents.common.application.federation import FederatedRoundController, InProcessFederationEngine
from dagents.common.application.governance_service import GovernanceService
from dagents.common.domain.federation import (
    AggregationMethod,
    CandidateModel,
    ReleaseDecision,
    RoundManifest,
    RoundRecord,
)
from dagents.common.domain.governance import GuardDecision, KyuAttribute, Requester, RestrictionRequest
from dagents.common.extensions import default_registry

from shop import rule
from shop.contract import APPROVED_PURPOSES, CLASSIFICATION, CONTRACT_VERSION
from shop.store_site import StoreSite

STUDY_ID = "returns-pilot"

#: The people who approve a release. Their identity is checked elsewhere; the
#: Guard reads the result of those checks as verified attributes.
RELEASE_COMMITTEE = Requester(
    requester_id="returns-release-committee",
    requester_kind="human",
    affiliation="returns-team",
    stated_purpose="order_returns",
    attributes=[
        KyuAttribute(attribute_id="verified_identity", verified=True),
        KyuAttribute(attribute_id="signed_study_protocol", verified=True),
    ],
    compliance_history=0.9,
)


class Coordinator:
    def __init__(self, sites: list[StoreSite]) -> None:
        self.sites = {site.site_id: site for site in sites}
        # The in-process engine runs every store's worker in this process. It is
        # a simulator for tests and examples; a deployment plugs in a federated
        # runtime behind the same FederationEngine interface.
        self.engine = InProcessFederationEngine()
        self.controller = FederatedRoundController(engine=self.engine)
        # The coordinator's own Guard, for the before_release boundary.
        self.governance = GovernanceService()
        for site in sites:
            self.controller.register_site(site.registration())

    def manifest(self, round_id: str) -> RoundManifest:
        """The round contract every store checks before it accepts."""
        return RoundManifest(
            round_id=round_id,
            study_id=STUDY_ID,
            condition_id="order_returns",
            phase="evaluation",
            model_version=rule.RULE_VERSION,
            model_artifact_digest=rule.RULE_DIGEST,
            training_code_digest=rule.RULE_DIGEST,
            feature_contract_version=CONTRACT_VERSION,
            privacy_profile="guarded-rows",
            aggregation=AggregationMethod(kind="fedavg"),
            minimum_participants=2,
            minimum_cohort_per_site=CLASSIFICATION.minimum_cohort,
            required_capabilities=["evaluate"],
            stop_conditions=["schema_failure", "quorum_not_met"],
            invited_sites=sorted(self.sites),
        )

    def run_round(self, manifest: RoundManifest) -> RoundRecord:
        """Give each store a worker for this round, then plan and dispatch it."""
        for site in self.sites.values():
            self.engine.register_worker(site.worker(manifest))
        return self.controller.dispatch_round(manifest)

    def release_check(self, round_id: str, candidate: CandidateModel) -> GuardDecision:
        """Ask the Guard whether the committee may release this candidate (before_release)."""
        request = RestrictionRequest(
            request_id=f"{round_id}:release",
            boundary="before_release",
            requester=RELEASE_COMMITTEE,
            classification=self.governance.get_classification(CLASSIFICATION.classification_id),
            requested_fields=["model_update"],
            granularity="model_update",
            cohort_size=candidate.contributed_examples,
            declared_purpose="order_returns",
            approved_purposes=APPROVED_PURPOSES,
        )
        return self.governance.enforce(request, correlation_id=round_id)

    def decide(self, round_id: str, candidate: CandidateModel) -> ReleaseDecision:
        """Evaluate the condition pack's release gates against the candidate."""
        gates = default_registry.condition_pack("order_returns").release_gates
        # If the candidate is not released, the stores keep the rule they use now.
        return self.controller.evaluate_release(
            round_id, gates, candidate, rollback_version=rule.IN_USE_VERSION
        )
