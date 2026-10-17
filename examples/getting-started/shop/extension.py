"""The application's whole integration with the Dagents extension registry."""

from __future__ import annotations

from typing import Any, Callable

from dagents.common.domain.governance import DataClassification
from dagents.common.extensions import (
    ConditionPack,
    ExtensionRegistry,
    FeatureContract,
    ModelKind,
    default_registry,
)

from shop import rule
from shop.contract import CLASSIFICATION, CONDITION, FEATURE_CONTRACT


class ReturnsExtension:
    """Contributes the order-returns study to a Dagents deployment."""

    extension_id = "example-retail-returns"
    version = "1.0.0"

    def feature_contracts(self) -> list[FeatureContract]:
        return [FEATURE_CONTRACT]

    def classifications(self) -> list[DataClassification]:
        return [CLASSIFICATION]

    def condition_packs(self) -> list[ConditionPack]:
        return [CONDITION]

    def pipeline_steps(self) -> dict[str, Callable[..., Any]]:
        return {}

    def model_adapters(self) -> dict[str, Callable[..., Any]]:
        # A round or a model job can select the rule by this name.
        return {rule.RULE_VERSION: rule.flag}

    def model_kinds(self) -> list[ModelKind]:
        # An LMA runs the rule in a round through this kind (step 5).
        return [rule.RuleKind()]


def install(registry: ExtensionRegistry | None = None) -> ReturnsExtension:
    """Register the extension once and return it.

    Registering the same extension id twice is an error in Dagents, so a second
    call returns the extension that is already registered.
    """
    registry = registry or default_registry
    for existing in registry.extensions():
        if existing.extension_id == ReturnsExtension.extension_id:
            return existing  # type: ignore[return-value]
    extension = ReturnsExtension()
    registry.register(extension)
    return extension
