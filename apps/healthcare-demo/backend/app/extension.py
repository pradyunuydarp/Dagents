"""The Dagents extension this app registers.

This file is the app's whole integration with the framework's extension
registry. The app contributes:

- a feature contract, a data classification and a condition pack, with the
  pack's label field and approved purposes;
- two model kinds, the seed points rule and the logistic regression
  (``app.domain.model_kinds``), which the LMA and the GMA run without
  importing the app;
- a site source, ``healthcare_site_store``, which an LMA started on its own
  reads a hospital's records with;
- named pipeline steps.

In return the framework provides validation, restriction planning, Guard
enforcement, round planning, quorum, job dispatch, the update store, the
model registry, release gates and workload compilation.

Every contribution is declarative. The file imports no framework internals and
patches nothing, and the stroke-specific code stays in ``domain/``.
"""

from __future__ import annotations

from typing import Any, Callable

from agents.common.domain.governance import DataClassification
from agents.common.extensions import ConditionPack, FeatureContract, ModelKind, SiteSourceFactory, register_extension

from app.domain import stroke_rule
from app.domain.conditions import (
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT,
    STROKE_LABEL_FIELD,
)
from app.domain.dataset import UnknownSiteError, any_site_for
from app.domain.model_kinds import MODEL_KINDS


class HealthcareStrokeExtension:
    """Contributes the stroke-triage domain to a Dagents deployment."""

    extension_id = "dagents-healthcare-stroke-demo"
    version = "1.1.0"

    def feature_contracts(self) -> list[FeatureContract]:
        """The fields participating hospitals agree to derive locally."""
        return [STROKE_FEATURE_CONTRACT]

    def classifications(self) -> list[DataClassification]:
        """What the Ethical-Restriction Rails read for stroke data."""
        return [STROKE_CLASSIFICATION]

    def condition_packs(self) -> list[ConditionPack]:
        """The conditions this app implements: suspected stroke only."""
        return [STROKE_CONDITION]

    def model_kinds(self) -> list[ModelKind]:
        """The seed points rule and the logistic regression."""
        return list(MODEL_KINDS)

    def site_sources(self) -> dict[str, SiteSourceFactory]:
        """The hospital's own store, for an LMA that runs in its own process."""
        return {"healthcare_site_store": healthcare_site_store}

    def pipeline_steps(self) -> dict[str, Callable[..., Any]]:
        """Steps the framework's pipeline executor can call by name.

        Each step takes a payload and returns a result for the next step.
        """
        return {
            "score_stroke_worklist": _score_worklist_step,
            "evaluate_stroke_rule": _evaluate_rule_step,
        }


def healthcare_site_store(lease: Any, role: str, site_id: str) -> Any:
    """The ``healthcare_site_store`` site source: one hospital's store, read as the hospital's role.

    An LMA started from settings calls it with its database lease, its
    ``LMA_SITE_ROLE`` and its ``LMA_SITE_ID``. Raises ``ValueError`` when:

    - there is no database: separate processes cannot share an in-memory
      store, so an entry made through the demo API would never reach the LMA;
    - the site is not one of the demo hospitals or a hospital that joins
      while the demo runs;
    - the role is not the hospital's own role.
    """
    try:
        site = any_site_for(site_id)
    except UnknownSiteError:
        raise ValueError(f"{site_id} is not one of the demo hospitals") from None
    if lease is None:
        raise ValueError(
            f"the healthcare_site_store source for {site_id} needs the hospital's database: set LMA_DATABASE_URL"
        )
    if role != site.role:
        raise ValueError(f"the store of {site_id} is read as role {site.role}, not {role!r}")
    # Imported here, so registering the extension does not load the database code.
    from app.services.site_source import site_store_source

    return site_store_source(lease, role, site_id)


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
