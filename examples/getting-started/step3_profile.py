"""Step 3: describe each store's orders locally, then combine what each store may report."""

from agents.common.domain.governance import KyuAttribute, Requester

from shop.extension import install
from shop.store import SITE_IDS, load_store
from shop.store_site import StoreSite

install()
sites = [StoreSite(load_store(site_id)) for site_id in SITE_IDS]

# 1. At each store: the running profile. It holds single orders' values
#    (min and max), so it stays at the store.
for site in sites:
    fields = site.store.profile()["fields"]
    value = fields["order_value"]["numeric"]
    print(
        f"{site.site_id}: {site.store.count()} orders, order_value mean {value['mean']:.2f} "
        f"(min {value['min']}, max {value['max']}), categories {fields['category']['categories']}"
    )
print()

# 2. What each store reports to the coordinator, decided by its own Guard at
#    table granularity: a count and means, and only for 20 orders or more.
coordinator = Requester(
    requester_id="returns-coordinator",
    requester_kind="coordinator",
    attributes=[
        KyuAttribute(attribute_id="signed_study_protocol", verified=True),
        KyuAttribute(attribute_id="service_identity", verified=True),
    ],
    compliance_history=1.0,
)
reports = []
for site in sites:
    decision = site.summary(coordinator, ["order_value", "items", "category"], purpose="order_returns")
    report = {key: round(value, 2) for key, value in decision.payload[0].items()} if decision.payload else {}
    print(f"{site.site_id} reports {report}")
    print("  strategies:", ", ".join(f"{r.field} {r.strategy}" for r in decision.plan.field_restrictions))
    reports.extend(decision.payload)
print()

# 3. The coordinator combines the reports. It never saw an order.
total = sum(report["record_count"] for report in reports)
mean = sum(report["order_value_mean"] * report["record_count"] for report in reports) / total
print(f"Combined: {total} orders, order_value mean {mean:.2f}")
