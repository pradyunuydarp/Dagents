"""The seed release, ``stroke-outcome-rule-v1``, and the evaluation every model shares.

**This rule is not a validated clinical model and must not be used for care.**
It is the model a consortium starts from before any federated training: a
transparent points score whose output a reader can work out by hand.

The points were chosen before looking at the demo's data, from well-known
predictors of death or dependency after a stroke: older age, reduced
consciousness, more neurological deficits, visual field loss, atrial
fibrillation, and a total anterior circulation stroke (TACS). Nothing in this
module was fitted to the IST records. Each weight is an illustration. The
federated candidate in ``stroke_model`` is trained to do better.

The rule does not use systolic blood pressure, hours since onset or whether the
stroke was noticed on waking, because their link to the six-month outcome is not
clear enough to give them points without fitting. It reports unusual values of
them as warnings instead.

Evaluation is shared. :func:`evaluate_against_labels` and
:func:`sweep_alert_thresholds` take a score function, so the seed rule and any
candidate model are measured with the same alert rule and the same metrics.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from app.domain.conditions import STROKE_FAIRNESS_SUBGROUP, STROKE_FIELD_DOMAINS, STROKE_LABEL_FIELD
from app.domain.stroke_model import resolve_category

#: The version the model registry records for this rule.
RULE_VERSION = "stroke-outcome-rule-v1"

#: The model kind the registry records for this rule.
RULE_KIND = "points_rule"

#: The outcome both models estimate: death or dependency at six months.
LABEL_FIELD = STROKE_LABEL_FIELD

# --- Points -------------------------------------------------------------------
# Every value below is an illustration chosen a priori, not an estimate.

#: Older patients do worse. One point per decade from 50.
AGE_BAND_POINTS: dict[str, int] = {"<50": 0, "50-59": 1, "60-69": 2, "70-79": 3, "80-89": 4, "90+": 5}

#: Reduced consciousness is one of the strongest signs of a severe stroke.
CONSCIOUS_POINTS: dict[str, int] = {"alert": 0, "drowsy": 2, "unconscious": 4}

#: One point for each deficit found on examination: more deficits, larger stroke.
DEFICIT_POINTS: dict[str, int] = {
    "face_weakness": 1,
    "arm_weakness": 1,
    "leg_weakness": 1,
    "speech_difficulty": 1,
    "visual_field_loss": 1,
}

#: Atrial fibrillation goes with larger, cardioembolic strokes.
ATRIAL_FIBRILLATION_POINTS = 1

#: A total anterior circulation stroke has the worst outlook of the four
#: Oxfordshire (OCSP) subtypes. The other subtypes add nothing.
SUBTYPE_POINTS: dict[str, int] = {"TACS": 2, "PACS": 0, "LACS": 0, "POCS": 0, "other": 0}

MAX_POINTS = (
    max(AGE_BAND_POINTS.values())
    + max(CONSCIOUS_POINTS.values())
    + sum(DEFICIT_POINTS.values())
    + ATRIAL_FIBRILLATION_POINTS
    + max(SUBTYPE_POINTS.values())
)

#: Points become a 0-1 risk through a fixed logistic curve: 5 points is a risk
#: of 0.5, and each 2 points add 1 to the log-odds. The anchors, also chosen
#: before looking at the data:
#:
#: - 3 points (in their 50s, alert, two deficits): 0.27.
#: - 6 points (in their 70s, alert, three deficits): 0.62, close to the rate of
#:   death or dependency at six months that the trial reported, about 62%.
#: - 15 points (in their 80s, unconscious, five deficits, TACS): 0.99.
#:
#: The curve puts the rule's score on the same scale as the model's predicted
#: probability, so one alert threshold means the same for both. It is not
#: calibrated to any hospital.
RISK_MIDPOINT_POINTS = 5.0
RISK_POINTS_PER_LOG_ODDS = 2.0

# --- Priority and the alert rule ---------------------------------------------

#: Priority bands a case can be sorted into. Bands, not diagnoses.
PRIORITY_BANDS = ("routine", "elevated", "urgent")

#: A case is an alert when its score is at least this. The same threshold
#: applies to every model, because every model's score is an estimated risk of
#: death or dependency at six months. 0.5 means "more likely than not". An alert
#: moves a case up the specialist's queue and removes nobody from it, so a
#: missed high-risk patient costs more than an extra alert.
ALERT_THRESHOLD = 0.5

#: A score at or above the alert threshold is banded elevated.
ELEVATED_THRESHOLD = ALERT_THRESHOLD

#: A score at or above this is banded urgent: an estimated risk of 4 in 5.
URGENT_THRESHOLD = 0.8

# --- Warnings and checks -------------------------------------------------------

SYSTOLIC_RANGE = (60.0, 300.0)
HOURS_RANGE = (0.0, 48.0)

#: Systolic pressure at or above this is reported to the reviewing clinician.
SYSTOLIC_WARNING_MMHG = 220.0

#: Inputs the rule needs. A missing one is reported as a data-quality issue.
REQUIRED_INPUTS = ("age_band", "conscious_level", "face_weakness", "arm_weakness", "leg_weakness", "speech_difficulty")

#: Examination findings. A record with none of them is not scored.
EXAMINATION_FIELDS = ("conscious_level", *DEFICIT_POINTS)

# --- Fairness subgroups --------------------------------------------------------

#: The field the subgroup AUC gap is measured over, and its groups.
SUBGROUP_FIELD = STROKE_FAIRNESS_SUBGROUP
SUBGROUPS = STROKE_FIELD_DOMAINS[STROKE_FAIRNESS_SUBGROUP].options

#: A group's AUC is computed only with at least this many positive and this many
#: negative labelled records. Below that, the AUC of a group moves too much with
#: a few patients to say anything about fairness.
SUBGROUP_MIN_POSITIVES = 10
SUBGROUP_MIN_NEGATIVES = 10


@dataclass(frozen=True)
class StrokeAssessment:
    """One case's output from the seed rule.

    ``contributions`` holds the points each finding added, so a clinician can
    see why the case scored as it did. ``points`` is their sum, and ``score``
    is the risk the points map to.
    """

    score: float
    priority: str
    points: int = 0
    contributions: dict[str, int] = field(default_factory=dict)
    basis: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_quality_issues: list[str] = field(default_factory=list)
    suppressed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_version": RULE_VERSION,
            "score": round(self.score, 4),
            "priority": self.priority,
            "points": self.points,
            "max_points": MAX_POINTS,
            "contributions": dict(self.contributions),
            "basis": list(self.basis),
            "warnings": list(self.warnings),
            "data_quality_issues": list(self.data_quality_issues),
            "suppressed": self.suppressed,
        }


def points_to_score(points: float) -> float:
    """Map rule points to a 0-1 risk with the fixed curve described above."""
    return 1.0 / (1.0 + math.exp(-(points - RISK_MIDPOINT_POINTS) / RISK_POINTS_PER_LOG_ODDS))


def priority_for(score: float) -> str:
    """The priority band for a score."""
    if score >= URGENT_THRESHOLD:
        return "urgent"
    if score >= ELEVATED_THRESHOLD:
        return "elevated"
    return "routine"


def is_alert(score: float | None) -> bool:
    """The alert rule: a score at or above :data:`ALERT_THRESHOLD`. No score, no alert."""
    return score is not None and score >= ALERT_THRESHOLD


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _points(count: int) -> str:
    return f"{count} point" if count == 1 else f"{count} points"


_DEFICIT_TEXT = {
    "face_weakness": "face weakness",
    "arm_weakness": "arm weakness",
    "leg_weakness": "leg weakness",
    "speech_difficulty": "speech difficulty",
    "visual_field_loss": "visual field loss",
}


def assess(record: Mapping[str, Any]) -> StrokeAssessment:
    """Score one record with the seed rule.

    Input: a record in the v3 feature contract. A missing input adds no points
    and is reported, so a gap in the record is visible rather than read as a
    normal finding. A record with no examination finding at all (conscious level
    and the five deficits all missing) is suppressed: it gets no score and stays
    in the worklist, marked as not assessed.
    """
    contributions: dict[str, int] = {}
    basis: list[str] = []
    warnings: list[str] = []
    issues: list[str] = []

    for name in REQUIRED_INPUTS:
        if record.get(name) is None:
            issues.append(f"{name} is missing")

    band_value = record.get("age_band")
    band = resolve_category(band_value, tuple(AGE_BAND_POINTS))
    if band is not None:
        contributions["age_band"] = AGE_BAND_POINTS[band]
        basis.append(f"age band {band}: {_points(AGE_BAND_POINTS[band])}")
    elif band_value is not None:
        issues.append(f"age_band {band_value!r} is not a contract age band")

    conscious_value = record.get("conscious_level")
    conscious = resolve_category(conscious_value, tuple(CONSCIOUS_POINTS))
    if conscious is not None:
        contributions["conscious_level"] = CONSCIOUS_POINTS[conscious]
        basis.append(f"conscious level {conscious}: {_points(CONSCIOUS_POINTS[conscious])}")
    elif conscious_value is not None:
        issues.append(f"conscious_level {conscious_value!r} is not alert, drowsy or unconscious")

    deficits = [name for name in DEFICIT_POINTS if record.get(name) is True]
    for name in deficits:
        contributions[name] = DEFICIT_POINTS[name]
    if deficits:
        listed = ", ".join(_DEFICIT_TEXT[name] for name in deficits)
        basis.append(f"{listed}: {_points(sum(DEFICIT_POINTS[name] for name in deficits))}")
    else:
        recorded = [name for name in DEFICIT_POINTS if record.get(name) is False]
        if recorded:
            basis.append("no deficit found on examination: 0 points")

    if record.get("atrial_fibrillation") is True:
        contributions["atrial_fibrillation"] = ATRIAL_FIBRILLATION_POINTS
        basis.append(f"atrial fibrillation: {_points(ATRIAL_FIBRILLATION_POINTS)}")

    subtype_value = record.get("stroke_subtype")
    subtype = resolve_category(subtype_value, tuple(SUBTYPE_POINTS))
    if subtype is not None and SUBTYPE_POINTS[subtype]:
        contributions["stroke_subtype"] = SUBTYPE_POINTS[subtype]
        basis.append(f"stroke subtype {subtype}: {_points(SUBTYPE_POINTS[subtype])}")
    elif subtype is None and subtype_value is not None:
        issues.append(f"stroke_subtype {subtype_value!r} is not an OCSP subtype")

    systolic = _number(record.get("systolic_bp"))
    if systolic is not None:
        if not SYSTOLIC_RANGE[0] <= systolic <= SYSTOLIC_RANGE[1]:
            issues.append(f"systolic_bp {systolic:g} is outside {SYSTOLIC_RANGE[0]:g}-{SYSTOLIC_RANGE[1]:g} mmHg")
        elif systolic >= SYSTOLIC_WARNING_MMHG:
            warnings.append(f"systolic blood pressure {systolic:g} mmHg is markedly raised")

    hours = _number(record.get("hours_since_onset"))
    if hours is not None and not HOURS_RANGE[0] <= hours <= HOURS_RANGE[1]:
        issues.append(f"hours_since_onset {hours:g} is outside {HOURS_RANGE[0]:g}-{HOURS_RANGE[1]:g} hours")

    if record.get("onset_on_waking") is True:
        warnings.append("symptoms were noticed on waking, so the onset time is uncertain")

    if all(record.get(name) is None for name in EXAMINATION_FIELDS):
        return StrokeAssessment(
            score=0.0,
            priority="routine",
            points=0,
            contributions={},
            basis=[],
            warnings=warnings,
            data_quality_issues=issues + ["no examination finding was recorded; no score was produced"],
            suppressed=True,
        )

    points = sum(contributions.values())
    score = points_to_score(points)
    if issues:
        warnings.append(f"{len(issues)} input(s) were missing or invalid; the score is less complete than usual")

    return StrokeAssessment(
        score=score,
        priority=priority_for(score),
        points=points,
        contributions=contributions,
        basis=basis,
        warnings=warnings,
        data_quality_issues=issues,
    )


def rule_score(record: Mapping[str, Any]) -> float | None:
    """The seed rule as a score function: the risk, or ``None`` when suppressed."""
    assessment = assess(record)
    return None if assessment.suppressed else assessment.score


def rule_parameters() -> dict[str, Any]:
    """Every number that defines the rule, for the model registry."""
    return {
        "version": RULE_VERSION,
        "age_band_points": AGE_BAND_POINTS,
        "conscious_points": CONSCIOUS_POINTS,
        "deficit_points": DEFICIT_POINTS,
        "atrial_fibrillation_points": ATRIAL_FIBRILLATION_POINTS,
        "subtype_points": SUBTYPE_POINTS,
        "risk_midpoint_points": RISK_MIDPOINT_POINTS,
        "risk_points_per_log_odds": RISK_POINTS_PER_LOG_ODDS,
    }


def rule_digest() -> str:
    """``sha256:`` and the hex digest of :func:`rule_parameters` as canonical JSON."""
    canonical = json.dumps(rule_parameters(), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def rank_worklist(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Order a worklist by the rule's score, highest first.

    Nothing is removed. Suppressed cases go to the end, marked as not assessed.
    Cases with the same score keep their input order.
    """
    scored = [{**record, "assessment": assess(record).as_dict()} for record in records]
    return sorted(
        scored,
        key=lambda item: (not item["assessment"]["suppressed"], item["assessment"]["score"]),
        reverse=True,
    )


# --- Evaluation ------------------------------------------------------------------

ScoreFunction = Callable[[Mapping[str, Any]], "float | None"]


def auc(pairs: Iterable[tuple[float, bool]]) -> float | None:
    """Area under the ROC curve, from ranks (ties count half).

    ``None`` unless there is at least one positive and one negative.
    """
    ordered = sorted(pairs, key=lambda pair: pair[0])
    positives = sum(1 for _, label in ordered if label)
    negatives = len(ordered) - positives
    if not positives or not negatives:
        return None
    rank_sum = 0.0
    index = 0
    while index < len(ordered):
        end = index
        while end + 1 < len(ordered) and ordered[end + 1][0] == ordered[index][0]:
            end += 1
        average_rank = (index + end) / 2.0 + 1.0
        rank_sum += average_rank * sum(1 for _, label in ordered[index : end + 1] if label)
        index = end + 1
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def _labelled(
    records: Iterable[Mapping[str, Any]], score_fn: ScoreFunction, label_field: str
) -> list[tuple[float | None, bool, str | None]]:
    return [
        (score_fn(record), record[label_field], resolve_category(record.get(SUBGROUP_FIELD), SUBGROUPS))
        for record in records
        if isinstance(record.get(label_field), bool)
    ]


def subgroup_aucs(
    records: Iterable[Mapping[str, Any]], score_fn: ScoreFunction = rule_score, label_field: str = LABEL_FIELD
) -> dict[str, float]:
    """AUC for each subgroup that has enough positive and negative records."""
    labelled = _labelled(records, score_fn, label_field)
    result: dict[str, float] = {}
    for group in SUBGROUPS:
        pairs = [(score, label) for score, label, member in labelled if member == group and score is not None]
        positives = sum(1 for _, label in pairs if label)
        negatives = len(pairs) - positives
        if positives >= SUBGROUP_MIN_POSITIVES and negatives >= SUBGROUP_MIN_NEGATIVES:
            value = auc(pairs)
            if value is not None:
                result[group] = value
    return result


def evaluate_against_labels(
    records: Iterable[Mapping[str, Any]],
    score_fn: ScoreFunction = rule_score,
    label_field: str = LABEL_FIELD,
    alert_threshold: float | None = None,
) -> dict[str, float]:
    """Measure a score function against recorded outcomes at one site.

    Every model is measured with this function, so the metrics mean the same
    for the seed rule and for a candidate. Only records with a yes/no label
    count.

    - ``auc``: over the records the model scored.
    - ``sensitivity``: alerts among patients with a poor outcome.
    - ``specificity``: no alert among patients without one.
    - ``alerts_per_1000``: alerts per 1,000 labelled records.
    - ``subgroup_auc_gap``: the absolute AUC difference between female and male
      patients. It is reported only when both groups have at least
      :data:`SUBGROUP_MIN_POSITIVES` positive and
      :data:`SUBGROUP_MIN_NEGATIVES` negative records. A missing gap blocks the
      fairness gate.

    A record the model could not score (score ``None``) is not an alert and is
    left out of the AUC. ``alert_threshold`` defaults to :data:`ALERT_THRESHOLD`;
    pass another value only to explore the tradeoff, as
    :func:`sweep_alert_thresholds` does. No labelled record gives no metrics.
    """
    threshold = ALERT_THRESHOLD if alert_threshold is None else alert_threshold
    records = list(records)
    labelled = _labelled(records, score_fn, label_field)
    if not labelled:
        return {}

    metrics: dict[str, float] = {}
    overall = auc((score, label) for score, label, _ in labelled if score is not None)
    if overall is not None:
        metrics["auc"] = overall

    alerted = [score is not None and score >= threshold for score, _, _ in labelled]
    metrics["alerts_per_1000"] = 1000.0 * sum(alerted) / len(labelled)
    positives = [flag for flag, (_, label, _) in zip(alerted, labelled) if label]
    negatives = [flag for flag, (_, label, _) in zip(alerted, labelled) if not label]
    if positives:
        metrics["sensitivity"] = sum(positives) / len(positives)
    if negatives:
        metrics["specificity"] = sum(1 for flag in negatives if not flag) / len(negatives)

    groups = subgroup_aucs(records, score_fn, label_field)
    if len(groups) == len(SUBGROUPS):
        metrics["subgroup_auc_gap"] = max(groups.values()) - min(groups.values())
    return metrics


def sweep_alert_thresholds(
    records: Iterable[Mapping[str, Any]],
    score_fn: ScoreFunction = rule_score,
    label_field: str = LABEL_FIELD,
    thresholds: list[float] | None = None,
) -> list[dict[str, float]]:
    """Sensitivity, specificity and alert burden at a range of alert thresholds.

    The sweep shows what moving the threshold would cost in alerts. Specificity
    is included because a low enough threshold reaches any sensitivity by
    alerting on everyone.
    """
    points = thresholds or [round(0.05 * step, 2) for step in range(1, 20)]
    labelled = _labelled(records, score_fn, label_field)
    if not labelled:
        return []
    positives = [score for score, label, _ in labelled if label]
    negatives = [score for score, label, _ in labelled if not label]

    def alerted(score: float | None, threshold: float) -> bool:
        return score is not None and score >= threshold

    sweep: list[dict[str, float]] = []
    for threshold in points:
        row = {
            "threshold": threshold,
            "alerts_per_1000": 1000.0 * sum(alerted(score, threshold) for score, _, _ in labelled) / len(labelled),
        }
        if positives:
            row["sensitivity"] = sum(alerted(score, threshold) for score in positives) / len(positives)
        if negatives:
            row["specificity"] = sum(not alerted(score, threshold) for score in negatives) / len(negatives)
        sweep.append(row)
    return sweep
