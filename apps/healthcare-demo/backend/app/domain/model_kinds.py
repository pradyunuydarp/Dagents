"""The app's two models, as model kinds that the Dagents agents run.

A model kind (``agents.common.extensions.ModelKind``) is how the LMA and the
GMA run an application's model without importing it. ``app.extension``
contributes the two kinds below:

| Kind | Model | Can be trained | Used for |
|---|---|---|---|
| ``points_rule`` | The seed points rule in :mod:`app.domain.stroke_rule` | no | The first release, evaluation rounds |
| ``logistic_regression`` | The federated candidate in :mod:`app.domain.stroke_model` | yes | Training rounds, evaluation rounds, release |

The maths stays in the app: how a model is measured, how a hospital trains,
how an update is checked and digested, and how the sites' updates are
combined. The agents move the jobs and the updates, store them, check the
digests and call these methods:

- the hospital's LMA calls ``parameter_digest``, ``code_digest``,
  ``evaluate``, ``train``, ``check_update`` and ``update_digest``;
- the GMA calls ``parameter_digest``, ``check_update``, ``update_digest``,
  ``combine`` and ``parameter_change``.

Every digest is the one the app computed before it used the agents, so the
model versions stored in the coordinator schema verify unchanged.

The module also has what the hospital views need to score a record with the
model the hospital's LMA reports in use (:func:`score` and :func:`assess`),
and the versions the app registers with the GMA when it starts
(:func:`startup_registrations`).
"""

from __future__ import annotations

import math
from typing import Any, Callable, Iterable, Mapping

from agents.common.domain.errors import ModelVerificationError
from agents.common.domain.federation import ModelVersionRegistration
from agents.common.extensions import TrainingOutcome

from app.domain import stroke_model, stroke_rule

#: The seed release: the hand-written points rule.
SEED_VERSION = stroke_rule.RULE_VERSION

#: Where federated training starts: the logistic regression with every
#: coefficient at zero. Every pilot starts from it, so it is registered once.
INITIAL_VERSION = "stroke-outcome-logreg-init"

#: Readable names for the logistic-regression coordinates, for the basis of an
#: assessment.
FEATURE_LABELS: dict[str, str] = {
    "age_band": "age band",
    "sex_female": "sex",
    "conscious_level": "conscious level",
    "face_weakness": "face weakness",
    "arm_weakness": "arm weakness",
    "leg_weakness": "leg weakness",
    "speech_difficulty": "speech difficulty",
    "visual_field_loss": "visual field loss",
    "hours_since_onset": "hours since onset",
    "systolic_bp": "systolic blood pressure",
    "atrial_fibrillation": "atrial fibrillation",
    "onset_on_waking": "onset on waking",
    "subtype_tacs": "subtype TACS",
    "subtype_pacs": "subtype PACS",
    "subtype_lacs": "subtype LACS",
    "subtype_pocs": "subtype POCS",
    "subtype_other": "subtype other",
}

ScoreFunction = Callable[[Mapping[str, Any]], "float | None"]


def _labelled(records: Iterable[Mapping[str, Any]], label_field: str) -> list[Mapping[str, Any]]:
    """The records with a yes/no label. Only those are measured or trained on."""
    return [record for record in records if isinstance(record.get(label_field), bool)]


def _log_loss(score_fn: ScoreFunction, labelled: list[Mapping[str, Any]], label_field: str) -> float | None:
    """Mean log loss over the labelled records the model scores, or ``None`` when it scores none."""
    total = 0.0
    count = 0
    for record in labelled:
        score = score_fn(record)
        if score is None:
            continue
        p = min(max(score, 1e-12), 1.0 - 1e-12)
        total -= math.log(p) if record[label_field] else math.log(1.0 - p)
        count += 1
    return total / count if count else None


def _measure(score_fn: ScoreFunction, records: Iterable[Mapping[str, Any]], label_field: str) -> dict[str, float]:
    """The metrics the release gates read, plus the log loss the training curve reads.

    Both models are measured with :func:`stroke_rule.evaluate_against_labels`,
    so the metrics mean the same for each. No labelled record gives no
    metrics.
    """
    labelled = _labelled(records, label_field)
    metrics = stroke_rule.evaluate_against_labels(labelled, score_fn=score_fn, label_field=label_field)
    loss = _log_loss(score_fn, labelled, label_field)
    if loss is not None:
        metrics["log_loss"] = loss
    return metrics


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class PointsRuleKind:
    """The seed points rule as a model kind. It cannot be trained.

    The rule is code. Its parameters record the numbers that code uses, and its
    digest is the digest of those numbers. Parameters that differ from the code
    do not verify; otherwise a site would run a different rule from the one
    registered.
    """

    kind = stroke_rule.RULE_KIND
    trainable = False
    #: Required of every kind. The rule is never trained, so no version is named with it.
    version_prefix = "stroke-outcome-rule"

    @staticmethod
    def _check(params: Any) -> None:
        if not isinstance(params, Mapping) or dict(params) != stroke_rule.rule_parameters():
            raise ModelVerificationError("the registered rule differs from the rule this site runs")

    def parameter_digest(self, params: Mapping[str, Any]) -> str:
        """The rule's digest. Raises ``ModelVerificationError`` for parameters that differ from the code."""
        self._check(params)
        return stroke_rule.rule_digest()

    def code_digest(self) -> str:
        """The rule's code is the rule itself, so this is the rule's digest."""
        return stroke_rule.rule_digest()

    def evaluate(
        self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str
    ) -> dict[str, float]:
        """The rule measured on the labelled records. Suppressed records are left out of the log loss."""
        self._check(params)
        return _measure(stroke_rule.rule_score, records, label_field)

    def score(self, params: Mapping[str, Any], record: Mapping[str, Any]) -> float | None:
        """The estimated risk, 0-1, or ``None`` when the rule suppresses the record."""
        return stroke_rule.rule_score(record)

    def assess(self, version: str, params: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
        """The rule's own assessment of one record, with the model's version and kind."""
        assessment = stroke_rule.assess(record).as_dict()
        assessment["model_version"] = version
        assessment["model_kind"] = self.kind
        return assessment


class LogisticRegressionKind:
    """The federated candidate, a logistic regression, as a model kind.

    Parameters and updates map coordinate names (:data:`stroke_model.FEATURES`)
    to finite numbers. A missing coordinate counts as 0. Parameters and updates
    share one digest rule, :func:`stroke_model.parameter_digest`.
    """

    kind = stroke_model.MODEL_KIND
    trainable = True
    #: A training round's candidate is named ``stroke-outcome-logreg+<round id>``.
    version_prefix = "stroke-outcome-logreg"

    @staticmethod
    def _vector(values: Any, what: str, error: type[Exception]) -> dict[str, float]:
        """``values`` as floats. Raises ``error`` for unknown coordinates or values that are not finite numbers."""
        if not isinstance(values, Mapping):
            raise error(f"{what} must map coordinate names to numbers")
        unknown = sorted(str(name) for name in set(values) - set(stroke_model.FEATURES))
        if unknown:
            raise error(f"{what} name unknown coordinates: {unknown}")
        for name, value in values.items():
            if not _finite(value):
                raise error(f"the value for {name} in {what} is not a finite number")
        return {name: float(value) for name, value in values.items()}

    def _parameters(self, params: Any) -> dict[str, float]:
        return self._vector(params, "the parameters", ModelVerificationError)

    # --- Every kind ----------------------------------------------------------------

    def parameter_digest(self, params: Mapping[str, Any]) -> str:
        """:func:`stroke_model.parameter_digest`. Raises ``ModelVerificationError`` for invalid parameters."""
        return stroke_model.parameter_digest(self._parameters(params))

    def code_digest(self) -> str:
        """The digest of ``stroke_model``'s source, which holds the encoding and the training settings."""
        return stroke_model.training_code_digest()

    def evaluate(
        self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str
    ) -> dict[str, float]:
        """The model measured on the labelled records."""
        weights = self._parameters(params)
        return _measure(lambda record: stroke_model.score(weights, record), records, label_field)

    # --- Training and updates ------------------------------------------------------

    def train(
        self, params: Mapping[str, Any], records: list[Mapping[str, Any]], label_field: str
    ) -> TrainingOutcome:
        """One hospital's local training on the Guard's payload.

        :func:`stroke_model.train_local` from ``params`` on the labelled
        records. The update is the change in parameters
        (:func:`stroke_model.delta`). With no labelled record there is nothing
        to send.
        """
        start = self._parameters(params)
        labelled = _labelled(records, label_field)
        if not labelled:
            return TrainingOutcome(update=None, metrics={"record_count": 0.0}, examples=0)
        local = stroke_model.train_local(start, labelled, label_field)
        return TrainingOutcome(
            update=stroke_model.delta(local, start),
            metrics={
                "start_loss": stroke_model.log_loss(start, labelled, label_field),
                "train_loss": stroke_model.log_loss(local, labelled, label_field),
                "record_count": float(len(labelled)),
            },
            examples=len(labelled),
        )

    def check_update(self, update: Mapping[str, Any]) -> dict[str, float]:
        """The update as floats. Raises ``ValueError`` for unknown coordinates or values that are not finite numbers."""
        return self._vector(update, "the update", ValueError)

    def update_digest(self, update: Mapping[str, Any]) -> str:
        """The digest a hospital reports for its update and the GMA checks."""
        return stroke_model.parameter_digest(self.check_update(update))

    def combine(
        self, params: Mapping[str, Any], weighted_updates: Iterable[tuple[float, Mapping[str, Any]]]
    ) -> dict[str, float]:
        """Add each update, times its weight, to ``params`` (:func:`stroke_model.apply_update`).

        The weights are the planner's FedAvg weights, applied as given. They
        sum to 1, so the model moves by the weighted mean of the sites'
        changes.
        """
        start = self._parameters(params)
        pairs: list[tuple[float, dict[str, float]]] = []
        for weight, update in weighted_updates:
            if not _finite(weight):
                raise ValueError(f"the weight {weight!r} is not a finite number")
            pairs.append((float(weight), self.check_update(update)))
        return stroke_model.apply_update(start, pairs)

    def parameter_change(self, params: Mapping[str, Any], reference: Mapping[str, Any]) -> float:
        """The size (L2 norm) of the change from ``reference`` to ``params``. The release Guard reads it."""
        return stroke_model.l2_norm(stroke_model.delta(self._parameters(params), self._parameters(reference)))

    # --- What the hospital views show ----------------------------------------------

    def score(self, params: Mapping[str, Any], record: Mapping[str, Any]) -> float:
        """The estimated probability of a poor outcome at six months, 0-1. Every record is scored."""
        return stroke_model.score(params, record)

    def assess(self, version: str, params: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
        """Score one record and explain the score.

        The basis lists the three coordinates that moved the log-odds most for
        this record. The rule's data checks are kept as warnings, because a
        missing or unusual input matters to the reviewing clinician whichever
        model scored the record.
        """
        checks = stroke_rule.assess(record)
        weights = [float(params.get(name, 0.0)) for name in stroke_model.FEATURES]
        values = stroke_model.encode(record)
        terms = {
            name: weight * value
            for name, weight, value in zip(stroke_model.FEATURES[1:], weights[1:], values[1:], strict=True)
            if weight * value != 0.0
        }
        largest = sorted(terms.items(), key=lambda item: (-abs(item[1]), item[0]))[:3]
        score = stroke_model.score(params, record)
        return {
            "model_version": version,
            "model_kind": self.kind,
            "score": round(score, 4),
            "priority": stroke_rule.priority_for(score),
            "contributions": {name: round(value, 4) for name, value in sorted(terms.items())},
            "basis": [f"{FEATURE_LABELS[name]}: {value:+.2f} on the log-odds scale" for name, value in largest],
            "warnings": list(checks.warnings),
            "data_quality_issues": list(checks.data_quality_issues),
            "suppressed": False,
        }


#: The kinds ``app.extension`` contributes. They hold no state, so one instance serves every registry.
POINTS_RULE = PointsRuleKind()
LOGISTIC_REGRESSION = LogisticRegressionKind()
MODEL_KINDS: tuple[PointsRuleKind | LogisticRegressionKind, ...] = (POINTS_RULE, LOGISTIC_REGRESSION)


def kind_for(name: str) -> PointsRuleKind | LogisticRegressionKind:
    """The kind with this name. Raises ``ModelVerificationError`` for a kind this app does not have."""
    for kind in MODEL_KINDS:
        if kind.kind == name:
            return kind
    raise ModelVerificationError(f"model kind {name!r} is not one this app can run")


def score(kind: str, params: Mapping[str, Any], record: Mapping[str, Any]) -> float | None:
    """Score one record with a model of this kind. ``None`` when the rule suppresses the record."""
    return kind_for(kind).score(params, record)


def assess(version: str, kind: str, params: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    """Score one record with a model of this kind and explain the score."""
    return kind_for(kind).assess(version, params, record)


def startup_registrations() -> list[tuple[str, ModelVersionRegistration]]:
    """The versions the app registers with the GMA when it starts, in order.

    1. The seed rule, asked for as ``released``. The GMA's lifecycle rule
       releases it only when nothing is released yet.
    2. The all-zero logistic regression, as a ``candidate``. Training starts
       from it.

    Registering the same content again changes nothing, so the app registers
    both at every start. Each call returns new objects.
    """
    return [
        (
            SEED_VERSION,
            ModelVersionRegistration(
                kind=POINTS_RULE.kind, params=stroke_rule.rule_parameters(), requested_status="released"
            ),
        ),
        (
            INITIAL_VERSION,
            ModelVersionRegistration(
                kind=LOGISTIC_REGRESSION.kind, params=stroke_model.initial_params(), requested_status="candidate"
            ),
        ),
    ]
