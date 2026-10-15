"""Governance application service shared by the LMA and the GMA.

Both agents need the same three things: somewhere to keep data classifications,
a Guard that enforces decisions against them, and an audit log that can be read
back. Only the boundary differs — an LMA guards reads, training, and egress; a
GMA guards release — and the boundary travels on the request.

Keeping this shared rather than duplicating it per agent is deliberate. Two
copies of an enforcement path drift, and the copy that drifts is the one nobody
is looking at.
"""

from __future__ import annotations

from typing import Any

from agents.common.application.ethical_guard import (
    AuditSink,
    DagentscRestrictionPlanner,
    EthicalGuard,
    InMemoryAuditLog,
    RestrictionPlanner,
)
from agents.common.domain.errors import ClassificationConflict
from agents.common.domain.governance import (
    AuditRecord,
    DataClassification,
    GuardDecision,
    RestrictionPlan,
    RestrictionRequest,
)
from agents.common.extensions import ExtensionError, ExtensionRegistry, default_registry


class GovernanceService:
    """Registers classifications, plans restrictions, and enforces them.

    Classifications registered here are additional to whatever extensions have
    contributed. An operator can add one at runtime; an extension ships them
    with the application. An extension's classification always wins: a runtime
    registration cannot replace it.

    Params:
    - `guard`, `planner`, `audit`: the Guard, or the planner and audit sink to
      build one from.
    - `registry`: where extension classifications are looked up. Defaults to
      ``default_registry``.
    - `strict_classifications`: when true, :meth:`plan` and :meth:`enforce`
      look a request's classification up by its id and refuse the request
      when the classification it carries differs, or when no classification
      has that id. An LMA started for a site and a GMA with condition packs
      run strict, so a caller cannot send its own, weaker policy. Off by
      default, which keeps the behaviour agents had before.
    """

    def __init__(
        self,
        guard: EthicalGuard | None = None,
        planner: RestrictionPlanner | None = None,
        audit: AuditSink | None = None,
        registry: ExtensionRegistry | None = None,
        strict_classifications: bool = False,
    ) -> None:
        if guard is not None:
            # Read the sink off the guard rather than keeping a separate one.
            # Holding our own would leave recent_audit() and
            # audit_chain_intact() reporting on an empty log that nothing ever
            # writes to, which is worse than having no audit view at all.
            self._guard = guard
            self._audit = guard.audit
        else:
            self._audit = audit or InMemoryAuditLog()
            self._guard = EthicalGuard(planner or DagentscRestrictionPlanner(), self._audit)
        self._registry = registry or default_registry
        self._classifications: dict[str, DataClassification] = {}
        self._strict = strict_classifications

    @property
    def strict_classifications(self) -> bool:
        """Whether requests must carry the registered classification for their id."""
        return self._strict

    def _extension_classification(self, classification_id: str) -> DataClassification | None:
        try:
            return self._registry.classification(classification_id)
        except ExtensionError:
            return None

    def register_classification(self, classification: DataClassification) -> DataClassification:
        """Add or replace one runtime data classification.

        Raises :class:`ClassificationConflict` when an extension contributes a
        different classification with the same id. Registering exactly the
        extension's classification changes nothing and returns it, so an
        application that registers its own extension's policy keeps working.
        """
        contributed = self._extension_classification(classification.classification_id)
        if contributed is not None:
            if contributed != classification:
                raise ClassificationConflict(
                    f"classification {classification.classification_id} is contributed by an extension; "
                    "a runtime registration cannot replace it"
                )
            return contributed
        self._classifications[classification.classification_id] = classification
        return classification

    def get_classification(self, classification_id: str) -> DataClassification:
        """Resolve a classification from extensions, then runtime registrations.

        The extension's classification comes first, so one registered after a
        runtime classification with the same id still applies. Raises
        ``ExtensionError`` when neither has the id.
        """
        contributed = self._extension_classification(classification_id)
        if contributed is not None:
            return contributed
        if classification_id in self._classifications:
            return self._classifications[classification_id]
        return self._registry.classification(classification_id)

    def list_classifications(self) -> list[DataClassification]:
        """Every classification this agent can enforce against."""
        merged = dict(self._classifications)
        merged.update({c.classification_id: c for c in self._registry.list_classifications()})
        return [merged[key] for key in sorted(merged)]

    def _checked(self, request: RestrictionRequest) -> RestrictionRequest:
        """Refuse a request whose classification is not the registered one, in strict mode.

        The refusal happens before the planner runs, like a malformed request,
        so it writes no audit record.
        """
        if not self._strict:
            return request
        classification_id = request.classification.classification_id
        try:
            registered = self.get_classification(classification_id)
        except ExtensionError as error:
            raise ClassificationConflict(
                f"classification {classification_id} is not registered with this agent"
            ) from error
        if registered != request.classification:
            raise ClassificationConflict(
                f"the request's classification {classification_id} differs from the one registered with this agent"
            )
        return request

    def plan(self, request: RestrictionRequest) -> RestrictionPlan:
        """Decide what protection a request needs, without enforcing it.

        Useful for showing an operator what would happen before it happens. The
        decision is not recorded, because nothing was enforced. Raises
        :class:`ClassificationConflict` in strict mode (see the class).
        """
        return self._guard.planner.plan(self._checked(request))

    def enforce(
        self,
        request: RestrictionRequest,
        payload: list[dict[str, Any]] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> GuardDecision:
        """Plan, apply, and record one governed request.

        Raises :class:`ClassificationConflict` in strict mode (see the class).
        """
        return self._guard.enforce(self._checked(request), payload, correlation_id=correlation_id)

    def recent_audit(self, limit: int = 50) -> list[AuditRecord]:
        """Recent guard decisions, newest first."""
        return self._audit.list_recent(limit=limit)

    def audit_chain_intact(self) -> bool:
        """Whether the audit log's digest chain verifies."""
        return self._audit.verify_chain()

    def audit_record_count(self) -> int:
        """How many decisions the audit log holds."""
        return self._audit.count()

    @property
    def guard(self) -> EthicalGuard:
        """The underlying guard, for services that enforce inline."""
        return self._guard
