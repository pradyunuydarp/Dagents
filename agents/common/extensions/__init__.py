"""Extension points for consumers building on Dagents.

Dagents is a framework that applications build on. A consumer such as a
healthcare triage app needs to add things the framework must not itself
contain — a clinical feature contract, a condition-specific rule or model, a
domain data classification — without forking the framework to do it.

This package is how. A consumer implements :class:`DagentsExtension`, registers
it, and the framework's services discover what it contributes through stable
contracts. The framework owns validation, planning, routing, governance,
federation and workload compilation; the extension owns domain semantics.

The registry is deliberately small. It holds named contributions of a few kinds
and answers lookups; it does not execute anything, own a lifecycle, or let an
extension reach into framework internals.
"""

from agents.common.extensions.registry import (
    ConditionPack,
    DagentsExtension,
    ExtensionError,
    ExtensionRegistry,
    ExtensionSummary,
    FeatureContract,
    FeatureField,
    ModelKind,
    SiteSourceFactory,
    TrainingOutcome,
    default_registry,
    discover_entry_point_extensions,
    load_extensions,
    register_extension,
)

__all__ = [
    "ConditionPack",
    "DagentsExtension",
    "ExtensionError",
    "ExtensionRegistry",
    "ExtensionSummary",
    "FeatureContract",
    "FeatureField",
    "ModelKind",
    "SiteSourceFactory",
    "TrainingOutcome",
    "default_registry",
    "discover_entry_point_extensions",
    "load_extensions",
    "register_extension",
]
