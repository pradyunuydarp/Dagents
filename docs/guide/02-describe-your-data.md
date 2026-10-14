# Step 2: Describe your data

Dagents cannot know which fields your application holds or which of them
identify a person. You declare that once, in three objects, and register them as
an extension:

| Object | What it says | Who reads it |
|---|---|---|
| Feature contract | The fields every source agrees to produce, their types and units, and a version | The planner, when it validates records and selects sources for a round |
| Data classification | How sensitive each field is, which regulations apply, and the smallest group that may be described | The planner, when it decides what a request may see |
| Condition pack | One study: its purpose, its contract, its classification and its release gates | Your coordinator, when it decides on a release |

An **extension** is a class that hands these objects to Dagents. Your
application never changes framework code. It registers an extension instead.

## 1. Write the feature contract

<!-- source: examples/getting-started/shop/contract.py -->
```python
from __future__ import annotations

from agents.common.domain.federation import GateComparison, ReleaseGate
from agents.common.domain.governance import DataClassification
from agents.common.extensions import ConditionPack, FeatureContract, FeatureField
```

<!-- source: examples/getting-started/shop/contract.py -->
```python
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
```

- `version` is what sources compare before a round. A store that produces
  `retail-orders-v0` is excluded from a round that needs `retail-orders-v1`
  (you will see this in [Step 6](06-federated-rounds.md#what-the-gma-does-over-http)).
- `unit` records what a number means. Two stores that both send `order_value`
  but in different currencies do not produce comparable data.
- `required=False` marks a field that a record may lack. Here `returned` is
  known only after the return window closes.

Fields that are not in the contract, such as an order id or a postcode, can
stay in the store. The Guard drops every field a request did not ask for.

## 2. Classify each field

<!-- source: examples/getting-started/shop/contract.py -->
```python
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
```

- **Sensitivity** is `low`, `medium` or `high`. The planner combines it with
  the requester's trust and the request's granularity (a cell, a row, a column,
  a table or a model update) to pick a protection for each field. The full rule
  table is in [Privacy and data governance](../learn/02-privacy-and-governance.md).
- **`default_sensitivity="high"`** protects any field you forgot to classify.
  Keep it.
- **`regulations`** become obligations in every decision, for example
  "apply controls required by GDPR". The Guard records them in the audit log.
- **`minimum_cohort`** is the smallest number of records a summary may
  describe. The planner denies a table request about 19 orders.
- **`model_update`** is the name the framework uses for a federated update. A
  round sends it through the Guard like any other field, so classify it.

## 3. Declare the study

<!-- source: examples/getting-started/shop/contract.py -->
```python
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
    release_gates=RELEASE_GATES,
    limitations=["The orders in data/ are made up for this example."],
    owner="returns-team",
)
```

- **Approved purposes.** Every request declares a purpose, and carries the list
  of approved purposes in `approved_purposes`. The planner denies a request
  whose purpose is not in that list, whoever asks. Neither the condition pack
  nor the classification holds this list: your code keeps it in its own
  configuration (here, `APPROVED_PURPOSES`) and puts it on every
  `RestrictionRequest`. An empty list turns the purpose check off, so a request
  sent without the list is not checked for its purpose.
- **Release gates.** Each gate names a metric and a threshold. A gate is
  blocking unless you set `blocking=False`. A gate whose metric is missing
  blocks the release. It never passes by default.
- **`intended_use` and `limitations`** are what an operator reads before using
  the study. Write them for that reader.

## 4. Write the extension

<!-- source: examples/getting-started/shop/extension.py -->
```python
"""The application's whole integration with the Dagents extension registry."""

from __future__ import annotations

from typing import Any, Callable

from agents.common.domain.governance import DataClassification
from agents.common.extensions import (
    ConditionPack,
    ExtensionRegistry,
    FeatureContract,
    default_registry,
)

from shop import rule
from shop.contract import CLASSIFICATION, CONDITION, FEATURE_CONTRACT


class ReturnsExtension:
    """Contributes the order-returns study to a Dagents deployment."""

    extension_id = "example-retail-returns"
    version = "1.0.0"

    def feature_contracts(self) -> list[FeatureContract]:
        return [FEATURE_CONTRACT]

    def classifications(self) -> list[DataClassification]:
        return [CLASSIFICATION]

    def condition_packs(self) -> list[ConditionPack]:
        return [CONDITION]

    def pipeline_steps(self) -> dict[str, Callable[..., Any]]:
        return {}

    def model_adapters(self) -> dict[str, Callable[..., Any]]:
        # A round or a model job can select the rule by this name.
        return {rule.RULE_VERSION: rule.flag}


def install(registry: ExtensionRegistry | None = None) -> ReturnsExtension:
    """Register the extension once and return it.

    Registering the same extension id twice is an error in Dagents, so a second
    call returns the extension that is already registered.
    """
    registry = registry or default_registry
    for existing in registry.extensions():
        if existing.extension_id == ReturnsExtension.extension_id:
            return existing  # type: ignore[return-value]
    extension = ReturnsExtension()
    registry.register(extension)
    return extension
```

An extension contributes five kinds of thing. Return an empty list or dict for
any you do not need.

| Method | Returns | Used by |
|---|---|---|
| `feature_contracts()` | `FeatureContract` objects | Validation and round planning |
| `classifications()` | `DataClassification` objects | Every Guard decision |
| `condition_packs()` | `ConditionPack` objects | Your release decision |
| `pipeline_steps()` | Named functions | Your own code, through `registry.pipeline_step(name)` |
| `model_adapters()` | Named functions | Your own code, through `registry.model_adapter(name)` |

No framework service calls a contributed pipeline step or model adapter today.
The pipeline service runs only its six built-in step kinds. The registry keeps
the names so that your application looks them up in one place.

## 5. Register it and check your records

<!-- source: examples/getting-started/step1_register.py -->
```python
"""Step 1: register the extension, then check each store's orders against the contract."""

from agents.common.extensions import default_registry
from agents.common.infrastructure.dagents_runner import validate_dataset_schema

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
```

Run it:

```bash
PYTHONPATH=. .venv/bin/python examples/getting-started/step1_register.py
```

Expected output:

<!-- output: examples/getting-started/step1_register.py -->
```text
Extensions:       example-retail-returns 1.0.0
Contracts:        retail-orders retail-orders-v1
Classifications:  retail-orders
Condition packs:  order_returns
Model adapters:   return-check-rule-v2

store-north: 60 orders, matches the contract: True
store-south: 45 orders, matches the contract: True
```

`validate_dataset_schema` asks the planner to infer a schema from the records
and compare it with the contract. It reports missing fields, type mismatches
and extra fields. It checks the schema of the whole list, not each record, so
your store still checks each record when it is added (Step 3).

## Registering with the LMA and GMA over HTTP

The LMA and GMA services do not load extensions on their own. If your
application uses them over HTTP, register the classification at runtime:

```bash
PYTHONPATH=.:examples/getting-started .venv/bin/python -c \
  "from shop.contract import CLASSIFICATION; print(CLASSIFICATION.model_dump_json())" > classification.json
curl -s -X PUT http://127.0.0.1:8010/api/v1/governance/classifications/retail-orders \
  -H 'Content-Type: application/json' -d @classification.json
```

The LMA answers with the classification it stored:

```json
{"classification_id":"retail-orders","field_sensitivity":{"order_value":"high","customer_age_band":"medium","loyalty_member":"medium","returned":"medium","items":"low","category":"low","model_update":"high"},"default_sensitivity":"high","regulations":["GDPR"],"minimum_cohort":20}
```

`GET /api/v1/governance/classifications` lists what the agent holds. A request
to the Guard over HTTP carries its classification in the request body, so your
client reads it from here or from your own code. The classification endpoints
are in the API reference, under
[LMA governance](../reference/api/agents.md#lma-governance).

## What can go wrong

| What you see | Why | What to do |
|---|---|---|
| `ExtensionError: extension example-retail-returns is already registered` | The extension was registered twice in one process | Register once at start-up. `install()` above returns the existing extension on a second call |
| `ExtensionError: extension X redefines classification Y, which another extension already provides` | Two extensions use the same id | Give each contract, classification and condition pack a unique id. Dagents refuses the second one, because otherwise a Guard decision would depend on import order |
| `ExtensionError: condition pack X references unknown classification Y` | The condition pack names a classification that nobody registered | Register the classification in the same extension, or in one registered earlier |
| `ExtensionError: unknown classification: Y` when a Guard check runs | The extension was not installed in this process | Call `install()` before the first request |

Next: [Step 3: Give each source its own store](03-store-per-source.md).
