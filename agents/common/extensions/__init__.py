"""Extension points for consumers building on Dagents.

Dagents is a framework, not a product backend. A consumer such as a healthcare
triage app needs to add things the framework must not itself contain — a
clinical feature contract, a condition-specific rule, a domain data
classification — without forking the framework to do it.

This package is how. A consumer implements :class:`DagentsExtension`, registers
it, and the framework's services discover what it contributes through stable
contracts. The boundary is the point: the framework owns validation, planning,
routing, governance, and workload compilation; the extension owns domain
semantics.

The registry is deliberately small. It holds named contributions of a few kinds
and answers lookups; it does not execute anything, own a lifecycle, or let an
extension reach into framework internals.
"""

from agents.common.extensions.registry import (
    ConditionPack,
    DagentsExtension,
    ExtensionRegistry,
    FeatureContract,
    FeatureField,
    ExtensionError,
    default_registry,
    discover_entry_point_extensions,
    register_extension,
)

__all__ = [
    "ConditionPack",
    "DagentsExtension",
    "ExtensionError",
    "ExtensionRegistry",
    "FeatureContract",
    "FeatureField",
    "default_registry",
    "discover_entry_point_extensions",
    "register_extension",
]
