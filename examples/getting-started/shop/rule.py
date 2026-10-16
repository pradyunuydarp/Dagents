"""The rule the stores evaluate, and how it is measured. Application code."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from agents.common.domain.errors import ModelVerificationError

#: The rule the stores evaluate in this example.
RULE_VERSION = "return-check-rule-v2"

#: The rule the stores use today. If the new rule is not released, they keep it.
IN_USE_VERSION = "return-check-rule-v1"

#: The rule's settings. Its digest goes into the round manifest, so a store can
#: check that the round runs the rule it has.
RULE_PARAMETERS: dict[str, Any] = {
    "risky_categories": ["clothing", "shoes"],
    "min_items": 3,
    "high_value": 150,
}

RULE_DIGEST = "sha256:" + hashlib.sha256(
    json.dumps(RULE_PARAMETERS, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def flag(order: Mapping[str, Any]) -> bool:
    """Whether staff should check this order before it is sent."""
    many_items = order.get("category") in RULE_PARAMETERS["risky_categories"] and (
        order.get("items", 0) >= RULE_PARAMETERS["min_items"]
    )
    expensive = order.get("order_value", 0) >= RULE_PARAMETERS["high_value"] and not order.get("loyalty_member")
    return bool(many_items or expensive)


def evaluate(orders: list[Mapping[str, Any]]) -> dict[str, float]:
    """Precision, recall and flag rate of the rule on orders with a known outcome."""
    labelled = [order for order in orders if isinstance(order.get("returned"), bool)]
    flagged = [order for order in labelled if flag(order)]
    hits = sum(1 for order in flagged if order["returned"])
    returned = sum(1 for order in labelled if order["returned"])
    return {
        "precision": hits / len(flagged) if flagged else 0.0,
        "recall": hits / returned if returned else 0.0,
        "flag_rate": len(flagged) / len(labelled) if labelled else 0.0,
    }


class RuleKind:
    """The rule as a model kind, so that a store's LMA can run it in a round.

    The extension contributes this kind. Before an LMA reads any order for a
    job, it checks that the job's parameters have the digest the manifest
    names, and that the manifest's code digest is this kind's. The rule
    cannot be trained, so the LMA refuses a training job that names it.
    """

    kind = "return_check_rule"
    trainable = False
    version_prefix = "return-check-rule"

    def parameter_digest(self, params: Mapping[str, Any]) -> str:
        try:
            material = json.dumps(dict(params), sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ModelVerificationError("the rule's parameters must be JSON values") from error
        digest = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
        # This store runs one rule: flag() reads RULE_PARAMETERS, not the job's.
        if digest != RULE_DIGEST:
            raise ModelVerificationError(f"this store runs only {RULE_VERSION}")
        return digest

    def code_digest(self) -> str:
        # The rule has no training code, so the manifest names the rule's own
        # digest as its code digest, as the coordinator in step 4 does.
        return RULE_DIGEST

    def evaluate(self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str) -> dict[str, float]:
        self.parameter_digest(params)
        if label_field != "returned":
            raise ModelVerificationError("the rule is measured against the returned field")
        return evaluate(records)
