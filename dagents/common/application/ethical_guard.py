"""The Ethical Guard: the enforcing half of GRAILS.

The Ethical-Restriction Rails decide what protection a request needs. They live
in the OCaml planner, are pure, and never see a record. The Guard is what
happens next: it applies the decision to real data, records what it did, and
answers the caller.

Because the two are separate, a policy change is a change to a classification
or a knowledge base. The code paths that read records do not change.

The Guard **fails closed**. If the planner cannot be reached, runs past its
limits, answers with something that is not a plan, or sends a plan with a
strategy the Guard cannot apply, or if applying the plan to the records fails,
the Guard denies the request and records the denial.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import threading
import time
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, NamedTuple, Protocol

from pydantic import ValidationError

from dagents.common.domain.governance import (
    AuditRecord,
    GuardDecision,
    RestrictionPlan,
    RestrictionRequest,
)
from dagents.common.infrastructure.dagents_runner import (
    PlannerIdentity,
    PlannerUnavailableError,
    plan_restrictions,
    planner_identity,
)

logger = logging.getLogger(__name__)


class AuditSink(Protocol):
    """Where guard decisions are recorded.

    Every enforcement writes one record, permitted or not. A denial that leaves
    no trace is indistinguishable from a request that never happened.
    """

    def append(self, record: AuditRecord) -> AuditRecord:
        """Persist one decision and return it with its chain digest filled in."""

    def list_recent(self, limit: int = 50) -> list[AuditRecord]:
        """Return the most recent decisions, newest first."""

    def count(self) -> int:
        """The number of stored decisions."""

    def verify_chain(self, full: bool = False) -> bool:
        """Recompute the digest chain and report whether it is intact.

        By default only the records added since the last successful check are
        re-hashed, and the check confirms that no record before them was
        removed or inserted. The first call checks every record, unless the
        sink stored the point its last check reached. ``full=True`` checks
        every record again. The default check never reports a break that a
        full check would not.
        """


class RestrictionPlanner(Protocol):
    """The Rails, as the Guard sees them.

    A planner may also have an ``identity()`` method that returns a
    :class:`~dagents.common.infrastructure.dagents_runner.PlannerIdentity`.
    The Guard asks for it before each plan and writes it into the audit
    record, so the record names the planner that decided, or failed to.
    """

    def plan(self, request: RestrictionRequest) -> RestrictionPlan:
        """Return the restriction plan for one request."""


class DagentscRestrictionPlanner:
    """Reaches the Ethical-Restriction Rails through the ``dagentsc`` binary.

    The subprocess boundary is deliberate and matches how every other Dagents
    planner is called: it keeps failure isolation and upgrade independence, and
    avoids an FFI dependency in a service that handles sensitive data.
    """

    def plan(self, request: RestrictionRequest) -> RestrictionPlan:
        """Ask the planner for the plan.

        Raises :class:`PlannerUnavailableError` when the planner cannot be run
        or its answer is not a restriction plan. The message names the fields
        that are wrong, never a value from the answer, which can repeat the
        request.
        """
        payload = plan_restrictions(request.model_dump(mode="json"))
        try:
            return RestrictionPlan.model_validate(payload)
        except ValidationError as error:
            raise PlannerUnavailableError(_not_a_plan(error)) from None

    def identity(self) -> PlannerIdentity:
        """The planner the next call starts: this package's version and the binary's SHA-256."""
        return planner_identity()


#: How many wrong fields a "not a plan" message names.
_FIELDS_NAMED = 10


def _not_a_plan(error: ValidationError) -> str:
    """Why the planner's answer is not a plan: the number of errors and the fields they are at.

    The locations are the plan's own field names and list positions;
    ``RestrictionPlan`` has no map, so none of them comes from the answer.
    """
    places = sorted({".".join(str(part) for part in item["loc"]) or "the answer" for item in error.errors()})
    named = ", ".join(places[:_FIELDS_NAMED]) + (", ..." if len(places) > _FIELDS_NAMED else "")
    count = error.error_count()
    return f"dagentsc's answer is not a restriction plan: {count} validation error{'' if count == 1 else 's'} at {named}"


#: Audit record fields added after audit chains were first stored. The digest
#: leaves out each one whose value is None (null in JSON), so a record written
#: before the field existed, which has no such key, still hashes to the digest
#: it was stored with. A record that has a value is hashed with it, so removing
#: or changing the value breaks the chain like any other edit. A field added to
#: ``AuditRecord`` later belongs here too, or every stored chain stops verifying.
DIGEST_FIELDS_LEFT_OUT_WHEN_NULL = ("planner_version", "planner_sha256")


def audit_digest(record: AuditRecord) -> str:
    """Hash every field of an audit record except the digest itself.

    Every `AuditSink` builds its chain with this function, so a chain written by
    one sink verifies with another.

    The hash covers the model's own dump, not a hand-written field list. A
    hand-written list once left out ``obligations``, so the Guard's duties could
    be changed without breaking the chain. The fields in
    :data:`DIGEST_FIELDS_LEFT_OUT_WHEN_NULL` are left out when they are None,
    so chains stored before those fields existed still verify.

    The material is JSON with sorted keys. Joining lists on a comma would make
    ``["a,b"]`` and ``["a", "b"]`` hash the same.

    Params:
    - `record`: the record to hash. Its `digest` field is ignored;
      `previous_digest` is included, which links the record to the one before.

    Returns:
    - the SHA-256 hex digest.
    """
    material = record.model_dump(mode="json", exclude={"digest"})
    for name in DIGEST_FIELDS_LEFT_OUT_WHEN_NULL:
        if material.get(name) is None:
            material.pop(name, None)
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class InMemoryAuditLog:
    """Digest-chained audit log for local runs and tests.

    Each record's digest covers the previous record's digest, so editing or
    removing an entry invalidates every entry after it. That makes tampering
    detectable in a demo or a test without pretending to be a production audit
    store: real deployments need append-only storage, retention rules, and
    signing, none of which an in-memory list provides.
    """

    #: The older name of `audit_digest`. dagents/tests/test_governance.py still
    #: calls it; new code calls `audit_digest`.
    _digest = staticmethod(audit_digest)

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []
        # The records the last successful check covered, as the same objects.
        self._verified: list[AuditRecord] = []
        # Agent handlers run in a thread pool. Without the lock, two appends
        # could read the same last digest and fork the chain.
        self._lock = threading.Lock()

    def append(self, record: AuditRecord) -> AuditRecord:
        with self._lock:
            previous = self._records[-1].digest if self._records else ""
            chained = record.model_copy(update={"previous_digest": previous})
            chained = chained.model_copy(update={"digest": audit_digest(chained)})
            self._records.append(chained)
            return chained

    def list_recent(self, limit: int = 50) -> list[AuditRecord]:
        with self._lock:
            return list(reversed(self._records[-limit:]))

    def count(self) -> int:
        with self._lock:
            return len(self._records)

    def verify_chain(self, full: bool = False) -> bool:
        """Recompute the digest chain and report whether it is intact.

        By default only the records added since the last successful check are
        re-hashed. The records checked before must still be the same objects in
        the same places, which costs one identity comparison each, so a record
        replaced, removed or inserted in the list is caught without
        re-hashing. Otherwise every record is checked, as with ``full=True``.
        """
        with self._lock:
            records = list(self._records)
        verified = self._verified
        start = 0
        if (
            not full
            and len(verified) <= len(records)
            and all(stored is checked for stored, checked in zip(records, verified))
        ):
            start = len(verified)
        previous = records[start - 1].digest if start else ""
        for record in records[start:]:
            if record.previous_digest != previous or record.digest != audit_digest(
                record.model_copy(update={"digest": ""})
            ):
                self._verified = []
                return False
            previous = record.digest
        self._verified = records
        return True


#: Each strategy the Guard can apply, and whether it takes a parameter
#: (``"generalize:1"``) or none (``"redact"``). The names are the planner's own,
#: from ``string_of_restriction_strategy`` in ``bindings/ocaml/lib/common_ir``;
#: a test reads that function and compares the two.
STRATEGY_PARAMETERS: Mapping[str, bool] = MappingProxyType(
    {
        "allow_full": False,
        "generalize": True,
        "clip_contribution": True,
        "add_noise": True,
        "aggregate_only": True,
        "redact": False,
        "refuse": False,
    }
)

#: The reason a denial gives when the planner cannot be reached or its answer cannot be read.
PLANNER_UNREACHABLE = "The planner could not be reached, so the Guard denied the request."

#: The reason a denial gives when the plan names a strategy the Guard cannot apply.
PLAN_NOT_APPLICABLE = "The planner's plan names a strategy the Guard cannot apply, so the Guard denied the request."

#: The reason a denial gives when applying a usable plan to the records fails,
#: for example on an integer too large to convert to a float.
PLAN_FAILED_ON_RECORDS = "The Guard could not apply the planner's plan to these records, so the Guard denied the request."

#: The highest ``generalize`` level the Guard applies. A numeric value is
#: rounded to a multiple of 10 to the power of the level, as a float, and 10**308
#: is the largest power of ten a float holds. The planner sends level 1.
MAX_GENERALIZE_LEVEL = 308


def unusable_restrictions(plan: RestrictionPlan) -> list[str]:
    """The ``field: strategy`` pairs of a plan that the Guard cannot apply.

    A strategy is unusable when its name is not in :data:`STRATEGY_PARAMETERS`,
    when it needs a parameter and has none that reads as a finite number, when
    it takes no parameter and has one, or when it is a ``generalize`` level
    above :data:`MAX_GENERALIZE_LEVEL`. A planner newer or older than this
    package can send such a plan; applying the rest of it would enforce only
    part of what was decided. The Guard denies a request whose plan has any,
    and code that acts on a plan without the Guard, such as an LMA deciding
    whether its label rate may leave the site, treats such a plan as no
    answer.
    """
    unusable = []
    for restriction in plan.field_restrictions:
        name, separator, parameter = restriction.strategy.partition(":")
        takes_parameter = STRATEGY_PARAMETERS.get(name)
        if takes_parameter is None:
            usable = False
        elif takes_parameter:
            try:
                value = float(parameter)
            except ValueError:
                value = math.nan
            usable = math.isfinite(value)
            if usable and name == "generalize":
                # Checked before any arithmetic: computing 10 ** level for a
                # level such as 1e18 would never finish.
                usable = int(value) <= MAX_GENERALIZE_LEVEL
        else:
            usable = not separator
        if not usable:
            unusable.append(f"{restriction.field}: {restriction.strategy}")
    return unusable


def _parse_strategy(strategy: str) -> tuple[str, float]:
    """Split a strategy string such as ``"clip_contribution:1"`` into its parts."""
    name, _, parameter = strategy.partition(":")
    if not parameter:
        return name, 0.0
    try:
        return name, float(parameter)
    except ValueError:
        return name, 0.0


#: Strategies that bound a contribution. The planner asks the Guard to record
#: privacy accounting whenever it selects one of them.
CONTRIBUTION_STRATEGIES = frozenset({"clip_contribution", "add_noise"})


def _is_finite_number(value: Any) -> bool:
    """Whether a value is a finite int or float. Booleans are not numbers here."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _coordinates(value: Any) -> list[float] | None:
    """Return a contribution's coordinates, or ``None`` if it is not a contribution.

    A contribution is a finite number, a list of finite numbers, or a dict from
    string keys to finite numbers. Tuples, nested values and booleans are not,
    so the Guard cannot be sure what it is bounding and withholds them.
    """
    if _is_finite_number(value):
        return [float(value)]
    if isinstance(value, list):
        if all(_is_finite_number(item) for item in value):
            return [float(item) for item in value]
        return None
    if isinstance(value, dict):
        if all(isinstance(key, str) and _is_finite_number(item) for key, item in value.items()):
            return [float(item) for item in value.values()]
        return None
    return None


def contribution_norm(value: Any) -> float | None:
    """Return the L2 norm of a contribution, or ``None`` if it is not one.

    A single number's norm is its absolute value. A list or dict is measured
    over its values. A norm too large to represent returns ``None`` too, so the
    caller withholds the value instead of scaling it by zero or by NaN.
    """
    coordinates = _coordinates(value)
    if coordinates is None:
        return None
    norm = math.hypot(*coordinates)
    return norm if math.isfinite(norm) else None


def _scaled(vector: list[Any] | dict[str, Any], factor: float) -> list[float] | dict[str, float]:
    """Multiply every coordinate of a list or dict vector by one factor."""
    if isinstance(vector, dict):
        return {key: float(item) * factor for key, item in vector.items()}
    return [float(item) * factor for item in vector]


def _clip_vector(vector: list[Any] | dict[str, Any], bound: float) -> list[float] | dict[str, float] | None:
    """Scale a vector by min(1, bound / ||vector||2), keeping its direction.

    Floating-point scaling can land one rounding step above the bound. The loop
    lowers the factor by one step at a time until the measured norm is within
    the bound, so a caller that checks ``norm <= bound`` never sees it fail.
    """
    norm = contribution_norm(vector)
    if norm is None:
        return None
    if norm <= bound:
        return _scaled(vector, 1.0)
    factor = bound / norm
    while True:
        clipped = _scaled(vector, factor)
        measured = contribution_norm(clipped)
        if factor == 0.0 or (measured is not None and measured <= bound):
            return clipped
        factor = math.nextafter(factor, 0.0)


def _noise_vector(
    vector: list[Any] | dict[str, Any], scale: float, seed: str
) -> list[float] | dict[str, float] | None:
    """Add seeded Gaussian noise to each coordinate of a vector.

    Each coordinate gets its own generator, seeded by the request seed and the
    coordinate's key (its name in a dict, its position in a list). Adding or
    reordering coordinates therefore does not change another coordinate's
    noise, and an auditor can reproduce every value from the request.
    """
    if contribution_norm(vector) is None:
        return None
    sigma = max(scale, 1e-9)
    if isinstance(vector, dict):
        return {
            key: float(item) + random.Random(f"{seed}:{key}").gauss(0.0, sigma) for key, item in vector.items()
        }
    return [float(item) + random.Random(f"{seed}:{index}").gauss(0.0, sigma) for index, item in enumerate(vector)]


class _Accounting(NamedTuple):
    """Privacy accounting for the contributions one decision bounded."""

    norm_before: float
    norm_after: float
    strategies: list[str]


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

        The audit record names the planner (see :meth:`_planner_identity`),
        including when the planner failed and the decision is a denial.
        """
        records = payload or []
        # Before the plan, so a denial for a planner that failed still names it.
        identity = self._planner_identity()
        try:
            plan = self._planner.plan(request)
        except Exception as exc:  # noqa: BLE001 - the failure mode matters more than the type
            # Fail closed. A planner that cannot be reached is not permission to
            # proceed, and the denial is recorded like any other decision.
            logger.error("restriction planner failed for %s: %s", request.request_id, exc)
            return self._deny_without_plan(request, correlation_id, identity=identity)

        unusable = unusable_restrictions(plan)
        if unusable:
            # Fail closed here too: a plan the Guard can apply only in part is
            # not the plan the planner decided.
            logger.error(
                "the restriction plan for %s names strategies the Guard cannot apply: %s",
                request.request_id,
                "; ".join(unusable),
            )
            return self._deny_without_plan(request, correlation_id, PLAN_NOT_APPLICABLE, identity=identity)

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
            return self._record(decision, plan, request, correlation_id, identity=identity)

        try:
            applied, withheld, transformed, accounting = self._apply(request, plan, records)
        except Exception as exc:  # noqa: BLE001 - any failure to apply the plan is a denial
            # Fail closed: the Guard cannot say what it returned, so it returns
            # nothing. Only the error's type is logged, because its message can
            # quote a record value.
            logger.error("the Guard could not apply the plan for %s: %s", request.request_id, type(exc).__name__)
            return self._deny_without_plan(request, correlation_id, PLAN_FAILED_ON_RECORDS, identity=identity)
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
        return self._record(decision, plan, request, correlation_id, accounting, identity=identity)

    def _planner_identity(self) -> PlannerIdentity | None:
        """Which planner this decision runs, or None for a planner that cannot say.

        A planner without an ``identity()`` method, such as a test double,
        gives None, and its records name no planner. A failure to read the
        identity is logged and gives None too; it never stops the decision.
        """
        identify = getattr(self._planner, "identity", None)
        if not callable(identify):
            return None
        try:
            return identify()
        except Exception as exc:  # noqa: BLE001 - naming the planner must not block a decision
            logger.error("could not read which planner the Guard runs: %s", exc)
            return None

    def _deny_without_plan(
        self,
        request: RestrictionRequest,
        correlation_id: str | None,
        message: str = PLANNER_UNREACHABLE,
        *,
        identity: PlannerIdentity | None = None,
    ) -> GuardDecision:
        """Build and record a denial when the planner fails or its plan cannot be applied.

        The reason shown to callers and written to the audit chain is a plain
        sentence, ``message``. The error behind it can name server paths or
        quote the request, so the caller logs it and it goes nowhere else.
        """
        rationale = (
            "planner unavailable; trust could not be assessed"
            if message == PLANNER_UNREACHABLE
            else "the planner's plan could not be applied, so its assessment was not used"
        )
        plan = RestrictionPlan(
            request_id=request.request_id,
            boundary=request.boundary,
            assessment={
                "requester_id": request.requester.requester_id,
                "kyu_score": 0.0,
                "trust": "low",
                "rationale": [rationale],
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
        return self._record(decision, plan, request, correlation_id, identity=identity)

    def _record(
        self,
        decision: GuardDecision,
        plan: RestrictionPlan,
        request: RestrictionRequest,
        correlation_id: str | None,
        accounting: _Accounting | None = None,
        *,
        identity: PlannerIdentity | None = None,
    ) -> GuardDecision:
        """Write the audit entry and stamp its id onto the decision.

        ``identity`` names the planner in the record. It holds a version and
        a hash, never a path, because the audit chain is shown to callers.
        """
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
                contribution_norm_before=accounting.norm_before if accounting else None,
                contribution_norm_after=accounting.norm_after if accounting else None,
                contribution_strategies=list(accounting.strategies) if accounting else [],
                planner_version=identity.version if identity else None,
                planner_sha256=identity.sha256 if identity else None,
            )
        )
        return decision.model_copy(update={"audit_id": record.audit_id})

    def _apply(
        self, request: RestrictionRequest, plan: RestrictionPlan, records: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str], list[str], _Accounting | None]:
        """Apply per-field strategies to the payload.

        Fields the request never asked for are dropped rather than passed
        through, so a permissive payload cannot widen what was approved.

        Returns the payload, the withheld fields, the transformed fields, and
        the privacy accounting for any contribution that was clipped or noised.
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
            summary = self._aggregate(records, aggregate_fields, strategies, withheld, request)
            if not summary:
                # The group was too small, so nothing describes it. Everything
                # the request asked for is withheld, and nothing was
                # transformed — reporting a field in both lists would say it
                # was protected and returned, which is two different claims.
                return [], sorted(set(request.requested_fields) | set(withheld)), [], None
            return summary, withheld, transformed, None

        surviving = [field for field in request.requested_fields if field not in withheld]
        if not surviving:
            # Every requested field was withheld. Returning one empty dict per
            # record would hand back the exact record count, which is a cohort
            # size the caller was not granted.
            return [], sorted(set(request.requested_fields) | set(withheld)), transformed, None

        applied: list[dict[str, Any]] = []
        unbounded: set[str] = set()
        for index, record in enumerate(records):
            projected: dict[str, Any] = {}
            for field in surviving:
                if field not in record:
                    continue
                name, parameter = _parse_strategy(strategies.get(field, "allow_full"))
                value = self._transform(record[field], name, parameter, seed=f"{request.request_id}:{field}:{index}")
                if value is None and record[field] is not None:
                    # The strategy could not be applied to this value.
                    unbounded.add(field)
                projected[field] = value
            applied.append(projected)

        if unbounded:
            # A field the Guard could not protect in one record is withheld
            # from every record. Returning it from the others would list it
            # as withheld and as returned at the same time.
            withheld = withheld + [field for field in surviving if field in unbounded]
            transformed = [field for field in transformed if field not in unbounded]
            surviving = [field for field in surviving if field not in unbounded]
            if not surviving:
                return [], sorted(set(request.requested_fields) | set(withheld)), transformed, None
            applied = [
                {field: value for field, value in projected.items() if field not in unbounded}
                for projected in applied
            ]

        return applied, withheld, transformed, self._account(records, applied, surviving, strategies)

    @staticmethod
    def _account(
        records: list[dict[str, Any]],
        applied: list[dict[str, Any]],
        fields: list[str],
        strategies: dict[str, str],
    ) -> _Accounting | None:
        """Measure each bounded contribution before and after the Guard changed it.

        The planner adds an obligation to record privacy accounting whenever it
        selects ``clip_contribution`` or ``add_noise``. The audit record keeps
        the largest norm on each side, because a bound applies to every
        contribution and the largest one shows whether it held.
        """
        bounded = [
            field
            for field in fields
            if _parse_strategy(strategies.get(field, "allow_full"))[0] in CONTRIBUTION_STRATEGIES
        ]
        before: list[float] = []
        after: list[float] = []
        applied_strategies: set[str] = set()
        for record, projected in zip(records, applied):
            for field in bounded:
                if field not in projected:
                    continue
                norm_before = contribution_norm(record[field])
                norm_after = contribution_norm(projected[field])
                if norm_before is not None and norm_after is not None:
                    before.append(norm_before)
                    after.append(norm_after)
                    applied_strategies.add(strategies[field])
        if not before:
            return None
        return _Accounting(norm_before=max(before), norm_after=max(after), strategies=sorted(applied_strategies))

    def _aggregate(
        self,
        records: list[dict[str, Any]],
        aggregate_fields: dict[str, int],
        strategies: dict[str, str],
        withheld: list[str],
        request: RestrictionRequest,
    ) -> list[dict[str, Any]]:
        """Reduce records to a single group summary, or to nothing if too small.

        A group smaller than the strategy's minimum is suppressed entirely: an
        aggregate over two subjects describes those two subjects.

        This check is on the actual record count, which is a second line of
        defence behind the planner's cohort gate. The planner sees the cohort
        size the caller *claimed*; the Guard sees how many records there really
        are, and those can differ.

        Per-field strategies are honoured here, not only in the row path. A
        field the planner protected must not have its exact values published
        because the shape changed: ``min`` and ``max`` over a group are two
        specific subjects' values, so they are released only for a field the
        planner left unrestricted. Everything else reports a count and a mean,
        and the mean is put through the field's own strategy.

        Every requested field that was not withheld gets an entry. A field in
        neither the summary nor ``withheld_fields`` would be one the caller
        cannot account for, which is the ambiguity the Guard exists to remove.
        """
        minimum = max(aggregate_fields.values()) if aggregate_fields else 0
        count = len(records)
        if count < minimum:
            return []

        summary: dict[str, Any] = {"record_count": count}
        for field in request.requested_fields:
            if field in withheld:
                continue
            name, parameter = _parse_strategy(strategies.get(field, "allow_full"))
            if name in {"redact", "refuse"}:
                continue
            values = [
                float(record[field])
                for record in records
                if isinstance(record.get(field), (int, float)) and not isinstance(record.get(field), bool)
            ]
            if values:
                mean = sum(values) / len(values)
                # "aggregate_only" is satisfied by the aggregation itself, so the
                # mean is published as computed. Every other strategy still
                # applies to it: a generalized field's mean is still a value
                # derived from that field.
                summary[f"{field}_mean"] = (
                    mean
                    if name == "aggregate_only"
                    else self._transform(mean, name, parameter, seed=f"{request.request_id}:{field}:mean")
                )
                if name == "allow_full":
                    # Only an unrestricted field may publish its extremes,
                    # because an extreme is one subject's actual value.
                    summary[f"{field}_min"] = min(values)
                    summary[f"{field}_max"] = max(values)
            else:
                summary[f"{field}_distinct"] = len(
                    {str(record.get(field)) for record in records if field in record}
                )
        return [summary]

    @staticmethod
    def _transform(value: Any, strategy: str, parameter: float, *, seed: str) -> Any:
        """Apply one strategy to one value.

        ``generalize``'s parameter is a coarsening level (higher is coarser),
        and ``clip_contribution``'s is a norm bound.

        ``clip_contribution`` and ``add_noise`` accept a number, a list of
        numbers, or a dict from names to numbers. A vector is clipped by L2
        norm, keeping its direction, and noised coordinate by coordinate. Any
        other value returns ``None``, so the caller withholds it: a value the
        Guard cannot bound must not leave as if it had been bounded.

        ``add_noise`` draws from generators seeded by the request, the field and
        the coordinate, so a decision is reproducible during an audit. It shows
        where noise belongs in the pipeline. It is not a calibrated
        differential-privacy mechanism: that needs a privacy accountant and a
        tracked budget, which are separate controls.

        A missing value (``None``) stays ``None`` under every strategy. There
        is nothing to protect, and ``generalize`` once turned it into
        ``"Non*"``.
        """
        if value is None:
            return None
        if strategy == "allow_full":
            return value
        if strategy == "generalize":
            # The parameter is a coarsening LEVEL, where higher is coarser. It
            # is not a decimal precision: reading it that way inverts the
            # strategy and makes level 1 a no-op on any value already recorded
            # to one decimal, while reporting the field as protected.
            level = max(int(parameter), 1)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                bucket = 10 ** level
                coarsened = round(float(value) / bucket) * bucket
                return int(coarsened) if isinstance(value, int) else float(coarsened)
            text = str(value)
            keep = max(4 - level, 1)
            return text if len(text) <= keep else text[:keep] + "*"
        if strategy == "clip_contribution":
            if isinstance(value, (list, dict)):
                return _clip_vector(value, abs(parameter))
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            bound = abs(parameter)
            return max(-bound, min(bound, float(value)))
        if strategy == "add_noise":
            if isinstance(value, (list, dict)):
                return _noise_vector(value, parameter, seed)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            generator = random.Random(seed)
            return float(value) + generator.gauss(0.0, max(parameter, 1e-9))
        # "aggregate_only" is not a per-value transform: it is satisfied by
        # reducing records to a group, which the aggregate path does. Reaching
        # here with it means a caller applied it to a single value, which would
        # publish that value, so it is withheld.
        #
        # Anything else the Guard does not recognize is withheld too, rather
        # than passed through: an unknown strategy is not permission.
        return None
