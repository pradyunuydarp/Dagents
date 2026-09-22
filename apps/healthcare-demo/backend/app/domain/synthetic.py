"""Synthetic patient generation for the stroke-triage demo.

**Every patient in this demo is generated here. No real record is used, and none
is required to run anything.** That is a design constraint, not a convenience:
a demonstration of privacy controls that shipped with real patient data would be
self-refuting.

Each simulated hospital gets its own generator seed and its own distribution
parameters, because the interesting behaviour only appears when sites differ.
A consortium where every site looks identical would never exercise the drift
checks, the per-site cohort floors, or the subgroup fairness gate.
"""

from __future__ import annotations

import random
from typing import Any

AGE_BANDS = ["40-49", "50-59", "60-69", "70-79", "80-89", "90+"]
ARRIVAL_MODES = ["ambulance", "walk_in", "transfer"]


class HospitalProfile:
    """How one simulated hospital's population and recording practice differ.

    ``missingness`` is the important one. A site that records everything is not
    a realistic site, and a demo where nothing is ever missing never shows the
    data-quality path working.
    """

    def __init__(
        self,
        site_id: str,
        display_name: str,
        seed: int,
        stroke_rate: float = 0.28,
        severity_bias: float = 0.0,
        missingness: float = 0.05,
        older_population: bool = False,
        glucose_unit_error_rate: float = 0.0,
    ) -> None:
        self.site_id = site_id
        self.display_name = display_name
        self.seed = seed
        self.stroke_rate = stroke_rate
        self.severity_bias = severity_bias
        self.missingness = missingness
        self.older_population = older_population
        self.glucose_unit_error_rate = glucose_unit_error_rate


#: The three hospitals in the demo consortium.
#:
#: They differ on purpose. Riverside sees an older, sicker population; Northgate
#: records less completely; Lakeside is small enough to sit near the per-site
#: cohort floor, which is what makes the quorum rules visible.
DEFAULT_HOSPITALS = [
    HospitalProfile(
        site_id="riverside-general",
        display_name="Riverside General",
        seed=11,
        stroke_rate=0.34,
        severity_bias=2.5,
        missingness=0.04,
        older_population=True,
    ),
    HospitalProfile(
        site_id="northgate-university",
        display_name="Northgate University Hospital",
        seed=23,
        stroke_rate=0.26,
        severity_bias=0.0,
        missingness=0.14,
    ),
    HospitalProfile(
        site_id="lakeside-community",
        display_name="Lakeside Community Hospital",
        seed=37,
        stroke_rate=0.21,
        severity_bias=-1.5,
        missingness=0.08,
        glucose_unit_error_rate=0.10,
    ),
]


def generate_cohort(profile: HospitalProfile, size: int) -> list[dict[str, Any]]:
    """Generate one hospital's synthetic suspected-stroke cohort.

    The generator is seeded per site, so a demo is reproducible: the same site
    produces the same cohort on every run, and a reviewer can re-derive any
    number the app reports.

    ``confirmed_stroke`` is the retrospective outcome label. It is generated so
    the demo has something to evaluate against; it is not a claim that the rule
    predicts anything about real patients. Any AUC this cohort yields measures
    how the generator was written, not how stroke triage behaves.
    """
    rng = random.Random(profile.seed)
    cohort: list[dict[str, Any]] = []

    for index in range(size):
        confirmed = rng.random() < profile.stroke_rate

        # Confirmed cases draw a higher severity, so the rule has something to
        # separate. The distributions overlap substantially on purpose: an
        # easily separable synthetic cohort would let the demo report an AUC
        # near 1.0, which would say something flattering about the generator
        # and nothing at all about stroke triage.
        if confirmed:
            nihss = max(0.0, min(42.0, rng.gauss(12.0 + profile.severity_bias, 7.5)))
            fast_probability = 0.66
            minutes = max(5.0, rng.gauss(140.0, 95.0))
        else:
            nihss = max(0.0, min(42.0, rng.gauss(7.0 + profile.severity_bias * 0.3, 6.0)))
            fast_probability = 0.34
            minutes = max(5.0, rng.gauss(210.0, 130.0))

        bands = AGE_BANDS[2:] if profile.older_population else AGE_BANDS
        record: dict[str, Any] = {
            "encounter_id": f"{profile.site_id}-{index:04d}",
            "age_band": rng.choice(bands),
            "arrival_mode": rng.choices(ARRIVAL_MODES, weights=[0.6, 0.3, 0.1])[0],
            "nihss_total": round(nihss, 1),
            "face_droop": rng.random() < fast_probability,
            "arm_weakness": rng.random() < fast_probability,
            "speech_difficulty": rng.random() < fast_probability * 0.9,
            "last_known_well_minutes": round(minutes, 1),
            "systolic_bp": round(max(80.0, rng.gauss(158.0, 24.0)), 1),
            "blood_glucose": round(max(1.8, rng.gauss(6.4, 1.6)), 2),
            "anticoagulated": rng.random() < 0.18,
            "confirmed_stroke": confirmed,
        }

        # A site that reports glucose in mg/dL without saying so. The ingestion
        # mapper catches this when it comes through FHIR with a unit; here it is
        # left in to show what an unconverted value looks like downstream.
        if rng.random() < profile.glucose_unit_error_rate:
            record["blood_glucose"] = round(record["blood_glucose"] * 18.0, 1)
            record["blood_glucose_unit"] = "mg/dL"

        # Real records have gaps. Required fields are dropped too, because the
        # contract check exists to catch exactly that.
        for field in ("nihss_total", "face_droop", "speech_difficulty", "systolic_bp", "blood_glucose"):
            if rng.random() < profile.missingness:
                record.pop(field, None)

        cohort.append(record)

    return cohort


def as_fhir_bundle(profile: HospitalProfile, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Re-express synthetic records as a FHIR-shaped bundle.

    Used to exercise the ingestion mapper on the same data the rest of the demo
    uses, so the mapping path is tested against something rather than mocked.
    """
    entries: list[dict[str, Any]] = []
    for record in records:
        encounter_id = record["encounter_id"]
        entries.append(
            {
                "resource": {
                    "resourceType": "Encounter",
                    "id": encounter_id,
                    "age": _age_from_band(record.get("age_band")),
                    "arrivalMode": record.get("arrival_mode"),
                    "minutesSinceLastKnownWell": record.get("last_known_well_minutes"),
                    "faceDroop": record.get("face_droop"),
                    "armWeakness": record.get("arm_weakness"),
                    "speechDifficulty": record.get("speech_difficulty"),
                    "anticoagulated": record.get("anticoagulated"),
                }
            }
        )
        for code, field, unit in (
            ("70182-1", "nihss_total", "{score}"),
            ("8480-6", "systolic_bp", "mmHg"),
            ("2339-0", "blood_glucose", record.get("blood_glucose_unit", "mmol/L")),
        ):
            if record.get(field) is None:
                continue
            entries.append(
                {
                    "resource": {
                        "resourceType": "Observation",
                        "encounter": {"reference": f"Encounter/{encounter_id}"},
                        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
                        "valueQuantity": {"value": record[field], "unit": unit},
                    }
                }
            )
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def _age_from_band(band: str | None) -> int | None:
    """Pick a representative age inside a band, for round-tripping to FHIR."""
    if not band:
        return None
    if band == "90+":
        return 92
    try:
        return int(band.split("-")[0]) + 5
    except (ValueError, IndexError):
        return None
