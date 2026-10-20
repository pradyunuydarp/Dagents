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
2. ``POST /api/v1/federation/jobs:run``: one call in which the site checks
   the job (the offer) and, when it accepts, runs it (the execution). A
   refusal becomes a ``rejected`` result with the site's reason. While the
   job runs, the LMA sends its clipped update to the GMA's update store. The
   LMA's ``jobs:offer`` and ``jobs:execute`` routes take the same two steps
   in two calls; one call per site saves a round trip and a second copy of
   the job.

A missing LMA address, a timeout or an HTTP error becomes a ``failed`` result
whose ``refusal_reason`` says the run failed, with the LMA's status and error
code, and the site counts as not having accepted the job. It never holds the
LMA's error detail, which can name a database host or a hosting vendor; the
engine logs the detail with addresses removed. Each call is recorded as a
:class:`DispatchCall` (site, step ``run``, status, milliseconds, outcome), in
the order the calls finished. Results come back sorted by site.

**Aggregation.** An analytics or evaluation round makes metrics only. For a
training round the engine:

1. loads the starting model and checks its digest;
2. when no update is stored for the round, returns the candidate an earlier
   attempt registered: ``<version_prefix>+<round_id>`` in the model registry
   with the round's starting version as its parent. It loads that version
   and checks its digest. It does this only when the GMA's round store keeps
   round ids across a restart (``round_ids_survive_restart``), as the table
   ``GMA_ROUND_TABLE`` names does. Then no other
   round can have used the id, and a registration request has no parent
   field, so only this round's aggregation can have registered the version.
   With the default round store in memory, a restart frees every round id
   while a model store in Postgres keeps the versions, so the engine never
   takes this step;
3. otherwise checks, for each site the planner accepted, that an update is
   stored, that the model kind's digest of it equals the digest stored with
   it and the digest the site reported, and that the planner gave the site a
   weight;
4. calls ``kind.combine(start.params, [(weight, update), ...])``;
5. registers ``<version_prefix>+<round_id>`` as a candidate through the model
   registry (the planner's ``aggregated`` lifecycle event). When the name is
   taken, the registry returns the stored version if its contents are the
   same, and raises :class:`ModelVersionConflict` if they differ.

Every failed check raises :class:`AggregationRefused`, so no candidate is
made from an update that cannot be traced to a site. After that refusal, and
after a name that holds another model, the engine deletes the round's stored
updates, because no retry of the round can pass. Any other error, such as a
model store that cannot be reached, keeps them, so a retry can combine them
again. After a successful aggregation the updates stay until the round
controller has stored the round as aggregated and calls
:meth:`HttpFederationEngine.discard`. A retry after a failure between the two
combines the kept updates again and gets the registered candidate back from
step 5. After a restart that lost the updates, step 2 returns it.

When the planner refuses aggregation (for example ``quorum_not_met``), the
round controller raises before it calls the engine, so the updates stay until
the round is discarded or closed.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Callable, Mapping, TypeVar

from dagents.common.application.federation import weighted_metrics
from dagents.common.domain.errors import AggregationRefused, ModelVerificationError
from dagents.common.domain.federation import (
    PHASE_TASKS,
    AggregationReadiness,
    CandidateModel,
    DispatchCall,
    FederatedJob,
    JobAcceptance,
    JobModel,
    JobRun,
    RoundManifest,
    RoundPlan,
    RoundSubmission,
    SiteResult,
)
from dagents.common.extensions.registry import ModelKind
from dagents.common.infrastructure.agent_client import AgentError, AgentNetwork, CallLog, without_addresses
from dagents.gma.application.model_registry import (
    ModelRegistry,
    ModelStatusConflict,
    ModelVersionConflict,
    ModelVersionExists,
    UnknownModelKind,
)
from dagents.gma.infrastructure.update_store import StoredUpdate, UpdateStore

logger = logging.getLogger(__name__)

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
    - `round_ids_survive_restart`: whether the GMA's round store keeps its
      records, and so keeps each round id used, after the process stops.
      Only then does an aggregation without stored updates return the
      candidate an earlier attempt registered, and only then does
      :meth:`candidate_registered` answer true. The GMA sets it from the
      round store's ``survives_restart`` attribute.
    """

    name = "http"
    #: Every result comes from a site's ``jobs:run`` answer, so the round
    #: controller refuses results posted through the results route.
    collects_results = True

    def __init__(
        self,
        network: AgentNetwork,
        *,
        models: ModelRegistry,
        updates: UpdateStore,
        lma_tokens: Mapping[str, str] | None = None,
        concurrency: int = 3,
        history: int = DEFAULT_HISTORY,
        round_ids_survive_restart: bool = False,
    ) -> None:
        if not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError("the dispatch concurrency must be a whole number of at least 1")
        if not isinstance(round_ids_survive_restart, bool):
            raise TypeError("round_ids_survive_restart must be True or False")
        self._network = network
        self._models = models
        self._updates = updates
        self._round_ids_survive_restart = round_ids_survive_restart
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

        A training round with no stored updates whose candidate an earlier
        attempt registered gets that candidate back, when the round store
        keeps round ids across a restart (see ``round_ids_survive_restart``).

        Raises :class:`AggregationRefused` when the starting model does not
        verify or cannot be trained, when the registered candidate does not
        verify, or when an accepted site's update is missing, changed, does
        not match the site's reported digest or has no planner weight. Raises
        :class:`ModelVersionConflict` when the candidate's name holds another
        model, as after a restart that freed a round id the model store still
        uses. The round's updates are deleted after both, because no retry of
        the round can pass. Other errors keep them, and so does a successful
        aggregation: the round controller calls :meth:`discard` once it has
        stored the round.
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
            version, digest = self._training_candidate(manifest, readiness, results)
        except AggregationRefused:
            self.discard(round_id)
            raise
        except ModelVersionConflict as error:
            # A status that changed while the request ran, or a store that
            # answers out of step, can pass on a retry, so they keep the
            # updates. A name that holds another model cannot.
            if not isinstance(error, (ModelStatusConflict, ModelVersionExists)):
                self.discard(round_id)
            raise
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

    def candidate_registered(self, manifest: RoundManifest) -> bool:
        """Whether a training round's candidate is in the model registry, as its aggregation registers it.

        The candidate is ``<version_prefix>+<round_id>``, with the round's
        starting version as its parent and the prefix of the starting model's
        kind. The answer is false when the round store does not keep round
        ids across a restart (``round_ids_survive_restart``), because a round
        with the same id that ran before the restart may have registered the
        version. Round recovery after a restart tells the planner the answer.
        """
        if manifest.phase != "training" or not self._round_ids_survive_restart:
            return False
        kind_name = self._models.kind_of(manifest.model_version)
        if kind_name is None:
            return False
        try:
            kind = self._models.kind(kind_name)
        except UnknownModelKind:
            return False
        return self._registered_candidate(manifest, kind) is not None

    def pending_update_ages(self, now: float) -> dict[str, float]:
        """For each round with stored updates, in id order, the age in seconds of its oldest update."""
        return self._updates.pending_ages(now)

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

        def not_accepted(reason: str) -> tuple[JobAcceptance, SiteResult]:
            refused = JobAcceptance(round_id=round_id, site_id=site_id, accepted=False, reason=reason)
            return refused, failed(reason)

        answer, error = self._call("run", record, lambda: client.run_job(job), _run_outcome)
        if answer is None:
            return not_accepted(f"the job run failed: {_failure(error, site_id, round_id)}")
        acceptance, result = answer.acceptance, answer.result
        if acceptance.site_id != site_id or acceptance.round_id != round_id:
            return not_accepted("the LMA answered the offer for another site or round")
        if not acceptance.accepted:
            rejected = SiteResult(
                round_id=round_id,
                site_id=site_id,
                job_digest=plan.round_digest,
                participation="rejected",
                refusal_reason=acceptance.reason or None,
            )
            return acceptance, rejected
        if result is None:
            return acceptance, failed("the LMA accepted the job but returned no result")
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

    def _training_candidate(
        self, manifest: RoundManifest, readiness: AggregationReadiness, results: list[SiteResult]
    ) -> tuple[str, str]:
        """The candidate's version and digest: the registered one, or one combined from the updates now."""
        round_id = manifest.round_id
        try:
            start = self._models.load(manifest.model_version, manifest.model_artifact_digest)
            kind = self._models.kind(start.kind)
        except (ModelVerificationError, UnknownModelKind) as error:
            raise AggregationRefused(f"the starting model of {round_id} cannot be used: {error}") from error
        if not getattr(kind, "trainable", False):
            raise AggregationRefused(f"training starts from a {kind.kind} model, which cannot be trained")
        stored = self._updates.updates(round_id)
        if not stored:
            version = self._registered_candidate(manifest, kind)
            if version is not None:
                try:
                    model = self._models.load(version)
                except ModelVerificationError as error:
                    raise AggregationRefused(f"the registered candidate {version} cannot be used: {error}") from error
                return model.version, model.digest
        return self._combine(manifest, readiness, results, start, kind, stored)

    def _registered_candidate(self, manifest: RoundManifest, kind: ModelKind) -> str | None:
        """``<version_prefix>+<round_id>``, when it can only be the candidate an earlier attempt of this round registered.

        That holds when the round store keeps round ids across a restart and
        the registry holds the version with the round's starting version as
        parent. Without such a round store, a round with the same id that ran
        before a restart may have registered the version from other updates,
        so this returns ``None`` without reading the registry.
        """
        if not self._round_ids_survive_restart:
            return None
        version = f"{kind.version_prefix}+{manifest.round_id}"
        stored = self._models.get(version)
        if stored is None or stored.parent_version != manifest.model_version:
            return None
        return version

    def _combine(
        self,
        manifest: RoundManifest,
        readiness: AggregationReadiness,
        results: list[SiteResult],
        start: JobModel,
        kind: ModelKind,
        stored: Mapping[str, StoredUpdate],
    ) -> tuple[str, str]:
        round_id = manifest.round_id
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


def _failure(error: AgentError | None, site_id: str, round_id: str) -> str:
    """Why a call to an LMA failed, as results may show it: who answered, the status and the code.

    Results reach the coordinator and an application's users. The LMA's own
    detail can quote a store error that names a database host or a hosting
    vendor, so it goes only to the log, with addresses removed.
    """
    assert error is not None
    logger.warning(
        "site %s, round %s: %s", site_id, round_id, without_addresses(str(error))
    )
    return error.without_detail()


def _run_outcome(answer: JobRun) -> str:
    """A run call's outcome: ``refused``, or the participation of the result the site returned."""
    if not answer.acceptance.accepted:
        return "refused"
    return answer.result.participation if answer.result is not None else "failed"


__all__ = ["DEFAULT_HISTORY", "HttpFederationEngine"]
