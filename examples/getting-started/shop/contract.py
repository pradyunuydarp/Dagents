"""What the two stores agree on: the fields, how sensitive each one is, and the study.

This is the application's own knowledge. Dagents cannot know which fields a
retailer collects or which of them identify a customer, so the application
declares them here and registers them through its extension (``extension.py``).
"""

from __future__ import annotations

from agents.common.domain.federation import GateComparison, ReleaseGate
from agents.common.domain.governance import DataClassification
from agents.common.extensions import ConditionPack, FeatureContract, FeatureField

#: Every store taking part produces records in this version of the contract.
#: A store on another version is excluded from a round by the planner.
CONTRACT_VERSION = "retail-orders-v1"

FEATURE_CONTRACT = FeatureContract(
    contract_id="retail-orders",
    version=CONTRACT_VERSION,
    description="Fields each store derives from its own order system.",
    fields=[
        FeatureField(name="order_value", dtype="float", unit="EUR", description="Total paid for the order."),
        FeatureField(name="items", dtype="int", description="Number of items in the order."),
        FeatureField(name="category", dtype="string", description="clothing, shoes, electronics or home."),
        FeatureField(name="customer_age_band", dtype="string", description="18-24, 25-34, 35-44, 45-54 or 55+."),
        FeatureField(name="loyalty_member", dtype="bool", description="Whether the customer is in the loyalty scheme."),
        FeatureField(
            name="returned",
            dtype="bool",
            required=False,
            description="Whether the order was returned. Known only after the return window closes.",
        ),
    ],
)

#: What the Guard reads. Changing a sensitivity here changes what each
#: requester gets, and no code path changes with it.
CLASSIFICATION = DataClassification(
    classification_id="retail-orders",
    field_sensitivity={
        # An exact amount, together with a date and a store, can point to one customer.
        "order_value": "high",
        "customer_age_band": "medium",
        "loyalty_member": "medium",
        "returned": "medium",
        "items": "low",
        "category": "low",
        # A model update is derived from the records, so it is protected like them.
        "model_update": "high",
    },
    # Any field not listed above, such as customer_postcode, is treated as high.
    default_sensitivity="high",
    regulations=["GDPR"],
    # A summary of fewer than 20 orders is not released.
    minimum_cohort=20,
)

#: The purposes a request may declare. The Guard denies any other purpose.
APPROVED_PURPOSES = ["order_returns", "returns_quality_review"]

#: What a candidate rule must reach before anyone may release it.
RELEASE_GATES = [
    ReleaseGate(gate_id="precision", metric="precision", comparison=GateComparison(kind="at_least", value=0.60)),
    ReleaseGate(gate_id="recall", metric="recall", comparison=GateComparison(kind="at_least", value=0.70)),
    # Advisory: a rule that flags almost every order does not help staff.
    ReleaseGate(
        gate_id="flag_rate",
        metric="flag_rate",
        comparison=GateComparison(kind="at_most", value=0.45),
        blocking=False,
    ),
]

CONDITION = ConditionPack(
    condition_id="order_returns",
    display_name="Orders likely to be returned",
    intended_use="Flag orders that are likely to be returned, so staff can check sizes before dispatch.",
    feature_contract_id="retail-orders",
    classification_id="retail-orders",
    # The field a model is measured against. An LMA reads it from the pack.
    label_field="returned",
    approved_purposes=APPROVED_PURPOSES,
    release_gates=RELEASE_GATES,
    limitations=["The orders in data/ are made up for this example."],
    owner="returns-team",
)
