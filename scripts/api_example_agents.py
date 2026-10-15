"""The configured agents behind some of the API reference's LMA and GMA examples.

`scripts/capture_api_examples.py` captures most LMA and GMA examples from an
agent started with nothing configured. The site, federated job, model version
and release examples need agents that serve a site and run a study, so their
request files name an app from this module (the `apps` of a request file; see
`docs/reference/api/README.md`).

The agents are the getting-started example's (`examples/getting-started`):
its two stores, store north and store south, with their orders, its extension,
its condition pack and its rule. One thing is added for the reference only:
:class:`ReturnScoreKind`, a small logistic regression on the orders. The
example's rule cannot be trained, and the training, update and approval
examples need a model kind that can.

Both apps are built the same way, as the example's step 5 builds its agents:
one GMA with the HTTP engine and one LMA per store, all in one
:class:`AgentNetwork` in process. :func:`store_north_lma` returns store north's
LMA; :func:`study_gma` returns the GMA. Each call builds a new network.

The tokens are fixed, so the captured requests repeat. They are example
values: an agent run for real reads its tokens from its settings. Every
setting here is one a deployment could set (``Settings`` of each agent).
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from fastapi import FastAPI

from agents.common.domain.errors import ModelVerificationError
from agents.common.domain.federation import ModelVersionRegistration
from agents.common.extensions import ExtensionRegistry, TrainingOutcome
from agents.common.infrastructure.agent_client import GMA_AGENT, AgentNetwork, lma_agent
from agents.common.infrastructure.site_sources import StaticSiteSource
from agents.gma.app_factory import GmaDeps
from agents.gma.app_factory import create_app as create_gma
from agents.gma.config import Settings as GmaSettings
from agents.lma.app_factory import LmaDeps
from agents.lma.app_factory import create_app as create_lma
from agents.lma.config import Settings as LmaSettings
from agents.lma.domain.site_config import SiteConfig

# The getting-started example. Its folder name has a hyphen, so the request
# files put `examples/getting-started` on the path and import `shop`.
from shop import rule
from shop.contract import CONDITION
from shop.coordinator import STUDY_ID
from shop.extension import install
from shop.store import SITE_IDS, load_store

#: The study operator's token for the GMA (scope `admin`).
ADMIN_TOKEN = "example-study-admin-token"

#: Each store's staff token for its own LMA (scope `site`).
STORE_TOKENS = {site_id: f"example-{site_id}-staff-token" for site_id in SITE_IDS}

#: The token the GMA sends to each store's LMA (scope `coordinator` at the LMA).
COORDINATOR_TOKENS = {site_id: f"example-{site_id}-coordinator-token" for site_id in SITE_IDS}

#: A site that is enrolled by hand at the GMA and has no LMA in its network.
#: Its job fails, which the update upload examples use.
UNREACHABLE_SITE = "store-east"

#: Each store's token for the GMA's update upload and released model (scope
#: `site`, bound to the store).
UPLOAD_TOKENS = {
    site_id: f"example-{site_id}-upload-token" for site_id in (*SITE_IDS, UNREACHABLE_SITE)
}


def _digest(values: Mapping[str, float]) -> str:
    material = json.dumps(dict(values), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class ReturnScoreKind:
    """A logistic regression that scores how likely an order is to be returned.

    It has the rule's two conditions as features: many items of clothing or
    shoes, and a high value from a customer outside the loyalty scheme. It
    reads `items`, `category`, `order_value` and `loyalty_member` from the
    Guard's payload, where `order_value` arrives rounded to the nearest 10. A
    field the Guard withheld counts as 0. An order is flagged when its score
    is at least 0.4. The kind reports the rule's metrics (precision, recall and
    flag rate), so the condition pack's release gates apply to it.

    Training runs a fixed number of gradient steps from the round's starting
    parameters and returns the change as the update. Each site's
    ``before_send`` Guard limits the update's length before it leaves, so one
    round moves the score a bounded step. That is why the starting score is
    written from the rule rather than all zeros.
    """

    kind = "return_score"
    trainable = True
    version_prefix = "return-score"

    NAMES = ("bias", "w.clothing_or_shoes_items", "w.value_without_loyalty")
    FLAG_AT = 0.4
    LEARNING_RATE = 1.0
    EPOCHS = 60

    def _vector(self, values: Mapping[str, Any], what: str, error: type[Exception]) -> dict[str, float]:
        if not isinstance(values, Mapping) or set(values) != set(self.NAMES):
            raise error(f"{what} must name exactly {', '.join(self.NAMES)}")
        if not all(_finite(values[name]) for name in self.NAMES):
            raise error(f"every value in {what} must be a finite number")
        return {name: float(values[name]) for name in self.NAMES}

    @staticmethod
    def _features(order: Mapping[str, Any]) -> dict[str, float]:
        items, value = order.get("items"), order.get("order_value")
        clothing_or_shoes = order.get("category") in ("clothing", "shoes")
        return {
            "bias": 1.0,
            "w.clothing_or_shoes_items": float(items) / 5.0 if clothing_or_shoes and _finite(items) else 0.0,
            "w.value_without_loyalty": (
                float(value) / 100.0 if order.get("loyalty_member") is False and _finite(value) else 0.0
            ),
        }

    def _score(self, params: Mapping[str, float], order: Mapping[str, Any]) -> float:
        features = self._features(order)
        z = sum(params[name] * features[name] for name in self.NAMES)
        return 1.0 / (1.0 + math.exp(-z))

    @staticmethod
    def _labelled(records: Iterable[Mapping[str, Any]], label_field: str) -> list[Mapping[str, Any]]:
        return [record for record in records if isinstance(record.get(label_field), bool)]

    def parameter_digest(self, params: Mapping[str, Any]) -> str:
        return _digest(self._vector(params, "the parameters", ModelVerificationError))

    def code_digest(self) -> str:
        material = (
            f"{self.kind}:{','.join(self.NAMES)}:flag_at={self.FLAG_AT}:lr={self.LEARNING_RATE}:epochs={self.EPOCHS}"
        )
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def evaluate(self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str) -> dict[str, float]:
        vector = self._vector(params, "the parameters", ModelVerificationError)
        labelled = self._labelled(records, label_field)
        flagged = [record for record in labelled if self._score(vector, record) >= self.FLAG_AT]
        hits = sum(1 for record in flagged if record[label_field])
        returned = sum(1 for record in labelled if record[label_field])
        return {
            "precision": hits / len(flagged) if flagged else 0.0,
            "recall": hits / returned if returned else 0.0,
            "flag_rate": len(flagged) / len(labelled) if labelled else 0.0,
        }

    def train(self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str) -> TrainingOutcome:
        start = self._vector(params, "the parameters", ModelVerificationError)
        labelled = self._labelled(records, label_field)
        if not labelled:
            return TrainingOutcome(update=None, metrics={}, examples=0)
        local = dict(start)
        for _ in range(self.EPOCHS):
            gradient = {name: 0.0 for name in self.NAMES}
            for record in labelled:
                error = self._score(local, record) - (1.0 if record[label_field] else 0.0)
                for name, value in self._features(record).items():
                    gradient[name] += error * value
            local = {name: local[name] - self.LEARNING_RATE * gradient[name] / len(labelled) for name in self.NAMES}
        return TrainingOutcome(
            update={name: local[name] - start[name] for name in self.NAMES},
            metrics=self.evaluate(local, labelled, label_field),
            examples=len(labelled),
        )

    def check_update(self, update: Mapping[str, Any]) -> dict[str, float]:
        return self._vector(update, "the update", ValueError)

    def update_digest(self, update: Mapping[str, Any]) -> str:
        return _digest(self.check_update(update))

    def combine(self, params: Mapping[str, Any], weighted_updates: Iterable[tuple[float, Mapping[str, Any]]]) -> dict[str, float]:
        combined = self._vector(params, "the parameters", ModelVerificationError)
        for weight, update in weighted_updates:
            change = self.check_update(update)
            combined = {name: combined[name] + float(weight) * change[name] for name in self.NAMES}
        return combined

    def parameter_change(self, params: Mapping[str, Any], reference: Mapping[str, Any]) -> float:
        current = self._vector(params, "the parameters", ModelVerificationError)
        base = self._vector(reference, "the reference parameters", ModelVerificationError)
        return math.sqrt(sum((current[name] - base[name]) ** 2 for name in self.NAMES))


#: The starting score, `return-score-v0`, written by hand from the rule. It
#: flags too many orders; one training round fixes that.
START_VERSION = "return-score-v0"
START_PARAMS = {"bias": -1.0, "w.clothing_or_shoes_items": 1.0, "w.value_without_loyalty": 0.5}


class ReturnScoreExtension:
    """Contributes :class:`ReturnScoreKind`, next to the example's own extension."""

    extension_id = "returns-score-reference"
    version = "1.0.0"

    def model_kinds(self) -> list[Any]:
        return [ReturnScoreKind()]


def _registry() -> ExtensionRegistry:
    registry = ExtensionRegistry()
    install(registry)
    registry.register(ReturnScoreExtension())
    return registry


def _study() -> tuple[AgentNetwork, FastAPI, dict[str, FastAPI]]:
    """One GMA and one LMA per store, on one in-process network."""
    registry = _registry()
    network = AgentNetwork.in_process()
    try:
        gma = create_gma(
            GmaSettings(
                engine="http",
                admin_token=ADMIN_TOKEN,
                lma_tokens=COORDINATOR_TOKENS,
                site_tokens=UPLOAD_TOKENS,
                # One site at a time, so the dispatch rows come in the same order every run.
                dispatch_concurrency=1,
                # The coordinator reads no orders.
                allow_source_reads=False,
            ),
            GmaDeps(registry=registry, network=network),
        )
        network.add_app(GMA_AGENT, gma)
        lmas: dict[str, FastAPI] = {}
        for site_id in SITE_IDS:
            store = load_store(site_id)
            lma = create_lma(
                LmaSettings(
                    site_token=STORE_TOKENS[site_id],
                    coordinator_token=COORDINATOR_TOKENS[site_id],
                    gma_token=UPLOAD_TOKENS[site_id],
                ),
                LmaDeps(
                    registry=registry,
                    site=SiteConfig(
                        site_id=site_id,
                        condition_id=CONDITION.condition_id,
                        study_id=STUDY_ID,
                        capabilities=("evaluate", "train"),
                    ),
                    source=StaticSiteSource(store.orders(), location={"store": site_id}),
                    network=network,
                ),
            )
            network.add_app(lma_agent(site_id), lma)
            lmas[site_id] = lma
    except Exception:
        network.close()
        raise
    return network, gma, lmas


def study_gma() -> FastAPI:
    """The GMA of the study, with nothing registered yet. Its examples register the models themselves."""
    _, gma, _ = _study()
    return gma


def store_north_lma() -> FastAPI:
    """Store north's LMA, with the example's rule registered and released at the GMA.

    The LMA reads the released model from the GMA, so the model examples need
    one. It is registered through the GMA's API before the examples run.
    """
    network, _, lmas = _study()
    network.gma(ADMIN_TOKEN).register_model_version(
        rule.RULE_VERSION,
        ModelVersionRegistration(kind=rule.RuleKind.kind, params=rule.RULE_PARAMETERS, requested_status="released"),
    )
    return lmas["store-north"]


__all__ = ["START_PARAMS", "START_VERSION", "ReturnScoreKind", "store_north_lma", "study_gma"]
