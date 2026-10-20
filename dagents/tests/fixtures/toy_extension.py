"""A small extension for framework tests: a condition pack and two model kinds.

The LMA, GMA and extension tests need an application to run against, and the
framework must not depend on the healthcare demo. This module is that
application. Nothing here belongs to a real domain: a record has two numeric
readings (``x1``, ``x2``), a group (``a`` or ``b``) and a yes/no ``label``.

It contributes:

- the feature contract ``toy-features`` and the classification
  ``toy-classification`` (minimum cohort 5);
- the condition pack ``toy_condition``, whose label field is ``label``;
- the model kind ``toy_linear``: a logistic regression on ``x1`` and ``x2``
  that can be trained, checked and combined;
- the model kind ``toy_threshold``: a fixed rule that cannot be trained;
- the site source ``toy_static``, which serves records made by
  :func:`toy_records`.

Every number is deterministic, so a test can compare results exactly.

One setting is read from the environment, by an LMA process that loads this
extension: ``TOY_TRAIN_HOLD_FILE`` (see :func:`wait_while_held`). It lets a
test stop the GMA while the LMAs still run a training job.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import random
import threading
import time
from typing import Any, Iterable, Mapping

from dagents.common.domain.errors import ModelVerificationError
from dagents.common.domain.federation import GateComparison, ReleaseGate
from dagents.common.domain.governance import DataClassification
from dagents.common.extensions import (
    ConditionPack,
    FeatureContract,
    FeatureField,
    TrainingOutcome,
)

TOY_CONDITION_ID = "toy_condition"
TOY_LABEL_FIELD = "label"
TOY_PURPOSES = [TOY_CONDITION_ID, "toy_research"]

TOY_CONTRACT = FeatureContract(
    contract_id="toy-features",
    version="toy-v1",
    fields=[
        FeatureField(name="x1", dtype="float", description="The first reading."),
        FeatureField(name="x2", dtype="float", description="The second reading."),
        FeatureField(name="group", dtype="string", description="`a` or `b`."),
        FeatureField(name=TOY_LABEL_FIELD, dtype="bool", required=False, description="The outcome, when known."),
    ],
)

#: The readings are low sensitivity, so a trusted coordinator's job reads them
#: unchanged and the linear model can learn from them. The label is high
#: sensitivity; the Guard's ``generalize`` leaves a yes/no value unchanged.
TOY_CLASSIFICATION = DataClassification(
    classification_id="toy-classification",
    field_sensitivity={
        "x1": "low",
        "x2": "low",
        "group": "medium",
        TOY_LABEL_FIELD: "high",
        "model_update": "high",
    },
    default_sensitivity="high",
    minimum_cohort=5,
)

TOY_RELEASE_GATES = [
    ReleaseGate(gate_id="accuracy", metric="accuracy", comparison=GateComparison(kind="at_least", value=0.6)),
    ReleaseGate(
        gate_id="log_loss",
        metric="log_loss",
        comparison=GateComparison(kind="at_most", value=0.69),
        blocking=False,
    ),
]

TOY_CONDITION = ConditionPack(
    condition_id=TOY_CONDITION_ID,
    display_name="Toy condition",
    intended_use="Framework tests only.",
    feature_contract_id=TOY_CONTRACT.contract_id,
    classification_id=TOY_CLASSIFICATION.classification_id,
    label_field=TOY_LABEL_FIELD,
    approved_purposes=list(TOY_PURPOSES),
    release_gates=TOY_RELEASE_GATES,
)


def toy_records(site_id: str, count: int = 30, labelled_share: float = 1.0) -> list[dict[str, Any]]:
    """Deterministic records for one site.

    The label is true when ``x1 + 0.5 * x2`` plus a little noise is above 0,
    so a linear model can learn it. The first ``labelled_share`` of the
    records carry a label; the rest have none.
    """
    generator = random.Random(f"toy:{site_id}")
    labelled = int(round(count * labelled_share))
    records: list[dict[str, Any]] = []
    for index in range(count):
        x1 = round(generator.uniform(-1.0, 1.0), 6)
        x2 = round(generator.uniform(-1.0, 1.0), 6)
        record: dict[str, Any] = {"x1": x1, "x2": x2, "group": "a" if index % 2 else "b"}
        noise = generator.gauss(0.0, 0.2)
        if index < labelled:
            record[TOY_LABEL_FIELD] = x1 + 0.5 * x2 + noise > 0.0
        records.append(record)
    return records


#: The variable that names the hold file of :func:`wait_while_held`.
TRAIN_HOLD_VARIABLE = "TOY_TRAIN_HOLD_FILE"

#: The longest a training job waits for the hold file to go, in seconds, so a
#: test that fails with the file in place cannot hold an LMA for ever.
TRAIN_HOLD_LIMIT_SECONDS = 60.0


def hold_markers(hold: Path) -> list[Path]:
    """The marker files of the training jobs that wait for ``hold`` now: one per job."""
    return sorted(hold.parent.glob(f"{hold.name}.*"))


def wait_while_held() -> None:
    """Wait while the file that ``TOY_TRAIN_HOLD_FILE`` names exists, for at most 60 seconds.

    :meth:`ToyLinearKind.train` calls this first, so a training job at an LMA
    started with the variable waits there: after its ``before_train`` and
    ``before_read`` Guard decisions, before its update goes to the GMA.
    ``test_gma_process_restart.py`` uses it to stop the GMA during a dispatch.

    While a job waits, a marker file named after the hold file, the process
    and the thread exists next to it (:func:`hold_markers`), so a test can see
    that every LMA's job has reached the hold. Without the variable, or when
    the file does not exist, nothing waits.
    """
    name = os.environ.get(TRAIN_HOLD_VARIABLE, "").strip()
    if not name:
        return
    hold = Path(name)
    if not hold.exists():
        return
    marker = hold.with_name(f"{hold.name}.{os.getpid()}-{threading.get_ident()}")
    marker.touch()
    try:
        deadline = time.monotonic() + TRAIN_HOLD_LIMIT_SECONDS
        while hold.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        marker.unlink(missing_ok=True)


def _digest(values: Mapping[str, float]) -> str:
    """``sha256:`` and the hex digest of the values as JSON with sorted keys."""
    material = json.dumps(dict(values), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _labelled(records: Iterable[Mapping[str, Any]], label_field: str) -> list[Mapping[str, Any]]:
    return [record for record in records if isinstance(record.get(label_field), bool)]


class ToyLinearKind:
    """A logistic regression on ``x1`` and ``x2``, trained by gradient descent."""

    kind = "toy_linear"
    trainable = True
    version_prefix = "toy-linear"

    NAMES = ("bias", "w.x1", "w.x2")
    LEARNING_RATE = 0.5
    EPOCHS = 25

    def initial_params(self) -> dict[str, float]:
        return {name: 0.0 for name in self.NAMES}

    def _vector(self, values: Mapping[str, Any], what: str, error: type[Exception]) -> dict[str, float]:
        if not isinstance(values, Mapping):
            raise error(f"{what} must map names to numbers")
        names = set(values)
        if names != set(self.NAMES):
            unknown = sorted(map(str, names - set(self.NAMES)))
            missing = sorted(set(self.NAMES) - names)
            raise error(f"{what} has unknown names {unknown} or missing names {missing}")
        if not all(_finite(values[name]) for name in self.NAMES):
            raise error(f"every value in {what} must be a finite number")
        return {name: float(values[name]) for name in self.NAMES}

    def parameter_digest(self, params: Mapping[str, Any]) -> str:
        return _digest(self._vector(params, "the parameters", ModelVerificationError))

    def code_digest(self) -> str:
        material = f"{self.kind}:lr={self.LEARNING_RATE}:epochs={self.EPOCHS}"
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _probability(self, params: Mapping[str, float], record: Mapping[str, Any]) -> float:
        z = params["bias"] + params["w.x1"] * float(record["x1"]) + params["w.x2"] * float(record["x2"])
        return 1.0 / (1.0 + math.exp(-z))

    def _loss(self, params: Mapping[str, float], labelled: list[Mapping[str, Any]], label_field: str) -> float:
        total = 0.0
        for record in labelled:
            p = min(max(self._probability(params, record), 1e-12), 1.0 - 1e-12)
            total -= math.log(p) if record[label_field] else math.log(1.0 - p)
        return total / len(labelled)

    def evaluate(
        self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str
    ) -> dict[str, float]:
        vector = self._vector(params, "the parameters", ModelVerificationError)
        labelled = _labelled(records, label_field)
        if not labelled:
            return {"labelled_count": 0.0}
        correct = sum(
            1 for record in labelled if (self._probability(vector, record) >= 0.5) == record[label_field]
        )
        return {
            "accuracy": correct / len(labelled),
            "log_loss": self._loss(vector, labelled, label_field),
            "labelled_count": float(len(labelled)),
        }

    def train(
        self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str
    ) -> TrainingOutcome:
        wait_while_held()
        start = self._vector(params, "the parameters", ModelVerificationError)
        labelled = _labelled(records, label_field)
        if not labelled:
            return TrainingOutcome(update=None, metrics={"record_count": 0.0}, examples=0)
        local = dict(start)
        for _ in range(self.EPOCHS):
            gradient = {name: 0.0 for name in self.NAMES}
            for record in labelled:
                error = self._probability(local, record) - (1.0 if record[label_field] else 0.0)
                gradient["bias"] += error
                gradient["w.x1"] += error * float(record["x1"])
                gradient["w.x2"] += error * float(record["x2"])
            local = {
                name: local[name] - self.LEARNING_RATE * gradient[name] / len(labelled) for name in self.NAMES
            }
        return TrainingOutcome(
            update={name: local[name] - start[name] for name in self.NAMES},
            metrics={
                "start_loss": self._loss(start, labelled, label_field),
                "train_loss": self._loss(local, labelled, label_field),
                "record_count": float(len(labelled)),
            },
            examples=len(labelled),
        )

    def check_update(self, update: Mapping[str, Any]) -> dict[str, float]:
        return self._vector(update, "the update", ValueError)

    def update_digest(self, update: Mapping[str, Any]) -> str:
        return _digest(self.check_update(update))

    def combine(
        self, params: Mapping[str, Any], weighted_updates: Iterable[tuple[float, Mapping[str, Any]]]
    ) -> dict[str, float]:
        combined = self._vector(params, "the parameters", ModelVerificationError)
        for weight, update in weighted_updates:
            change = self.check_update(update)
            combined = {name: combined[name] + float(weight) * change[name] for name in self.NAMES}
        return combined

    def parameter_change(self, params: Mapping[str, Any], reference: Mapping[str, Any]) -> float:
        current = self._vector(params, "the parameters", ModelVerificationError)
        base = self._vector(reference, "the reference parameters", ModelVerificationError)
        return math.sqrt(sum((current[name] - base[name]) ** 2 for name in self.NAMES))


class ToyThresholdKind:
    """A fixed rule: the label is predicted true when ``x1`` is above a threshold."""

    kind = "toy_threshold"
    trainable = False
    version_prefix = "toy-threshold"

    def parameter_digest(self, params: Mapping[str, Any]) -> str:
        if not isinstance(params, Mapping) or set(params) != {"threshold"} or not _finite(params["threshold"]):
            raise ModelVerificationError("the rule's parameters must be one finite number named threshold")
        return _digest({"threshold": float(params["threshold"])})

    def code_digest(self) -> str:
        return "sha256:" + hashlib.sha256(f"{self.kind}:x1-above-threshold".encode("utf-8")).hexdigest()

    def evaluate(
        self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str
    ) -> dict[str, float]:
        self.parameter_digest(params)
        labelled = _labelled(records, label_field)
        if not labelled:
            return {"labelled_count": 0.0}
        threshold = float(params["threshold"])
        correct = sum(1 for record in labelled if (float(record["x1"]) > threshold) == record[label_field])
        return {"accuracy": correct / len(labelled), "labelled_count": float(len(labelled))}


class ToySiteSource:
    """Records held in memory, as a site source serves them.

    ``version()`` changes with every :meth:`append`, so a test can check that
    an LMA notices new records.
    """

    def __init__(self, records: list[dict[str, Any]], location: Mapping[str, str] | None = None) -> None:
        self._records = [dict(record) for record in records]
        self._writes = 0
        self._location = dict(location or {"memory": "toy"})
        self.closed = False

    def records(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self._records]

    def count(self) -> int:
        return len(self._records)

    def version(self) -> str:
        labelled = len(_labelled(self._records, TOY_LABEL_FIELD))
        return f"{self._writes}:{len(self._records)}:{labelled}"

    def location(self) -> dict[str, str]:
        return dict(self._location)

    def close(self) -> None:
        self.closed = True

    def append(self, record: Mapping[str, Any]) -> None:
        self._records.append(dict(record))
        self._writes += 1


def toy_site_source(lease: Any, role: str, site_id: str) -> ToySiteSource:
    """The ``toy_static`` site source factory. It ignores the lease and the role."""
    return ToySiteSource(toy_records(site_id), location={"memory": "toy", "site": site_id})


class ToyExtension:
    """The extension the framework tests register."""

    extension_id = "dagents-toy-extension"
    version = "1.0.0"

    def feature_contracts(self) -> list[FeatureContract]:
        return [TOY_CONTRACT]

    def classifications(self) -> list[DataClassification]:
        return [TOY_CLASSIFICATION]

    def condition_packs(self) -> list[ConditionPack]:
        return [TOY_CONDITION]

    def model_kinds(self) -> list[Any]:
        return [ToyLinearKind(), ToyThresholdKind()]

    def site_sources(self) -> dict[str, Any]:
        return {"toy_static": toy_site_source}


class SpareExtension:
    """An extension that contributes nothing, for tests that load two extensions."""

    extension_id = "dagents-spare-extension"
    version = "0.1.0"


#: A ready-made instance, for loading by ``module:attribute`` without a call.
SPARE_EXTENSION = SpareExtension()
