"""The Dagents extension this app registers.

This file is the app's whole integration with the framework's extension
registry. The app contributes a feature contract, a data classification, a
condition pack, named pipeline steps and named model adapters. In return the
framework provides validation, restriction planning, Guard enforcement, round
planning, quorum, release gates and workload compilation.

Every contribution is declarative. The file imports no framework internals and
patches nothing, and the stroke-specific code stays in ``domain/``.
"""

from __future__ import annotations

from typing import Any, Callable

from agents.common.domain.governance import DataClassification
from agents.common.extensions import ConditionPack, FeatureContract, register_extension

from app.domain import stroke_model, stroke_rule
from app.domain.conditions import (
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT,
    STROKE_LABEL_FIELD,
)


class HealthcareStrokeExtension:
    """Contributes the stroke-triage domain to a Dagents deployment."""

    extension_id = "dagents-healthcare-stroke-demo"
    version = "0.3.0"

    def feature_contracts(self) -> list[FeatureContract]:
        """The fields participating hospitals agree to derive locally."""
        return [STROKE_FEATURE_CONTRACT]

    def classifications(self) -> list[DataClassification]:
        """What the Ethical-Restriction Rails read for stroke data."""
        return [STROKE_CLASSIFICATION]

    def condition_packs(self) -> list[ConditionPack]:
        """The conditions this app implements: suspected stroke only."""
        return [STROKE_CONDITION]

    def pipeline_steps(self) -> dict[str, Callable[..., Any]]:
        """Steps the framework's pipeline executor can call by name.

        Each step takes a payload and returns a result for the next step.
        """
        return {
            "score_stroke_worklist": _score_worklist_step,
            "evaluate_stroke_rule": _evaluate_rule_step,
        }

    def model_adapters(self) -> dict[str, Callable[..., Any]]:
        """Named scorers a round or a model job can select.

        ``stroke_outcome_rule_v1`` scores one record. ``stroke_outcome_logreg_v1``
        takes the model's parameters and one record.
        """
        return {
            "stroke_outcome_rule_v1": stroke_rule.assess,
            "stroke_outcome_logreg_v1": stroke_model.score,
        }


def _score_worklist_step(payload: dict[str, Any]) -> dict[str, Any]:
    """Pipeline step: rank a worklist with the seed rule."""
    records = payload.get("records") or []
    return {"worklist": stroke_rule.rank_worklist(records)}


def _evaluate_rule_step(payload: dict[str, Any]) -> dict[str, Any]:
    """Pipeline step: measure the seed rule against a site's labelled records."""
    records = payload.get("records") or []
    label_field = payload.get("label_field", STROKE_LABEL_FIELD)
    return {"metrics": stroke_rule.evaluate_against_labels(records, label_field=label_field)}


def install(registry: Any | None = None) -> HealthcareStrokeExtension:
    """Register this extension and return it.

    Calling it again with the same registry returns the extension already
    registered instead of raising, so an app that starts twice in one process
    keeps working.
    """
    extension = HealthcareStrokeExtension()
    if registry is None:
        from agents.common.extensions import default_registry

        registry = default_registry
    for existing in registry.extensions():
        if existing.extension_id == extension.extension_id:
            return existing
    register = registry.register if registry is not None else register_extension
    register(extension)
    return extension
