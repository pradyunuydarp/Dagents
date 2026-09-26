"""scikit-learn provider adapters.

The supervised check families — ``random_forest``, ``naive_bayes``, ``linear``
— were reachable only through the check functions, which build an estimator,
fit it, score it and throw it away in one call. That is the right shape for a
check and the wrong shape for anything else: a caller that wants to fit once and
predict later had nowhere to go.

These adapters put those estimators behind the same provisioning contract as the
PyTorch and Hugging Face ones, and reuse the check module's builders so there is
exactly one place that decides what ``linear`` means for a given task.

`sklearn` is imported inside `load`, keeping `app.ml.inventory` importable
without it.
"""

from __future__ import annotations

from typing import Any

from app.ml.inventory import (
    CLASSIFICATION,
    REGRESSION,
    MissingModelDependencyError,
    ModelCapability,
)


class SklearnEstimatorAdapter:
    """A scikit-learn estimator for one supervised family.

    Params:
    - `capability`: the inventory entry that selected this adapter; its `task`
      decides whether a classifier or a regressor is built.
    - `random_seed`: seed passed to the estimators that accept one, so a fit is
      reproducible.

    What it does:
    - Holds only configuration until `load`, which builds the estimator through
      `app.ml.checks`, the same builders the check endpoints use.
    - Exposes `fit` and `predict`, plus `decision_scores` for the ranking-style
      metrics a bare `predict` cannot support.

    Returns:
    - Consumed through `load`, `fit`, `predict` and `decision_scores`.
    """

    def __init__(self, capability: ModelCapability, *, random_seed: int = 42) -> None:
        if capability.task not in (CLASSIFICATION, REGRESSION):
            raise ValueError(
                f"{capability.family}/{capability.task} is not a supervised scikit-learn task"
            )
        self.capability = capability
        self.random_seed = random_seed
        self._estimator: Any = None
        self._fitted = False

    @property
    def is_loaded(self) -> bool:
        """Whether the estimator has been constructed."""
        return self._estimator is not None

    @property
    def is_fitted(self) -> bool:
        """Whether the estimator has seen training data."""
        return self._fitted

    @property
    def estimator(self) -> Any:
        """The underlying scikit-learn estimator, once loaded."""
        if self._estimator is None:
            raise RuntimeError("load() must be called before the estimator is available")
        return self._estimator

    def load(self) -> None:
        """Construct the estimator for this capability's family and task."""
        if self.is_loaded:
            return
        try:
            from app.ml.checks import _build_classifier, _build_regressor
        except ImportError as exc:  # pragma: no cover - the service pins scikit-learn
            raise MissingModelDependencyError(
                f"Could not import the scikit-learn stack for {self.capability.family}: {exc}",
                family=self.capability.family,
                task=self.capability.task,
            ) from exc
        builder = _build_classifier if self.capability.task == CLASSIFICATION else _build_regressor
        self._estimator = builder(self.capability.family, random_seed=self.random_seed)

    def fit(self, features: Any, targets: Any) -> None:
        """Fit the estimator, loading it first if the caller has not."""
        if not self.is_loaded:
            self.load()
        self._estimator.fit(features, targets)
        self._fitted = True

    def predict(self, features: Any) -> Any:
        """Predict for each row. Requires a prior `fit`."""
        self._require_fitted()
        return self._estimator.predict(features)

    def decision_scores(self, features: Any) -> Any:
        """Return ranking scores for the positive class, or `None` if unavailable.

        Delegates to the check module's extractor so ROC AUC and average
        precision are computed from the same scores here as in the check
        endpoints.
        """
        self._require_fitted()
        from app.ml.checks import _classification_scores

        return _classification_scores(self._estimator, features)

    def _require_fitted(self) -> None:
        """Fail with a clear message rather than scikit-learn's internal one."""
        if not self._fitted:
            raise RuntimeError(
                f"{self.capability.family}/{self.capability.task} must be fit() before use"
            )


def build_classifier_adapter(
    capability: ModelCapability, **options: Any
) -> SklearnEstimatorAdapter:
    """Inventory entrypoint for the supervised classification families."""
    return SklearnEstimatorAdapter(capability, **options)


def build_regressor_adapter(
    capability: ModelCapability, **options: Any
) -> SklearnEstimatorAdapter:
    """Inventory entrypoint for the supervised regression families."""
    return SklearnEstimatorAdapter(capability, **options)
