"""The site-local half of a federated round.

This is what an LMA does when a coordinator offers it work. The framework owns
the sequence and where the Ethical Guard stands in it; the consumer owns what
"read the approved fields" and "train or evaluate" actually mean, supplied
through :class:`LocalDataSource` and :class:`LocalRunner`.

The sequence exists to make one thing true: the hospital decides. A job is
verified before it is accepted, the data is guarded before it is read, and the
result is guarded before it leaves. A site that cannot satisfy any of those
refuses, and the refusal is recorded like any other outcome.
"""

from __future__ import annotations

from typing import Any, Protocol

from agents.common.application.governance_service import GovernanceService
from agents.common.domain.federation import FederatedJob, JobAcceptance, SiteResult
from agents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest


class LocalRunOutcome:
    """What a local training or evaluation run produced.

    ``update_norm`` is the magnitude of the contribution this site would send.
    It is reported so the coordinator's planner can reject an oversized update
    without ever seeing the update itself.
    """

    def __init__(
        self,
        metrics: dict[str, float],
        examples: int,
        update_norm: float | None = None,
        evidence_pointer: str | None = None,
    ) -> None:
        self.metrics = metrics
        self.examples = examples
        self.update_norm = update_norm
        self.evidence_pointer = evidence_pointer


class LocalDataSource(Protocol):
    """Where a site's approved local records come from."""

    def records(self, job: FederatedJob) -> list[dict[str, Any]]:
        """Return the local records this job is permitted to consider."""

    def cohort_size(self, job: FederatedJob) -> int:
        """How many local subjects this job's cohort contains.

        Optional. The worker falls back to counting records when a source does
        not implement it. Implement it when counting is cheaper than
        materializing, which for a real hospital source it usually is: the
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

    def _fields_for(self, job: FederatedJob) -> list[str]:
        """Resolve which fields this job may touch at this site.

        A job's field list is a request, not an instruction. Where the site has
        declared approved fields, the job is narrowed to their intersection, so
        a round asking for more than a site agreed to gets less rather than
        being refused outright.
        """
        requested = list(job.feature_fields)
        if not self._approved_fields:
            return requested
        if not requested:
            return list(self._approved_fields)
        approved = set(self._approved_fields)
        return [field for field in requested if field in approved]

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
        """
        records = self._data_source.records(job)
        read_fields = self._fields_for(job) or sorted({key for record in records for key in record})
        read_decision = self._governance.enforce(
            self._request(job, "before_read", read_fields, "row", len(records)),
            records,
            correlation_id=job.round_id,
        )
        if not read_decision.permitted:
            return self._refused(job, read_decision.message or "local read was not permitted")

        outcome = self._runner.run(job, read_decision.payload)

        send_decision = self._governance.enforce(
            self._request(job, "before_send", ["model_update"], "model_update", outcome.examples),
            [{"model_update": outcome.update_norm if outcome.update_norm is not None else 0.0}],
            correlation_id=job.round_id,
        )
        if not send_decision.permitted:
            return self._refused(job, send_decision.message or "outbound contribution was not permitted")

        bounded_norm = outcome.update_norm
        if send_decision.payload:
            # The Guard may have clipped or noised the contribution. What it
            # returned is what leaves, not what the runner produced.
            bounded_norm = send_decision.payload[0].get("model_update", bounded_norm)

        return SiteResult(
            round_id=job.round_id,
            site_id=self.site_id,
            job_digest=job.manifest_digest,
            participation="completed",
            code_verified=True,
            privacy_checks_passed=True,
            contributed_examples=outcome.examples,
            update_norm=bounded_norm,
            metrics=outcome.metrics,
            local_evidence_pointer=outcome.evidence_pointer or f"site-local://{self.site_id}/{job.round_id}",
        )

    def _refused(self, job: FederatedJob, reason: str) -> SiteResult:
        """Return a result that records a refusal rather than a gap."""
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
        )
