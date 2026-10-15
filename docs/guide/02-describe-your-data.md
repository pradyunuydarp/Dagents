# Step 2: Describe your data

Dagents cannot know which fields your application holds or which of them
identify a person. You declare that once, in three objects, and register them as
an extension:

| Object | What it says | Who reads it |
|---|---|---|
| Feature contract | The fields every source agrees to produce, their types and units, and a version | The planner, when it validates records and selects sources for a round |
| Data classification | How sensitive each field is, which regulations apply, and the smallest group that may be described | The planner, when it decides what a request may see |
| Condition pack | One study: its purpose, its contract, its classification, its label field, its approved purposes and its release gates | The LMA, which reads the label field and the purposes, and the coordinator or the GMA, when it decides on a release |

An **extension** is a class that hands these objects to Dagents. Your
application never changes framework code. It registers an extension instead.
An extension can also contribute **model kinds**: the code that runs your model
or rule, so that the Dagents agents can run it in a round (section 5).

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
  (you will see this in [Step 6](06-federated-rounds.md#post-results-to-the-gma-yourself)).
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
    # The field a model is measured against. An LMA reads it from the pack.
    label_field="returned",
    approved_purposes=APPROVED_PURPOSES,
    release_gates=RELEASE_GATES,
    limitations=["The orders in data/ are made up for this example."],
    owner="returns-team",
)
```

- **Approved purposes.** Every request declares a purpose, and carries the list
  of approved purposes in `approved_purposes`. The planner denies a request
  whose purpose is not in that list, whoever asks. The condition pack holds the
  list in `approved_purposes`. An LMA or a GMA started with the pack puts it on
  every Guard request it builds. Code that builds its own `RestrictionRequest`,
  such as `StoreSite` in Step 4, puts the list on the request itself (here,
  `APPROVED_PURPOSES`). An empty list turns the purpose check off, so a request
  sent without the list is not checked for its purpose.
- **Label field.** `label_field` names the field that holds the outcome a model
  is measured against, here `returned`. It must be a field of the pack's
  feature contract; otherwise registration fails. An LMA reads it to measure
  or train a model in a round, and to decide whether the share of positive
  labels may leave the site (Step 5).
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
    ModelKind,
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

    def model_kinds(self) -> list[ModelKind]:
        # An LMA runs the rule in a round through this kind (step 5).
        return [rule.RuleKind()]


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

An extension contributes up to seven kinds of thing. Return an empty list or
dict for any you do not need. The registry reads `model_kinds()` and
`site_sources()` only when the extension has them, so an extension written
before they existed still registers.

| Method | Returns | Used by |
|---|---|---|
| `feature_contracts()` | `FeatureContract` objects | Validation and round planning |
| `classifications()` | `DataClassification` objects | Every Guard decision |
| `condition_packs()` | `ConditionPack` objects | The LMA (label field, purposes) and the release decision (gates) |
| `pipeline_steps()` | Named functions | Your own code, through `registry.pipeline_step(name)` |
| `model_adapters()` | Named functions | Your own code, through `registry.model_adapter(name)` |
| `model_kinds()` | `ModelKind` objects | The LMA, to run a round's model; the GMA, to check, combine and register model versions |
| `site_sources()` | Named factories that build a source's record reader | An LMA started from settings, through `LMA_SITE_SOURCE` |

No framework service calls a contributed pipeline step or model adapter today.
The pipeline service runs only its six built-in step kinds. The registry keeps
the names so that your application looks them up in one place. The LMA and GMA
do call model kinds and site sources.

## 5. Contribute a model kind

A **model kind** is the code that runs one kind of model, such as a rule or a
logistic regression. The Dagents agents never import your model code. An LMA
runs your model in a round through its kind, and the GMA uses the kind to check
a model's digest and to combine training updates. The example's rule is a kind
that cannot be trained:

<!-- source: examples/getting-started/shop/rule.py -->
```python
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
```

The members of `ModelKind` (`agents/common/extensions/registry.py`):

| Member | Needed by | What it does |
|---|---|---|
| `kind`, `trainable`, `version_prefix` | every kind | The kind's name, whether a training round can train it, and the start of a candidate's version (`<version_prefix>+<round_id>`) |
| `parameter_digest(params)` | every kind | The digest of a model's parameters. Raises `ModelVerificationError` for parameters the kind cannot run |
| `code_digest()` | every kind | The digest a round manifest's `training_code_digest` must equal |
| `evaluate(params, records, label_field)` | every kind | Metrics the release gates read, by name |
| `train(params, records, label_field)` | a trainable kind | Local training on the Guard's payload; returns a `TrainingOutcome` with the update, metrics and example count |
| `check_update(update)` | a trainable kind | Returns the update as floats; raises `ValueError` for an unknown name or a value that is not finite |
| `update_digest(update)` | a trainable kind | The digest the site reports and the GMA checks |
| `combine(params, weighted_updates)` | a trainable kind | Adds each `(weight, update)` pair to the parameters, with the planner's weights |
| `parameter_change(params, reference)` | a trainable kind | The size of the change between two models, which the release Guard reads |

The registry checks these members when the extension registers, so a malformed
kind fails at startup and not during a round. Two kinds with the same `kind`
name are an error, as for every other contribution.

## 6. Register it and check your records

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

An LMA or a GMA run as its own process loads the extensions named in
`LMA_EXTENSIONS` or `GMA_EXTENSIONS`: a comma-separated list of
`module:attribute` names, imported from `PYTHONPATH`. Start an LMA with the
example's extension:

```bash
LMA_EXTENSIONS=shop.extension:ReturnsExtension PYTHONPATH=.:examples/getting-started \
  .venv/bin/uvicorn agents.lma.main:app --port 8010
```

In a second terminal:

```bash
curl -s http://127.0.0.1:8010/api/v1/extensions
```

The LMA lists what the extension contributes:

```json
{"extensions":[{"extension_id":"example-retail-returns","version":"1.0.0"}],"feature_contracts":[{"contract_id":"retail-orders","version":"retail-orders-v1"}],"classifications":["retail-orders"],"condition_packs":[{"condition_id":"order_returns","display_name":"Orders likely to be returned"}],"pipeline_steps":[],"model_adapters":["return-check-rule-v2"],"model_kinds":["return_check_rule"],"site_sources":[]}
```

A classification an extension contributes cannot be replaced at runtime. A
`PUT /api/v1/governance/classifications/retail-orders` with different content
answers `409`:

```json
{"detail":"classification retail-orders is contributed by an extension; a runtime registration cannot replace it","code":"conflict"}
```

The same content again answers `200` and changes nothing.

An agent started without extensions holds no classification. Register one at
runtime:

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
client reads it from here or from your own code. An LMA started for a site, and
a GMA with condition packs, refuse a request whose classification differs from
the registered one ([Step 4](04-govern-every-read.md#5-the-guard-over-http)).
The classification endpoints are in the API reference, under
[LMA governance](../reference/api/agents.md#lma-governance).

## What can go wrong

| What you see | Why | What to do |
|---|---|---|
| `ExtensionError: extension example-retail-returns is already registered` | The extension was registered twice in one process | Register once at start-up. `install()` above returns the existing extension on a second call |
| `ExtensionError: extension X redefines classification Y, which another extension already provides` | Two extensions use the same id | Give each contract, classification and condition pack a unique id. Dagents refuses the second one, because otherwise a Guard decision would depend on import order |
| `ExtensionError: condition pack X references unknown classification Y` | The condition pack names a classification that nobody registered | Register the classification in the same extension, or in one registered earlier |
| `ExtensionError: unknown classification: Y` when a Guard check runs | The extension was not installed in this process | Call `install()` before the first request |
| `ExtensionError: condition pack X names label field Y, which is not a field of feature contract Z` | `label_field` is not in the pack's contract | Name a field of the contract, or add the field to the contract |
| `ExtensionError: model kind X from extension Y lacks train, check_update, ...` | A kind sets `trainable = True` but does not have every member a trainable kind needs | Add the members the message lists, or set `trainable = False` |
| `409` with "classification X is contributed by an extension; a runtime registration cannot replace it" | A `PUT` tried to change an extension's classification on a running agent | Change the classification in the extension and restart the agent |

Next: [Step 3: Give each source its own store](03-store-per-source.md).
