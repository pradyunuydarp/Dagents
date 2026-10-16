"""Release evaluation at the GMA: the release Guard, the gates and the lifecycle rule.

``POST /api/v1/federation/rounds/{round_id}:evaluate-release`` has two flows
(:class:`~agents.common.domain.federation.ReleaseEvaluationRequest`):

**Round flow** (the request names a ``condition_id``). In this order:

1. The request must not carry gates or metrics, and must name the release
   requester (422 otherwise).
2. ``candidate_version`` must be the candidate the round produced (409).
3. The condition pack must be registered (404) and be the round's condition
   (409).
4. The candidate's metrics are the stored aggregate of ``metrics_from_round``,
   which must have measured this candidate (409). The baseline metrics are the
   stored aggregate of ``baseline_from_round``. A round that was not
   aggregated gives no metrics, so every gate is ``not_evaluated`` and a
   blocking gate rejects the candidate.
5. A round that already holds a release decision, or is ``closed``, is
   refused (409), so a repeated request leaves no audit record.
6. The ``before_release`` Guard runs with the pack's classification and
   purposes, the requester and purpose from the request, the candidate's
   contributed examples as the cohort, and the model kind's
   ``parameter_change`` from ``change_from_version`` (default: the version the
   training round started from) as the payload. Its decision joins the
   coordinator audit chain.
7. The planner evaluates the pack's gates.
8. When the model registry holds the candidate, the planner's lifecycle rule
   (``release_evaluated``) keeps it a candidate or rejects it, and the decision
   is stored with the version, with its ``metrics_source`` and the
   ``baseline_version``: the version the baseline round measured, or, without
   a baseline round, the version released now. A version that is no longer a
   candidate is refused (409, ``not_candidate``): neither the registry nor the
   round stores anything, and only the Guard's audit record stays.
9. The decision is saved on the round, which becomes ``closed``.

``metrics_source`` is ``rounds`` only when the GMA's engine collects every
result from the sites itself (the HTTP engine), so no caller can post the
results the metrics come from. With the in-process simulator, a GMA's results
are posted by callers, so it is ``request``, and the lifecycle rule refuses to
approve the candidate (``metrics_not_from_rounds``).

**Bare flow** (no ``condition_id``). The planner evaluates the gates in the
body against the metrics in the body, as the GMA did before the round flow
existed, and the decision is saved on the round. No Guard runs, and the model
registry is not changed: metrics a caller sends can never lead to an approval.
A round takes one release decision in either flow.
"""

from __future__ import annotations

from agents.common.application.federation import FederatedRoundController
from agents.common.application.governance_service import GovernanceService
from agents.common.domain.errors import ModelVerificationError, RoundConflict
from agents.common.domain.federation import (
    CandidateModel,
    MetricsSource,
    ModelReleaseDecision,
    ModelVersion,
    ReleaseEvaluation,
    ReleaseEvaluationRequest,
    ReleaseGuardCheck,
    RoundRecord,
)
from agents.common.domain.governance import RestrictionRequest
from agents.common.extensions import ExtensionError, ExtensionRegistry
from agents.common.extensions.registry import ConditionPack
from agents.gma.application.model_registry import ModelRegistry
from agents.gma.domain.errors import InvalidReleaseRequest, UnknownConditionPack, UnknownRound


class ReleaseService:
    """Decide on a round's candidate.

    Params:
    - `rounds`: the round controller; it evaluates the gates and saves the decision.
    - `models`: the model registry; it holds the candidate and applies the lifecycle rule.
    - `governance`: the GMA's Guard, at the release boundary.
    - `extensions`: where condition packs are looked up.
    """

    def __init__(
        self,
        rounds: FederatedRoundController,
        models: ModelRegistry,
        governance: GovernanceService,
        extensions: ExtensionRegistry,
    ) -> None:
        self._rounds = rounds
        self._models = models
        self._governance = governance
        self._extensions = extensions

    def evaluate(self, round_id: str, request: ReleaseEvaluationRequest) -> ReleaseEvaluation:
        """Evaluate the release of the candidate ``round_id`` produced.

        Raises ``KeyError`` for an unknown round, and the errors listed in the
        module docstring.
        """
        record = self._rounds.get_round(round_id)
        if record is None:
            raise KeyError(f"unknown round: {round_id}")
        if request.condition_id is None:
            return self._bare(record, request)
        return self._round_flow(record, request)

    # --- Bare flow ----------------------------------------------------------------------

    def _bare(self, record: RoundRecord, request: ReleaseEvaluationRequest) -> ReleaseEvaluation:
        round_id = record.manifest.round_id
        stored = record.candidate
        if stored is not None and stored.candidate_version != request.candidate_version:
            raise RoundConflict(f"{request.candidate_version} is not the candidate round {round_id} produced")
        candidate = CandidateModel(
            round_id=round_id,
            candidate_version=request.candidate_version,
            parent_version=record.manifest.model_version,
            aggregation_method=record.manifest.aggregation.kind,
            metrics=request.candidate_metrics,
        )
        decision = self._rounds.evaluate_release(
            round_id,
            request.gates,
            candidate,
            baseline_metrics=request.baseline_metrics,
            rollback_version=request.rollback_version,
        )
        model = self._models.get(request.candidate_version)
        released = self._models.released(with_params=False)
        return ReleaseEvaluation(
            decision=decision,
            guard=None,
            candidate_status=model.status if model is not None else None,
            metrics_source="request",
            baseline_version=released.version if released is not None else None,
        )

    # --- Round flow ---------------------------------------------------------------------

    def _round_flow(self, record: RoundRecord, request: ReleaseEvaluationRequest) -> ReleaseEvaluation:
        round_id = record.manifest.round_id
        sent = [
            name
            for name, value in (
                ("gates", request.gates),
                ("candidate_metrics", request.candidate_metrics),
                ("baseline_metrics", request.baseline_metrics),
            )
            if value
        ]
        if sent:
            raise InvalidReleaseRequest(
                "a release evaluation for a condition reads its gates from the condition pack and its metrics from "
                f"the rounds; leave out {', '.join(sent)}"
            )
        if request.release_requester is None:
            raise InvalidReleaseRequest(
                "a release evaluation for a condition needs release_requester, for the before_release Guard"
            )
        stored = record.candidate
        if stored is None or stored.candidate_version != request.candidate_version:
            raise RoundConflict(f"{request.candidate_version} is not the candidate round {round_id} produced")
        pack = self._pack(str(request.condition_id))
        if pack.condition_id != record.manifest.condition_id:
            raise RoundConflict(
                f"round {round_id} is for condition {record.manifest.condition_id}, not {pack.condition_id}"
            )

        metrics_round = self._metrics_round(request.metrics_from_round)
        if metrics_round is not None and metrics_round.manifest.model_version != request.candidate_version:
            raise RoundConflict(
                f"round {metrics_round.manifest.round_id} measured {metrics_round.manifest.model_version}, "
                f"not the candidate {request.candidate_version}"
            )
        baseline_round = self._metrics_round(request.baseline_from_round)
        metrics = _aggregate_metrics(metrics_round)
        baseline = _aggregate_metrics(baseline_round)
        if baseline_round is not None:
            baseline_version: str | None = baseline_round.manifest.model_version
        else:
            released = self._models.released(with_params=False)
            baseline_version = released.version if released is not None else None

        # A repeated request is refused here, before the Guard writes an audit record.
        self._rounds.require_undecided(round_id)
        source: MetricsSource = "rounds" if self._rounds.engine_collects_results else "request"
        model = self._models.get(request.candidate_version)
        change = self._parameter_change(model, request.change_from_version or record.manifest.model_version)
        guard = self._release_guard(record, request, pack, stored.contributed_examples, change)

        # The decision is saved on the round only after the model registry
        # accepted it, so a lifecycle refusal (409) leaves the round as it was.
        decision = self._rounds.decide_release(
            round_id,
            pack.release_gates,
            stored.model_copy(update={"metrics": metrics}),
            baseline_metrics=baseline,
            rollback_version=request.rollback_version,
        )
        status = None
        if model is not None:
            updated = self._models.record_release_decision(
                request.candidate_version,
                ModelReleaseDecision(
                    action=decision.action,
                    guard_permitted=guard.permitted,
                    gate_results=decision.gate_results,
                    round_id=round_id,
                    baseline_version=baseline_version,
                    metrics_source=source,
                ),
            )
            status = updated.status
        self._rounds.save_release_decision(round_id, decision)
        return ReleaseEvaluation(
            decision=decision,
            guard=guard,
            candidate_status=status,
            metrics_source=source,
            baseline_version=baseline_version,
        )

    def _pack(self, condition_id: str) -> ConditionPack:
        try:
            return self._extensions.condition_pack(condition_id)
        except ExtensionError as error:
            raise UnknownConditionPack(f"no extension contributes the condition pack {condition_id}") from error

    def _metrics_round(self, round_id: str | None) -> RoundRecord | None:
        if round_id is None:
            return None
        record = self._rounds.get_round(round_id)
        if record is None:
            raise UnknownRound(f"unknown round: {round_id}")
        return record

    def _parameter_change(self, model: ModelVersion | None, reference_version: str) -> float:
        """How far the candidate moved from the reference version, as its model kind measures it.

        ``0.0`` when the registry does not hold the candidate, or when its kind
        cannot be trained. Raises :class:`ModelVerificationError` when the
        reference is not registered, does not verify or is another kind.
        """
        if model is None:
            return 0.0
        candidate = self._models.load(model.version)
        reference = self._models.load(reference_version)
        if reference.kind != candidate.kind:
            raise ModelVerificationError(
                f"the change from {reference_version} cannot be measured: it is a {reference.kind} model and "
                f"{candidate.version} is a {candidate.kind} model"
            )
        kind = self._models.kind(candidate.kind)
        if not getattr(kind, "trainable", False):
            return 0.0
        return float(kind.parameter_change(candidate.params, reference.params))

    def _release_guard(
        self,
        record: RoundRecord,
        request: ReleaseEvaluationRequest,
        pack: ConditionPack,
        cohort: int,
        change: float,
    ) -> ReleaseGuardCheck:
        round_id = record.manifest.round_id
        assert request.release_requester is not None  # checked by the caller
        decision = self._governance.enforce(
            RestrictionRequest(
                request_id=f"{round_id}:release:{request.candidate_version}",
                boundary="before_release",
                requester=request.release_requester,
                classification=self._governance.get_classification(pack.classification_id),
                requested_fields=["model_update"],
                granularity="model_update",
                cohort_size=cohort,
                declared_purpose=request.declared_purpose,
                approved_purposes=list(pack.approved_purposes),
            ),
            [{"model_update": change}],
            correlation_id=round_id,
        )
        return ReleaseGuardCheck(
            request_id=decision.request_id,
            boundary=decision.boundary,
            permitted=decision.permitted,
            plan=decision.plan,
            payload=decision.payload,
            withheld_fields=list(decision.withheld_fields),
            transformed_fields=list(decision.transformed_fields),
            audit_id=decision.audit_id,
            message=decision.message,
        )


def _aggregate_metrics(record: RoundRecord | None) -> dict[str, float]:
    """The metrics a round's stored aggregate holds; none for a round that was not aggregated."""
    if record is None or record.candidate is None:
        return {}
    return dict(record.candidate.metrics)


__all__ = ["ReleaseService"]
