"""The site's side of a federated round, as an LMA serves it to the GMA.

The GMA offers each selected site a job, then asks it to execute the job if
the site accepted. The site checks the job before it reads any data, in this
order:

1. the job is addressed to this site, for the round its manifest names, and
   the manifest is for this site's condition;
2. the job's task is the task of the round's phase;
3. the job carries a model (``parameters["model"]``), for the manifest's
   model version and model digest;
4. a model kind the site's extensions contribute runs that model, the
   parameters match their digest, and the manifest's training code digest is
   the code the kind runs;
5. a training job's kind can be trained;
6. the site computes the manifest's round digest with its own planner;
7. :class:`LocalFederatedWorker` checks the round digest, the feature contract
   version and the approved fields, and runs the ``before_train`` Guard with
   the site's cohort size.

A refusal at any step is an answer, not an error, and is recorded in the job
log. A planner failure refuses the job.

Executing runs :class:`LocalFederatedWorker` on the Guard's payload. A training
job's clipped update goes to the update channel, which sends it to the GMA's
update store; the result carries only its norm and digest. Each accepted offer
can be executed once, with the same job.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import logging
import threading
from typing import Any, Callable

from dagents.common.application.federation_worker import LocalFederatedWorker, LocalRunOutcome, UpdateChannel
from dagents.common.application.ports import JobLog
from dagents.common.domain.errors import ModelVerificationError, RoundConflict
from dagents.common.domain.federation import (
    PHASE_TASKS,
    FederatedJob,
    JobAcceptance,
    JobModel,
    JobRun,
    RoundManifest,
    SiteResult,
)
from dagents.common.domain.site import JobLogEntry
from dagents.common.extensions import ExtensionError, ModelKind, TrainingOutcome
from dagents.common.infrastructure.dagents_runner import dagentsc_binary, round_digest
from dagents.lma.application.site_service import SiteService, has_label

logger = logging.getLogger(__name__)

#: How many rounds the service remembers: offered rounds, accepted jobs and round digests.
REMEMBERED_ROUNDS = 1024

#: Computes a manifest's round digest with the planner. Raises when the planner cannot be run.
RoundDigester = Callable[[RoundManifest], str]

#: Builds the update channel for a trainable model kind.
ChannelFactory = Callable[[ModelKind], UpdateChannel]


def _canonical(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


#: Round digests the planner computed in this process, by planner binary and manifest.
_PLANNER_DIGESTS: OrderedDict[tuple[str, str], str] = OrderedDict()
#: The manifests whose digest the planner is computing now, so others wait for it.
_PLANNER_DIGESTS_IN_FLIGHT: dict[tuple[str, str], threading.Event] = {}
_PLANNER_DIGESTS_LOCK = threading.Lock()
#: How many manifests' digests the process keeps.
PLANNER_DIGESTS_KEPT = 512


def planner_round_digest(manifest: RoundManifest) -> str:
    """The manifest's round digest from the planner (``dagentsc federation round digest``).

    The digest is a pure function of the manifest, so the process keeps each
    one the planner computed, by planner binary and manifest. Several LMAs in
    one process that are offered the same round at the same time run the
    planner once: the first computes the digest and the others wait for it.
    Each LMA still compares the job's digest with this one. A planner that
    could not be run is asked again by the next caller, so a failure is never
    kept and the job is refused.
    """
    payload = manifest.model_dump(mode="json")
    key = (dagentsc_binary(), _canonical(payload))
    while True:
        with _PLANNER_DIGESTS_LOCK:
            known = _PLANNER_DIGESTS.get(key)
            if known is not None:
                return known
            running = _PLANNER_DIGESTS_IN_FLIGHT.get(key)
            if running is None:
                running = _PLANNER_DIGESTS_IN_FLIGHT[key] = threading.Event()
                break
        # Another caller is asking the planner for this manifest: use its answer,
        # or ask the planner after it, when it failed.
        running.wait(timeout=60.0)
    try:
        digest = round_digest(payload).get("round_digest")
        if not isinstance(digest, str) or not digest:
            raise ValueError("the planner returned no round digest")
        with _PLANNER_DIGESTS_LOCK:
            _PLANNER_DIGESTS[key] = digest
            while len(_PLANNER_DIGESTS) > PLANNER_DIGESTS_KEPT:
                _PLANNER_DIGESTS.popitem(last=False)
        return digest
    finally:
        with _PLANNER_DIGESTS_LOCK:
            _PLANNER_DIGESTS_IN_FLIGHT.pop(key, None)
        running.set()


class SourceDataSource:
    """The records a job at this site may consider: the site's records, read now.

    The label is kept for evaluation and training, which need it, and stays at
    the site: the job returns metrics and a bounded update, never rows. The
    label is removed for profile jobs, so profiling code never reads an
    individual outcome.
    """

    def __init__(self, site: SiteService) -> None:
        self._site = site

    def records(self, job: FederatedJob) -> list[dict[str, Any]]:
        rows = self._site.job_records()
        label = self._site.label_field
        if job.task == "profile" and label is not None:
            return [{key: value for key, value in row.items() if key != label} for row in rows]
        return rows

    def cohort_size(self, job: FederatedJob) -> int:
        return len(self._site.job_records())


class KindRunner:
    """What a site computes for each task, on the Guard's payload.

    - ``profile``: :meth:`SiteService.profile_metrics`.
    - ``evaluate``: the model kind's ``evaluate`` on the labelled records of
      the payload. ``examples`` is how many there are.
    - ``train``: the model kind's ``train`` on the labelled records of the
      payload. The update goes to the worker, which bounds it with the Guard.
    """

    def __init__(self, site: SiteService, kind: ModelKind | None, model: JobModel | None) -> None:
        self._site = site
        self._kind = kind
        self._model = model

    def run(self, job: FederatedJob, records: list[dict[str, Any]]) -> LocalRunOutcome:
        pointer = f"site-local://{self._site.site_id}/rounds/{job.round_id}/evidence"
        if job.task == "profile":
            return LocalRunOutcome(metrics=self._site.profile_metrics(records), examples=len(records), evidence_pointer=pointer)
        if self._kind is None or self._model is None:
            raise ModelVerificationError("the job carries no model")
        label = self._site.label_field
        labelled = [record for record in records if has_label(record, label)]
        if job.task == "evaluate":
            metrics = self._kind.evaluate(self._model.params, labelled, label)
            return LocalRunOutcome(metrics=_floats(metrics), examples=len(labelled), evidence_pointer=pointer)
        outcome = self._kind.train(self._model.params, labelled, label)  # type: ignore[attr-defined]
        if not isinstance(outcome, TrainingOutcome):
            outcome = TrainingOutcome.model_validate(outcome)
        return LocalRunOutcome(
            metrics=_floats(outcome.metrics),
            examples=outcome.examples,
            evidence_pointer=pointer,
            update=dict(outcome.update) if outcome.update is not None else None,
        )


def _floats(metrics: dict[str, Any]) -> dict[str, float]:
    return {str(name): float(value) for name, value in metrics.items()}


@dataclass(frozen=True)
class _Accepted:
    fingerprint: str
    expected_digest: str
    kind: str


class FederationJobService:
    """Offers, executions and the job log of one site.

    Params:
    - `site`: the site's service: identity, records, Guard and model kinds.
    - `job_log`: where each refusal and each result is recorded.
    - `channel_factory`: builds the update channel for a trainable kind.
      Without one, a training job is refused by the worker, because the only
      other place for the update is the result the coordinator reads.
    - `digester`: computes a manifest's round digest. Defaults to the planner.
    """

    def __init__(
        self,
        site: SiteService,
        *,
        job_log: JobLog,
        channel_factory: ChannelFactory | None = None,
        digester: RoundDigester = planner_round_digest,
    ) -> None:
        self._site = site
        self._log = job_log
        self._channels = channel_factory
        self._digester = digester
        self._lock = threading.Lock()
        self._offered: OrderedDict[str, None] = OrderedDict()
        self._accepted: OrderedDict[str, _Accepted] = OrderedDict()
        self._digests: OrderedDict[str, str] = OrderedDict()

    @property
    def site_id(self) -> str:
        return self._site.site_id

    # --- Offer -----------------------------------------------------------------

    def offer(self, job: FederatedJob) -> JobAcceptance:
        """Check the job before any data is read. Raises :class:`RoundConflict` for a second offer of a round."""
        with self._lock:
            if job.round_id in self._offered:
                raise RoundConflict(f"this site was already offered a job for round {job.round_id}")
            self._offered[job.round_id] = None
            while len(self._offered) > REMEMBERED_ROUNDS:
                self._offered.popitem(last=False)

        reason, kind, model = self._check(job)
        expected = None
        if reason is None:
            try:
                expected = self._expected_digest(job.manifest)
            except Exception:  # noqa: BLE001 - a planner failure refuses the job
                logger.warning("site %s: the planner could not compute the digest of round %s", self.site_id, job.round_id)
                reason = "the site could not compute the round digest with its planner, so it refuses the job"
        if reason is not None:
            self._log_refusal(job, reason)
            return JobAcceptance(
                round_id=job.round_id,
                site_id=self.site_id,
                accepted=False,
                reason=reason,
                verified_digest=False,
                feature_contract_version=self._site.feature_contract_version,
            )

        assert kind is not None and expected is not None
        acceptance = self._worker(job, kind, model, expected).offer(job)
        if acceptance.accepted:
            with self._lock:
                self._accepted[job.round_id] = _Accepted(_canonical(job.model_dump(mode="json")), expected, kind.kind)
                while len(self._accepted) > REMEMBERED_ROUNDS:
                    self._accepted.popitem(last=False)
        else:
            self._log_refusal(job, acceptance.reason)
        return acceptance

    def _check(self, job: FederatedJob) -> tuple[str | None, ModelKind | None, JobModel | None]:
        """The checks that need no data and no planner. Returns the refusal reason, or the kind and model."""
        site = self._site
        manifest = job.manifest
        if job.site_id != site.site_id:
            return f"the job is addressed to site {job.site_id}, not to this site", None, None
        if job.round_id != manifest.round_id:
            return "the job's round id differs from the round id in its manifest", None, None
        if manifest.condition_id != site.condition_id:
            return f"this site serves {site.condition_id}, not {manifest.condition_id}", None, None
        expected_task = PHASE_TASKS[manifest.phase]
        if job.task != expected_task:
            return f"the job's task {job.task} does not match the round's phase {manifest.phase}", None, None
        try:
            model = JobModel.from_parameters(job.parameters)
        except ValueError:
            return "the job's model is not a valid model description", None, None
        if model is None:
            return "the job carries no model", None, None
        if model.version != manifest.model_version:
            return f"the job's model {model.version} is not the round's model {manifest.model_version}", None, None
        if model.digest != manifest.model_artifact_digest:
            return "the job's model digest is not the round's model artifact digest", None, None
        try:
            kind = site.registry.model_kind(model.kind)
        except ExtensionError:
            return f"model kind {model.kind} is not one this site can run", None, None
        try:
            digest = kind.parameter_digest(model.params)
        except (ModelVerificationError, TypeError, ValueError) as error:
            return f"the job's model parameters are not valid: {error}", None, None
        if digest != model.digest:
            return "the job's model parameters do not match its digest", None, None
        if kind.code_digest() != manifest.training_code_digest:
            return f"the round's code digest is not the code this site runs for a {kind.kind}", None, None
        if job.task == "train" and not kind.trainable:
            return f"{model.version} is a {kind.kind}, which cannot be trained", None, None
        if job.task != "profile" and site.label_field is None:
            return f"condition pack {site.condition_id} names no label field, so this site cannot {job.task}", None, None
        return None, kind, model

    def _expected_digest(self, manifest: RoundManifest) -> str:
        """The round digest this site computes for the manifest, kept per manifest."""
        key = _canonical(manifest.model_dump(mode="json"))
        with self._lock:
            known = self._digests.get(key)
        if known is not None:
            return known
        digest = self._digester(manifest)
        with self._lock:
            self._digests[key] = digest
            while len(self._digests) > REMEMBERED_ROUNDS:
                self._digests.popitem(last=False)
        return digest

    def _worker(self, job: FederatedJob, kind: ModelKind, model: JobModel | None, expected: str) -> LocalFederatedWorker:
        site = self._site
        trainable = bool(getattr(kind, "trainable", False))
        return LocalFederatedWorker(
            site_id=site.site_id,
            governance=site.governance,
            data_source=SourceDataSource(site),
            runner=KindRunner(site, kind, model),
            classification_id=site.classification.classification_id,
            feature_contract_version=site.feature_contract_version,
            approved_fields=site.approved_fields,
            approved_purposes=site.approved_purposes,
            expected_digest=expected,
            update_channel=self._channels(kind) if trainable and self._channels is not None else None,
            update_digest=kind.update_digest if trainable else None,  # type: ignore[attr-defined]
        )

    # --- Execute -----------------------------------------------------------------

    def execute(self, job: FederatedJob) -> SiteResult:
        """Run an accepted job once. Raises :class:`RoundConflict` for a job this site did not accept, or ran already."""
        with self._lock:
            accepted = self._accepted.pop(job.round_id, None)
        if accepted is None:
            raise RoundConflict(f"this site has no accepted job for round {job.round_id} to run")
        if accepted.fingerprint != _canonical(job.model_dump(mode="json")):
            raise RoundConflict(f"the job for round {job.round_id} differs from the job this site accepted")

        try:
            kind = self._site.registry.model_kind(accepted.kind)
            model = JobModel.from_parameters(job.parameters)
            result = self._worker(job, kind, model, accepted.expected_digest).execute(job)
        except (ModelVerificationError, ExtensionError) as error:
            result = self._ended(job, "rejected", str(error))
        except Exception as error:  # noqa: BLE001 - a local failure is a failed result, never a crash
            logger.warning(
                "site %s: the local run of round %s failed with %s", self.site_id, job.round_id, type(error).__name__
            )
            result = self._ended(job, "failed", f"the local run failed ({type(error).__name__})")
        self._log_result(job, result)
        return result

    # --- Run ---------------------------------------------------------------------

    def run(self, job: FederatedJob) -> JobRun:
        """Offer the job and, when the site accepts it, execute it: the two steps of :meth:`offer` and :meth:`execute`.

        A refused job has no result. Raises what :meth:`offer` and
        :meth:`execute` raise, such as :class:`RoundConflict` for a round this
        site was offered before.
        """
        acceptance = self.offer(job)
        if not acceptance.accepted:
            return JobRun(acceptance=acceptance)
        return JobRun(acceptance=acceptance, result=self.execute(job))

    def _ended(self, job: FederatedJob, participation: str, reason: str) -> SiteResult:
        return SiteResult(
            round_id=job.round_id,
            site_id=self.site_id,
            job_digest=job.manifest_digest,
            participation=participation,  # type: ignore[arg-type]
            code_verified=False,
            privacy_checks_passed=False,
            contributed_examples=0,
            metrics={},
            local_evidence_pointer=f"site-local://{self.site_id}/{job.round_id}#refused:{reason}",
            refusal_reason=reason,
        )

    # --- The job log -------------------------------------------------------------

    def _log_refusal(self, job: FederatedJob, reason: str) -> None:
        self._log.append(
            JobLogEntry(
                round_id=job.round_id,
                phase=job.manifest.phase,
                participation="rejected",
                contributed_examples=0,
                job_digest=job.manifest_digest,
                metrics={},
                reason=reason or "the site refused the job",
            )
        )

    def _log_result(self, job: FederatedJob, result: SiteResult) -> None:
        self._log.append(
            JobLogEntry(
                round_id=job.round_id,
                phase=job.manifest.phase,
                participation=result.participation,
                contributed_examples=result.contributed_examples,
                update_norm=result.update_norm,
                update_digest=result.update_digest,
                job_digest=result.job_digest,
                metrics=dict(result.metrics),
                reason=result.refusal_reason,
            )
        )

    def recent(self, limit: int = 20) -> list[JobLogEntry]:
        """The site's job log, newest first."""
        return self._log.recent(limit)
