"""The Ethical Guard: the enforcing half of GRAILS.

The Ethical-Restriction Rails decide what protection a request needs. They live
in the OCaml planner, are pure, and never see a record. The Guard is what
happens next: it applies the decision to real data, records what it did, and
answers the caller.

Keeping the two apart is the whole point of the split. A policy change is a
change to a classification or a knowledge base, not a change to the code paths
that read patient data.

The Guard is designed to **fail closed**. If the planner cannot be reached, the
request is denied rather than permitted, and the failure is recorded. An
enforcement layer whose absence silently grants access is not an enforcement
layer.
"""

from __future__ import annotations

import hashlib
import random
import time
import uuid
from typing import Any, Protocol

from agents.common.domain.governance import (
    AuditRecord,
    GuardDecision,
    RestrictionPlan,
    RestrictionRequest,
)
from agents.common.infrastructure.dagents_runner import plan_restrictions


class AuditSink(Protocol):
    """Where guard decisions are recorded.

    Every enforcement writes one record, permitted or not. A denial that leaves
    no trace is indistinguishable from a request that never happened.
    """

    def append(self, record: AuditRecord) -> AuditRecord:
        """Persist one decision and return it with its chain digest filled in."""

    def list_recent(self, limit: int = 50) -> list[AuditRecord]:
        """Return the most recent decisions, newest first."""

    def verify_chain(self) -> bool:
        """Recompute the digest chain and report whether it is intact."""


class RestrictionPlanner(Protocol):
    """The Rails, as the Guard sees them."""

    def plan(self, request: RestrictionRequest) -> RestrictionPlan:
        """Return the restriction plan for one request."""


class DagentscRestrictionPlanner:
    """Reaches the Ethical-Restriction Rails through the ``dagentsc`` binary.

    The subprocess boundary is deliberate and matches how every other Dagents
    planner is called: it keeps failure isolation and upgrade independence, and
    avoids an FFI dependency in a service that handles sensitive data.
    """

    def plan(self, request: RestrictionRequest) -> RestrictionPlan:
        payload = plan_restrictions(request.model_dump(mode="json"))
        return RestrictionPlan.model_validate(payload)


class InMemoryAuditLog:
    """Digest-chained audit log for local runs and tests.

    Each record's digest covers the previous record's digest, so editing or
    removing an entry invalidates every entry after it. That makes tampering
    detectable in a demo or a test without pretending to be a production audit
    store: real deployments need append-only storage, retention rules, and
    signing, none of which an in-memory list provides.
    """

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    @staticmethod
    def _digest(record: AuditRecord) -> str:
        material = "|".join(
            [
                record.audit_id,
                str(record.recorded_at),
                record.boundary,
                record.request_id,
                record.requester_id,
                record.decision,
                str(record.permitted),
                record.granularity,
                f"{record.filtering_score:.6f}",
                ",".join(record.withheld_fields),
                ",".join(record.violations),
                record.correlation_id or "",
                record.previous_digest,
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def append(self, record: AuditRecord) -> AuditRecord:
        previous = self._records[-1].digest if self._records else ""
        chained = record.model_copy(update={"previous_digest": previous})
        chained = chained.model_copy(update={"digest": self._digest(chained)})
        self._records.append(chained)
        return chained

    def list_recent(self, limit: int = 50) -> list[AuditRecord]:
        return list(reversed(self._records[-limit:]))

    def verify_chain(self) -> bool:
        previous = ""
        for record in self._records:
            if record.previous_digest != previous:
                return False
            if record.digest != self._digest(record.model_copy(update={"digest": ""})):
                return False
            previous = record.digest
        return True


def _parse_strategy(strategy: str) -> tuple[str, float]:
    """Split a strategy string such as ``"clip_contribution:1"`` into its parts."""
    name, _, parameter = strategy.partition(":")
    if not parameter:
        return name, 0.0
    try:
        return name, float(parameter)
    except ValueError:
        return name, 0.0


class EthicalGuard:
    """Applies a restriction plan to a payload and records what it did.

    The Guard stands at four boundaries: before local data is read, before
    training touches it, before anything leaves the boundary, and before a
    candidate model is released. The boundary travels on the request, so the
    same Guard serves all four and the audit log says which one made each call.
    """

    def __init__(self, planner: RestrictionPlanner, audit: AuditSink) -> None:
        self._planner = planner
        self._audit = audit

    @property
    def audit(self) -> AuditSink:
        """The audit sink, for callers that need to read or verify the log."""
        return self._audit

    @property
    def planner(self) -> RestrictionPlanner:
        """The Rails behind this guard, for callers that want a decision only."""
        return self._planner

    def enforce(
        self,
        request: RestrictionRequest,
        payload: list[dict[str, Any]] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> GuardDecision:
        """Plan, apply, record, and answer.

        Inputs:
        - ``request``: who is asking, for what, at which granularity.
        - ``payload``: the records the request would return. ``None`` asks only
          for a decision, which is how the before-train and before-release
          boundaries use the Guard.
        - ``correlation_id``: a round id or run id, so one decision can be tied
          back to the work that triggered it.

        Output: a :class:`GuardDecision` carrying the surviving payload, what
        was withheld, and the audit id of the record written.
        """
        records = payload or []
        try:
            plan = self._planner.plan(request)
        except Exception as exc:  # noqa: BLE001 - the failure mode matters more than the type
            # Fail closed. A planner that cannot be reached is not permission to
            # proceed, and the denial is recorded like any other decision.
            return self._deny_without_plan(request, exc, correlation_id)

        if plan.decision == "deny":
            decision = GuardDecision(
                request_id=request.request_id,
                boundary=request.boundary,
                permitted=False,
                plan=plan,
                payload=[],
                withheld_fields=list(request.requested_fields),
                transformed_fields=[],
                audit_id="",
                message="; ".join(plan.violations) or "the restriction planner denied this request",
            )
            return self._record(decision, plan, request, correlation_id)

        applied, withheld, transformed = self._apply(request, plan, records)
        decision = GuardDecision(
            request_id=request.request_id,
            boundary=request.boundary,
            permitted=True,
            plan=plan,
            payload=applied,
            withheld_fields=withheld,
            transformed_fields=transformed,
            audit_id="",
            message="permitted without modification" if plan.decision == "permit" else "permitted with restrictions",
        )
        return self._record(decision, plan, request, correlation_id)

    def _deny_without_plan(
        self, request: RestrictionRequest, error: Exception, correlation_id: str | None
    ) -> GuardDecision:
        """Build and record a denial for an unreachable or failing planner."""
        message = f"restriction planner unavailable, denying by default: {error}"
        plan = RestrictionPlan(
            request_id=request.request_id,
            boundary=request.boundary,
            assessment={
                "requester_id": request.requester.requester_id,
                "kyu_score": 0.0,
                "trust": "low",
                "rationale": ["planner unavailable; trust could not be assessed"],
            },
            granularity=request.granularity,
            field_restrictions=[],
            decision="deny",
            filtering_score=1.0,
            obligations=["investigate the planner failure before retrying this request"],
            violations=[message],
        )
        decision = GuardDecision(
            request_id=request.request_id,
            boundary=request.boundary,
            permitted=False,
            plan=plan,
            payload=[],
            withheld_fields=list(request.requested_fields),
            transformed_fields=[],
            audit_id="",
            message=message,
        )
        return self._record(decision, plan, request, correlation_id)

    def _record(
        self,
        decision: GuardDecision,
        plan: RestrictionPlan,
        request: RestrictionRequest,
        correlation_id: str | None,
    ) -> GuardDecision:
        """Write the audit entry and stamp its id onto the decision."""
        record = self._audit.append(
            AuditRecord(
                audit_id=uuid.uuid4().hex,
                recorded_at=int(time.time()),
                boundary=request.boundary,
                request_id=request.request_id,
                requester_id=request.requester.requester_id,
                decision=plan.decision,
                permitted=decision.permitted,
                granularity=request.granularity,
                filtering_score=plan.filtering_score,
                withheld_fields=decision.withheld_fields,
                obligations=plan.obligations,
                violations=plan.violations,
                correlation_id=correlation_id,
            )
        )
        return decision.model_copy(update={"audit_id": record.audit_id})

    def _apply(
        self, request: RestrictionRequest, plan: RestrictionPlan, records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str], list[str]]:
        """Apply per-field strategies to the payload.

        Fields the request never asked for are dropped rather than passed
        through, so a permissive payload cannot widen what was approved.
        """
        strategies = {restriction.field: restriction.strategy for restriction in plan.field_restrictions}
        withheld: list[str] = []
        transformed: list[str] = []
        aggregate_fields: dict[str, int] = {}

        for field, strategy in strategies.items():
            name, parameter = _parse_strategy(strategy)
            if name in {"redact", "refuse"}:
                withheld.append(field)
            elif name == "aggregate_only":
                aggregate_fields[field] = int(parameter)
                transformed.append(field)
            elif name != "allow_full":
                transformed.append(field)

        if aggregate_fields:
            # Aggregate-only changes the shape of the answer, not just its
            # values: the caller gets one summary row instead of the records.
            return self._aggregate(records, aggregate_fields, strategies, withheld), withheld, transformed

        applied: list[dict[str, Any]] = []
        for index, record in enumerate(records):
            projected: dict[str, Any] = {}
            for field in request.requested_fields:
                if field in withheld or field not in record:
                    continue
                name, parameter = _parse_strategy(strategies.get(field, "allow_full"))
                projected[field] = self._transform(
                    record[field], name, parameter, seed=f"{request.request_id}:{field}:{index}"
                )
            applied.append(projected)
        return applied, withheld, transformed

    def _aggregate(
        self,
        records: list[dict[str, Any]],
        aggregate_fields: dict[str, int],
        strategies: dict[str, str],
        withheld: list[str],
    ) -> list[dict[str, Any]]:
        """Reduce records to a single group summary, or to nothing if too small.

        A group smaller than the strategy's minimum is suppressed entirely: an
        aggregate over two subjects describes those two subjects.

        This check is on the actual record count, which is a second line of
        defence behind the planner's cohort gate. The planner sees the cohort
        size the caller *claimed*; the Guard sees how many records there really
        are, and those can differ.

        Every requested field that was not withheld gets an entry here. A field
        that appeared in neither the summary nor ``withheld_fields`` would be
        one the caller cannot account for, which is exactly the ambiguity the
        Guard exists to remove.
        """
        minimum = max(aggregate_fields.values()) if aggregate_fields else 0
        count = len(records)
        if count < minimum:
            withheld.extend(field for field in strategies if field not in withheld)
            return []
        summary: dict[str, Any] = {"record_count": count}
        for field, strategy in strategies.items():
            if field in withheld:
                continue
            name, _ = _parse_strategy(strategy)
            if name in {"redact", "refuse"}:
                continue
            values = [
                float(record[field])
                for record in records
                if isinstance(record.get(field), (int, float)) and not isinstance(record.get(field), bool)
            ]
            if values:
                summary[f"{field}_mean"] = sum(values) / len(values)
                summary[f"{field}_min"] = min(values)
                summary[f"{field}_max"] = max(values)
            else:
                summary[f"{field}_distinct"] = len({str(record.get(field)) for record in records if field in record})
        return [summary]

    @staticmethod
    def _transform(value: Any, strategy: str, parameter: float, *, seed: str) -> Any:
        """Apply one strategy to one value.

        ``add_noise`` draws from a generator seeded by the request and field, so
        a decision is reproducible during an audit. That is a demonstration
        mechanism for showing where noise belongs in the pipeline, not a
        calibrated differential-privacy mechanism: a real deployment needs a
        privacy accountant and a tracked budget, which are separate controls.
        """
        if strategy == "allow_full":
            return value
        if strategy == "generalize":
            precision = max(int(parameter), 0)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return round(float(value), precision)
            text = str(value)
            keep = max(precision, 1)
            return text if len(text) <= keep else text[:keep] + "*"
        if strategy == "clip_contribution":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return value
            bound = abs(parameter)
            return max(-bound, min(bound, float(value)))
        if strategy == "add_noise":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return value
            generator = random.Random(seed)
            return float(value) + generator.gauss(0.0, max(parameter, 1e-9))
        # Anything the Guard does not recognize is withheld, not passed through.
        return None
