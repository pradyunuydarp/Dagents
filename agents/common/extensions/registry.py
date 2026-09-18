"""The extension registry and the contracts an extension contributes.

An extension contributes four kinds of thing, each one a place where a consumer
legitimately knows something the framework cannot:

- **Feature contracts** — the fields a domain agrees on, their types, units, and
  which are required. The framework validates against a contract; it cannot
  invent one.
- **Data classifications** — which of those fields are sensitive and which
  regulations cover them. This is what the GRAILS Rails read, and it is policy,
  not code.
- **Condition packs** — one studied thing: its cohort, its approved fields, its
  model or rule, its release gates. A platform can be shared across conditions;
  a clinical application cannot.
- **Pipeline steps and model adapters** — named callables the framework's
  generic executors can invoke without knowing what they do.

Registration is explicit. A registry that silently imports whatever it finds is
hard to audit, and these contributions decide what a guard permits.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

from agents.common.domain.base import DagentsModel
from agents.common.domain.federation import ReleaseGate
from agents.common.domain.governance import DataClassification
from pydantic import Field


class ExtensionError(RuntimeError):
    """Raised when an extension is malformed or conflicts with a registered one."""


class FeatureField(DagentsModel):
    """One field in a domain's agreed feature contract.

    ``unit`` and ``terminology`` exist because the same field name can mean
    different things at different sources. A contract that records neither
    cannot detect a site reporting minutes where others report hours.
    """

    name: str
    dtype: str
    required: bool = True
    unit: str | None = None
    terminology: str | None = None
    description: str = ""


class FeatureContract(DagentsModel):
    """The versioned set of fields a domain agrees on.

    The version is what sites compare before joining a federated round: a site
    on a different contract version is not producing comparable data, however
    similar its field names look.
    """

    contract_id: str
    version: str
    fields: list[FeatureField] = Field(default_factory=list)
    description: str = ""

    def required_fields(self) -> list[str]:
        """Names of the fields a record must carry."""
        return [field.name for field in self.fields if field.required]

    def field_names(self) -> list[str]:
        """Every field name in the contract."""
        return [field.name for field in self.fields]

    def as_schema_contract(self) -> dict[str, Any]:
        """Render this contract in the shape the OCaml schema validator expects."""
        return {
            "required_fields": [
                {"name": field.name, "type": field.dtype} for field in self.fields if field.required
            ],
            "optional_fields": [
                {"name": field.name, "type": field.dtype} for field in self.fields if not field.required
            ],
            "allow_extra_fields": True,
        }


class ConditionPack(DagentsModel):
    """One studied condition and everything specific to it.

    A consortium reuses the platform across conditions. It must not reuse the
    clinical application: each condition needs its own cohort definition,
    approved fields, rule or model, thresholds, and release gates, with its own
    evidence. Keeping that in a pack is what makes "one platform, separate
    applications" real rather than aspirational.

    ``intended_use`` and ``limitations`` are required reading, not decoration.
    They are what an operator sees before enabling a pack.
    """

    condition_id: str
    display_name: str
    intended_use: str
    feature_contract_id: str
    classification_id: str
    cohort_description: str = ""
    release_gates: list[ReleaseGate] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    owner: str = ""


@runtime_checkable
class DagentsExtension(Protocol):
    """What a consumer implements to extend the framework.

    Every method is optional in practice: return an empty collection for
    anything the extension does not contribute.
    """

    extension_id: str
    version: str

    def feature_contracts(self) -> list[FeatureContract]:
        """Feature contracts this extension defines."""

    def classifications(self) -> list[DataClassification]:
        """Data classifications the governance Rails should read."""

    def condition_packs(self) -> list[ConditionPack]:
        """Condition packs this extension defines."""

    def pipeline_steps(self) -> dict[str, Callable[..., Any]]:
        """Named pipeline step handlers this extension contributes."""

    def model_adapters(self) -> dict[str, Callable[..., Any]]:
        """Named model adapters this extension contributes."""


class ExtensionRegistry:
    """Holds registered extensions and answers lookups by name.

    Conflicts are errors, not last-write-wins. Two extensions claiming the same
    classification id would mean a guard decision depending on import order,
    which is not a property anyone can audit.
    """

    def __init__(self) -> None:
        self._extensions: dict[str, DagentsExtension] = {}
        self._contracts: dict[str, FeatureContract] = {}
        self._classifications: dict[str, DataClassification] = {}
        self._conditions: dict[str, ConditionPack] = {}
        self._steps: dict[str, Callable[..., Any]] = {}
        self._adapters: dict[str, Callable[..., Any]] = {}

    def register(self, extension: DagentsExtension) -> DagentsExtension:
        """Register one extension and index everything it contributes."""
        extension_id = getattr(extension, "extension_id", None)
        if not extension_id:
            raise ExtensionError("an extension must declare a non-empty extension_id")
        if extension_id in self._extensions:
            raise ExtensionError(f"extension {extension_id} is already registered")

        contracts = list(self._call(extension, "feature_contracts"))
        classifications = list(self._call(extension, "classifications"))
        conditions = list(self._call(extension, "condition_packs"))
        steps = dict(self._call(extension, "pipeline_steps", default={}))
        adapters = dict(self._call(extension, "model_adapters", default={}))

        # Check every contribution before storing any of it, so a conflict
        # cannot leave the registry half-populated.
        self._assert_free(self._contracts, [c.contract_id for c in contracts], "feature contract", extension_id)
        self._assert_free(
            self._classifications,
            [c.classification_id for c in classifications],
            "classification",
            extension_id,
        )
        self._assert_free(self._conditions, [c.condition_id for c in conditions], "condition pack", extension_id)
        self._assert_free(self._steps, list(steps), "pipeline step", extension_id)
        self._assert_free(self._adapters, list(adapters), "model adapter", extension_id)

        known_contracts = {contract.contract_id for contract in contracts} | set(self._contracts)
        known_classifications = {c.classification_id for c in classifications} | set(self._classifications)
        for condition in conditions:
            if condition.feature_contract_id not in known_contracts:
                raise ExtensionError(
                    f"condition pack {condition.condition_id} references unknown feature contract "
                    f"{condition.feature_contract_id}"
                )
            # The classification is what the governance Rails read. A pack
            # pointing at one nobody defines is a dangling policy, and left
            # unchecked it surfaces much later as a guard-time failure on a
            # request that was already in flight.
            if condition.classification_id not in known_classifications:
                raise ExtensionError(
                    f"condition pack {condition.condition_id} references unknown classification "
                    f"{condition.classification_id}"
                )

        self._extensions[extension_id] = extension
        self._contracts.update({contract.contract_id: contract for contract in contracts})
        self._classifications.update(
            {classification.classification_id: classification for classification in classifications}
        )
        self._conditions.update({condition.condition_id: condition for condition in conditions})
        self._steps.update(steps)
        self._adapters.update(adapters)
        return extension

    @staticmethod
    def _call(extension: DagentsExtension, name: str, default: Any = ()) -> Any:
        method = getattr(extension, name, None)
        if method is None:
            return default
        return method()

    @staticmethod
    def _assert_free(existing: dict[str, Any], keys: list[str], label: str, extension_id: str) -> None:
        for key in keys:
            if key in existing:
                raise ExtensionError(
                    f"extension {extension_id} redefines {label} {key}, which another extension already provides"
                )

    def extensions(self) -> list[DagentsExtension]:
        """Every registered extension, in registration order."""
        return list(self._extensions.values())

    def feature_contract(self, contract_id: str) -> FeatureContract:
        """Look up one feature contract, raising if it is not registered."""
        if contract_id not in self._contracts:
            raise ExtensionError(f"unknown feature contract: {contract_id}")
        # A copy, not the stored instance. Pydantic does not re-copy a model
        # nested into another model, so handing out the original would let
        # anything holding a request rewrite registered policy in place.
        return self._contracts[contract_id].model_copy(deep=True)

    def classification(self, classification_id: str) -> DataClassification:
        """Look up one data classification, raising if it is not registered."""
        if classification_id not in self._classifications:
            raise ExtensionError(f"unknown classification: {classification_id}")
        # A copy, not the stored instance. Pydantic does not re-copy a model
        # nested into another model, so handing out the original would let
        # anything holding a request rewrite registered policy in place.
        return self._classifications[classification_id].model_copy(deep=True)

    def condition_pack(self, condition_id: str) -> ConditionPack:
        """Look up one condition pack, raising if it is not registered."""
        if condition_id not in self._conditions:
            raise ExtensionError(f"unknown condition pack: {condition_id}")
        # A copy, not the stored instance. Pydantic does not re-copy a model
        # nested into another model, so handing out the original would let
        # anything holding a request rewrite registered policy in place.
        return self._conditions[condition_id].model_copy(deep=True)

    def pipeline_step(self, kind: str) -> Callable[..., Any] | None:
        """Look up one contributed pipeline step handler."""
        return self._steps.get(kind)

    def model_adapter(self, name: str) -> Callable[..., Any] | None:
        """Look up one contributed model adapter."""
        return self._adapters.get(name)

    def list_feature_contracts(self) -> list[FeatureContract]:
        """Every registered feature contract, sorted by id."""
        return [self._contracts[key].model_copy(deep=True) for key in sorted(self._contracts)]

    def list_classifications(self) -> list[DataClassification]:
        """Every registered data classification, sorted by id."""
        return [self._classifications[key].model_copy(deep=True) for key in sorted(self._classifications)]

    def list_condition_packs(self) -> list[ConditionPack]:
        """Every registered condition pack, sorted by id."""
        return [self._conditions[key].model_copy(deep=True) for key in sorted(self._conditions)]

    def list_pipeline_steps(self) -> list[str]:
        """Names of every contributed pipeline step handler."""
        return sorted(self._steps)

    def list_model_adapters(self) -> list[str]:
        """Names of every contributed model adapter."""
        return sorted(self._adapters)

    def describe(self) -> dict[str, Any]:
        """Summarize the registry, for a service's capability endpoint."""
        return {
            "extensions": [
                {"extension_id": getattr(e, "extension_id", ""), "version": getattr(e, "version", "")}
                for e in self._extensions.values()
            ],
            "feature_contracts": [
                {"contract_id": c.contract_id, "version": c.version} for c in self.list_feature_contracts()
            ],
            "classifications": [c.classification_id for c in self.list_classifications()],
            "condition_packs": [
                {"condition_id": p.condition_id, "display_name": p.display_name}
                for p in self.list_condition_packs()
            ],
            "pipeline_steps": self.list_pipeline_steps(),
            "model_adapters": self.list_model_adapters(),
        }

    def clear(self) -> None:
        """Drop every registration. Intended for tests."""
        self.__init__()  # type: ignore[misc]


#: The registry framework services read by default.
default_registry = ExtensionRegistry()


def register_extension(extension: DagentsExtension) -> DagentsExtension:
    """Register one extension with the default registry."""
    return default_registry.register(extension)


def discover_entry_point_extensions(group: str = "dagents.extensions") -> list[DagentsExtension]:
    """Register extensions published under a setuptools entry-point group.

    This is opt-in: a service calls it when it wants installed packages to
    contribute, and a consumer that prefers explicit wiring simply does not.
    Discovery that happened automatically on import would make a guard's
    behaviour depend on which packages are installed, which is not something an
    operator could reason about.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib on 3.8+
        return []

    registered: list[DagentsExtension] = []
    for entry_point in entry_points(group=group):
        factory = entry_point.load()
        extension = factory() if callable(factory) else factory
        registered.append(register_extension(extension))
    return registered
