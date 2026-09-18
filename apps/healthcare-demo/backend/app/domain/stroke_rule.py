"""A transparent scoring rule for suspected-stroke triage support.

**This is not a validated clinical model and must not be used for care.** It
exists to occupy, visibly, the place where a real condition-specific model or
ruleset plugs into Dagents, and to make the framework's behaviour observable
end to end. Every weight below is an illustration chosen for legibility.

Why a rule rather than a trained model. The demo needs something whose output a
reader can predict from the inputs, so that when a release gate blocks a
candidate it is obvious why. A trained model would make the governance layer
harder to see, not easier, and this repository does not contain a clinically
appropriate stroke model to train.

The signals are the publicly documented stroke warning signs (the FAST
examination findings), the assessed NIHSS total, and the time since the patient
was last known well, because a stroke pathway is time-critical. Hypoglycaemia is
flagged as a common stroke mimic rather than used to exclude a case: ruling a
patient out is a clinical decision, not a triage-support one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Priority bands a case can be sorted into. Bands, not diagnoses.
PRIORITY_BANDS = ("routine", "elevated", "urgent")

#: Score at or above which a case is banded urgent.
URGENT_THRESHOLD = 0.62

#: Score at or above which a case is banded elevated.
ELEVATED_THRESHOLD = 0.38

#: Minutes within which a stroke pathway is most time-critical.
#:
#: Chosen to reflect that stroke care is measured in minutes, not to encode any
#: particular treatment window. A real deployment takes this from its own
#: pathway definition.
TIME_CRITICAL_MINUTES = 270.0

#: Capillary glucose below this is flagged as a possible stroke mimic.
HYPOGLYCAEMIA_MMOL_L = 3.5


@dataclass(frozen=True)
class StrokeAssessment:
    """One case's triage-support output.

    ``contributions`` is not decoration. A recommendation a clinician cannot
    interrogate is a recommendation they cannot safely act on, so the rule
    reports what each signal added rather than only the total.
    """

    score: float
    priority: str
    contributions: dict[str, float] = field(default_factory=dict)
    basis: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    data_quality_issues: list[str] = field(default_factory=list)
    suppressed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 4),
            "priority": self.priority,
            "contributions": {key: round(value, 4) for key, value in self.contributions.items()},
            "basis": list(self.basis),
            "warnings": list(self.warnings),
            "data_quality_issues": list(self.data_quality_issues),
            "suppressed": self.suppressed,
        }


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in {"true", "false", "yes", "no"}:
        return value.lower() in {"true", "yes"}
    return None


def assess(record: dict[str, Any]) -> StrokeAssessment:
    """Score one canonical stroke-triage record.

    Inputs: a record already mapped into the stroke feature contract. The rule
    does not parse FHIR; ingestion is a separate concern.

    Output: a :class:`StrokeAssessment`. Missing required signals do not produce
    a confident zero — they degrade the score's completeness and are reported as
    data-quality issues, and a record missing every clinical signal is
    suppressed rather than scored, because a recommendation built from nothing
    is worse than no recommendation.
    """
    contributions: dict[str, float] = {}
    basis: list[str] = []
    warnings: list[str] = []
    issues: list[str] = []

    nihss = _as_float(record.get("nihss_total"))
    if nihss is None:
        issues.append("nihss_total is missing")
    else:
        if nihss < 0 or nihss > 42:
            issues.append(f"nihss_total {nihss} is outside the scale's 0-42 range")
            nihss = max(0.0, min(42.0, nihss))
        # The single strongest signal available, so it carries the most weight.
        contributions["nihss_total"] = 0.45 * (nihss / 42.0)
        basis.append(f"NIHSS total recorded as {nihss:g}")

    fast_signals = {
        "face_droop": "facial droop observed",
        "arm_weakness": "unilateral arm weakness observed",
        "speech_difficulty": "new speech disturbance observed",
    }
    present = 0
    for name, description in fast_signals.items():
        flag = _as_bool(record.get(name))
        if flag is None:
            issues.append(f"{name} is missing")
            continue
        if flag:
            present += 1
            basis.append(description)
    if present:
        # Each additional FAST finding raises concern, with all three weighted
        # highest because concurrent findings are the documented warning pattern.
        contributions["fast_findings"] = 0.30 * (present / len(fast_signals))

    minutes = _as_float(record.get("last_known_well_minutes"))
    if minutes is None:
        issues.append("last_known_well_minutes is missing")
    elif minutes < 0:
        issues.append(f"last_known_well_minutes {minutes:g} is negative")
    else:
        # Recency raises priority: the sooner since last known well, the more a
        # fast specialist review matters.
        recency = max(0.0, 1.0 - (minutes / TIME_CRITICAL_MINUTES))
        contributions["recency"] = 0.25 * recency
        basis.append(f"{minutes:g} minutes since last known well")

    glucose = _as_float(record.get("blood_glucose"))
    if glucose is not None and glucose < HYPOGLYCAEMIA_MMOL_L:
        # Flagged, never used to exclude. Ruling a patient out is a clinical
        # decision that triage support has no business making.
        warnings.append(
            f"blood glucose {glucose:g} mmol/L is low; hypoglycaemia is a common stroke mimic "
            "and should be considered by the reviewing clinician"
        )

    if _as_bool(record.get("anticoagulated")):
        warnings.append("patient is recorded as anticoagulated, which is relevant to specialist review")

    systolic = _as_float(record.get("systolic_bp"))
    if systolic is not None and systolic > 220:
        warnings.append(f"systolic blood pressure {systolic:g} mmHg is markedly raised")

    if not contributions:
        # Nothing clinical was available. Suppressing is the honest outcome:
        # a score of zero would read as "low priority" rather than "unknown".
        return StrokeAssessment(
            score=0.0,
            priority="routine",
            contributions={},
            basis=[],
            warnings=warnings,
            data_quality_issues=issues + ["no clinical signal was available; no recommendation was produced"],
            suppressed=True,
        )

    score = min(1.0, sum(contributions.values()))
    if score >= URGENT_THRESHOLD:
        priority = "urgent"
    elif score >= ELEVATED_THRESHOLD:
        priority = "elevated"
    else:
        priority = "routine"

    if issues:
        warnings.append(
            f"{len(issues)} input(s) were missing or invalid; this recommendation is less complete than usual"
        )

    return StrokeAssessment(
        score=score,
        priority=priority,
        contributions=contributions,
        basis=basis,
        warnings=warnings,
        data_quality_issues=issues,
    )


def rank_worklist(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order a worklist by triage priority, highest first.

    Ordering is what this demo actually supports: the queue is re-sorted so an
    urgent case reaches a specialist sooner. Nothing is removed from it, and the
    suppressed cases stay in the list rather than disappearing from view.
    """
    scored = []
    for record in records:
        assessment = assess(record)
        scored.append({**record, "assessment": assessment.as_dict()})
    return sorted(
        scored,
        key=lambda item: (not item["assessment"]["suppressed"], item["assessment"]["score"]),
        reverse=True,
    )


def evaluate_against_labels(
    records: list[dict[str, Any]],
    label_field: str = "confirmed_stroke",
    alert_threshold: float | None = None,
) -> dict[str, float]:
    """Measure the rule against recorded outcomes on a local cohort.

    These are the metrics a site returns from a federated round, so they are
    computed the same way at every site. ``subgroup_auc_gap`` is the widest
    AUC difference across age bands, which is what the fairness release gate
    reads; a candidate that improves overall while harming one band fails it.

    AUC is computed by direct rank comparison rather than by trapezoid
    integration, so a tiny cohort cannot produce a misleadingly smooth curve.

    ``alert_threshold`` sets the score at or above which a case counts as an
    alert. It is a parameter rather than a constant because sensitivity and
    alert burden trade directly against each other, and which point on that
    curve is acceptable is a clinical decision, not a library default.
    """
    threshold = ELEVATED_THRESHOLD if alert_threshold is None else alert_threshold
    labelled = [
        (assess(record).score, bool(record.get(label_field)), str(record.get("age_band", "unknown")))
        for record in records
        if record.get(label_field) is not None
    ]
    if not labelled:
        return {}

    def auc(pairs: list[tuple[float, bool]]) -> float | None:
        positives = [score for score, label in pairs if label]
        negatives = [score for score, label in pairs if not label]
        if not positives or not negatives:
            return None
        wins = sum(
            1.0 if p > n else 0.5 if p == n else 0.0 for p in positives for n in negatives
        )
        return wins / (len(positives) * len(negatives))

    overall = auc([(score, label) for score, label, _ in labelled])
    metrics: dict[str, float] = {}
    if overall is not None:
        metrics["auc"] = overall

    alerts = [score for score, _, _ in labelled if score >= threshold]
    metrics["alerts_per_1000"] = 1000.0 * len(alerts) / len(labelled)

    positives = [(score, label) for score, label, _ in labelled if label]
    if positives:
        metrics["sensitivity"] = sum(1 for score, _ in positives if score >= threshold) / len(positives)

    bands: dict[str, list[tuple[float, bool]]] = {}
    for score, label, band in labelled:
        bands.setdefault(band, []).append((score, label))
    band_aucs = [value for value in (auc(pairs) for pairs in bands.values()) if value is not None]
    if len(band_aucs) >= 2:
        metrics["subgroup_auc_gap"] = max(band_aucs) - min(band_aucs)

    return metrics


def sweep_alert_thresholds(
    records: list[dict[str, Any]],
    label_field: str = "confirmed_stroke",
    thresholds: list[float] | None = None,
) -> list[dict[str, float]]:
    """Report sensitivity and alert burden across candidate alert thresholds.

    A failed sensitivity gate on its own tells a committee the candidate is not
    releasable. It does not tell them what would change that. This sweep does:
    it shows how far the threshold has to move to clear the gate and what that
    costs in alerts, which is the tradeoff a clinical owner actually decides.

    Specificity is reported alongside, because lowering a threshold until
    sensitivity passes is trivially achievable by alerting on everything.
    """
    points = thresholds or [round(0.10 + step * 0.05, 2) for step in range(15)]
    labelled = [
        (assess(record).score, bool(record.get(label_field)))
        for record in records
        if record.get(label_field) is not None
    ]
    if not labelled:
        return []
    positives = [score for score, label in labelled if label]
    negatives = [score for score, label in labelled if not label]

    sweep: list[dict[str, float]] = []
    for threshold in points:
        alerts = sum(1 for score, _ in labelled if score >= threshold)
        row = {
            "threshold": threshold,
            "alerts_per_1000": 1000.0 * alerts / len(labelled),
        }
        if positives:
            row["sensitivity"] = sum(1 for score in positives if score >= threshold) / len(positives)
        if negatives:
            row["specificity"] = sum(1 for score in negatives if score < threshold) / len(negatives)
        sweep.append(row)
    return sweep
