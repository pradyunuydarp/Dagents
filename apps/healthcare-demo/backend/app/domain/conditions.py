"""Clinical condition definitions for the stroke-triage demo.

This module is the demo app's business logic and deliberately does not belong in
Dagents. The framework knows how to validate a feature contract, plan a round,
and govern an egress; it must not know what a NIHSS score is or which arrival
window matters for a stroke pathway. That knowledge lives here.

**One platform, separate clinical applications.** A consortium reuses the
coordination platform across conditions. It must not reuse the clinical
application: every condition needs its own cohort definition, approved fields,
rule or model, thresholds, clinician workflow, owner, and safety evidence. Each
:class:`~agents.common.extensions.ConditionPack` below carries its own, and only
suspected stroke is built out, because only suspected stroke has been worked
through. The others are declared as planned so the boundary is visible rather
than implied.

Nothing here is clinically validated. The stroke rule in ``stroke_rule`` is a
transparent demonstration of where a model or ruleset plugs in, not a triage
model anyone should use.
"""

from __future__ import annotations

from agents.common.domain.federation import GateComparison, ReleaseGate
from agents.common.domain.governance import DataClassification
from agents.common.extensions import ConditionPack, FeatureContract, FeatureField

#: The version every participating hospital must agree on before a round.
#:
#: Bumping this is a governance act, not a refactor: a site on a different
#: version is not producing comparable data however similar its field names look,
#: and the framework will exclude it from a round rather than silently mix it in.
STROKE_FEATURE_CONTRACT_VERSION = "stroke-triage-features-v2"

STROKE_FEATURE_CONTRACT = FeatureContract(
    contract_id="stroke-triage-features",
    version=STROKE_FEATURE_CONTRACT_VERSION,
    description=(
        "Fields a hospital agrees to derive locally for suspected-stroke triage support. "
        "Units and terminology are part of the agreement because the same field name "
        "means different things at different sources."
    ),
    fields=[
        FeatureField(
            name="nihss_total",
            dtype="float",
            unit="points",
            terminology="NIHSS",
            description="Total NIH Stroke Scale score recorded at assessment, 0-42.",
        ),
        FeatureField(
            name="face_droop",
            dtype="bool",
            terminology="FAST",
            description="Facial droop observed on examination.",
        ),
        FeatureField(
            name="arm_weakness",
            dtype="bool",
            terminology="FAST",
            description="Unilateral arm weakness or drift observed on examination.",
        ),
        FeatureField(
            name="speech_difficulty",
            dtype="bool",
            terminology="FAST",
            description="New speech disturbance observed on examination.",
        ),
        FeatureField(
            name="last_known_well_minutes",
            dtype="float",
            unit="minutes",
            description="Minutes between last known well and emergency arrival.",
        ),
        FeatureField(
            name="age_band",
            dtype="string",
            description="Ten-year age band. Bands rather than ages, by design.",
        ),
        FeatureField(
            name="systolic_bp",
            dtype="float",
            unit="mmHg",
            required=False,
            description="Systolic blood pressure at arrival.",
        ),
        FeatureField(
            name="blood_glucose",
            dtype="float",
            unit="mmol/L",
            required=False,
            description="Capillary glucose, which rules out a common stroke mimic.",
        ),
        FeatureField(
            name="arrival_mode",
            dtype="string",
            required=False,
            description="How the patient arrived, for example ambulance or walk-in.",
        ),
        FeatureField(
            name="anticoagulated",
            dtype="bool",
            required=False,
            description="Whether the patient is on anticoagulant therapy.",
        ),
        FeatureField(
            name="confirmed_stroke",
            dtype="bool",
            required=False,
            description=(
                "Retrospective outcome label. Part of the contract because an evaluation "
                "round cannot measure anything without it, optional because a live or "
                "training round must never read it."
            ),
        ),
    ],
)

#: What the Ethical-Restriction Rails read when a request touches stroke data.
#:
#: This is policy, not code. Changing a field's sensitivity or the minimum cohort
#: does not change a single code path: the Rails re-derive the strategy and the
#: Guard enforces whatever comes back.
#:
#: The minimum cohort of 20 is a demonstration value. A real consortium sets it
#: from a disclosure-risk analysis, not from a framework default.
STROKE_CLASSIFICATION = DataClassification(
    classification_id="stroke-triage-phi",
    field_sensitivity={
        # Clinical measurements that are identifying in combination.
        "nihss_total": "high",
        "last_known_well_minutes": "high",
        "systolic_bp": "high",
        "blood_glucose": "high",
        "face_droop": "medium",
        "arm_weakness": "medium",
        "speech_difficulty": "medium",
        "anticoagulated": "medium",
        "age_band": "medium",
        # The outcome label is as identifying as any diagnosis, and knowing it
        # about one patient is knowing their clinical history.
        "confirmed_stroke": "high",
        # Operational context that carries little patient signal on its own.
        "arrival_mode": "low",
        # The federated egress channel. High by construction: an update derived
        # from these records carries their signal even though it is not a row.
        "model_update": "high",
    },
    default_sensitivity="high",
    regulations=["HIPAA Security Rule", "HIPAA Privacy Rule", "HIPAA Minimum Necessary"],
    minimum_cohort=20,
)

#: The purposes a stroke request may declare. Anything else is refused.
STROKE_APPROVED_PURPOSES = ["suspected_stroke", "stroke_triage_research", "stroke_quality_review"]

#: What a candidate stroke model must clear before anyone may release it.
#:
#: ``subgroup_auc_gap`` is here because a model that improves overall while
#: harming one group is a failure, and because a gate whose metric the candidate
#: never reported blocks rather than silently passing. Omitting the measurement
#: is not a way to pass the gate.
STROKE_RELEASE_GATES = [
    ReleaseGate(
        gate_id="discrimination",
        metric="auc",
        comparison=GateComparison(kind="at_least", value=0.80),
        blocking=True,
    ),
    ReleaseGate(
        gate_id="improves_on_current_release",
        metric="auc",
        comparison=GateComparison(kind="improves_on_baseline", margin=0.01),
        blocking=True,
    ),
    ReleaseGate(
        gate_id="subgroup_fairness",
        metric="subgroup_auc_gap",
        comparison=GateComparison(kind="at_most", value=0.05),
        blocking=True,
    ),
    ReleaseGate(
        gate_id="sensitivity_at_alert_budget",
        metric="sensitivity",
        comparison=GateComparison(kind="at_least", value=0.70),
        blocking=True,
    ),
    # Calibrated to a suspected-stroke cohort, which has already been filtered
    # by a clinician, not to a general emergency population. A budget set for
    # the latter would fail every round here and teach nobody anything.
    ReleaseGate(
        gate_id="alert_burden",
        metric="alerts_per_1000",
        comparison=GateComparison(kind="at_most", value=400.0),
        blocking=False,
    ),
]

STROKE_CONDITION = ConditionPack(
    condition_id="suspected_stroke",
    display_name="Suspected stroke",
    intended_use=(
        "Prioritize suspected-stroke brain scans for faster specialist review in an emergency "
        "pathway. Decision support only: it does not diagnose stroke, does not choose treatment, "
        "and does not remove any case from the normal work queue. A clinician reviews every "
        "recommendation and retains the decision."
    ),
    feature_contract_id="stroke-triage-features",
    classification_id="stroke-triage-phi",
    cohort_description=(
        "Adults presenting to the emergency department with suspected stroke symptoms, "
        "within the local pathway's assessment window."
    ),
    release_gates=STROKE_RELEASE_GATES,
    limitations=[
        "The scoring rule in this demo is a transparent illustration, not a validated triage model.",
        "All patient data in this demo is synthetic. No real record is used or required.",
        "No external or site-specific clinical validation has been performed.",
        "Retrospective or silent-live use only; no clinician-facing alerting without a separate protocol.",
        "Stroke mimics such as hypoglycaemia are flagged, not excluded.",
    ],
    owner="demo-stroke-pathway-lead",
)

#: Conditions the platform is designed to carry but which are not built.
#:
#: They are declared rather than omitted because the honest claim is narrow: the
#: coordination platform is reusable, each clinical application is not. Naming
#: what has not been done keeps that visible instead of letting a reader assume
#: the platform ships five working applications.
PLANNED_CONDITIONS = [
    {
        "condition_id": "suspected_myocardial_infarction",
        "display_name": "Heart attack",
        "status": "not implemented",
        "needs": "Its own ECG and troponin feature contract, model, thresholds, workflow, and safety case.",
    },
    {
        "condition_id": "acute_kidney_injury",
        "display_name": "Sudden kidney injury",
        "status": "not implemented",
        "needs": "Creatinine and urine-output trend rules with their own validation and baseline.",
    },
    {
        "condition_id": "respiratory_decline",
        "display_name": "Serious breathing decline",
        "status": "not implemented",
        "needs": "Oxygenation and support-device monitoring with its own escalation workflow.",
    },
    {
        "condition_id": "diabetic_emergency",
        "display_name": "Diabetic emergency",
        "status": "not implemented",
        "needs": "Glucose and ketone thresholds with their own clinician response plan.",
    },
]
