"""Clinical condition definitions for the stroke demo.

This module is the demo app's own logic and does not belong in Dagents. The
framework validates a feature contract, plans a round and governs what leaves a
site. It does not know what a stroke subtype is or which outcome matters. That
knowledge lives here.

**One platform, separate clinical applications.** A consortium can reuse the
coordination platform across conditions. It cannot reuse the clinical
application: every condition needs its own cohort definition, approved fields,
rule or model, thresholds, clinician workflow, owner and safety evidence. Each
:class:`~agents.common.extensions.ConditionPack` carries its own. Only suspected
stroke is built; the others are listed as planned.

Nothing here is clinically validated. The seed rule in ``stroke_rule`` and the
federated model in ``stroke_model`` show where a condition's model plugs in.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.common.domain.federation import GateComparison, ReleaseGate
from agents.common.domain.governance import DataClassification
from agents.common.extensions import ConditionPack, FeatureContract, FeatureField

from app.domain.dataset import AGE_BANDS, AGE_MAX, AGE_MIN

#: The version every participating hospital must agree on before a round.
#:
#: Changing it is a governance decision. A site on a different version does not
#: produce comparable data, and the framework excludes it from a round.
STROKE_FEATURE_CONTRACT_VERSION = "stroke-triage-features-v3"

#: The outcome both models estimate: death or dependency at six months.
STROKE_LABEL_FIELD = "poor_outcome_6m"

#: The field the fairness gate compares groups over.
STROKE_FAIRNESS_SUBGROUP = "sex"

STROKE_FEATURE_CONTRACT = FeatureContract(
    contract_id="stroke-triage-features",
    version=STROKE_FEATURE_CONTRACT_VERSION,
    description=(
        "Fields a hospital agrees to derive locally for prognostic stroke triage. The fields "
        "follow the International Stroke Trial's baseline form. Units and codes are part of "
        "the agreement, because the same field name can mean different things at different sites."
    ),
    fields=[
        FeatureField(
            name="age_band",
            dtype="string",
            description="Age band: <50, 50-59, 60-69, 70-79, 80-89 or 90+. The exact age stays at the site.",
        ),
        FeatureField(name="sex", dtype="string", description="female or male."),
        FeatureField(
            name="conscious_level",
            dtype="string",
            description="Conscious level at assessment: alert, drowsy or unconscious.",
        ),
        FeatureField(
            name="face_weakness",
            dtype="bool",
            description="Face weakness on examination. Missing when it could not be assessed.",
        ),
        FeatureField(
            name="arm_weakness",
            dtype="bool",
            description="Arm or hand weakness on examination. Missing when it could not be assessed.",
        ),
        FeatureField(
            name="leg_weakness",
            dtype="bool",
            description="Leg or foot weakness on examination. Missing when it could not be assessed.",
        ),
        FeatureField(
            name="speech_difficulty",
            dtype="bool",
            description="Dysphasia (difficulty with language) on examination. Missing when it could not be assessed.",
        ),
        FeatureField(
            name="hours_since_onset",
            dtype="float",
            unit="hours",
            description="Hours from symptom onset to assessment, 0-48. In the IST records, hours to randomisation.",
        ),
        FeatureField(
            name="systolic_bp",
            dtype="float",
            unit="mmHg",
            description="Systolic blood pressure at assessment, 60-300 mmHg.",
        ),
        FeatureField(
            name="visual_field_loss",
            dtype="bool",
            required=False,
            description="Hemianopia (loss of half the visual field). Missing when it could not be assessed.",
        ),
        FeatureField(
            name="atrial_fibrillation",
            dtype="bool",
            required=False,
            description="Atrial fibrillation at assessment.",
        ),
        FeatureField(
            name="onset_on_waking",
            dtype="bool",
            required=False,
            description="Whether the symptoms were first noticed on waking.",
        ),
        FeatureField(
            name="stroke_subtype",
            dtype="string",
            required=False,
            terminology="OCSP",
            description="Oxfordshire Community Stroke Project subtype: TACS, PACS, LACS, POCS or other.",
        ),
        FeatureField(
            name=STROKE_LABEL_FIELD,
            dtype="bool",
            required=False,
            description=(
                "Outcome label: dead or dependent at six months. It is in the contract because "
                "evaluation and training need it. It is optional because it is known only six "
                "months later, and the label never leaves the site."
            ),
        ),
    ],
)


@dataclass(frozen=True)
class FieldDomain:
    """How a record field is shown, entered and checked.

    The feature contract states names, types and units. This adds what a form
    and a validator need: a label, the allowed values or range, and help text.
    ``dtype`` is set only for a field outside the contract (``age``); a
    contract field takes the contract's type.
    """

    label: str
    options: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    unit: str | None = None
    help: str = ""
    dtype: str | None = None


#: Help for a finding the form offers as yes, no or "Not assessed".
NOT_ASSESSED_HELP = "Choose Not assessed if it could not be assessed."

#: Allowed values for every record field a hospital enters, plus the derived
#: ``age_band`` and the outcome label. ``age`` is not in the contract: it stays
#: at the site, and the site derives ``age_band`` from it.
STROKE_FIELD_DOMAINS: dict[str, FieldDomain] = {
    "age": FieldDomain(
        label="Age",
        minimum=AGE_MIN,
        maximum=AGE_MAX,
        unit="years",
        help="Stays at this hospital. Only the age band is shared.",
        dtype="int",
    ),
    "age_band": FieldDomain(label="Age band", options=AGE_BANDS, help="Derived from the age."),
    "sex": FieldDomain(label="Sex", options=("female", "male")),
    "conscious_level": FieldDomain(label="Conscious level", options=("alert", "drowsy", "unconscious")),
    "face_weakness": FieldDomain(label="Face weakness", help=NOT_ASSESSED_HELP),
    "arm_weakness": FieldDomain(label="Arm or hand weakness", help=NOT_ASSESSED_HELP),
    "leg_weakness": FieldDomain(label="Leg or foot weakness", help=NOT_ASSESSED_HELP),
    "speech_difficulty": FieldDomain(
        label="Speech difficulty (dysphasia)", help=NOT_ASSESSED_HELP
    ),
    "hours_since_onset": FieldDomain(
        label="Time since symptom onset", minimum=0, maximum=48, unit="hours"
    ),
    "systolic_bp": FieldDomain(label="Systolic blood pressure", minimum=60, maximum=300, unit="mmHg"),
    "visual_field_loss": FieldDomain(
        label="Visual field loss (hemianopia)", help=NOT_ASSESSED_HELP
    ),
    "atrial_fibrillation": FieldDomain(label="Atrial fibrillation"),
    "onset_on_waking": FieldDomain(label="Symptoms noticed on waking"),
    "stroke_subtype": FieldDomain(
        label="Stroke subtype (OCSP)",
        options=("TACS", "PACS", "LACS", "POCS", "other"),
        help="Total anterior, partial anterior, lacunar or posterior circulation stroke.",
    ),
    STROKE_LABEL_FIELD: FieldDomain(
        label="Dead or dependent at six months",
        help="Recorded six months after the stroke, not at entry.",
    ),
}

#: The fields a hospital enters for a new patient, in form order. ``age_band``
#: is derived from ``age``, and the outcome is recorded later.
STROKE_ENTRY_FIELDS: tuple[str, ...] = (
    "age",
    "sex",
    "conscious_level",
    "face_weakness",
    "arm_weakness",
    "leg_weakness",
    "speech_difficulty",
    "hours_since_onset",
    "systolic_bp",
    "visual_field_loss",
    "atrial_fibrillation",
    "onset_on_waking",
    "stroke_subtype",
)

#: Fields the guard probe asks for by default: one high, one medium and one low
#: sensitivity field, so each of the probe's controls changes something visible.
GUARD_PROBE_DEFAULT_FIELDS: tuple[str, ...] = ("systolic_bp", "age_band", "onset_on_waking")

#: What the Ethical-Restriction Rails read when a request touches stroke data.
#:
#: This is policy, not code. Changing a field's sensitivity or the minimum
#: cohort changes no code path: the Rails derive the strategy again and the
#: Guard enforces it.
#:
#: The minimum cohort of 20 is a demonstration value. A real consortium sets it
#: from a disclosure-risk analysis.
STROKE_CLASSIFICATION = DataClassification(
    classification_id="stroke-triage-phi",
    field_sensitivity={
        # Exact measurements identify a patient when combined with other fields.
        "hours_since_onset": "high",
        "systolic_bp": "high",
        # A diagnosis in the patient's history.
        "atrial_fibrillation": "high",
        # The outcome is as sensitive as any diagnosis.
        STROKE_LABEL_FIELD: "high",
        # Examination findings and coarse demographics.
        "age_band": "medium",
        "sex": "medium",
        "conscious_level": "medium",
        "face_weakness": "medium",
        "arm_weakness": "medium",
        "leg_weakness": "medium",
        "speech_difficulty": "medium",
        "visual_field_loss": "medium",
        "stroke_subtype": "medium",
        # Says little about a patient on its own.
        "onset_on_waking": "low",
        # The federated update. It is derived from the records and carries their
        # signal, even though it is not a row.
        "model_update": "high",
    },
    default_sensitivity="high",
    # The hospitals are in the United Kingdom, Italy and Switzerland, so the
    # law that applies is each one's own data protection law, not a US law.
    regulations=["each hospital's national data protection law"],
    minimum_cohort=20,
)

#: The purposes a stroke request may declare. Anything else is refused.
STROKE_APPROVED_PURPOSES = ["suspected_stroke", "stroke_triage_research", "stroke_quality_review"]

#: The study the hospitals take part in. Rounds and reports name it.
STROKE_STUDY_ID = "stroke-triage-pilot"

#: What a candidate must clear before anyone may release it.
#:
#: Each threshold comes from the reason written next to it, not from the
#: decision it produces, and the pilot reports whatever the gates decide. The
#: metrics come from ``stroke_rule.evaluate_against_labels``, which measures
#: every model the same way. A gate whose metric is missing blocks the release.
STROKE_RELEASE_GATES = [
    # 0.70 is the usual lower bound for acceptable discrimination. Below it a
    # model orders the queue too poorly to help. Published prognostic models
    # built from similar bedside fields reach about 0.75-0.80.
    ReleaseGate(
        gate_id="discrimination",
        metric="auc",
        comparison=GateComparison(kind="at_least", value=0.70),
        blocking=True,
    ),
    # A replacement must be clearly better than the current release, for two
    # reasons. The candidate is measured on the records it was trained on, which
    # flatters it: on these fields a logistic regression scores about 0.02
    # higher on its own training records than on records it has not seen. And
    # at these hospital sizes, the AUC difference between two models varies by
    # about 0.01 from sampling alone. A gain below 0.02 is within both.
    ReleaseGate(
        gate_id="improves_on_current_release",
        metric="auc",
        comparison=GateComparison(kind="improves_on_baseline", margin=0.02),
        blocking=True,
    ),
    # The gap between the AUC for female and for male patients. With 100-500
    # patients per hospital, sampling alone produces gaps of about 0.04-0.06,
    # so a limit of 0.05 would block fair models by chance. A gap above 0.10,
    # about twice that, is unlikely to be chance alone.
    ReleaseGate(
        gate_id="subgroup_fairness",
        metric="subgroup_auc_gap",
        comparison=GateComparison(kind="at_most", value=0.10),
        blocking=True,
    ),
    # Sensitivity at the alert threshold (an estimated risk of 0.5). An alert
    # moves a patient up the specialist's queue and removes nobody from it, so a
    # missed high-risk patient is the costlier error. The alert must catch at
    # least four in five of the patients who die or become dependent.
    ReleaseGate(
        gate_id="sensitivity_at_alert_threshold",
        metric="sensitivity",
        comparison=GateComparison(kind="at_least", value=0.80),
        blocking=True,
    ),
    # An alert that fires for nearly everyone does not change the order of the
    # queue. The limit asks that at least one patient in four is not flagged.
    # It is advisory, not blocking: most patients in this cohort have a poor
    # outcome, so a high alert rate is expected and is for the committee to
    # weigh.
    ReleaseGate(
        gate_id="alert_burden",
        metric="alerts_per_1000",
        comparison=GateComparison(kind="at_most", value=750.0),
        blocking=False,
    ),
]

STROKE_CONDITION = ConditionPack(
    condition_id="suspected_stroke",
    display_name="Suspected stroke",
    intended_use=(
        "Flag patients with suspected acute stroke who are at high risk of death or dependency at "
        "six months, so a specialist reviews them first. Decision support only: it does not diagnose "
        "stroke, does not choose treatment, and does not remove any case from the normal work queue. "
        "A clinician reviews every recommendation and retains the decision."
    ),
    feature_contract_id="stroke-triage-features",
    classification_id="stroke-triage-phi",
    cohort_description=(
        "Adults with a suspected acute stroke, assessed within 48 hours of symptom onset. The demo's "
        "records come from three hospitals in the International Stroke Trial."
    ),
    release_gates=STROKE_RELEASE_GATES,
    limitations=[
        "The records are real, de-identified patient records from the International Stroke Trial, "
        "recruited in 1991-1996. Stroke care has changed since then, so outcomes today differ.",
        "The trial enrolled patients with a clinical diagnosis of acute ischaemic stroke within 48 hours "
        "of onset and no clear reason for or against aspirin or heparin. That is a narrower group than "
        "everyone seen with a suspected stroke.",
        "The seed rule's points were chosen by hand as an illustration. They were not fitted or validated.",
        "The candidate model is trained only in the demo's federated rounds, on three hospitals' records. "
        "No model here has been validated on other patients.",
        "This is a demonstration, not a medical device. Do not use it for patient care.",
    ],
    owner="demo-stroke-pathway-lead",
)

#: Conditions the platform could carry but which are not built.
#:
#: They are listed so a reader can see the boundary: the coordination platform
#: is reusable, and each clinical application needs its own work.
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
