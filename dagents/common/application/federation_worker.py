"""The site-local half of a federated round.

This is what an LMA does when a coordinator offers it work. The framework owns
the sequence and where the Ethical Guard stands in it; the consumer owns what
"read the approved fields" and "train or evaluate" actually mean, supplied
through :class:`LocalDataSource` and :class:`LocalRunner`.

The sequence makes sure the site decides. A job is verified before it is
accepted, the data is guarded before it is read, and the result is guarded
before it leaves. A site that cannot satisfy any of those refuses, and the
refusal is recorded like any other outcome.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping, Protocol

from dagents.common.application.ethical_guard import contribution_norm
from dagents.common.application.governance_service import GovernanceService
from dagents.common.domain.federation import FederatedJob, JobAcceptance, SiteResult
from dagents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest


class LocalRunOutcome:
    """What a local training or evaluation run produced.

    ``update_norm`` is the magnitude of the contribution this site would send.
    It is reported so the coordinator's planner can reject an oversized update
    without ever seeing the update itself.

    ``update`` is the parameter update a training run produced, keyed by
    parameter name. When it is set, the worker ignores ``update_norm``: it
    sends the update through the Guard, measures the vector the Guard
    returned, and passes that vector to the site's :class:`UpdateChannel`.
    """

    def __init__(
        self,
        metrics: dict[str, float],
        examples: int,
        update_norm: float | None = None,
        evidence_pointer: str | None = None,
        update: dict[str, float] | None = None,
    ) -> None:
        self.metrics = metrics
        self.examples = examples
        self.update_norm = update_norm
        self.evidence_pointer = evidence_pointer
        self.update = update


class UpdateChannel(Protocol):
    """Where a site sends a model update after the Guard has bounded it.

    The channel stands for the federated runtime's transport. It receives the
    vector the ``before_send`` Guard returned, never the runner's raw output,
    and returns a digest of what it stored. The site computes its own digest of
    the vector it sent and refuses the job when the two differ. The site's
    digest goes into its :class:`SiteResult`, so the runtime can check that the
    update it aggregates is the one the site sent, even if the transport
    changed it. The channel and the site must use the same digest rule (see
    ``LocalFederatedWorker``'s ``update_digest``).
    """

    def submit(self, job: FederatedJob, update: dict[str, float]) -> str:
        """Store one site's bounded update for this job and return its digest."""


class LocalDataSource(Protocol):
    """Where a site's approved local records come from."""

    def records(self, job: FederatedJob) -> list[dict[str, Any]]:
        """Return the local records this job is permitted to consider."""

    def cohort_size(self, job: FederatedJob) -> int:
        """How many local subjects this job's cohort contains.

        Optional. The worker falls back to counting records when a source does
        not implement it. Implement it when counting is cheaper than
        materializing, which for a real database source it usually is: the
        before-train check needs the size, not the rows.
        """


class LocalRunner(Protocol):
    """What a site actually computes for a job."""

    def run(self, job: FederatedJob, records: list[dict[str, Any]]) -> LocalRunOutcome:
        """Train or evaluate locally and return the permitted summary."""


class LocalFederatedWorker:
    """Runs one site's side of a round, with the Guard at every boundary.

    Inputs:
    - ``site_id``: this site's identity in the study.
    - ``governance``: the service that plans and enforces restrictions.
    - ``data_source`` / ``runner``: the consumer's local data and computation.
    - ``classification_id``: which classification governs this site's fields.
    - ``feature_contract_version``: what this site is able to produce, checked
      against the round's contract before accepting.
    - ``approved_fields``: the fields this site has agreed may be used. A job
      that names its own fields is still intersected against these, so a round
      cannot widen what a site approved.
    - ``expected_digest``: the manifest digest this site independently computed.
      Passing it is what makes digest verification meaningful; without it the
      site is only checking a job against itself.
    - ``update_channel``: where bounded model updates go. Needed only when the
      runner returns an update. A site whose runner returns an update and that
      has no channel refuses the job, because the only other place to put the
      vector is the result the coordinator reads.
    - ``update_digest``: how this site digests the update it sends. It must be
      the rule the channel uses. Defaults to :meth:`default_update_digest`.
    """

    def __init__(
        self,
        site_id: str,
        governance: GovernanceService,
        data_source: LocalDataSource,
        runner: LocalRunner,
        classification_id: str,
        feature_contract_version: str,
        approved_fields: list[str] | None = None,
        approved_purposes: list[str] | None = None,
        expected_digest: str | None = None,
        update_channel: UpdateChannel | None = None,
        update_digest: Callable[[Mapping[str, float]], str] | None = None,
    ) -> None:
        self.site_id = site_id
        self._governance = governance
        self._data_source = data_source
        self._runner = runner
        self._classification_id = classification_id
        self._feature_contract_version = feature_contract_version
        self._approved_fields = approved_fields or []
        self._approved_purposes = approved_purposes or []
        self._expected_digest = expected_digest
        self._update_channel = update_channel
        self._update_digest = update_digest or self.default_update_digest

    @staticmethod
    def default_update_digest(update: Mapping[str, float]) -> str:
        """``sha256:`` and the hex digest of the update as JSON with sorted keys and no spaces."""
        material = json.dumps(dict(update), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _fields_for(self, job: FederatedJob) -> list[str]:
        """Resolve which fields this job may touch at this site.

        A job's field list is a request, not an instruction. Where the site has
        declared approved fields, the job is narrowed to their intersection, so
        a round asking for more than a site agreed to gets less rather than
        being refused outright.

        An empty result is meaningful and must not be treated as "no
        constraint": it means the site approved nothing the job asked for. Use
        :meth:`_narrowing_rejected_everything` to tell that apart from the case
        where neither side declared any fields at all.
        """
        requested = list(job.feature_fields)
        if not self._approved_fields:
            return requested
        if not requested:
            return list(self._approved_fields)
        approved = set(self._approved_fields)
        return [field for field in requested if field in approved]

    def _narrowing_rejected_everything(self, job: FederatedJob) -> bool:
        """Whether narrowing left nothing because the site approved none of it.

        This is the distinction that makes the approved-field list a guarantee
        rather than a suggestion. Without it an empty intersection is falsy and
        falls through to "read whatever is there", which is the exact inversion
        of what the site agreed to.
        """
        if not self._approved_fields or not job.feature_fields:
            return False
        return not self._fields_for(job)

    def _cohort_size(self, job: FederatedJob) -> int:
        """Ask the source how large the cohort is, counting records if it cannot."""
        resolver = getattr(self._data_source, "cohort_size", None)
        if callable(resolver):
            return int(resolver(job))
        return len(self._data_source.records(job))

    def _requester(self, job: FederatedJob) -> Requester:
        """Describe the coordinator as the Guard sees it.

        The coordinator is trusted through the round contract, not through a
        person: the verified attributes are the digests it supplied and the
        privacy profile it named.
        """
        return Requester(
            requester_id=job.manifest.study_id,
            requester_kind="coordinator",
            affiliation=job.manifest.study_id,
            stated_purpose=job.manifest.condition_id,
            attributes=[
                KyuAttribute(attribute_id="round_manifest_digest", verified=bool(job.manifest_digest)),
                KyuAttribute(
                    attribute_id="training_code_digest", verified=bool(job.manifest.training_code_digest)
                ),
                KyuAttribute(
                    attribute_id="privacy_profile", verified=job.manifest.privacy_profile != "default"
                ),
            ],
            compliance_history=1.0 if job.manifest.model_artifact_digest else 0.5,
        )

    def _request(
        self, job: FederatedJob, boundary: str, fields: list[str], granularity: str, cohort: int | None
    ) -> RestrictionRequest:
        return RestrictionRequest(
            request_id=f"{job.round_id}:{self.site_id}:{boundary}",
            boundary=boundary,  # type: ignore[arg-type]
            requester=self._requester(job),
            classification=self._governance.get_classification(self._classification_id),
            requested_fields=fields,
            granularity=granularity,  # type: ignore[arg-type]
            cohort_size=cohort,
            declared_purpose=job.manifest.condition_id,
            approved_purposes=self._approved_purposes,
        )

    def offer(self, job: FederatedJob) -> JobAcceptance:
        """Verify the job before any code touches local data.

        Three checks, in order of how cheaply they can be refused: the digest
        must match what this site computed, the feature contract must be the one
        this site can produce, and the Guard must permit training at all.
        """
        if self._narrowing_rejected_everything(job):
            return JobAcceptance(
                round_id=job.round_id,
                site_id=self.site_id,
                accepted=False,
                reason="no field this round requests is within this site's approved field list",
                verified_digest=self._expected_digest is None
                or job.manifest_digest == self._expected_digest,
                feature_contract_version=self._feature_contract_version,
            )
        if self._expected_digest and job.manifest_digest != self._expected_digest:
            return JobAcceptance(
                round_id=job.round_id,
                site_id=self.site_id,
                accepted=False,
                reason="job digest does not match the manifest this site computed",
                verified_digest=False,
                feature_contract_version=self._feature_contract_version,
            )
        if job.manifest.feature_contract_version != self._feature_contract_version:
            return JobAcceptance(
                round_id=job.round_id,
                site_id=self.site_id,
                accepted=False,
                reason=(
                    f"site produces {self._feature_contract_version}, "
                    f"round requires {job.manifest.feature_contract_version}"
                ),
                verified_digest=True,
                feature_contract_version=self._feature_contract_version,
            )

        # The before-train check is about the whole local table and the cohort
        # behind it, so it supplies the site's cohort size rather than leaving
        # it unstated. An unstated cohort is refused by the Rails, which is
        # correct for a data request and wrong for a training approval.
        decision = self._governance.enforce(
            self._request(
                job, "before_train", self._fields_for(job), "table", self._cohort_size(job)
            ),
            correlation_id=job.round_id,
        )
        if not decision.permitted:
            return JobAcceptance(
                round_id=job.round_id,
                site_id=self.site_id,
                accepted=False,
                reason=decision.message or "local governance declined this job",
                verified_digest=True,
                feature_contract_version=self._feature_contract_version,
            )
        return JobAcceptance(
            round_id=job.round_id,
            site_id=self.site_id,
            accepted=True,
            reason="job verified and permitted by local governance",
            verified_digest=True,
            feature_contract_version=self._feature_contract_version,
        )

    def execute(self, job: FederatedJob) -> SiteResult:
        """Read, run, and return only what the Guard permits to leave.

        The before-send check is the one that matters most: it is the last point
        at which this site controls what crosses its boundary, and it treats the
        outbound contribution as ``model_update`` granularity rather than as
        rows, because that is what is actually leaving.

        The runner is handed the *guarded* payload, not the raw records. That is
        deliberate, and it is the stricter of two defensible readings. The code
        running here is the coordinator's, shipped in with the round, so what it
        observes is what the coordinator observes; generalizing before it runs
        limits what foreign code can see even though the data never leaves the
        building. The looser reading — guard only what crosses the boundary — is
        cheaper in accuracy but gives a round's code unrestricted sight of every
        approved field.

        It is not free. Generalizing a high-sensitivity numeric field costs
        accuracy, and the healthcare demo measures that cost. A deployment that
        judges the cost too high changes the field's sensitivity in its
        classification; it does not change this code path.
        """
        if self._narrowing_rejected_everything(job):
            return self._refused(job, "no requested field is within this site's approved field list")

        records = self._data_source.records(job)
        read_fields = self._fields_for(job)
        if not read_fields:
            # Reached only when neither the site nor the round declared any
            # fields, which is the one case where inferring them from the
            # records does not widen anything anyone agreed to.
            read_fields = sorted({key for record in records for key in record})
        read_decision = self._governance.enforce(
            self._request(job, "before_read", read_fields, "row", len(records)),
            records,
            correlation_id=job.round_id,
        )
        if not read_decision.permitted:
            return self._refused(job, read_decision.message or "local read was not permitted")

        outcome = self._runner.run(job, read_decision.payload)
        if outcome.update is not None:
            return self._send_update(job, outcome)

        send_decision = self._governance.enforce(
            self._request(job, "before_send", ["model_update"], "model_update", outcome.examples),
            [{"model_update": outcome.update_norm if outcome.update_norm is not None else 0.0}],
            correlation_id=job.round_id,
        )
        if not send_decision.permitted:
            return self._refused(job, send_decision.message or "outbound contribution was not permitted")

        # The Guard may have clipped or noised the contribution. What it returned
        # is what leaves, not what the runner produced — and if it returned
        # nothing for the field, nothing leaves. Falling back to the runner's own
        # value here would send the unclipped norm precisely when the Guard had
        # decided to withhold it.
        if not send_decision.payload or "model_update" not in send_decision.payload[0]:
            return self._refused(job, "the guard withheld the outbound contribution")
        bounded_norm = send_decision.payload[0]["model_update"]
        return self._completed(job, outcome, bounded_norm)

    def _send_update(self, job: FederatedJob, outcome: LocalRunOutcome) -> SiteResult:
        """Bound a model update with the Guard and pass it to the update channel.

        The update is sent through the ``before_send`` Guard as one
        ``model_update`` value. The channel receives the vector the Guard
        returned, and ``update_norm`` is that vector's L2 norm. The result
        carries the norm and the site's own digest of the vector, never the
        vector.

        Every failure refuses the job: no channel, a Guard denial, a withheld or
        malformed update, a channel that cannot store it, or a channel whose
        digest differs from the site's. Sending nothing is the only safe
        fallback.
        """
        if self._update_channel is None:
            return self._refused(job, "the runner produced a model update but this site has no update channel")
        if not isinstance(outcome.update, dict):
            return self._refused(job, "a model update must map parameter names to numbers")

        send_decision = self._governance.enforce(
            self._request(job, "before_send", ["model_update"], "model_update", outcome.examples),
            [{"model_update": dict(outcome.update)}],
            correlation_id=job.round_id,
        )
        if not send_decision.permitted:
            return self._refused(job, send_decision.message or "outbound contribution was not permitted")

        bounded = send_decision.payload[0].get("model_update") if send_decision.payload else None
        bounded_norm = contribution_norm(bounded) if isinstance(bounded, dict) else None
        if bounded_norm is None:
            return self._refused(job, "the guard withheld the outbound model update")

        try:
            own_digest = self._update_digest(dict(bounded))
        except (TypeError, ValueError) as exc:
            return self._refused(job, f"the site could not digest the bounded update: {exc}")
        try:
            digest = self._update_channel.submit(job, dict(bounded))
        except Exception as exc:  # noqa: BLE001 - any failure to store the update refuses the job
            return self._refused(job, f"the update channel did not accept the update: {exc}")
        if not isinstance(digest, str) or not digest:
            return self._refused(job, "the update channel returned no digest")
        if digest != own_digest:
            return self._refused(job, "the update channel stored a different update than the site sent")
        return self._completed(job, outcome, bounded_norm, update_digest=own_digest)

    def _completed(
        self,
        job: FederatedJob,
        outcome: LocalRunOutcome,
        update_norm: float,
        update_digest: str | None = None,
    ) -> SiteResult:
        """Return the result of a job whose contribution the Guard permitted."""
        return SiteResult(
            round_id=job.round_id,
            site_id=self.site_id,
            job_digest=job.manifest_digest,
            participation="completed",
            code_verified=True,
            privacy_checks_passed=True,
            contributed_examples=outcome.examples,
            update_norm=update_norm,
            update_digest=update_digest,
            metrics=outcome.metrics,
            local_evidence_pointer=outcome.evidence_pointer or f"site-local://{self.site_id}/{job.round_id}",
        )

    def _refused(self, job: FederatedJob, reason: str) -> SiteResult:
        """Return a result that records a refusal rather than a gap.

        The reason is in ``refusal_reason``. The evidence pointer still ends
        with ``#refused:<reason>``, because callers written before that field
        existed read the reason from there.
        """
        return SiteResult(
            round_id=job.round_id,
            site_id=self.site_id,
            job_digest=job.manifest_digest,
            participation="rejected",
            code_verified=True,
            privacy_checks_passed=False,
            contributed_examples=0,
            metrics={},
            local_evidence_pointer=f"site-local://{self.site_id}/{job.round_id}#refused:{reason}",
            refusal_reason=reason,
        )
