"""Step 2: read orders at one store through the Guard, as three different people."""

from dagents.common.domain.governance import KyuAttribute, Requester

from shop.extension import install
from shop.store import load_store
from shop.store_site import StoreSite

install()
north = StoreSite(load_store("store-north"))

REQUESTERS = [
    # Staff whose identity and training the store has checked.
    Requester(
        requester_id="analyst-anna",
        requester_kind="human",
        attributes=[
            KyuAttribute(attribute_id="staff_id", verified=True),
            KyuAttribute(attribute_id="data_training", verified=True),
        ],
        compliance_history=0.9,
    ),
    # Staff with a checked identity but no data training on record.
    Requester(
        requester_id="new-hire-ben",
        requester_kind="human",
        attributes=[
            KyuAttribute(attribute_id="staff_id", verified=True),
            KyuAttribute(attribute_id="data_training", verified=False),
        ],
        compliance_history=0.5,
    ),
    # Someone whose identity nobody has checked.
    Requester(
        requester_id="contractor-cy",
        requester_kind="human",
        attributes=[KyuAttribute(attribute_id="staff_id", verified=False)],
        compliance_history=0.5,
    ),
]
FIELDS = ["order_value", "category", "customer_age_band", "returned"]

for requester in REQUESTERS:
    decision = north.read(requester, FIELDS, purpose="returns_quality_review")
    plan = decision.plan
    print(f"{requester.requester_id}: trust {plan.assessment.trust}, decision {plan.decision}, permitted {decision.permitted}")
    for restriction in plan.field_restrictions:
        print(f"  {restriction.field:<18} {restriction.sensitivity:<7} {restriction.strategy}")
    if decision.permitted:
        print("  first two rows:", decision.payload[:2])
    else:
        print("  reason:", decision.message)
    print()

audit = north.governance
print(f"Audit log at store-north: {audit.audit_record_count()} records, chain intact: {audit.audit_chain_intact()}")
