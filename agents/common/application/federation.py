"""Federated round control plane.

Dagents' job in a federation is governance, not execution: define the study,
decide who may take part, record what happened, and decide whether a candidate
may be released. The distributed protocol itself belongs to a specialist runtime
such as NVIDIA FLARE.

:class:`FederationEngine` is the seam between the two. The GMA speaks Dagents
contracts; an adapter translates them into a runtime's job format and translates
the runtime's results back. That keeps the study, the evidence, and the release
policy portable across runtimes, and keeps a federated optimizer out of the GMA.

Every deterministic decision here — eligibility, quorum, aggregation readiness,
release gates — is delegated to the OCaml planner through ``dagentsc``. The
controller owns state and side effects; it does not re-derive planning rules in
Python.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Protocol

from agents.common.domain.federation import (
    AggregationReadiness,
    CandidateModel,
    FederatedJob,
    JobAcceptance,
    ReleaseDecision,
    ReleaseGate,
    RoundManifest,
    RoundPlan,
    RoundRecord,
    RoundSubmission,
    SiteRegistration,
    SiteResult,
)
from agents.common.infrastructure.dagents_runner import (
    compile_round_plan,
    evaluate_aggregation_readiness,
    evaluate_release,
    round_digest,
)


class SiteWorker(Protocol):
    """What a site does when it is offered a job.

    This is the LMA's side of a round. A site may always refuse, and refusal
    before any code touches local data is the reason a signed job is distributed
    at all rather than a task simply being executed.
    """

    site_id: str

    def offer(self, job: FederatedJob) -> JobAcceptance:
        """Verify the job and accept or refuse it."""

    def execute(self, job: FederatedJob) -> SiteResult:
        """Run the accepted job locally and return the permitted result."""


class FederationEngine(Protocol):
    """The federated runtime, as the control plane sees it.

    Implement this against NVIDIA FLARE, another approved runtime, or the
    in-process simulator below. Nothing above this interface should know which.
    """

    name: str

    def submit_round(self, manifest: RoundManifest, plan: RoundPlan) -> RoundSubmission:
        """Offer the round to every selected site and collect acceptances."""

    def collect_results(self, round_id: str) -> list[SiteResult]:
        """Return what the accepting sites produced."""

    def aggregate(
        self, manifest: RoundManifest, readiness: AggregationReadiness, results: list[SiteResult]
    ) -> CandidateModel:
        """Combine permitted contributions into a candidate model."""


class SiteRegistry(Protocol):
    """Where standing site enrolments are kept."""

    def save(self, registration: SiteRegistration) -> SiteRegistration:
        """Persist one site enrolment."""

    def get(self, site_id: str) -> SiteRegistration | None:
        """Load one site enrolment."""

    def list(self) -> list[SiteRegistration]:
        """List every known site enrolment."""


class RoundRepository(Protocol):
    """Where round lineage is kept."""

    def save(self, record: RoundRecord) -> RoundRecord:
        """Persist one round record."""

    def get(self, round_id: str) -> RoundRecord | None:
        """Load one round record."""

    def list(self, limit: int = 20) -> list[RoundRecord]:
        """List recent round records, newest first."""


class InMemorySiteRegistry:
    """In-memory site enrolments, matching the agent layer's current delivery."""

    def __init__(self) -> None:
        self._sites: dict[str, SiteRegistration] = {}

    def save(self, registration: SiteRegistration) -> SiteRegistration:
        self._sites[registration.site_id] = registration
        return registration

    def get(self, site_id: str) -> SiteRegistration | None:
        return self._sites.get(site_id)

    def list(self) -> list[SiteRegistration]:
        return [self._sites[key] for key in sorted(self._sites)]


class InMemoryRoundRepository:
    """In-memory round lineage, newest first."""

    def __init__(self) -> None:
        self._rounds: dict[str, RoundRecord] = {}
        self._order: list[str] = []

    def save(self, record: RoundRecord) -> RoundRecord:
        round_id = record.manifest.round_id
        if round_id not in self._rounds:
            self._order.insert(0, round_id)
        self._rounds[round_id] = record
        return record

    def get(self, round_id: str) -> RoundRecord | None:
        return self._rounds.get(round_id)

    def list(self, limit: int = 20) -> list[RoundRecord]:
        return [self._rounds[key] for key in self._order[:limit]]


class InProcessFederationEngine:
    """A federated runtime that runs every site in this process.

    This exists so the control plane is runnable, testable, and demonstrable
    without provisioning a distributed runtime — the equivalent of FLARE's
    simulator mode. It exercises the real contracts: sites are offered jobs,
    may refuse them, and return only the permitted result shape.

    What it does not prove is production privacy, security, reliability, or
    network behaviour. A simulator result is evidence about job logic and
    nothing more.
    """

    name = "in-process-simulator"

    def __init__(self, workers: list[SiteWorker] | None = None) -> None:
        self._workers: dict[str, SiteWorker] = {worker.site_id: worker for worker in workers or []}
        self._results: dict[str, list[SiteResult]] = {}

    def register_worker(self, worker: SiteWorker) -> None:
        """Attach one site worker to the simulated federation."""
        self._workers[worker.site_id] = worker

    def submit_round(self, manifest: RoundManifest, plan: RoundPlan) -> RoundSubmission:
        digest = plan.round_digest
        acceptances: list[JobAcceptance] = []
        dispatched: list[str] = []
        results: list[SiteResult] = []
        task = {"analytics": "profile", "evaluation": "evaluate", "training": "train"}[manifest.phase]

        for site_id in plan.selected_sites:
            worker = self._workers.get(site_id)
            if worker is None:
                # An eligible site with no worker is an infrastructure gap, not
                # a refusal. It still gets a result, because a round whose
                # evidence simply omits a selected site is indistinguishable
                # from one where that site was never selected.
                reason = "no worker is attached for this site"
                acceptances.append(
                    JobAcceptance(
                        round_id=manifest.round_id, site_id=site_id, accepted=False, reason=reason
                    )
                )
                results.append(
                    SiteResult(
                        round_id=manifest.round_id,
                        site_id=site_id,
                        job_digest=digest,
                        participation="failed",
                        local_evidence_pointer=f"unreachable://{site_id}#{reason}",
                    )
                )
                continue
            job = FederatedJob(
                round_id=manifest.round_id,
                site_id=site_id,
                manifest=manifest,
                manifest_digest=digest,
                task=task,
            )
            acceptance = worker.offer(job)
            acceptances.append(acceptance)
            if not acceptance.accepted:
                # A refusal is recorded as a result too, so a round's evidence
                # shows which sites declined rather than showing a gap.
                results.append(
                    SiteResult(
                        round_id=manifest.round_id,
                        site_id=site_id,
                        job_digest=digest,
                        participation="rejected",
                    )
                )
                continue
            dispatched.append(site_id)
            results.append(worker.execute(job))

        self._results[manifest.round_id] = results
        return RoundSubmission(
            round_id=manifest.round_id,
            engine=self.name,
            dispatched_sites=dispatched,
            acceptances=acceptances,
            message=f"{len(dispatched)} of {len(plan.selected_sites)} selected sites accepted the job",
        )

    def collect_results(self, round_id: str) -> list[SiteResult]:
        return list(self._results.get(round_id, []))

    def aggregate(
        self, manifest: RoundManifest, readiness: AggregationReadiness, results: list[SiteResult]
    ) -> CandidateModel:
        accepted = [result for result in results if result.site_id in readiness.accepted_sites]
        weights = readiness.site_weights
        metrics: dict[str, float] = {}
        # Weighted mean of each metric every accepted site reported. A metric
        # only some sites reported is deliberately left out: a partial average
        # would be reported as if it covered the whole consortium.
        metric_names = set.intersection(*[set(result.metrics) for result in accepted]) if accepted else set()
        for name in sorted(metric_names):
            metrics[name] = sum(
                result.metrics[name] * weights.get(result.site_id, 0.0) for result in accepted
            )
        return CandidateModel(
            round_id=manifest.round_id,
            candidate_version=f"{manifest.model_version}+{manifest.round_id}",
            parent_version=manifest.model_version,
            aggregation_method=manifest.aggregation.kind,
            contributing_sites=list(readiness.accepted_sites),
            site_weights=weights,
            contributed_examples=readiness.accepted_examples,
            metrics=metrics,
            artifact_uri=f"memory://candidates/{manifest.round_id}",
            produced_at=int(time.time()),
        )


class FederatedRoundController:
    """The GMA's federated round lifecycle.

    Every deterministic decision is delegated to the OCaml planner. This class
    owns registration state, round lineage, and the calls into the engine; it
    does not decide eligibility, quorum, or release in Python.
    """

    def __init__(
        self,
        engine: FederationEngine,
        sites: SiteRegistry | None = None,
        rounds: RoundRepository | None = None,
    ) -> None:
        self._engine = engine
        self._sites = sites or InMemorySiteRegistry()
        self._rounds = rounds or InMemoryRoundRepository()

    @property
    def engine_name(self) -> str:
        """Which federated runtime is attached."""
        return self._engine.name

    def register_site(self, registration: SiteRegistration) -> SiteRegistration:
        """Enrol or update one site."""
        return self._sites.save(registration)

    def list_sites(self) -> list[SiteRegistration]:
        """List enrolled sites."""
        return self._sites.list()

    def list_rounds(self, limit: int = 20) -> list[RoundRecord]:
        """List recent rounds, newest first."""
        return self._rounds.list(limit=limit)

    def get_round(self, round_id: str) -> RoundRecord | None:
        """Load one round's lineage."""
        return self._rounds.get(round_id)

    def digest(self, manifest: RoundManifest) -> str:
        """Return the manifest's deterministic content digest."""
        return str(round_digest(manifest.model_dump(mode="json"))["round_digest"])

    def plan_round(self, manifest: RoundManifest) -> RoundPlan:
        """Select the sites eligible for this round."""
        payload = compile_round_plan(
            manifest.model_dump(mode="json"),
            [registration.model_dump(mode="json") for registration in self._sites.list()],
        )
        return RoundPlan.model_validate(payload)

    def dispatch_round(self, manifest: RoundManifest) -> RoundRecord:
        """Plan the round and, if quorum allows, offer it to the selected sites.

        A round that cannot reach quorum is recorded and closed rather than
        dispatched. Offering a job to too few sites is what a participation
        threshold exists to prevent.
        """
        plan = self.plan_round(manifest)
        now = int(time.time())
        record = RoundRecord(manifest=manifest, plan=plan, created_at=now, updated_at=now, status="planned")
        if not plan.quorum_met:
            record = record.model_copy(update={"status": "closed"})
            return self._rounds.save(record)

        self._engine.submit_round(manifest, plan)
        results = self._engine.collect_results(manifest.round_id)
        results.sort(key=lambda item: item.site_id)
        return self._rounds.save(
            record.model_copy(
                update={"status": "collecting", "results": results, "updated_at": int(time.time())}
            )
        )

    def submit_result(self, result: SiteResult) -> RoundRecord:
        """Record one site's returned result against its round."""
        record = self._rounds.get(result.round_id)
        if record is None:
            raise KeyError(f"unknown round: {result.round_id}")
        results = [existing for existing in record.results if existing.site_id != result.site_id]
        results.append(result)
        results.sort(key=lambda item: item.site_id)
        return self._rounds.save(
            record.model_copy(update={"results": results, "updated_at": int(time.time()), "status": "collecting"})
        )

    def evaluate_readiness(self, round_id: str) -> AggregationReadiness:
        """Ask the planner whether the returned contributions may be aggregated."""
        record = self._require(round_id)
        payload = evaluate_aggregation_readiness(
            record.manifest.model_dump(mode="json"),
            [result.model_dump(mode="json") for result in record.results],
        )
        readiness = AggregationReadiness.model_validate(payload)
        self._rounds.save(record.model_copy(update={"readiness": readiness, "updated_at": int(time.time())}))
        return readiness

    def aggregate(self, round_id: str) -> CandidateModel:
        """Produce a candidate model, if and only if aggregation is permitted.

        This is the boundary the whole design protects: a candidate is created
        here, and a candidate is not a release.
        """
        record = self._require(round_id)
        readiness = self.evaluate_readiness(round_id)
        if not readiness.aggregation_permitted:
            raise PermissionError(
                f"aggregation is not permitted for {round_id}: {readiness.stop_reason or 'insufficient contributions'}"
            )
        candidate = self._engine.aggregate(record.manifest, readiness, record.results)
        self._rounds.save(
            self._require(round_id).model_copy(update={"status": "aggregated", "updated_at": int(time.time())})
        )
        return candidate

    def evaluate_release(
        self,
        round_id: str,
        gates: list[ReleaseGate],
        candidate: CandidateModel,
        baseline_metrics: dict[str, float] | None = None,
        rollback_version: str | None = None,
    ) -> ReleaseDecision:
        """Evaluate release gates against a candidate and record the verdict."""
        record = self._require(round_id)
        payload = evaluate_release(
            [gate.model_dump(mode="json") for gate in gates],
            candidate.metrics,
            baseline_metrics or {},
            candidate.candidate_version,
            rollback_version or candidate.parent_version,
            round_id,
        )
        decision = ReleaseDecision.model_validate(payload)
        self._rounds.save(
            record.model_copy(update={"decision": decision, "status": "closed", "updated_at": int(time.time())})
        )
        return decision

    def _require(self, round_id: str) -> RoundRecord:
        record = self._rounds.get(round_id)
        if record is None:
            raise KeyError(f"unknown round: {round_id}")
        return record


def new_round_id(prefix: str = "round") -> str:
    """Generate a round identifier for callers that do not supply one."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


__all__ = [
    "FederatedRoundController",
    "FederationEngine",
    "InMemoryRoundRepository",
    "InMemorySiteRegistry",
    "InProcessFederationEngine",
    "RoundRepository",
    "SiteRegistry",
    "SiteWorker",
    "new_round_id",
]
