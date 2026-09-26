"""Provider-agnostic model inventory for the model service.

The OCaml ``model_router`` decides *which* model family a dataset and task
should get. It is a pure planner, so it happily selects ``transformer`` or
``xgboost`` — families this runtime has never been able to execute. Before this
module, that mismatch surfaced as a ``ValueError`` raised from inside a check
function, with nothing anywhere saying which families were real.

This module is the other half of that contract: **the planner says what should
run, the inventory says what can run, and with what.** A family is either
``implemented`` — with an entrypoint to call — or ``planned``, which records the
gap deliberately instead of letting it be a surprise.

The provider axis is the point. A model family is backed by one of:

- ``pytorch`` — an in-repo ``nn.Module`` in `app.ml.modules` or `app.ml.checks`;
  no weights to fetch, nothing to install beyond the service's requirements.
- ``scikit_learn`` — an estimator built in `app.ml.checks`; same.
- ``huggingface`` — a checkpoint on the Hub, which needs `transformers` and a
  download. Provisioning is therefore a real concern with real failure modes,
  and the adapter contract in this module makes them explicit rather than
  letting a request block on a several-gigabyte fetch.
- ``extension`` — supplied by a consumer app through
  `agents.common.extensions`, which is how ``custom`` is meant to be served.

Nothing in this module imports torch, transformers or scikit-learn. Listing the
families a service supports must not cost a framework import, so the entrypoints
are dotted strings resolved lazily by `load_adapter`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import importlib
from typing import Any, Protocol


# --------------------------------------------------------------------------- #
# Vocabulary shared with the OCaml planner
# --------------------------------------------------------------------------- #

#: Task names as they cross the ``dagentsc`` JSON boundary. These mirror
#: ``Dagents_common_ir.task_type``; the vocabulary test in
#: ``tests/test_model_inventory.py`` reads the OCaml source and fails if the two
#: sides drift, because a family or task name is just a string on the wire and
#: no compiler checks it.
ANOMALY_DETECTION = "anomaly_detection"
CLASSIFICATION = "classification"
FORECASTING = "forecasting"
EMBEDDING = "embedding"
REGRESSION = "regression"

TASK_TYPES = frozenset(
    {ANOMALY_DETECTION, CLASSIFICATION, FORECASTING, EMBEDDING, REGRESSION}
)


class ModelProvider(str, Enum):
    """Where a model family's implementation and weights come from."""

    PYTORCH = "pytorch"
    SCIKIT_LEARN = "scikit_learn"
    HUGGINGFACE = "huggingface"
    EXTENSION = "extension"


class CapabilityStatus(str, Enum):
    """Whether this runtime can execute a family the planner may select."""

    IMPLEMENTED = "implemented"
    PLANNED = "planned"


class UnsupportedModelError(ValueError):
    """Raised when a requested `(family, task)` pair cannot be executed.

    Carries the family and task so a service can turn it into a useful API error
    rather than a bare string.
    """

    def __init__(self, message: str, *, family: str, task: str) -> None:
        super().__init__(message)
        self.family = family
        self.task = task


class MissingModelDependencyError(UnsupportedModelError):
    """Raised when a capability's optional Python requirements are not installed."""


class ModelDownloadNotPermittedError(UnsupportedModelError):
    """Raised when weights are absent and downloading was not permitted.

    The default is to refuse. A training or inference path that silently pulls
    several gigabytes the first time it is called is not something a caller can
    reason about, and a test that does it is a test that needs the network.
    """


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ModelCapability:
    """One `(family, task)` pair, and what it takes to run it.

    Params:
    - `family`: model family name, matching the planner's vocabulary.
    - `task`: one of `TASK_TYPES`.
    - `provider`: where the implementation and any weights come from.
    - `status`: whether this runtime can execute it today.
    - `entrypoint`: `module:attribute` to load, required when implemented.
    - `default_checkpoint`: Hub id or artifact name, for providers that fetch.
    - `extra_requirements`: pip requirements beyond the service's own.
    - `notes`: why this entry exists, or what implementing it would take.
    """

    family: str
    task: str
    provider: ModelProvider
    status: CapabilityStatus
    notes: str
    entrypoint: str | None = None
    default_checkpoint: str | None = None
    extra_requirements: tuple[str, ...] = ()

    @property
    def requires_download(self) -> bool:
        """Whether running this capability means fetching weights first."""
        return self.provider is ModelProvider.HUGGINGFACE and self.default_checkpoint is not None

    def as_dict(self) -> dict[str, Any]:
        """Render the capability as JSON-serializable data for an API response."""
        return {
            "family": self.family,
            "task": self.task,
            "provider": self.provider.value,
            "status": self.status.value,
            "entrypoint": self.entrypoint,
            "default_checkpoint": self.default_checkpoint,
            "extra_requirements": list(self.extra_requirements),
            "requires_download": self.requires_download,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


class ModelInventory:
    """The set of model families this runtime knows about.

    Params:
    - `capabilities`: initial capabilities to register.

    What it does:
    - Keys capabilities by `(family, task)` and rejects duplicates, because two
      entries for one pair would make resolution depend on registration order —
      the same reasoning that makes extension id conflicts an error.
    - Answers "can this run" without importing anything heavy.

    Returns:
    - Consumed through its methods.
    """

    def __init__(self, capabilities: tuple[ModelCapability, ...] = ()) -> None:
        self._capabilities: dict[tuple[str, str], ModelCapability] = {}
        for capability in capabilities:
            self.register(capability)

    def register(self, capability: ModelCapability) -> ModelCapability:
        """Add one capability, rejecting anything ambiguous or incomplete.

        Params:
        - `capability`: the entry to add.

        What it does:
        - Validates the task against the shared vocabulary, requires an
          entrypoint for anything claiming to be implemented, and refuses a
          second entry for an existing `(family, task)`.

        Returns:
        - The registered capability.
        """
        if capability.task not in TASK_TYPES:
            raise ValueError(
                f"Unknown task {capability.task!r} for family {capability.family!r}; "
                f"expected one of {sorted(TASK_TYPES)}"
            )
        if capability.status is CapabilityStatus.IMPLEMENTED and not capability.entrypoint:
            raise ValueError(
                f"{capability.family}/{capability.task} is marked implemented but names no "
                "entrypoint; an implemented capability must say what to call"
            )
        key = (capability.family, capability.task)
        if key in self._capabilities:
            raise ValueError(
                f"{capability.family}/{capability.task} is already registered; two entries for "
                "one family and task would make resolution depend on import order"
            )
        self._capabilities[key] = capability
        return capability

    def capabilities(self) -> tuple[ModelCapability, ...]:
        """Every registered capability, ordered by family then task."""
        return tuple(
            self._capabilities[key] for key in sorted(self._capabilities)
        )

    def families(self) -> tuple[str, ...]:
        """Every family named by any capability, sorted and deduplicated."""
        return tuple(sorted({family for family, _ in self._capabilities}))

    def families_for_task(self, task: str, *, implemented_only: bool = True) -> tuple[str, ...]:
        """List the families available for one task.

        Params:
        - `task`: one of `TASK_TYPES`.
        - `implemented_only`: when true (the default), omit `planned` entries, so
          a caller offering choices to a user only offers ones that work.

        Returns:
        - A sorted tuple of family names.
        """
        return tuple(
            sorted(
                capability.family
                for capability in self._capabilities.values()
                if capability.task == task
                and (not implemented_only or capability.status is CapabilityStatus.IMPLEMENTED)
            )
        )

    def get(self, family: str, task: str) -> ModelCapability | None:
        """Look up one capability, returning `None` when it is not registered."""
        return self._capabilities.get((family, task))

    def resolve(self, family: str, task: str) -> ModelCapability:
        """Return the capability for `(family, task)` or explain why it cannot run.

        Params:
        - `family`: requested model family.
        - `task`: requested ML task.

        What it does:
        - Distinguishes the three ways this can fail, because they need
          different responses: the family is unknown here, the family exists but
          not for this task, or it is a recorded gap someone has yet to
          implement.

        Returns:
        - The `ModelCapability`.

        Raises:
        - `UnsupportedModelError`, whose message names what *is* available.
        """
        capability = self._capabilities.get((family, task))
        if capability is None:
            available = self.families_for_task(task, implemented_only=False)
            if family in self.families():
                other_tasks = sorted(
                    other_task
                    for (other_family, other_task) in self._capabilities
                    if other_family == family
                )
                raise UnsupportedModelError(
                    f"Model family {family!r} is not available for task {task!r} "
                    f"(it serves {other_tasks}). Available for {task!r}: {list(available)}",
                    family=family,
                    task=task,
                )
            raise UnsupportedModelError(
                f"Unknown model family {family!r} for task {task!r}. "
                f"Available: {list(available)}",
                family=family,
                task=task,
            )
        if capability.status is CapabilityStatus.PLANNED:
            raise UnsupportedModelError(
                f"Model family {family!r} for task {task!r} is planned, not implemented: "
                f"{capability.notes} Implementing it means a {capability.provider.value} "
                f"adapter"
                + (
                    f" and installing {list(capability.extra_requirements)}."
                    if capability.extra_requirements
                    else "."
                ),
                family=family,
                task=task,
            )
        return capability

    def gaps(self) -> tuple[ModelCapability, ...]:
        """Every capability the planner may route to that this runtime cannot run.

        Exposed deliberately: a framework that plans more than it can execute
        should be able to say so, rather than discovering it per request.
        """
        return tuple(
            capability
            for capability in self.capabilities()
            if capability.status is CapabilityStatus.PLANNED
        )

    def describe(self) -> dict[str, Any]:
        """Render the whole inventory as JSON-serializable data.

        Used by the `/api/v1/model-families` endpoint so a consumer can ask what
        this deployment supports instead of hardcoding a list.
        """
        return {
            "tasks": sorted(TASK_TYPES),
            "providers": [provider.value for provider in ModelProvider],
            "families": list(self.families()),
            "implemented": {
                task: list(self.families_for_task(task)) for task in sorted(TASK_TYPES)
            },
            "capabilities": [capability.as_dict() for capability in self.capabilities()],
            "gaps": [capability.as_dict() for capability in self.gaps()],
        }


# --------------------------------------------------------------------------- #
# Adapter contracts
# --------------------------------------------------------------------------- #


class ProvisionedModel(Protocol):
    """What every provider's adapter has in common: provisioning.

    Deliberately narrow. An anomaly scorer, a classifier and an embedding model
    do not share a useful `predict`, and pretending they do produces an
    interface that every caller has to work around. What they *do* share is the
    question this abstraction exists to answer: where does this model come from,
    what does it need, and is it here yet.

    Task-specific behaviour belongs on the task-specific protocols below.
    """

    capability: ModelCapability

    @property
    def is_loaded(self) -> bool:
        """Whether the model is resident and ready to be called."""
        ...

    def load(self) -> None:
        """Make the model resident, fetching or importing whatever that takes."""
        ...


class EmbeddingModel(ProvisionedModel, Protocol):
    """A model that turns text into vectors."""

    def embed(self, texts: list[str]) -> Any:
        """Return one vector per input text, shaped `[len(texts), dim]`."""
        ...


class AnomalyScorer(ProvisionedModel, Protocol):
    """A model that scores rows by how anomalous they are."""

    def score(self, rows: Any) -> Any:
        """Return one score per row; higher means more anomalous."""
        ...


class SupervisedEstimator(ProvisionedModel, Protocol):
    """A model fit on labelled rows and then asked to predict."""

    def fit(self, features: Any, targets: Any) -> None:
        """Fit on labelled training rows."""
        ...

    def predict(self, features: Any) -> Any:
        """Predict one value per row."""
        ...


def load_adapter(capability: ModelCapability, /, **options: Any) -> Any:
    """Instantiate the adapter a capability names, importing it only now.

    Params:
    - `capability`: an implemented capability with an entrypoint.
    - `**options`: forwarded to the adapter's constructor.

    What it does:
    - Splits the `module:attribute` entrypoint and imports the module at call
      time, which is what keeps `app.ml.inventory` free of torch and
      transformers imports.

    Returns:
    - Whatever the entrypoint produces — for HF and PyTorch adapters, an
      unloaded `ProvisionedModel`.

    Raises:
    - `UnsupportedModelError` for a planned capability.
    - `MissingModelDependencyError` when the adapter's own imports fail, with
      the pip requirements named.
    """
    if capability.status is not CapabilityStatus.IMPLEMENTED or not capability.entrypoint:
        raise UnsupportedModelError(
            f"{capability.family}/{capability.task} has no entrypoint to load",
            family=capability.family,
            task=capability.task,
        )
    module_name, _, attribute = capability.entrypoint.partition(":")
    if not attribute:
        raise ValueError(
            f"Entrypoint {capability.entrypoint!r} must be 'module:attribute'"
        )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise MissingModelDependencyError(
            f"Could not import {module_name} for {capability.family}/{capability.task}: {exc}. "
            + (
                f"Install {list(capability.extra_requirements)}."
                if capability.extra_requirements
                else ""
            ),
            family=capability.family,
            task=capability.task,
        ) from exc
    factory = getattr(module, attribute)
    return factory(capability, **options)


# --------------------------------------------------------------------------- #
# The default inventory
# --------------------------------------------------------------------------- #

#: The optional requirements file that carries the Hugging Face stack. Kept as a
#: separate file, as in `services/nl2sql-demo/backend`, so the model service
#: still installs and boots on a machine that will never run a Hub model.
HUGGINGFACE_REQUIREMENTS = ("transformers>=4.45,<5", "safetensors>=0.5,<1")


def build_default_inventory() -> ModelInventory:
    """Build the inventory this service ships with.

    Params:
    - None.

    What it does:
    - Registers every family the OCaml router can select, marking the ones this
      runtime executes today and recording the rest as gaps with what they
      would need. The vocabulary test asserts the coverage is complete, so a
      family added to the planner cannot be forgotten here.

    Returns:
    - A fresh `ModelInventory`; callers that need process-wide state should hold
      one instance rather than rebuilding per request.
    """
    inventory = ModelInventory()

    # --- PyTorch: the in-repo anomaly modules and recurrent forecaster -------
    inventory.register(
        ModelCapability(
            family="autoencoder",
            task=ANOMALY_DETECTION,
            provider=ModelProvider.PYTORCH,
            status=CapabilityStatus.IMPLEMENTED,
            entrypoint="app.ml.providers.torch_provider:build_anomaly_adapter",
            notes="Feed-forward reconstruction autoencoder from app.ml.modules.",
        )
    )
    inventory.register(
        ModelCapability(
            family="variational_autoencoder",
            task=ANOMALY_DETECTION,
            provider=ModelProvider.PYTORCH,
            status=CapabilityStatus.IMPLEMENTED,
            entrypoint="app.ml.providers.torch_provider:build_anomaly_adapter",
            notes="Variational autoencoder from app.ml.modules, scored by reconstruction error.",
        )
    )
    for family in ("gru", "lstm"):
        inventory.register(
            ModelCapability(
                family=family,
                task=FORECASTING,
                provider=ModelProvider.PYTORCH,
                status=CapabilityStatus.IMPLEMENTED,
                entrypoint="app.ml.providers.torch_provider:build_forecasting_adapter",
                notes=f"Recurrent regressor from app.ml.checks using an {family.upper()} cell.",
            )
        )

    # --- scikit-learn: the supervised check estimators -----------------------
    for family in ("random_forest", "naive_bayes", "linear"):
        inventory.register(
            ModelCapability(
                family=family,
                task=CLASSIFICATION,
                provider=ModelProvider.SCIKIT_LEARN,
                status=CapabilityStatus.IMPLEMENTED,
                entrypoint="app.ml.providers.sklearn_provider:build_classifier_adapter",
                notes="Supervised classifier used by the classification check.",
            )
        )
    for family in ("random_forest", "linear"):
        inventory.register(
            ModelCapability(
                family=family,
                task=REGRESSION,
                provider=ModelProvider.SCIKIT_LEARN,
                status=CapabilityStatus.IMPLEMENTED,
                entrypoint="app.ml.providers.sklearn_provider:build_regressor_adapter",
                notes="Supervised regressor used by the regression check.",
            )
        )

    # --- Hugging Face: the families no in-repo module covers -----------------
    inventory.register(
        ModelCapability(
            family="transformer",
            task=EMBEDDING,
            provider=ModelProvider.HUGGINGFACE,
            status=CapabilityStatus.IMPLEMENTED,
            entrypoint="app.ml.providers.huggingface_provider:build_embedding_adapter",
            default_checkpoint="sentence-transformers/all-MiniLM-L6-v2",
            extra_requirements=HUGGINGFACE_REQUIREMENTS,
            notes=(
                "Mean-pooled encoder embeddings. The router selects `transformer` for the "
                "embedding task and no in-repo module serves it."
            ),
        )
    )
    inventory.register(
        ModelCapability(
            family="transformer",
            task=FORECASTING,
            provider=ModelProvider.HUGGINGFACE,
            status=CapabilityStatus.PLANNED,
            default_checkpoint="huggingface/time-series-transformer-tourism-monthly",
            extra_requirements=HUGGINGFACE_REQUIREMENTS,
            notes=(
                "The router lists `transformer` as a forecasting candidate; a time-series "
                "transformer would need windowing and scaling the recurrent path does not do."
            ),
        )
    )

    # --- Recorded gaps ------------------------------------------------------
    for task in (CLASSIFICATION, REGRESSION):
        inventory.register(
            ModelCapability(
                family="xgboost",
                task=task,
                provider=ModelProvider.SCIKIT_LEARN,
                status=CapabilityStatus.PLANNED,
                extra_requirements=("xgboost>=2.1,<4",),
                notes=(
                    f"The router lists `xgboost` as a {task} candidate, but the package is "
                    "not a model-service dependency."
                ),
            )
        )
    inventory.register(
        ModelCapability(
            family="random_forest",
            task=ANOMALY_DETECTION,
            provider=ModelProvider.SCIKIT_LEARN,
            status=CapabilityStatus.PLANNED,
            notes=(
                "The router falls back to `random_forest` for anomaly detection when the "
                "dataset profile suggests nothing; serving it means an unsupervised "
                "estimator such as IsolationForest, not the supervised classifier."
            ),
        )
    )

    # --- Consumer-supplied ---------------------------------------------------
    for task in sorted(TASK_TYPES):
        inventory.register(
            ModelCapability(
                family="custom",
                task=task,
                provider=ModelProvider.EXTENSION,
                status=CapabilityStatus.PLANNED,
                notes=(
                    "`custom` is the planner's escape hatch: a consumer app registers a named "
                    "model adapter through agents.common.extensions, so the framework has "
                    "nothing to implement here."
                ),
            )
        )

    return inventory


#: Process-wide inventory. Held as a module attribute, in the same style as the
#: service's other singletons, so route handlers share one instance.
default_inventory = build_default_inventory()
