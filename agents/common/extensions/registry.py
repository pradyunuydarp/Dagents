"""The extension registry and the contracts an extension contributes.

An extension contributes these kinds of thing, each one a place where a
consumer knows something the framework cannot:

- **Feature contracts** — the fields a domain agrees on, their types, units, and
  which are required. The framework validates against a contract; it cannot
  invent one.
- **Data classifications** — which of those fields are sensitive and which
  regulations cover them. This is what the GRAILS Rails read, and it is policy,
  not code.
- **Condition packs** — one studied thing: its cohort, its approved fields, its
  label field and purposes, its release gates. A platform can be shared across
  conditions; a clinical application cannot.
- **Model kinds** — typed objects (:class:`ModelKind`) that compute a model's
  digests, evaluate and train it, and check and combine updates. The LMA and
  GMA run an application's model through them without importing it.
- **Site sources** — named factories that build a :class:`SiteRecordSource`,
  so an LMA started from settings can read a site's records.
- **Pipeline steps and model adapters** — named callables the framework's
  generic executors can invoke without knowing what they do.

Registration is explicit. A registry that silently imports whatever it finds is
hard to audit, and these contributions decide what a guard permits.
"""

from __future__ import annotations

import importlib
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from agents.common.application.ports import SiteRecordSource
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
    #: The field that holds the outcome a model learns. It must be a field of
    #: the pack's feature contract; registration fails otherwise.
    label_field: str | None = None
    #: The purposes a request about this condition may declare. The LMA and
    #: GMA pass them to the Guard as the request's approved purposes.
    approved_purposes: list[str] = Field(default_factory=list)


class TrainingOutcome(DagentsModel):
    """What a model kind's ``train`` returns for one site.

    ``update`` is the change in parameters the site would send, by name, or
    ``None`` when there is nothing to send (for example, no labelled records).
    The LMA sends it through the ``before_send`` Guard; it never enters a
    ``SiteResult``.
    """

    update: dict[str, float] | None = Field(
        default=None, description="The parameter update, by name. Null when there is nothing to send."
    )
    metrics: dict[str, float] = Field(default_factory=dict, description="Measurements of the local run, by name.")
    examples: int = Field(default=0, ge=0, description="How many records the run trained on. At least 0.")


class ModelKind(Protocol):
    """One kind of model an extension contributes, such as a rule or a logistic regression.

    Every kind has:

    - ``kind``: its name, unique across the registry.
    - ``trainable``: whether a training round can train it.
    - ``version_prefix``: the start of a candidate's version, which is
      ``<version_prefix>+<round_id>``.
    - ``parameter_digest(params)``: the digest of a model's parameters. It
      raises ``ModelVerificationError`` (``agents.common.domain.errors``) for
      parameters this kind cannot run.
    - ``code_digest()``: the digest a round manifest's
      ``training_code_digest`` must equal.
    - ``evaluate(params, records, label_field)``: metrics the release gates
      read, by name.

    A trainable kind also has:

    - ``train(params, records, label_field)``: local training on the Guard's
      payload. Returns a :class:`TrainingOutcome`.
    - ``check_update(update)``: returns the update as floats. Raises
      ``ValueError`` for unknown names or values that are not finite.
    - ``update_digest(update)``: the digest the site reports and the GMA
      checks.
    - ``combine(params, weighted_updates)``: adds each ``(weight, update)``
      pair to the parameters, with the planner's weights.
    - ``parameter_change(params, reference)``: the size of the change between
      two sets of parameters, which the release Guard reads.

    The registry checks these members when the extension registers, so a
    malformed kind fails at startup rather than during a round.
    """

    kind: str
    trainable: bool
    version_prefix: str

    def parameter_digest(self, params: Mapping[str, Any]) -> str:
        """The digest of these parameters."""

    def code_digest(self) -> str:
        """The digest of the code this kind runs."""

    def evaluate(self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str) -> dict[str, float]:
        """Measure the model on labelled records."""


#: Members every model kind must have, and the members a trainable kind adds.
_KIND_METHODS = ("parameter_digest", "code_digest", "evaluate")
_TRAINABLE_METHODS = ("train", "check_update", "update_digest", "combine", "parameter_change")

#: Builds a site's record source from a database lease (or ``None``), the
#: site's role and the site's id.
SiteSourceFactory = Callable[[Any, str, str], SiteRecordSource]


class RegisteredExtension(DagentsModel):
    """One registered extension, as `ExtensionRegistry.describe` lists it."""

    extension_id: str = Field(description="The extension's id. Two extensions cannot share one.")
    version: str = Field(description="The version the extension declares.")


class RegisteredFeatureContract(DagentsModel):
    """One registered feature contract, as `ExtensionRegistry.describe` lists it."""

    contract_id: str = Field(description="The contract's id.")
    version: str = Field(description="The contract's version. Sites compare it before they join a federated round.")


class RegisteredConditionPack(DagentsModel):
    """One registered condition pack, as `ExtensionRegistry.describe` lists it."""

    condition_id: str = Field(description="The condition pack's id.")
    display_name: str = Field(description="The name shown to operators.")


class ExtensionSummary(DagentsModel):
    """What the registered extensions contribute, as an agent's extensions endpoint returns it.

    `ExtensionRegistry.describe` builds this shape as a plain dict. The model
    documents it in the OpenAPI schema; it is not used to build the response.
    """

    extensions: list[RegisteredExtension] = Field(
        default_factory=list, description="The registered extensions, in the order they were registered."
    )
    feature_contracts: list[RegisteredFeatureContract] = Field(
        default_factory=list, description="Every registered feature contract, sorted by id."
    )
    classifications: list[str] = Field(
        default_factory=list,
        description=(
            "The ids of the data classifications extensions contribute, sorted. Classifications registered "
            "at runtime through the governance endpoints are not listed here."
        ),
    )
    condition_packs: list[RegisteredConditionPack] = Field(
        default_factory=list, description="Every registered condition pack, sorted by id."
    )
    pipeline_steps: list[str] = Field(
        default_factory=list, description="The names of the contributed pipeline steps, sorted."
    )
    model_adapters: list[str] = Field(
        default_factory=list, description="The names of the contributed model adapters, sorted."
    )
    model_kinds: list[str] = Field(
        default_factory=list, description="The names of the contributed model kinds, sorted."
    )
    site_sources: list[str] = Field(
        default_factory=list, description="The names of the contributed site source factories, sorted."
    )


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

    def model_kinds(self) -> list[ModelKind]:
        """Model kinds this extension contributes. Optional: the registry reads it only if present."""

    def site_sources(self) -> dict[str, SiteSourceFactory]:
        """Named site source factories. Optional: the registry reads it only if present."""


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
        self._kinds: dict[str, ModelKind] = {}
        self._site_sources: dict[str, SiteSourceFactory] = {}

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
        kinds = list(self._call(extension, "model_kinds"))
        site_sources = dict(self._call(extension, "site_sources", default={}))

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
        kind_names = [self._checked_kind(kind, extension_id) for kind in kinds]
        self._assert_unique(kind_names, "model kind", extension_id)
        self._assert_free(self._kinds, kind_names, "model kind", extension_id)
        for name, factory in site_sources.items():
            if not isinstance(name, str) or not name or not callable(factory):
                raise ExtensionError(
                    f"extension {extension_id} contributes site source {name!r}, which needs a non-empty name "
                    "and a callable factory"
                )
        self._assert_free(self._site_sources, list(site_sources), "site source", extension_id)

        all_contracts = {**self._contracts, **{contract.contract_id: contract for contract in contracts}}
        known_classifications = {c.classification_id for c in classifications} | set(self._classifications)
        for condition in conditions:
            if condition.feature_contract_id not in all_contracts:
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
            # The label is read from records that follow the contract. A label
            # outside it is a field no site agreed to provide.
            contract = all_contracts[condition.feature_contract_id]
            if condition.label_field is not None and condition.label_field not in contract.field_names():
                raise ExtensionError(
                    f"condition pack {condition.condition_id} names label field {condition.label_field}, "
                    f"which is not a field of feature contract {contract.contract_id}"
                )

        self._extensions[extension_id] = extension
        self._contracts.update({contract.contract_id: contract for contract in contracts})
        self._classifications.update(
            {classification.classification_id: classification for classification in classifications}
        )
        self._conditions.update({condition.condition_id: condition for condition in conditions})
        self._steps.update(steps)
        self._adapters.update(adapters)
        self._kinds.update(zip(kind_names, kinds))
        self._site_sources.update(site_sources)
        return extension

    @staticmethod
    def _checked_kind(kind: Any, extension_id: str) -> str:
        """Check that a model kind has every member the agents call, and return its name."""
        name = getattr(kind, "kind", None)
        if not isinstance(name, str) or not name:
            raise ExtensionError(f"extension {extension_id} contributes a model kind without a non-empty kind name")
        trainable = getattr(kind, "trainable", None)
        if not isinstance(trainable, bool):
            raise ExtensionError(f"model kind {name} from extension {extension_id} must set trainable to true or false")
        prefix = getattr(kind, "version_prefix", None)
        if not isinstance(prefix, str) or not prefix:
            raise ExtensionError(f"model kind {name} from extension {extension_id} needs a non-empty version_prefix")
        required = _KIND_METHODS + (_TRAINABLE_METHODS if trainable else ())
        missing = [member for member in required if not callable(getattr(kind, member, None))]
        if missing:
            raise ExtensionError(
                f"model kind {name} from extension {extension_id} lacks {', '.join(missing)}"
            )
        return name

    @staticmethod
    def _call(extension: DagentsExtension, name: str, default: Any = ()) -> Any:
        method = getattr(extension, name, None)
        if method is None:
            return default
        result = method()
        # A class that inherits the protocol's empty method gets None back.
        return default if result is None else result

    @staticmethod
    def _assert_free(existing: dict[str, Any], keys: list[str], label: str, extension_id: str) -> None:
        for key in keys:
            if key in existing:
                raise ExtensionError(
                    f"extension {extension_id} redefines {label} {key}, which another extension already provides"
                )

    @staticmethod
    def _assert_unique(keys: list[str], label: str, extension_id: str) -> None:
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                raise ExtensionError(f"extension {extension_id} contributes {label} {key} twice")
            seen.add(key)

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

    def model_kind(self, kind: str) -> ModelKind:
        """Look up one model kind, raising if it is not registered.

        The registered object is returned, not a copy: a model kind holds code,
        not policy.
        """
        if kind not in self._kinds:
            raise ExtensionError(f"unknown model kind: {kind}")
        return self._kinds[kind]

    def site_source(self, name: str) -> SiteSourceFactory:
        """Look up one site source factory, raising if it is not registered."""
        if name not in self._site_sources:
            raise ExtensionError(f"unknown site source: {name}")
        return self._site_sources[name]

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

    def list_model_kinds(self) -> list[str]:
        """Names of every contributed model kind, sorted."""
        return sorted(self._kinds)

    def list_site_sources(self) -> list[str]:
        """Names of every contributed site source factory, sorted."""
        return sorted(self._site_sources)

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
            "model_kinds": self.list_model_kinds(),
            "site_sources": self.list_site_sources(),
        }

    def clear(self) -> None:
        """Drop every registration. Intended for tests."""
        self.__init__()  # type: ignore[misc]


#: The registry framework services read by default.
default_registry = ExtensionRegistry()


def register_extension(extension: DagentsExtension) -> DagentsExtension:
    """Register one extension with the default registry."""
    return default_registry.register(extension)


def _resolve_extension(spec: str) -> DagentsExtension:
    """Import ``module:attribute`` and return the extension it names.

    A class, or a factory function, is called with no arguments. Any other
    object is used as it is.
    """
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ExtensionError(f"extension spec {spec!r} must have the form module:attribute")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise ExtensionError(f"extension spec {spec!r}: cannot import module {module_name}: {error}") from error
    try:
        target = getattr(module, attribute)
    except AttributeError as error:
        raise ExtensionError(f"extension spec {spec!r}: module {module_name} has no attribute {attribute}") from error
    if isinstance(target, type) or (callable(target) and not hasattr(target, "extension_id")):
        target = target()
    return target


def load_extensions(specs: str, registry: ExtensionRegistry | None = None) -> list[DagentsExtension]:
    """Register the extensions named in a setting such as ``LMA_EXTENSIONS``.

    Params:
    - `specs`: comma-separated ``module:attribute`` names, for example
      ``app.extension:MyExtension``. Spaces around a name and
      empty entries are ignored, so an empty setting registers nothing.
    - `registry`: where to register them. Defaults to ``default_registry``.

    Every name is imported before any extension is registered, so a name that
    cannot be loaded registers none of them. A registration conflict raises
    like :meth:`ExtensionRegistry.register`; extensions registered before the
    conflicting one stay registered.

    Returns the registered extensions, in the order named. Raises
    :class:`ExtensionError` for a malformed name, a module that cannot be
    imported, a missing attribute or a conflict.
    """
    target = registry if registry is not None else default_registry
    names = [part.strip() for part in specs.split(",") if part.strip()]
    extensions = [_resolve_extension(name) for name in names]
    return [target.register(extension) for extension in extensions]


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
