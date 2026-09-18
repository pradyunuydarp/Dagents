"""The Dagents extension this app registers.

This file is the whole integration boundary, and it is deliberately short. The
app hands Dagents four things it cannot know — a feature contract, a data
classification, a condition pack, and a named scoring adapter — and gets back
validation, restriction planning, guard enforcement, round planning, quorum,
release gates, and workload compilation.

Everything the framework needs is declarative. There is no subclassing, no
patching, and no import of a framework internal: the app states what it
contributes and the framework does the rest. That is the test of whether the
extension point is real, and it is why the healthcare specifics live in
``domain/`` rather than anywhere in ``agents/``.
"""

from __future__ import annotations

from typing import Any, Callable

from agents.common.domain.governance import DataClassification
from agents.common.extensions import ConditionPack, FeatureContract, register_extension

from app.domain import stroke_rule
from app.domain.conditions import (
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT,
)


class HealthcareStrokeExtension:
    """Contributes the stroke-triage domain to a Dagents deployment."""

    extension_id = "dagents-healthcare-stroke-demo"
    version = "0.1.0"

    def feature_contracts(self) -> list[FeatureContract]:
        """The fields participating hospitals agree to derive locally."""
        return [STROKE_FEATURE_CONTRACT]

    def classifications(self) -> list[DataClassification]:
        """What the Ethical-Restriction Rails read for stroke data."""
        return [STROKE_CLASSIFICATION]

    def condition_packs(self) -> list[ConditionPack]:
        """The conditions this app implements. One, and only one."""
        return [STROKE_CONDITION]

    def pipeline_steps(self) -> dict[str, Callable[..., Any]]:
        """Steps the framework's generic pipeline executor can call by name.

        The executor does not know what stroke triage is; it knows how to run a
        named step over a payload and pass the result to the next one.
        """
        return {
            "score_stroke_worklist": _score_worklist_step,
            "evaluate_stroke_rule": _evaluate_rule_step,
        }

    def model_adapters(self) -> dict[str, Callable[..., Any]]:
        """Named scorers a round or a model job can select."""
        return {"stroke_triage_rule_v1": stroke_rule.assess}


def _score_worklist_step(payload: dict[str, Any]) -> dict[str, Any]:
    """Pipeline step: rank a worklist by triage priority."""
    records = payload.get("records") or []
    return {"worklist": stroke_rule.rank_worklist(records)}


def _evaluate_rule_step(payload: dict[str, Any]) -> dict[str, Any]:
    """Pipeline step: measure the rule against a labelled local cohort."""
    records = payload.get("records") or []
    label_field = payload.get("label_field", "confirmed_stroke")
    return {"metrics": stroke_rule.evaluate_against_labels(records, label_field)}


def install(registry: Any | None = None) -> HealthcareStrokeExtension:
    """Register this extension, returning it.

    Registration is explicit and idempotent from the caller's point of view: a
    second call with the same registry returns the already-registered instance
    rather than raising, because a demo that starts twice in one process should
    not fall over.
    """
    extension = HealthcareStrokeExtension()
    if registry is None:
        from agents.common.extensions import default_registry

        registry = default_registry
    if any(existing.extension_id == extension.extension_id for existing in registry.extensions()):
        return next(
            existing
            for existing in registry.extensions()
            if existing.extension_id == extension.extension_id
        )
    register = registry.register if registry is not None else register_extension
    register(extension)
    return extension
