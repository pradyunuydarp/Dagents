"""One store's side of Dagents: its store, its Guard and its federated worker.

This is the role an LMA (local agent) plays. Each store has its own Guard and
its own audit log, and nothing here reads another store's orders.
"""

from __future__ import annotations

from typing import Any

from dagents.common.application.federation_worker import LocalFederatedWorker, LocalRunOutcome
from dagents.common.application.governance_service import GovernanceService
from dagents.common.domain.federation import FederatedJob, JobAcceptance, RoundManifest, SiteRegistration, SiteResult
from dagents.common.domain.governance import GuardDecision, Requester, RestrictionRequest
from dagents.common.infrastructure.dagents_runner import round_digest

from shop import rule
from shop.contract import APPROVED_PURPOSES, CLASSIFICATION, CONTRACT_VERSION, FEATURE_CONTRACT
from shop.store import OrderStore


class StoreDataSource:
    """The orders a round may read at this store."""

    def __init__(self, store: OrderStore) -> None:
        self._store = store

    def records(self, job: FederatedJob) -> list[dict[str, Any]]:
        return self._store.orders()

    def cohort_size(self, job: FederatedJob) -> int:
        return self._store.count()


class RuleRunner:
    """What a store computes in a round: the rule's metrics on the Guard's payload."""

    def run(self, job: FederatedJob, records: list[dict[str, Any]]) -> LocalRunOutcome:
        labelled = [record for record in records if isinstance(record.get("returned"), bool)]
        return LocalRunOutcome(metrics=rule.evaluate(labelled), examples=len(labelled))


class StoreWorker:
    """The framework's worker, plus one check this application adds.

    Before it accepts a job, the store checks that the round names the rule it
    runs. The framework's worker then checks the manifest digest, the contract
    version and the before_train Guard.
    """

    def __init__(self, site_id: str, inner: LocalFederatedWorker) -> None:
        self.site_id = site_id
        self._inner = inner

    def offer(self, job: FederatedJob) -> JobAcceptance:
        if job.manifest.model_artifact_digest != rule.RULE_DIGEST:
            return JobAcceptance(
                round_id=job.round_id,
                site_id=self.site_id,
                accepted=False,
                reason="the round names a rule this store does not run",
            )
        return self._inner.offer(job)

    def execute(self, job: FederatedJob) -> SiteResult:
        return self._inner.execute(job)


class StoreSite:
    """One store, acting as its own local agent."""

    def __init__(self, store: OrderStore) -> None:
        self.site_id = store.site_id
        self.store = store
        # Each store has its own Guard and its own audit log.
        self.governance = GovernanceService()

    def registration(self) -> SiteRegistration:
        """How this store enrols in the study."""
        return SiteRegistration(
            site_id=self.site_id,
            capabilities=["evaluate"],
            feature_contract_version=CONTRACT_VERSION,
            approved_conditions=["order_returns"],
            cohort_size=self.store.count(),
            enrolled=True,
        )

    def read(self, requester: Requester, fields: list[str], purpose: str) -> GuardDecision:
        """Read orders for someone at this store, through the Guard (before_read)."""
        request = RestrictionRequest(
            request_id=f"{self.site_id}:read:{requester.requester_id}",
            boundary="before_read",
            requester=requester,
            classification=self.governance.get_classification(CLASSIFICATION.classification_id),
            requested_fields=fields,
            granularity="row",
            cohort_size=self.store.count(),
            declared_purpose=purpose,
            approved_purposes=APPROVED_PURPOSES,
        )
        return self.governance.enforce(request, self.store.orders())

    def summary(self, requester: Requester, fields: list[str], purpose: str) -> GuardDecision:
        """What this store may report about all its orders (table granularity)."""
        request = RestrictionRequest(
            request_id=f"{self.site_id}:summary:{requester.requester_id}",
            boundary="before_send",
            requester=requester,
            classification=self.governance.get_classification(CLASSIFICATION.classification_id),
            requested_fields=fields,
            granularity="table",
            cohort_size=self.store.count(),
            declared_purpose=purpose,
            approved_purposes=APPROVED_PURPOSES,
        )
        return self.governance.enforce(request, self.store.orders())

    def worker(self, manifest: RoundManifest) -> StoreWorker:
        """This store's worker for one round.

        The store computes the manifest digest itself, so a job whose manifest
        was changed on the way does not match and is refused.
        """
        expected = round_digest(manifest.model_dump(mode="json"))["round_digest"]
        inner = LocalFederatedWorker(
            site_id=self.site_id,
            governance=self.governance,
            data_source=StoreDataSource(self.store),
            runner=RuleRunner(),
            classification_id=CLASSIFICATION.classification_id,
            feature_contract_version=CONTRACT_VERSION,
            approved_fields=FEATURE_CONTRACT.field_names(),
            approved_purposes=APPROVED_PURPOSES,
            expected_digest=expected,
        )
        return StoreWorker(self.site_id, inner)
