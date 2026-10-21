"""Federated round control plane.

The controller in this module defines each round, decides who may take part,
records what happened and asks whether a candidate may be released. The jobs
and the model updates move through a runtime behind :class:`FederationEngine`:
the GMA's HTTP engine, the in-process simulator below, or an adapter for a
specialist runtime such as NVIDIA FLARE. How updates are combined comes from a
model kind the application contributes, so no federated optimizer lives here.

Every deterministic decision (eligibility, quorum, aggregation readiness,
release gates) is made by the OCaml planner through ``dagentsc``. The
controller owns state and side effects and does not derive planning rules in
Python.

How a round's stored status moves:

| Status | Set by | What it accepts next |
|---|---|---|
| ``dispatched`` | :meth:`FederatedRoundController.dispatch_round`, before the engine offers the jobs | results, uploads to the update store |
| ``collecting`` | the end of a dispatch that met quorum | results, uploads, aggregation, a release decision |
| ``aggregated`` | :meth:`FederatedRoundController.aggregate` | a release decision |
| ``closed`` | a dispatch without quorum, a release decision, a discard, or recovery after a restart | nothing more |

A round id can be used once, a round is aggregated once, and it takes one
release decision. Results come through :meth:`FederatedRoundController.submit_result`
only when the engine does not collect them from the sites itself: the GMA's
HTTP engine does, so a posted result cannot replace what a site returned.
Every read-modify-write of a stored round goes through
:meth:`RoundRepository.update`, so results posted at the same time do not
overwrite each other.

After a restart, :meth:`FederatedRoundController.recover` asks the planner
about each round the restart left unfinished and closes the rounds that
cannot finish.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import threading
import time
import uuid
from typing import Any, Callable, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import ValidationError

from dagents.common.domain.errors import AggregationRefused, InvalidRoundRecord, NotSelected, RoundConflict
from dagents.common.domain.federation import (
    PHASE_TASKS,
    AggregationReadiness,
    CandidateModel,
    DispatchCall,
    FederatedJob,
    JobAcceptance,
    ReleaseDecision,
    ReleaseGate,
    RoundManifest,
    RoundCloseReason,
    RoundPhase,
    RoundPlan,
    RoundRecord,
    RoundRecoveryVerdict,
    RoundSubmission,
    SiteRegistration,
    SiteResult,
)
from dagents.common.infrastructure.dagents_runner import (
    PlannerUnavailableError,
    compile_round_plan,
    evaluate_aggregation_readiness,
    evaluate_release,
    round_digest,
    round_recovery,
)

logger = logging.getLogger(__name__)

_Parsed = TypeVar("_Parsed")

#: The reason stored on a round whose engine raised while it dispatched it.
ENGINE_FAILED_REASON = "the engine failed while it dispatched the round"

#: The ``abandoned_reason`` of a round whose readiness the planner could not check after its dispatch.
READINESS_FAILED_REASON = "the planner could not check the round's readiness after its dispatch"

#: Statuses in which a round takes no more results.
_FINISHED = ("aggregated", "closed")

#: Statuses of a round that a restart can leave unfinished. The planner's
#: recovery rule (``dagentsc federation round recover``) may close a round in
#: one of them, so :meth:`RoundRepository.unfinished` lists them.
UNFINISHED_STATUSES = ("planned", "dispatched", "collecting")

#: The status of an unfinished round.
UnfinishedStatus = Literal["planned", "dispatched", "collecting"]


def _planner_output(what: str, parse: Callable[[], _Parsed]) -> _Parsed:
    """Parse the planner's answer, or raise :class:`PlannerUnavailableError`.

    A planner that answers with the wrong shape is treated like a planner that
    cannot be reached: the caller cannot use the answer, and a later request
    may succeed once a working planner is installed.
    """
    try:
        return parse()
    except (KeyError, TypeError, ValidationError) as error:
        raise PlannerUnavailableError(f"the planner's {what} answer has the wrong shape: {error}") from error


def _recovery_verdict(payload: object) -> RoundRecoveryVerdict:
    """Parse the planner's recovery answer, or raise :class:`PlannerUnavailableError`."""
    return _planner_output("round recovery", lambda: RoundRecoveryVerdict.model_validate(payload))


def _by_site(item: SiteResult | JobAcceptance) -> str:
    return item.site_id


def weighted_metrics(readiness: AggregationReadiness, results: list[SiteResult]) -> dict[str, float]:
    """The weighted mean of each metric that every accepted site reported.

    Params:
    - `readiness`: the planner's readiness answer. Only results from its
      ``accepted_sites`` count, each with its weight from ``site_weights``.
    - `results`: the round's stored results.

    Returns the metrics by name, in name order. A metric that only some
    accepted sites reported is left out, because a partial average would be
    reported as if it covered the whole consortium. Both the in-process
    simulator and the GMA's HTTP engine use this, so a candidate's metrics
    mean the same thing whichever engine made it.
    """
    accepted = [result for result in results if result.site_id in readiness.accepted_sites]
    if not accepted:
        return {}
    weights = readiness.site_weights
    names = set.intersection(*[set(result.metrics) for result in accepted])
    return {
        name: sum(result.metrics[name] * weights.get(result.site_id, 0.0) for result in accepted)
        for name in sorted(names)
    }


class SiteWorker(Protocol):
    """What a site does when it is offered a job.

    This is the LMA's side of a round. A site may always refuse, and refusal
    before any code touches local data is the reason a signed job is
    distributed instead of a task that runs at once.
    """

    site_id: str

    def offer(self, job: FederatedJob) -> JobAcceptance:
        """Verify the job and accept or refuse it."""

    def execute(self, job: FederatedJob) -> SiteResult:
        """Run the accepted job locally and return the permitted result."""


class FederationEngine(Protocol):
    """The federated runtime, as the control plane sees it.

    Implement this against the GMA's HTTP engine, NVIDIA FLARE, another
    approved runtime, or the in-process simulator below. Nothing above this
    interface should know which.

    Five members are optional. The controller uses them only when the engine
    has them:

    - ``dispatch_calls(round_id) -> list[DispatchCall]``: the calls the engine
      made to the sites for the round. The controller stores them in the
      round's ``dispatch`` field.
    - ``discard(round_id)``: delete what the engine still holds for the round,
      such as stored model updates. The controller calls it from
      :meth:`FederatedRoundController.discard`, when ``submit_round`` or
      ``collect_results`` raises, after ``aggregate`` once the round is
      stored as aggregated, and from :meth:`FederatedRoundController.recover`.
      An engine that refuses an aggregation deletes the round's updates
      itself.
    - ``collects_results: bool``: true when the engine collects every result
      from the sites itself, as the GMA's HTTP engine does. The controller
      then refuses results posted through
      :meth:`FederatedRoundController.submit_result`, so a caller cannot
      replace a site's result, its metrics or its example count.
    - ``candidate_registered(manifest) -> bool``: whether the candidate of a
      training round is already in the model registry, as an aggregation that
      stopped before the round was stored leaves it.
      :meth:`FederatedRoundController.recover` tells the planner.
    - ``pending_update_ages(now) -> Mapping[str, float]``: for each round
      with stored updates, the age in seconds of its oldest update.
      :meth:`FederatedRoundController.recover` tells the planner the age, and
      deletes the updates of rounds that are no longer unfinished.
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


@runtime_checkable
class RoundRepository(Protocol):
    """Where round lineage is kept.

    ``create`` and ``update`` must be atomic: the controller relies on them to
    refuse a reused round id and to keep results that arrive at the same time.
    ``unfinished`` and ``quarantine`` are what recovery after a restart reads
    and writes.

    One member is optional: ``survives_restart: bool``, true when the store
    keeps its records after the process stops, so a round id stays used
    across restarts. A store without it counts as false. The GMA's HTTP
    engine returns a candidate an earlier attempt registered only when it is
    true, because otherwise a round that ran before a restart with the same
    id may have registered that version.
    """

    def save(self, record: RoundRecord) -> RoundRecord:
        """Persist one round record, replacing any record with its round id."""

    def create(self, record: RoundRecord) -> RoundRecord:
        """Persist a new round record. Raises :class:`RoundConflict` when its round id is stored."""

    def update(self, round_id: str, change: Callable[[RoundRecord], RoundRecord]) -> RoundRecord:
        """Replace one round record with ``change(record)`` in one atomic step.

        Raises ``KeyError`` for an unknown round. An exception from ``change``
        leaves the stored record as it was.
        """

    def get(self, round_id: str) -> RoundRecord | None:
        """Load one round record."""

    def list(self, limit: int = 20) -> list[RoundRecord]:
        """List recent round records, newest first."""

    def unfinished(self) -> list[UnfinishedRound]:
        """Every round in a status of :data:`UNFINISHED_STATUSES`, oldest first.

        A store that keeps records outside the process reads the status and
        phase without reading the record, so a record it cannot read is
        listed with ``readable`` false instead of stopping the list.
        """

    def quarantine(self, round_id: str, reason: str) -> bool:
        """Close an unfinished round without reading its record.

        The round becomes ``closed`` with ``reason`` as its
        ``abandoned_reason`` (a reason it already had is kept), and its id
        stays reserved, so :meth:`create` refuses it. Returns ``True`` when
        the round was closed, and ``False`` when it was already
        ``aggregated`` or ``closed``, which it leaves as it was. Raises
        ``KeyError`` for an unknown round and ``ValueError`` for an empty
        reason.
        """


@dataclass(frozen=True)
class UnfinishedRound:
    """A stored round that recovery after a restart may need to close.

    - `round_id`: the round's id.
    - `status`: ``planned``, ``dispatched`` or ``collecting``.
    - `phase`: ``analytics``, ``evaluation`` or ``training``.
    - `readable`: whether the store can read the round's record. The planner
      closes a round whose record cannot be read.
    """

    round_id: str
    status: UnfinishedStatus
    phase: RoundPhase
    readable: bool


@dataclass(frozen=True)
class RoundRecovery:
    """What :meth:`FederatedRoundController.recover` did with one unfinished round.

    - `round_id`, `status`, `phase`, `readable`: the round as
      :meth:`RoundRepository.unfinished` listed it, before recovery.
    - `verdict`: the planner's answer, ``keep`` or ``close``.
    - `reason`: why the planner closed the round, or ``None`` for a kept one.
    - `message`: the planner's sentence. A closed round keeps it as its
      ``abandoned_reason``.
    """

    round_id: str
    status: UnfinishedStatus
    phase: RoundPhase
    readable: bool
    verdict: Literal["keep", "close"]
    reason: RoundCloseReason | None
    message: str


@dataclass(frozen=True)
class RecoveryReport:
    """What one call of :meth:`FederatedRoundController.recover` did.

    - `rounds`: one entry per unfinished round, oldest first.
    - `deleted_updates`: the rounds, in id order, whose stored updates were
      deleted because the round was no longer unfinished or not stored.
    """

    rounds: tuple[RoundRecovery, ...]
    deleted_updates: tuple[str, ...]


def _require_reason(reason: str, action: str) -> None:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError(f"a {action} needs a reason")


def round_record_json(record: RoundRecord) -> dict[str, Any]:
    """The record as the JSON a round store keeps, or :class:`InvalidRoundRecord` when it would not read back equal.

    Every round store calls it before it writes a record, so the memory and
    Postgres stores refuse the same records. A record that holds NaN or an
    infinite number is refused: JSON has no form for either, and pydantic
    writes both as ``null``, which reads back as another value or not at
    all. Fields filled from a request or a site already refuse them; this
    check also covers values an engine or an extension computes.
    """
    try:
        data = record.model_dump(mode="json")
        json.dumps(data, allow_nan=False)
        same = RoundRecord.model_validate(data) == record
    except (ValueError, RecursionError):  # a pydantic ValidationError is a ValueError
        same = False
    if not same:
        raise InvalidRoundRecord(
            f"round {record.manifest.round_id} holds a value that would not read back as it is, "
            "such as NaN or an infinite number"
        )
    return data


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


def _unknown_round(round_id: str) -> KeyError:
    return KeyError(f"unknown round: {round_id}")


class InMemoryRoundRepository:
    """In-memory round lineage, newest first.

    One lock covers every read and write. Agent handlers run in a thread
    pool, so two requests can change the same round at the same time.

    ``create``, ``save`` and ``update`` raise :class:`InvalidRoundRecord`
    for a record that :func:`round_record_json` refuses, as the Postgres
    store does, and store nothing.
    """

    #: A restart empties the store, so a round id can be used again after it.
    survives_restart = False

    def __init__(self) -> None:
        self._rounds: dict[str, RoundRecord] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()

    def _store(self, record: RoundRecord) -> RoundRecord:
        round_id = record.manifest.round_id
        if round_id not in self._rounds:
            self._order.insert(0, round_id)
        self._rounds[round_id] = record
        return record

    def save(self, record: RoundRecord) -> RoundRecord:
        round_record_json(record)
        with self._lock:
            return self._store(record)

    def create(self, record: RoundRecord) -> RoundRecord:
        round_record_json(record)
        with self._lock:
            round_id = record.manifest.round_id
            if round_id in self._rounds:
                raise RoundConflict(f"round {round_id} already exists; a new round needs a new round id")
            return self._store(record)

    def update(self, round_id: str, change: Callable[[RoundRecord], RoundRecord]) -> RoundRecord:
        with self._lock:
            record = self._rounds.get(round_id)
            if record is None:
                raise _unknown_round(round_id)
            changed = change(record)
            if changed.manifest.round_id != round_id:
                raise ValueError(f"a change to round {round_id} returned round {changed.manifest.round_id}")
            round_record_json(changed)
            return self._store(changed)

    def get(self, round_id: str) -> RoundRecord | None:
        with self._lock:
            return self._rounds.get(round_id)

    def list(self, limit: int = 20) -> list[RoundRecord]:
        with self._lock:
            return [self._rounds[key] for key in self._order[:limit]]

    def unfinished(self) -> list[UnfinishedRound]:
        # ``_order`` is newest first. Records in memory are always readable.
        with self._lock:
            records = [self._rounds[round_id] for round_id in reversed(self._order)]
        return [
            UnfinishedRound(
                round_id=record.manifest.round_id,
                status=record.status,  # type: ignore[arg-type]
                phase=record.manifest.phase,
                readable=True,
            )
            for record in records
            if record.status in UNFINISHED_STATUSES
        ]

    def quarantine(self, round_id: str, reason: str) -> bool:
        _require_reason(reason, "quarantine")
        with self._lock:
            record = self._rounds.get(round_id)
            if record is None:
                raise _unknown_round(round_id)
            if record.status not in UNFINISHED_STATUSES:
                return False
            update: dict[str, object] = {"status": "closed", "updated_at": int(time.time())}
            if record.abandoned_reason is None:
                update["abandoned_reason"] = reason
            self._store(record.model_copy(update=update))
            return True


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
        task = PHASE_TASKS[manifest.phase]

        for site_id in plan.selected_sites:
            worker = self._workers.get(site_id)
            if worker is None:
                # An eligible site with no worker is an infrastructure gap, not
                # a refusal. It still gets a result, because a round whose
                # evidence omits a selected site looks the same as one where
                # that site was never selected.
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
                        refusal_reason=reason,
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
                        refusal_reason=acceptance.reason or None,
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
        return CandidateModel(
            round_id=manifest.round_id,
            candidate_version=f"{manifest.model_version}+{manifest.round_id}",
            parent_version=manifest.model_version,
            aggregation_method=manifest.aggregation.kind,
            contributing_sites=list(readiness.accepted_sites),
            site_weights=readiness.site_weights,
            contributed_examples=readiness.accepted_examples,
            metrics=weighted_metrics(readiness, results),
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
        self._sites = sites if sites is not None else InMemorySiteRegistry()
        self._rounds = rounds if rounds is not None else InMemoryRoundRepository()

    @property
    def engine_name(self) -> str:
        """Which federated runtime is attached."""
        return self._engine.name

    @property
    def engine_collects_results(self) -> bool:
        """True when the engine collects every result from the sites itself, so none can be posted."""
        return bool(getattr(self._engine, "collects_results", False))

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
        payload = round_digest(manifest.model_dump(mode="json"))
        return _planner_output("round digest", lambda: str(payload["round_digest"]))

    def plan_round(self, manifest: RoundManifest) -> RoundPlan:
        """Select the sites eligible for this round."""
        payload = compile_round_plan(
            manifest.model_dump(mode="json"),
            [registration.model_dump(mode="json") for registration in self._sites.list()],
        )
        return _planner_output("round plan", lambda: RoundPlan.model_validate(payload))

    def dispatch_round(self, manifest: RoundManifest) -> RoundRecord:
        """Plan the round and, if quorum allows, offer it to the selected sites.

        A round that cannot reach quorum is recorded and closed rather than
        dispatched. Offering a job to too few sites is what a participation
        threshold exists to prevent.

        The steps, each stored as it happens:

        1. The round is stored as ``dispatched``, with its plan, before the
           engine offers any job, so the update store can check uploads
           against the plan while the jobs run.
        2. The sites' answers to the offer are stored, sorted by site.
        3. The round becomes ``collecting``, with the results sorted by site
           and the engine's dispatch calls, if it reports them.
        4. With an engine that collects the results itself, every result is in
           when the dispatch ends, so the planner's readiness is stored on the
           round and returned with it: the caller needs no readiness call.
           ``aggregate`` still asks the planner again.

        Raises :class:`RoundConflict` when the round id is already stored: a
        second dispatch would replace the first round's evidence and decision.
        When the engine raises, or the round store refuses the results it
        returned (:class:`InvalidRoundRecord` for a result that holds NaN or
        an infinite number), the round is discarded with
        :data:`ENGINE_FAILED_REASON`, and when the readiness check raises, with
        :data:`READINESS_FAILED_REASON`; the error is raised again.
        """
        round_id = manifest.round_id
        if self._rounds.get(round_id) is not None:
            raise RoundConflict(f"round {round_id} already exists; a new round needs a new round id")
        plan = self.plan_round(manifest)
        now = int(time.time())
        record = self._rounds.create(
            RoundRecord(
                manifest=manifest,
                plan=plan,
                created_at=now,
                updated_at=now,
                status="dispatched" if plan.quorum_met else "closed",
            )
        )
        if not plan.quorum_met:
            return record

        try:
            submission = self._engine.submit_round(manifest, plan)
            acceptances = sorted(submission.acceptances, key=_by_site)
            self._rounds.update(
                round_id,
                lambda current: current.model_copy(
                    update={"acceptances": acceptances, "updated_at": int(time.time())}
                ),
            )
            results = self._engine.collect_results(round_id)
            dispatch = self._dispatch_calls(round_id)
        except Exception:
            self._discard_after_failure(round_id)
            raise

        def collected(current: RoundRecord) -> RoundRecord:
            # A result posted while the jobs ran is kept unless the engine
            # returned one for the same site.
            by_site = {result.site_id: result for result in current.results}
            by_site.update({result.site_id: result for result in results})
            return current.model_copy(
                update={
                    "results": sorted(by_site.values(), key=_by_site),
                    "dispatch": dispatch,
                    # A round discarded while its jobs ran stays closed.
                    "status": current.status if current.status in _FINISHED else "collecting",
                    "updated_at": int(time.time()),
                }
            )

        try:
            record = self._rounds.update(round_id, collected)
        except Exception:
            # Results the store cannot keep, such as one holding NaN, end the
            # dispatch as an engine failure does, so the engine's updates go too.
            self._discard_after_failure(round_id)
            raise
        if not self.engine_collects_results or record.status != "collecting":
            return record
        try:
            self.evaluate_readiness(round_id)
        except Exception:
            self._discard_after_failure(round_id, READINESS_FAILED_REASON)
            raise
        return self._require(round_id)

    def _dispatch_calls(self, round_id: str) -> list[DispatchCall]:
        dispatch_calls = getattr(self._engine, "dispatch_calls", None)
        if not callable(dispatch_calls):
            return []
        return [DispatchCall.model_validate(call) for call in dispatch_calls(round_id)]

    def _discard_after_failure(self, round_id: str, reason: str = ENGINE_FAILED_REASON) -> None:
        """Close a round whose dispatch failed, and delete what the engine holds for it.

        The first error is the one the caller needs, so an error here is not
        raised in its place.
        """
        try:
            self.discard(round_id, reason)
        except Exception:  # noqa: BLE001 - the engine's error is raised instead
            pass

    def submit_result(self, result: SiteResult) -> RoundRecord:
        """Record one site's returned result against its round.

        The result replaces any earlier result from the same site.

        Raises:
        - ``KeyError`` for an unknown round.
        - :class:`NotSelected` (a ``PermissionError``) for a site the round's
          plan did not select.
        - :class:`RoundConflict` for a round that is ``aggregated`` or
          ``closed``, so a late result cannot reopen it, and for any round
          when the engine collects the results from the sites itself.
        """
        collects = self.engine_collects_results
        engine = self.engine_name

        def change(record: RoundRecord) -> RoundRecord:
            if result.site_id not in record.plan.selected_sites:
                raise NotSelected(f"site {result.site_id} was not selected for round {result.round_id}")
            if record.status in _FINISHED:
                raise RoundConflict(f"round {result.round_id} is {record.status}; it takes no more results")
            if collects:
                raise RoundConflict(
                    f"the results of round {result.round_id} come from the sites through the {engine} engine; "
                    "a result cannot be posted"
                )
            results = [existing for existing in record.results if existing.site_id != result.site_id]
            results.append(result)
            results.sort(key=_by_site)
            return record.model_copy(
                update={
                    "results": results,
                    "updated_at": int(time.time()),
                    # While the engine still runs the jobs, the round stays dispatched.
                    "status": "dispatched" if record.status == "dispatched" else "collecting",
                }
            )

        return self._rounds.update(result.round_id, change)

    def evaluate_readiness(self, round_id: str, save: bool = True) -> AggregationReadiness:
        """Ask the planner whether the returned contributions may be aggregated.

        The planner receives the plan's ``selected_sites``, so a result from
        any other site is rejected with the reason "site was not selected for
        this round" and counts toward neither quorum nor weights.

        Params:
        - `save`: store the answer in the round's ``readiness`` field. The
          GMA's readiness route passes ``False``, so a read changes nothing.
        """
        record = self._require(round_id)
        payload = evaluate_aggregation_readiness(
            record.manifest.model_dump(mode="json"),
            [result.model_dump(mode="json") for result in record.results],
            selected_sites=record.plan.selected_sites,
        )
        readiness = _planner_output("aggregation readiness", lambda: AggregationReadiness.model_validate(payload))
        if save:
            self._rounds.update(
                round_id,
                lambda current: current.model_copy(update={"readiness": readiness, "updated_at": int(time.time())}),
            )
        return readiness

    def aggregate(self, round_id: str) -> CandidateModel:
        """Produce a candidate model, if and only if aggregation is permitted.

        This is the boundary the whole design protects: a candidate is created
        here, and a candidate is not a release. The candidate is stored on the
        round, which becomes ``aggregated``.

        Raises:
        - ``KeyError`` for an unknown round.
        - :class:`RoundConflict` for a round that was discarded, is already
          ``aggregated``, holds a release decision or is ``closed`` (a round
          without quorum is closed when it is dispatched). A second aggregate
          would replace the candidate a decision may rest on.
        - :class:`AggregationRefused` (a ``PermissionError``) when the planner
          does not permit aggregation. The controller raises it before it
          calls the engine, so what the engine holds is kept until the round
          is discarded. The engine may raise it too, for example when a stored
          update fails a check, and then deletes what it holds itself.

        What the engine holds for the round, such as stored updates, is
        deleted through the engine's ``discard`` after the round is stored as
        ``aggregated``, or when that write finds the round finished
        (:class:`RoundConflict`). Any other error from that write keeps it, so
        a retry can aggregate again. The GMA's HTTP engine then combines the
        kept updates again and gets the candidate it registered in the first
        attempt back from the model registry.
        """
        record = self._require(round_id)
        _require_aggregatable(record)
        readiness = self.evaluate_readiness(round_id)
        if not readiness.aggregation_permitted:
            raise AggregationRefused(
                f"aggregation is not permitted for {round_id}: {readiness.stop_reason or 'insufficient contributions'}"
            )
        candidate = self._engine.aggregate(record.manifest, readiness, record.results)

        def aggregated(current: RoundRecord) -> RoundRecord:
            # Another aggregate, a decision or a discard may have finished
            # while the engine combined the contributions.
            _require_aggregatable(current)
            return current.model_copy(
                update={"status": "aggregated", "candidate": candidate, "updated_at": int(time.time())}
            )

        try:
            self._rounds.update(round_id, aggregated)
        except RoundConflict:
            # The round finished meanwhile, so nothing will use what the engine holds for it.
            self._discard_after_aggregate(round_id)
            raise
        self._discard_after_aggregate(round_id)
        return candidate

    def _discard_in_engine(self, round_id: str) -> None:
        engine_discard = getattr(self._engine, "discard", None)
        if callable(engine_discard):
            engine_discard(round_id)

    def _discard_after_aggregate(self, round_id: str) -> None:
        """Delete what the engine holds for a round whose aggregated write has finished.

        The write's outcome is stored, so an error here is logged instead of
        raised. A later :meth:`discard` or :meth:`recover` deletes what is
        left.
        """
        try:
            self._discard_in_engine(round_id)
        except Exception as error:  # noqa: BLE001 - the aggregate's outcome is already stored
            logger.warning(
                "the engine could not delete what it holds for round %s after its aggregation (%s); "
                "a discard or a recovery deletes it",
                round_id,
                type(error).__name__,
            )

    def discard(self, round_id: str, reason: str) -> RoundRecord:
        """Stop a round and delete what the engine still holds for it.

        The round keeps its first discard reason in ``abandoned_reason``. A
        ``dispatched`` or ``collecting`` round becomes ``closed``, so it takes
        no more results and no more uploads. An ``aggregated`` or ``closed``
        round keeps its status and candidate. Then the engine's ``discard`` is
        called, when the engine has one, to delete the round's stored updates.

        Raises ``KeyError`` for an unknown round and ``ValueError`` for an
        empty reason.
        """
        _require_reason(reason, "discard")

        def change(record: RoundRecord) -> RoundRecord:
            update: dict[str, object] = {"updated_at": int(time.time())}
            if record.abandoned_reason is None:
                update["abandoned_reason"] = reason
            if record.status not in _FINISHED:
                update["status"] = "closed"
            return record.model_copy(update=update)

        record = self._rounds.update(round_id, change)
        self._discard_in_engine(round_id)
        return record

    def recover(
        self, *, updates_kept: bool, now: float, update_max_age_seconds: float | None = None
    ) -> RecoveryReport:
        """Close the rounds a restart left unfinished that cannot finish, as the planner decides.

        Call it once when the process starts, before it takes round writes: a
        dispatch still running in this process would be closed too.

        1. For each round :meth:`RoundRepository.unfinished` lists, oldest
           first, the planner's recovery rule (``dagentsc federation round
           recover``) answers keep or close, in one call per round. It reads
           the round's status, its phase, whether its record can be read,
           ``updates_kept``, whether the engine has registered the round's
           candidate, the age of the round's oldest stored update and
           ``update_max_age_seconds``.
        2. A readable round the planner closes goes through :meth:`discard`,
           with the planner's sentence as its reason, which also deletes its
           updates. A round whose record cannot be read is closed with
           :meth:`RoundRepository.quarantine`, and the engine deletes its
           updates.
        3. The engine deletes the stored updates of every round that is no
           longer unfinished or that the store does not hold, such as an
           aggregated round whose updates a restart left behind.

        Params:
        - `updates_kept`: whether the engine's stored updates survive a
          restart. A memory store does not keep them.
        - `now`: the Unix time the updates' ages are measured from. The
          caller reads the clock.
        - `update_max_age_seconds`: the age limit for stored updates, or
          ``None`` for no limit.

        Returns what it did. Raises ``TypeError`` when ``updates_kept`` is not
        a bool, and :class:`PlannerUnavailableError` when the planner cannot
        be run or refuses an input; the rounds closed before the error stay
        closed, and a later call goes on with the rest.
        """
        if not isinstance(updates_kept, bool):
            raise TypeError("updates_kept must be True or False")
        ages = self._pending_update_ages(now)
        candidate_registered = getattr(self._engine, "candidate_registered", None)
        outcomes: list[RoundRecovery] = []
        for summary in self._rounds.unfinished():
            registered = False
            if summary.readable and callable(candidate_registered):
                registered = bool(candidate_registered(self._require(summary.round_id).manifest))
            verdict = _recovery_verdict(
                round_recovery(
                    status=summary.status,
                    phase=summary.phase,
                    readable=summary.readable,
                    updates_kept=updates_kept,
                    candidate_registered=registered,
                    oldest_update_age_seconds=ages.get(summary.round_id),
                    update_max_age_seconds=update_max_age_seconds,
                )
            )
            if verdict.verdict == "close":
                self._close_after_restart(summary, verdict.message)
            outcomes.append(
                RoundRecovery(
                    round_id=summary.round_id,
                    status=summary.status,
                    phase=summary.phase,
                    readable=summary.readable,
                    verdict=verdict.verdict,
                    reason=verdict.reason,
                    message=verdict.message,
                )
            )
        still_unfinished = {summary.round_id for summary in self._rounds.unfinished()}
        leftovers = tuple(
            round_id for round_id in self._pending_update_ages(now) if round_id not in still_unfinished
        )
        for round_id in leftovers:
            self._discard_in_engine(round_id)
        return RecoveryReport(rounds=tuple(outcomes), deleted_updates=leftovers)

    def _pending_update_ages(self, now: float) -> dict[str, float]:
        pending = getattr(self._engine, "pending_update_ages", None)
        if not callable(pending):
            return {}
        return dict(pending(now))

    def _close_after_restart(self, summary: UnfinishedRound, message: str) -> None:
        """Close a round the planner's recovery rule closed, with its sentence as the reason."""
        if summary.readable:
            self.discard(summary.round_id, message)
        else:
            # Its record cannot be read, so it is closed from the store's columns.
            self._rounds.quarantine(summary.round_id, message)
            self._discard_in_engine(summary.round_id)
        logger.warning("recovery closed round %s: %s", summary.round_id, message)

    def evaluate_release(
        self,
        round_id: str,
        gates: list[ReleaseGate],
        candidate: CandidateModel,
        baseline_metrics: dict[str, float] | None = None,
        rollback_version: str | None = None,
    ) -> ReleaseDecision:
        """Evaluate release gates against a candidate and record the verdict.

        This is :meth:`decide_release` followed by
        :meth:`save_release_decision`. The round becomes ``closed``.

        Raises ``KeyError`` for an unknown round, and :class:`RoundConflict`
        for a round that already holds a release decision or is ``closed``.
        """
        decision = self.decide_release(round_id, gates, candidate, baseline_metrics, rollback_version)
        self.save_release_decision(round_id, decision)
        return decision

    def require_undecided(self, round_id: str) -> RoundRecord:
        """Return the round, or raise :class:`RoundConflict` when it takes no release decision.

        A round takes one decision, and a ``closed`` round takes none. The GMA
        calls this before it runs the release Guard, so a request that would be
        refused leaves no audit record. Raises ``KeyError`` for an unknown round.
        """
        record = self._require(round_id)
        _require_undecided(record)
        return record

    def decide_release(
        self,
        round_id: str,
        gates: list[ReleaseGate],
        candidate: CandidateModel,
        baseline_metrics: dict[str, float] | None = None,
        rollback_version: str | None = None,
    ) -> ReleaseDecision:
        """Ask the planner for the release decision, without saving it.

        The GMA saves the decision with :meth:`save_release_decision` only
        after the model registry accepted it, so a refused request leaves the
        round as it was. Raises like :meth:`require_undecided`.
        """
        self.require_undecided(round_id)
        payload = evaluate_release(
            [gate.model_dump(mode="json") for gate in gates],
            candidate.metrics,
            baseline_metrics or {},
            candidate.candidate_version,
            rollback_version or candidate.parent_version,
            round_id,
        )
        return _planner_output("release decision", lambda: ReleaseDecision.model_validate(payload))

    def save_release_decision(self, round_id: str, decision: ReleaseDecision) -> RoundRecord:
        """Store a decision on the round, which becomes ``closed``.

        Raises ``KeyError`` for an unknown round, and :class:`RoundConflict`
        when the round already holds a decision or is ``closed``, checked in
        the same atomic step that stores it.
        """

        def decided(current: RoundRecord) -> RoundRecord:
            _require_undecided(current)
            return current.model_copy(update={"decision": decision, "status": "closed", "updated_at": int(time.time())})

        return self._rounds.update(round_id, decided)

    def _require(self, round_id: str) -> RoundRecord:
        record = self._rounds.get(round_id)
        if record is None:
            raise _unknown_round(round_id)
        return record


def _require_aggregatable(record: RoundRecord) -> None:
    """Raise :class:`RoundConflict` for a round that cannot be aggregated (again)."""
    round_id = record.manifest.round_id
    if record.abandoned_reason is not None:
        raise RoundConflict(f"round {round_id} was discarded ({record.abandoned_reason}); it cannot be aggregated")
    if record.status == "aggregated":
        raise RoundConflict(f"round {round_id} is aggregated; it cannot be aggregated again")
    if record.decision is not None or record.status == "closed":
        raise RoundConflict(f"round {round_id} is closed; it cannot be aggregated")


def _require_undecided(record: RoundRecord) -> None:
    """Raise :class:`RoundConflict` for a round that takes no release decision."""
    round_id = record.manifest.round_id
    if record.decision is not None:
        raise RoundConflict(f"round {round_id} already holds a release decision; a new decision needs a new round")
    if record.status == "closed":
        raise RoundConflict(f"round {round_id} is closed; it takes no release decision")


def new_round_id(prefix: str = "round") -> str:
    """Generate a round identifier for callers that do not supply one."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


__all__ = [
    "ENGINE_FAILED_REASON",
    "FederatedRoundController",
    "FederationEngine",
    "InMemoryRoundRepository",
    "InMemorySiteRegistry",
    "InProcessFederationEngine",
    "READINESS_FAILED_REASON",
    "RecoveryReport",
    "RoundRecovery",
    "RoundRepository",
    "SiteRegistry",
    "SiteWorker",
    "UNFINISHED_STATUSES",
    "UnfinishedRound",
    "new_round_id",
    "round_record_json",
    "weighted_metrics",
]
