"""Mapping hospital-shaped source data into the stroke feature contract.

Real hospital integration means FHIR resources, HL7 v2 feeds, relational
extracts, and device streams, each with its own units, code systems, and local
conventions. That work is genuinely hard and genuinely site-specific, and it is
exactly the kind of thing Dagents must not contain.

What this module does is the demo-scale version of it: take FHIR-shaped
``Observation`` and ``Encounter`` payloads and produce canonical records matching
the stroke feature contract, recording what it could not map instead of quietly
dropping it. The point is to show the seam, not to be a conformant FHIR client.

Not implemented, and named so nobody assumes otherwise: terminology-server
lookups, USCDI conformance, patient matching across sources, HL7 v2 parsing, and
unit conversion beyond the handful below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: LOINC codes this mapper recognizes, and the contract field each becomes.
#:
#: A real deployment resolves these through a terminology service. Hardcoding a
#: handful is honest for a demo and dishonest as a production strategy, which is
#: why the unmapped ones are reported rather than ignored.
LOINC_TO_FIELD = {
    "70182-1": "nihss_total",
    "8480-6": "systolic_bp",
    "2339-0": "blood_glucose",
    "72514-3": "pain_score",
}

#: Unit conversions the mapper performs, keyed by (field, source unit).
UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    # Glucose is reported in mg/dL in some systems and mmol/L in others. Mixing
    # them silently would put a normal reading in the hypoglycaemia band.
    ("blood_glucose", "mg/dL"): 1 / 18.0,
    ("blood_glucose", "mg/dl"): 1 / 18.0,
    ("last_known_well_minutes", "h"): 60.0,
    ("last_known_well_minutes", "hours"): 60.0,
}

#: The canonical unit each convertible field is normalized to.
CANONICAL_UNITS = {"blood_glucose": "mmol/L", "last_known_well_minutes": "min", "systolic_bp": "mmHg"}


@dataclass
class MappingResult:
    """One mapped record plus everything the mapper could not account for.

    ``unmapped`` and ``issues`` exist so a source that changed shape shows up as
    a reported gap rather than as a record that simply scores lower.
    """

    record: dict[str, Any] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"record": self.record, "unmapped": self.unmapped, "issues": self.issues}


def _age_band(age: Any) -> str | None:
    """Reduce an age to a ten-year band.

    Bands rather than ages, at the point of ingestion. An exact age is more
    identifying than the triage rule needs, and the cheapest way not to disclose
    a field is never to derive it.
    """
    try:
        value = int(age)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > 120:
        return None
    if value >= 90:
        return "90+"
    lower = (value // 10) * 10
    return f"{lower}-{lower + 9}"


def _observation_value(observation: dict[str, Any]) -> tuple[Any, str | None]:
    """Pull a value and unit out of a FHIR-shaped observation."""
    quantity = observation.get("valueQuantity")
    if isinstance(quantity, dict):
        return quantity.get("value"), quantity.get("unit")
    if "valueBoolean" in observation:
        return observation["valueBoolean"], None
    if "valueString" in observation:
        return observation["valueString"], None
    if "valueInteger" in observation:
        return observation["valueInteger"], None
    return None, None


def _observation_code(observation: dict[str, Any]) -> str | None:
    """Pull the first coding code out of a FHIR-shaped observation."""
    coding = (observation.get("code") or {}).get("coding") or []
    for entry in coding:
        if isinstance(entry, dict) and entry.get("code"):
            return str(entry["code"])
    return None


def map_encounter(encounter: dict[str, Any], observations: list[dict[str, Any]]) -> MappingResult:
    """Map one encounter and its observations into a canonical record.

    Inputs:
    - ``encounter``: a FHIR-shaped Encounter with demographics and arrival data.
    - ``observations``: FHIR-shaped Observations recorded during it.

    Output: a :class:`MappingResult` carrying the canonical record, the codes
    that could not be mapped, and any conversion or range problems found.
    """
    result = MappingResult()
    record: dict[str, Any] = {}

    band = _age_band(encounter.get("age"))
    if band:
        record["age_band"] = band
    else:
        result.issues.append("age could not be reduced to a band")

    if encounter.get("arrivalMode"):
        record["arrival_mode"] = str(encounter["arrivalMode"])

    minutes = encounter.get("minutesSinceLastKnownWell")
    if minutes is not None:
        record["last_known_well_minutes"] = float(minutes)
    elif encounter.get("hoursSinceLastKnownWell") is not None:
        record["last_known_well_minutes"] = float(encounter["hoursSinceLastKnownWell"]) * 60.0

    for name in ("faceDroop", "armWeakness", "speechDifficulty", "anticoagulated"):
        if name in encounter:
            snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in name).lstrip("_")
            record[snake] = bool(encounter[name])

    for observation in observations:
        code = _observation_code(observation)
        if code is None:
            result.issues.append("observation has no coding and was skipped")
            continue
        target = LOINC_TO_FIELD.get(code)
        if target is None:
            # Reported, not dropped. A code this mapper does not know may be the
            # one a site relies on, and silence would hide that.
            result.unmapped.append(code)
            continue
        value, unit = _observation_value(observation)
        if value is None:
            result.issues.append(f"observation {code} carried no readable value")
            continue
        if unit and (target, unit) in UNIT_CONVERSIONS:
            value = float(value) * UNIT_CONVERSIONS[(target, unit)]
        elif unit and target in CANONICAL_UNITS and unit != CANONICAL_UNITS[target]:
            result.issues.append(
                f"observation {code} reported unit {unit}, expected {CANONICAL_UNITS[target]}; "
                "value was left unconverted"
            )
        record[target] = value

    # Fields the contract requires but the source did not supply. Naming them
    # here is what lets the framework's schema validation reject the record
    # instead of a scorer silently treating absence as a negative finding.
    for required in ("nihss_total", "face_droop", "arm_weakness", "speech_difficulty", "last_known_well_minutes"):
        if required not in record:
            result.issues.append(f"required contract field {required} was not present in the source")

    result.record = record
    return result


def map_bundle(bundle: dict[str, Any]) -> list[MappingResult]:
    """Map a FHIR-shaped bundle of encounters into canonical records."""
    results: list[MappingResult] = []
    for entry in bundle.get("entry", []):
        resource = entry.get("resource") or {}
        if resource.get("resourceType") != "Encounter":
            continue
        observations = [
            item.get("resource", {})
            for item in bundle.get("entry", [])
            if (item.get("resource") or {}).get("resourceType") == "Observation"
            and (item.get("resource") or {}).get("encounter", {}).get("reference")
            == f"Encounter/{resource.get('id')}"
        ]
        results.append(map_encounter(resource, observations))
    return results
