"""The GMA's HTTP engine: a round's jobs go to each site's LMA through its HTTP API.

This is the generic part of a parameter-averaging runtime. The engine moves
jobs and checks updates; how updates are combined comes from the model kind an
extension contributes (``ModelKind.combine``), so no averaging rule lives here.

**Dispatch.** For each site the plan selected, with up to ``concurrency`` sites
at a time:

1. The job carries the round's starting model in ``parameters["model"]``,
   read from the model registry with its digest checked against the
   manifest's ``model_artifact_digest``. A model that does not verify stops
   the dispatch before any site is called.
2. ``POST /api/v1/federation/jobs:offer``. A refusal becomes a ``rejected``
   result with the site's reason.
3. ``POST /api/v1/federation/jobs:execute`` when the site accepted. While the
   job runs, the LMA sends its clipped update to the GMA's update store.

A missing LMA address, a timeout or an HTTP error becomes a ``failed`` result
whose ``refusal_reason`` says which call failed and how. It never holds a
host, a port or the body of an answer that is not JSON. Each call is recorded
as a :class:`DispatchCall` (site, step, status, milliseconds, outcome), in the
order the calls finished. Results come back sorted by site.

**Aggregation.** An analytics or evaluation round makes metrics only. For a
training round the engine:

1. loads the starting model and checks its digest;
2. checks, for each site the planner accepted, that an update is stored, that
   the model kind's digest of it equals the digest stored with it and the
   digest the site reported, and that the planner gave the site a weight;
3. calls ``kind.combine(start.params, [(weight, update), ...])``;
4. registers ``<version_prefix>+<round_id>`` as a candidate through the model
   registry (the planner's ``aggregated`` lifecycle event);
5. always deletes the round's stored updates, whether or not it succeeded.

Every failed check raises :class:`AggregationRefused`, so no candidate is
made from an update that cannot be traced to a site.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import threading
import time
from typing import Callable, Mapping, TypeVar

from agents.common.application.federation import weighted_metrics
from agents.common.domain.errors import AggregationRefused, ModelVerificationError
from agents.common.domain.federation import (
    PHASE_TASKS,
    AggregationReadiness,
    CandidateModel,
    DispatchCall,
    FederatedJob,
    JobAcceptance,
    JobModel,
    RoundManifest,
    RoundPlan,
    RoundSubmission,
    SiteResult,
)
from agents.common.infrastructure.agent_client import AgentError, AgentNetwork, CallLog
from agents.gma.application.model_registry import ModelRegistry, UnknownModelKind
from agents.gma.infrastructure.update_store import InMemoryUpdateStore

T = TypeVar("T")

#: How many rounds' results and dispatch calls the engine keeps.
DEFAULT_HISTORY = 256


@dataclass
class _RoundState:
    results: list[SiteResult] = field(default_factory=list)
    calls: list[DispatchCall] = field(default_factory=list)


class HttpFederationEngine:
    """Dispatch a round's jobs to the LMAs over HTTP and aggregate their updates.

    Params:
    - `network`: the agents the GMA can call; each site's LMA is
      ``network.lma(site_id, token)``.
    - `models`: the model registry. Jobs carry its models; candidates go into it.
    - `updates`: the update store the LMAs upload to.
    - `lma_tokens`: the coordinator token to send to each site's LMA.
    - `concurrency`: how many sites are called at once.
    """

    name = "http"
    #: Every result comes from a site's ``jobs:execute`` answer, so the round
    #: controller refuses results posted through the results route.
    collects_results = True

    def __init__(
        self,
        network: AgentNetwork,
        *,
        models: ModelRegistry,
        updates: InMemoryUpdateStore,
        lma_tokens: Mapping[str, str] | None = None,
        concurrency: int = 3,
        history: int = DEFAULT_HISTORY,
    ) -> None:
        if not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError("the dispatch concurrency must be a whole number of at least 1")
        self._network = network
        self._models = models
        self._updates = updates
        self._tokens = dict(lma_tokens or {})
        self._concurrency = concurrency
        self._history = history
        self._rounds: OrderedDict[str, _RoundState] = OrderedDict()
        self._lock = threading.Lock()

    # --- FederationEngine ---------------------------------------------------------

    def submit_round(self, manifest: RoundManifest, plan: RoundPlan) -> RoundSubmission:
        """Offer the round's job to every selected site, and run it where it was accepted.

        Raises :class:`ModelVerificationError` when the starting model is not
        in the registry or does not match the manifest's digest; no site is
        called then.
        """
        round_id = manifest.round_id
        model = self._models.load(manifest.model_version, manifest.model_artifact_digest)
        sites = list(plan.selected_sites)
        state = _RoundState()
        record_lock = threading.Lock()

        def run(site_id: str) -> tuple[JobAcceptance, SiteResult]:
            return self._run_site(manifest, plan, model, site_id, state, record_lock)

        if sites:
            with ThreadPoolExecutor(
                max_workers=min(self._concurrency, len(sites)), thread_name_prefix="gma-dispatch"
            ) as executor:
                outcomes = list(executor.map(run, sites))
        else:
            outcomes = []

        acceptances = sorted((acceptance for acceptance, _ in outcomes), key=lambda item: item.site_id)
        state.results = sorted((result for _, result in outcomes), key=lambda item: item.site_id)
        self._remember(round_id, state)
        dispatched = [acceptance.site_id for acceptance in acceptances if acceptance.accepted]
        return RoundSubmission(
            round_id=round_id,
            engine=self.name,
            dispatched_sites=dispatched,
            acceptances=acceptances,
            message=f"{len(dispatched)} of {len(sites)} selected sites accepted the job",
        )

    def collect_results(self, round_id: str) -> list[SiteResult]:
        with self._lock:
            state = self._rounds.get(round_id)
            return list(state.results) if state is not None else []

    def dispatch_calls(self, round_id: str) -> list[DispatchCall]:
        """The calls made to the LMAs for a round, in the order they finished."""
        with self._lock:
            state = self._rounds.get(round_id)
            return list(state.calls) if state is not None else []

    def aggregate(
        self, manifest: RoundManifest, readiness: AggregationReadiness, results: list[SiteResult]
    ) -> CandidateModel:
        """Average the metrics; for a training round, combine the stored updates into a candidate.

        Raises :class:`AggregationRefused` when the starting model does not
        verify or cannot be trained, or when an accepted site's update is
        missing, changed, does not match the site's reported digest or has no
        planner weight. The round's updates are deleted either way.
        """
        round_id = manifest.round_id
        metrics_only = CandidateModel(
            round_id=round_id,
            candidate_version=f"{manifest.model_version}+{round_id}",
            parent_version=manifest.model_version,
            aggregation_method=manifest.aggregation.kind,
            contributing_sites=list(readiness.accepted_sites),
            site_weights=dict(readiness.site_weights),
            contributed_examples=readiness.accepted_examples,
            metrics=weighted_metrics(readiness, results),
            produced_at=int(time.time()),
        )
        if manifest.phase != "training":
            return metrics_only
        try:
            version, digest = self._combine(manifest, readiness, results)
        finally:
            self.discard(round_id)
        return metrics_only.model_copy(
            update={
                "candidate_version": version,
                "artifact_uri": f"registry://models/{version}",
                "artifact_digest": digest,
            }
        )

    def discard(self, round_id: str) -> None:
        """Delete the round's stored updates; later uploads for it are refused."""
        self._updates.discard(round_id)

    # --- Dispatch -------------------------------------------------------------------

    def _run_site(
        self,
        manifest: RoundManifest,
        plan: RoundPlan,
        model: JobModel,
        site_id: str,
        state: _RoundState,
        record_lock: threading.Lock,
    ) -> tuple[JobAcceptance, SiteResult]:
        round_id = manifest.round_id
        job = FederatedJob(
            round_id=round_id,
            site_id=site_id,
            manifest=manifest,
            manifest_digest=plan.round_digest,
            task=PHASE_TASKS[manifest.phase],
            parameters={"model": model.model_dump(mode="json")},
        )
        client = self._network.lma(site_id, self._tokens.get(site_id))

        def record(step: str, log: CallLog, outcome: str) -> None:
            last = log.calls[-1] if log.calls else None
            call = DispatchCall(
                site_id=site_id,
                step=step,  # type: ignore[arg-type]
                status_code=last.status if last is not None else None,
                ms=max(0.0, last.ms) if last is not None else 0.0,
                outcome=outcome,  # type: ignore[arg-type]
            )
            with record_lock:
                state.calls.append(call)

        def failed(reason: str) -> SiteResult:
            return SiteResult(
                round_id=round_id,
                site_id=site_id,
                job_digest=plan.round_digest,
                participation="failed",
                local_evidence_pointer=f"unreachable://{site_id}#{reason}",
                refusal_reason=reason,
            )

        acceptance, error = self._call("offer", record, lambda: client.offer_job(job), _offer_outcome)
        if acceptance is None:
            reason = f"the job offer failed: {error}"
            refused = JobAcceptance(round_id=round_id, site_id=site_id, accepted=False, reason=reason)
            return refused, failed(reason)
        if acceptance.site_id != site_id or acceptance.round_id != round_id:
            reason = "the LMA answered the offer for another site or round"
            refused = JobAcceptance(round_id=round_id, site_id=site_id, accepted=False, reason=reason)
            return refused, failed(reason)
        if not acceptance.accepted:
            rejected = SiteResult(
                round_id=round_id,
                site_id=site_id,
                job_digest=plan.round_digest,
                participation="rejected",
                refusal_reason=acceptance.reason or None,
            )
            return acceptance, rejected

        result, error = self._call("execute", record, lambda: client.execute_job(job), _execute_outcome)
        if result is None:
            return acceptance, failed(f"the job did not run: {error}")
        if result.site_id != site_id or result.round_id != round_id:
            return acceptance, failed("the LMA returned a result for another site or round")
        return acceptance, result

    @staticmethod
    def _call(
        step: str,
        record: Callable[[str, CallLog, str], None],
        call: Callable[[], T],
        outcome: Callable[[T], str],
    ) -> tuple[T | None, AgentError | None]:
        """Make one call to an LMA, record it, and return its answer or the error."""
        with CallLog() as log:
            try:
                answer = call()
            except AgentError as error:
                record(step, log, "failed")
                return None, error
        record(step, log, outcome(answer))
        return answer, None

    def _remember(self, round_id: str, state: _RoundState) -> None:
        with self._lock:
            self._rounds[round_id] = state
            self._rounds.move_to_end(round_id)
            while len(self._rounds) > self._history:
                self._rounds.popitem(last=False)

    # --- Aggregation ----------------------------------------------------------------

    def _combine(
        self, manifest: RoundManifest, readiness: AggregationReadiness, results: list[SiteResult]
    ) -> tuple[str, str]:
        round_id = manifest.round_id
        try:
            start = self._models.load(manifest.model_version, manifest.model_artifact_digest)
            kind = self._models.kind(start.kind)
        except (ModelVerificationError, UnknownModelKind) as error:
            raise AggregationRefused(f"the starting model of {round_id} cannot be used: {error}") from error
        if not getattr(kind, "trainable", False):
            raise AggregationRefused(f"training starts from a {kind.kind} model, which cannot be trained")

        stored = self._updates.updates(round_id)
        reported = {result.site_id: result for result in results}
        weighted: list[tuple[float, dict[str, float]]] = []
        for site_id in sorted(readiness.accepted_sites):
            update = stored.get(site_id)
            if update is None:
                raise AggregationRefused(f"no update from {site_id} is stored for round {round_id}")
            try:
                digest = kind.update_digest(update.update)
            except ValueError as error:
                raise AggregationRefused(f"the stored update from {site_id} cannot be checked: {error}") from error
            if digest != update.digest:
                raise AggregationRefused(f"the stored update from {site_id} changed after it was received")
            result = reported.get(site_id)
            if result is None or result.update_digest != update.digest:
                raise AggregationRefused(
                    f"the update stored for {site_id} does not match the digest the site reported"
                )
            weight = readiness.site_weights.get(site_id)
            if weight is None:
                raise AggregationRefused(f"the planner gave no weight for {site_id}")
            weighted.append((float(weight), dict(update.update)))
        try:
            params = kind.combine(start.params, weighted)
        except (TypeError, ValueError) as error:
            raise AggregationRefused(f"the model kind {kind.kind} could not combine the updates: {error}") from error
        version = f"{kind.version_prefix}+{round_id}"
        model = self._models.register_candidate(version, kind.kind, dict(params), manifest.model_version)
        return model.version, model.digest


def _offer_outcome(acceptance: JobAcceptance) -> str:
    return "accepted" if acceptance.accepted else "refused"


def _execute_outcome(result: SiteResult) -> str:
    return result.participation


__all__ = ["DEFAULT_HISTORY", "HttpFederationEngine"]
