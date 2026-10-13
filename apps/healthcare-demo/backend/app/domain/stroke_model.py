"""The federated candidate: a logistic-regression model of poor outcome at six months.

The model is trained only through the demo's federated rounds. Each hospital
runs :func:`train_local` on the records its Ethical Guard releases to the round,
returns the change in parameters (:func:`delta`), and the coordinator applies
the clipped, weighted changes with :func:`apply_update`. Nothing in this module
pools records from several hospitals.

**The encoding is fixed and the same at every site.** Each numeric field is
centred and scaled with a constant written below, chosen from clinical ranges,
never from a site's own data. Per-site means or standard deviations would make
the same coefficient mean different things at different hospitals, and the
average of the updates would then mean nothing.

The encoding also accepts the values the Guard produces when it generalizes a
field: a number rounded to the nearest 10, or a text value cut short and ending
in ``*`` (``"fem*"`` for ``"female"``). A shortened value counts as the one
category it is the start of. If it could be more than one, it counts as missing.

A missing value adds nothing beyond the intercept. A missing yes/no field
encodes as 0, between +0.5 (yes) and -0.5 (no). A missing number encodes as its
centre (24 hours, 160 mmHg), a missing age band as age 70, a missing conscious
level as alert, and a missing subtype sets all five subtype indicators to 0.

The module is pure apart from :func:`training_code_digest`, which reads this
file's own source. Training is full-batch gradient descent with a fixed order of
operations and no randomness, so the same inputs give the same parameters.
"""

from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

#: The model kind the coordinator registry records.
MODEL_KIND = "logistic_regression"

#: The coordinates of the parameter vector, in a fixed order.
FEATURES: tuple[str, ...] = (
    "intercept",
    "age_band",
    "sex_female",
    "conscious_level",
    "face_weakness",
    "arm_weakness",
    "leg_weakness",
    "speech_difficulty",
    "visual_field_loss",
    "hours_since_onset",
    "systolic_bp",
    "atrial_fibrillation",
    "onset_on_waking",
    "subtype_tacs",
    "subtype_pacs",
    "subtype_lacs",
    "subtype_pocs",
    "subtype_other",
)

#: Age band encoded as decades from 70: the band's midpoint minus 70, over 10.
#: The open bands take 45 (``<50``) and 95 (``90+``) as their midpoints.
AGE_BAND_DECADES: dict[str, float] = {
    "<50": -2.5,
    "50-59": -1.5,
    "60-69": -0.5,
    "70-79": 0.5,
    "80-89": 1.5,
    "90+": 2.5,
}

#: Conscious level as steps down from alert. One coefficient for the steps
#: keeps the model's ordering clinical (unconscious is worse than drowsy) even
#: though few patients are unconscious. A missing level encodes as alert.
CONSCIOUS_STEPS: dict[str, float] = {"alert": 0.0, "drowsy": 1.0, "unconscious": 2.0}

#: Hours since onset: (hours - 24) / 24, so the contract's 0-48 hours maps to -1..1.
HOURS_CENTRE = 24.0
HOURS_SCALE = 24.0
HOURS_RANGE = (0.0, 48.0)

#: Systolic blood pressure: (mmHg - 160) / 30. Around 160 mmHg is a usual value
#: in acute stroke, and 30 mmHg is one step of the encoded scale.
SYSTOLIC_CENTRE = 160.0
SYSTOLIC_SCALE = 30.0
SYSTOLIC_RANGE = (60.0, 300.0)

_SUBTYPES = ("TACS", "PACS", "LACS", "POCS", "other")
_SEXES = ("female", "male")

#: Decimal places kept in parameters, so a digest does not depend on the last
#: bit of a floating-point result.
PARAMETER_DECIMALS = 10

#: Default settings for one site's local run, and the number of training rounds
#: a pilot runs. They come from a simulation of the pilot on the three hospitals'
#: records: size-weighted averaging, with each round's delta clipped to an L2
#: norm of 1.0. With these settings the first round's deltas have norms of about
#: 1.3, so the clip bound applies; later rounds stay under it. After 5 rounds
#: the training loss is within 0.001 of its value after 10. The L2 penalty keeps
#: rare categories, such as the 5 patients whose subtype is "other", from
#: setting their own coefficients.
DEFAULT_EPOCHS = 30
DEFAULT_LEARNING_RATE = 0.5
DEFAULT_L2 = 0.01
DEFAULT_ROUNDS = 5


def resolve_category(value: Any, options: tuple[str, ...]) -> str | None:
    """Resolve a value, possibly shortened by the Guard, to one category."""
    if not isinstance(value, str):
        return None
    if value in options:
        return value
    if value.endswith("*"):
        prefix = value[:-1]
        matches = [option for option in options if option.startswith(prefix)]
        if len(matches) == 1:
            return matches[0]
    return None


def _age_band(value: Any) -> float:
    """Resolve an age band, possibly shortened (``"70-*"``), to decades from 70.

    Every band starts with a different character, so a band shortened to one
    character and ``*`` still names one band.
    """
    band = resolve_category(value, tuple(AGE_BAND_DECADES))
    return AGE_BAND_DECADES[band] if band else 0.0


def _yes_no(value: Any) -> float:
    if value is True:
        return 0.5
    if value is False:
        return -0.5
    return 0.0


def _number(value: Any, centre: float, scale: float, bounds: tuple[float, float]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        return 0.0
    low, high = bounds
    return (min(high, max(low, float(value))) - centre) / scale


def encode(record: Mapping[str, Any]) -> list[float]:
    """Encode one record as a feature vector in the order of :data:`FEATURES`."""
    conscious = resolve_category(record.get("conscious_level"), tuple(CONSCIOUS_STEPS))
    subtype = resolve_category(record.get("stroke_subtype"), _SUBTYPES)
    sex = resolve_category(record.get("sex"), _SEXES)
    return [
        1.0,
        _age_band(record.get("age_band")),
        0.5 if sex == "female" else -0.5 if sex == "male" else 0.0,
        CONSCIOUS_STEPS[conscious] if conscious else 0.0,
        _yes_no(record.get("face_weakness")),
        _yes_no(record.get("arm_weakness")),
        _yes_no(record.get("leg_weakness")),
        _yes_no(record.get("speech_difficulty")),
        _yes_no(record.get("visual_field_loss")),
        _number(record.get("hours_since_onset"), HOURS_CENTRE, HOURS_SCALE, HOURS_RANGE),
        _number(record.get("systolic_bp"), SYSTOLIC_CENTRE, SYSTOLIC_SCALE, SYSTOLIC_RANGE),
        _yes_no(record.get("atrial_fibrillation")),
        _yes_no(record.get("onset_on_waking")),
        1.0 if subtype == "TACS" else 0.0,
        1.0 if subtype == "PACS" else 0.0,
        1.0 if subtype == "LACS" else 0.0,
        1.0 if subtype == "POCS" else 0.0,
        1.0 if subtype == "other" else 0.0,
    ]


def initial_params() -> dict[str, float]:
    """The starting point of federated training: every coefficient is zero.

    With all zeros the model scores every patient 0.5, so the first round's
    updates come only from the sites' data.
    """
    return {name: 0.0 for name in FEATURES}


def _vector(params: Mapping[str, float]) -> list[float]:
    unknown = set(params) - set(FEATURES)
    if unknown:
        raise ValueError(f"unknown parameters: {sorted(unknown)}")
    return [float(params.get(name, 0.0)) for name in FEATURES]


def _named(vector: list[float]) -> dict[str, float]:
    return {name: round(value, PARAMETER_DECIMALS) + 0.0 for name, value in zip(FEATURES, vector)}


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def _dot(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def score(params: Mapping[str, float], record: Mapping[str, Any]) -> float:
    """The model's estimated probability of poor outcome at six months, 0-1."""
    return _sigmoid(_dot(_vector(params), encode(record)))


def _labelled(records: Iterable[Mapping[str, Any]], label_field: str) -> list[tuple[list[float], float]]:
    return [
        (encode(record), 1.0 if record[label_field] else 0.0)
        for record in records
        if isinstance(record.get(label_field), bool)
    ]


def log_loss(
    params: Mapping[str, float], records: Iterable[Mapping[str, Any]], label_field: str = "poor_outcome_6m"
) -> float | None:
    """Mean log loss over the labelled records, or ``None`` when none are labelled."""
    rows = _labelled(records, label_field)
    if not rows:
        return None
    weights = _vector(params)
    total = 0.0
    for features, label in rows:
        p = min(max(_sigmoid(_dot(weights, features)), 1e-12), 1.0 - 1e-12)
        total -= label * math.log(p) + (1.0 - label) * math.log(1.0 - p)
    return total / len(rows)


def train_local(
    global_params: Mapping[str, float],
    records: Iterable[Mapping[str, Any]],
    label_field: str = "poor_outcome_6m",
    epochs: int = DEFAULT_EPOCHS,
    learning_rate: float = DEFAULT_LEARNING_RATE,
    l2: float = DEFAULT_L2,
) -> dict[str, float]:
    """Train from ``global_params`` on one site's labelled records.

    Full-batch gradient descent on the mean log loss plus ``l2 / 2`` times the
    squared norm of the coefficients. The intercept is not penalised. Records
    without a yes/no label are skipped. With no labelled record the parameters
    come back unchanged.
    """
    if epochs < 0 or learning_rate <= 0 or l2 < 0:
        raise ValueError("epochs must be >= 0, learning_rate > 0 and l2 >= 0")
    weights = _vector(global_params)
    rows = _labelled(records, label_field)
    if not rows:
        return _named(weights)

    count = len(rows)
    width = len(FEATURES)
    # Columns, so each gradient coordinate is one dot product.
    columns = [[features[j] for features, _ in rows] for j in range(width)]
    labels = [label for _, label in rows]
    for _ in range(epochs):
        residuals = [
            _sigmoid(_dot(weights, features)) - label for (features, _), label in zip(rows, labels)
        ]
        for j in range(width):
            gradient = _dot(columns[j], residuals) / count
            if j > 0:
                gradient += l2 * weights[j]
            weights[j] -= learning_rate * gradient
    return _named(weights)


def delta(local: Mapping[str, float], global_params: Mapping[str, float]) -> dict[str, float]:
    """The change a site returns: its trained parameters minus the global ones."""
    return _named([a - b for a, b in zip(_vector(local), _vector(global_params))])


def apply_update(
    params: Mapping[str, float], weighted_deltas: Iterable[tuple[float, Mapping[str, float]]]
) -> dict[str, float]:
    """Add each delta, times its weight, to ``params``.

    The weights are applied as given. FedAvg weights from the planner sum to 1,
    so the result moves by the weighted mean of the sites' changes.
    """
    vector = _vector(params)
    for weight, change in weighted_deltas:
        vector = [value + float(weight) * step for value, step in zip(vector, _vector(change))]
    return _named(vector)


def l2_norm(vector: Mapping[str, float]) -> float:
    """The Euclidean norm of a parameter vector or a delta."""
    return math.sqrt(sum(float(value) ** 2 for value in vector.values()))


def parameter_digest(params: Mapping[str, float]) -> str:
    """``sha256:`` and the hex digest of the parameters as canonical JSON.

    Canonical JSON: keys sorted, no spaces, each value rounded to
    :data:`PARAMETER_DECIMALS` places.
    """
    canonical = json.dumps(_named(_vector(params)), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def training_code_digest() -> str:
    """``sha256:`` and the hex digest of this module's source file.

    A round manifest carries it, so a site can check which training code it is
    asked to run. Any change to the encoding or the training loop changes it.
    """
    return "sha256:" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
