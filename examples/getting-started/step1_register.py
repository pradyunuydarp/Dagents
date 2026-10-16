"""Step 1: register the extension, then check each store's orders against the contract."""

from dagents.common.extensions import default_registry
from dagents.common.infrastructure.dagents_runner import validate_dataset_schema

from shop.contract import FEATURE_CONTRACT
from shop.extension import install
from shop.store import SITE_IDS, load_store

install()
registered = default_registry.describe()
print("Extensions:      ", ", ".join(f"{e['extension_id']} {e['version']}" for e in registered["extensions"]))
print("Contracts:       ", ", ".join(f"{c['contract_id']} {c['version']}" for c in registered["feature_contracts"]))
print("Classifications: ", ", ".join(registered["classifications"]))
print("Condition packs: ", ", ".join(p["condition_id"] for p in registered["condition_packs"]))
print("Model adapters:  ", ", ".join(registered["model_adapters"]))
print()

for site_id in SITE_IDS:
    store = load_store(site_id)
    # The planner compares the schema of the orders with the contract.
    report = validate_dataset_schema(store.orders(), FEATURE_CONTRACT.as_schema_contract())
    print(f"{site_id}: {store.count()} orders, matches the contract: {report['valid']}")
