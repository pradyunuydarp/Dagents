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
from agents.common.domain.governance import (
    AuditRecord,
    DataClassification,
    GuardDecision,
    RestrictionPlan,
    RestrictionRequest,
)
from agents.common.extensions import ExtensionRegistry, default_registry


class GovernanceService:
    """Registers classifications, plans restrictions, and enforces them.

    Classifications registered here are additional to whatever extensions have
    contributed. An operator can add one at runtime; an extension ships them
    with the application.
    """

    def __init__(
        self,
        guard: EthicalGuard | None = None,
        planner: RestrictionPlanner | None = None,
        audit: AuditSink | None = None,
        registry: ExtensionRegistry | None = None,
    ) -> None:
        self._audit = audit or InMemoryAuditLog()
        self._guard = guard or EthicalGuard(planner or DagentscRestrictionPlanner(), self._audit)
        self._registry = registry or default_registry
        self._classifications: dict[str, DataClassification] = {}

    def register_classification(self, classification: DataClassification) -> DataClassification:
        """Add or replace one runtime data classification."""
        self._classifications[classification.classification_id] = classification
        return classification

    def get_classification(self, classification_id: str) -> DataClassification:
        """Resolve a classification from runtime registrations, then extensions."""
        if classification_id in self._classifications:
            return self._classifications[classification_id]
        return self._registry.classification(classification_id)

    def list_classifications(self) -> list[DataClassification]:
        """Every classification this agent can enforce against."""
        merged = {c.classification_id: c for c in self._registry.list_classifications()}
        merged.update(self._classifications)
        return [merged[key] for key in sorted(merged)]

    def plan(self, request: RestrictionRequest) -> RestrictionPlan:
        """Decide what protection a request needs, without enforcing it.

        Useful for showing an operator what would happen before it happens. The
        decision is not recorded, because nothing was enforced.
        """
        return self._guard.planner.plan(request)

    def enforce(
        self,
        request: RestrictionRequest,
        payload: list[dict[str, Any]] | None = None,
        *,
        correlation_id: str | None = None,
    ) -> GuardDecision:
        """Plan, apply, and record one governed request."""
        return self._guard.enforce(request, payload, correlation_id=correlation_id)

    def recent_audit(self, limit: int = 50) -> list[AuditRecord]:
        """Recent guard decisions, newest first."""
        return self._audit.list_recent(limit=limit)

    def audit_chain_intact(self) -> bool:
        """Whether the audit log's digest chain verifies."""
        return self._audit.verify_chain()

    @property
    def guard(self) -> EthicalGuard:
        """The underlying guard, for services that enforce inline."""
        return self._guard
