"""Mapping FHIR-shaped hospital data onto the stroke feature contract (v3).

Real hospital integration means FHIR resources, HL7 v2 feeds, relational
extracts and device streams, each with its own units, code systems and local
conventions. That work is specific to each site, and it does not belong in
Dagents.

This module is the demo-scale version of it. It takes FHIR-shaped
``Encounter`` and ``Observation`` payloads and produces records in the v3
contract. It reports what it could not map instead of dropping it. It is not a
conformant FHIR client.

Coded observations map only where a standard code exists and is known here:
LOINC 8480-6, systolic blood pressure. The examination findings, conscious
level and stroke subtype come from the demo's Encounter fields, because this
module does not hold a terminology for them.

Not implemented: terminology-server lookups, USCDI conformance, patient matching
across sources, HL7 v2 parsing, and unit conversion beyond the table below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.domain.conditions import STROKE_FEATURE_CONTRACT, STROKE_FIELD_DOMAINS
from app.domain.dataset import age_band_for

#: LOINC codes this mapper recognizes, and the contract field each becomes.
#:
#: A real deployment resolves codes through a terminology service. A code not
#: listed here is reported in ``unmapped``.
LOINC_TO_FIELD = {
    "8480-6": "systolic_bp",
}

#: The canonical unit of each coded field, and the unit spellings accepted as it.
#: ``mm[Hg]`` is the UCUM code FHIR systems often send.
CANONICAL_UNITS = {"systolic_bp": "mmHg"}
UNIT_ALIASES = {"systolic_bp": {"mmHg", "mm[Hg]", "mm Hg"}}

#: Unit conversions, keyed by (field, source unit): value in source unit times factor.
UNIT_CONVERSIONS: dict[tuple[str, str], float] = {
    # 1 kPa is 7.50062 mmHg. Some European systems report pressure in kPa.
    ("systolic_bp", "kPa"): 7.50062,
}

#: Encounter fields that become yes/no contract fields.
_YES_NO_FIELDS = {
    "faceWeakness": "face_weakness",
    "armWeakness": "arm_weakness",
    "legWeakness": "leg_weakness",
    "speechDifficulty": "speech_difficulty",
    "visualFieldLoss": "visual_field_loss",
    "atrialFibrillation": "atrial_fibrillation",
    "onsetOnWaking": "onset_on_waking",
}

#: Encounter fields that become coded text contract fields.
_CHOICE_FIELDS = {
    "gender": "sex",
    "consciousLevel": "conscious_level",
    "strokeSubtype": "stroke_subtype",
}


@dataclass
class MappingResult:
    """One mapped record, plus everything the mapper could not account for.

    ``unmapped`` and ``issues`` make a source that changed shape show up as a
    reported gap, not as a record that scores differently.
    """

    record: dict[str, Any] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"record": self.record, "unmapped": self.unmapped, "issues": self.issues}


def _observation_value(observation: dict[str, Any]) -> tuple[Any, str | None]:
    """Pull a value and unit out of a FHIR-shaped observation."""
    quantity = observation.get("valueQuantity")
    if isinstance(quantity, dict):
        return quantity.get("value"), quantity.get("unit") or quantity.get("code")
    for key in ("valueBoolean", "valueString", "valueInteger"):
        if key in observation:
            return observation[key], None
    return None, None


def _observation_code(observation: dict[str, Any]) -> str | None:
    """Pull the first coding code out of a FHIR-shaped observation."""
    coding = (observation.get("code") or {}).get("coding") or []
    for entry in coding:
        if isinstance(entry, dict) and entry.get("code"):
            return str(entry["code"])
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _in_range(name: str, value: float, result: MappingResult) -> bool:
    domain = STROKE_FIELD_DOMAINS[name]
    low = domain.minimum if domain.minimum is not None else float("-inf")
    high = domain.maximum if domain.maximum is not None else float("inf")
    if not low <= value <= high:
        result.issues.append(f"{name} {value:g} is outside {low:g}-{high:g}; it was not used")
        return False
    return True


def map_encounter(encounter: dict[str, Any], observations: list[dict[str, Any]]) -> MappingResult:
    """Map one encounter and its observations onto a v3 record.

    Inputs:
    - ``encounter``: a FHIR-shaped Encounter carrying the demo's demographic and
      examination fields (``age``, ``gender``, ``consciousLevel``,
      ``faceWeakness`` and so on, ``hoursSinceOnset`` or ``minutesSinceOnset``).
    - ``observations``: FHIR-shaped Observations recorded during it.

    Output: a :class:`MappingResult` with the record, the codes that could not
    be mapped, and any unit or range problem. The exact age is kept in the
    record, because the record stays at the site; ``age_band`` is the contract
    field derived from it. A value with an unknown unit or out of range is
    reported and left out, never stored unconverted.
    """
    result = MappingResult()
    record: dict[str, Any] = {}

    age = _number(encounter.get("age"))
    if age is None:
        result.issues.append("age is missing, so no age band was derived")
    else:
        try:
            record["age_band"] = age_band_for(age)
            record["age"] = int(age)
        except ValueError as error:
            result.issues.append(f"{error}; no age band was derived")

    for source, target in _CHOICE_FIELDS.items():
        value = encounter.get(source)
        if value is None:
            continue
        options = STROKE_FIELD_DOMAINS[target].options
        if value in options:
            record[target] = value
        else:
            result.issues.append(f"{source} {value!r} is not one of {', '.join(options)}; it was not used")

    for source, target in _YES_NO_FIELDS.items():
        value = encounter.get(source)
        if value is None:
            continue
        if isinstance(value, bool):
            record[target] = value
        else:
            result.issues.append(f"{source} {value!r} is not true or false; it was not used")

    hours = _number(encounter.get("hoursSinceOnset"))
    minutes = _number(encounter.get("minutesSinceOnset"))
    if hours is None and minutes is not None:
        hours = minutes / 60.0
    if hours is not None and _in_range("hours_since_onset", hours, result):
        record["hours_since_onset"] = hours

    for observation in observations:
        code = _observation_code(observation)
        if code is None:
            result.issues.append("observation has no coding and was skipped")
            continue
        target = LOINC_TO_FIELD.get(code)
        if target is None:
            # Reported, not dropped: a code this mapper does not know may be the
            # one a site relies on.
            result.unmapped.append(code)
            continue
        raw, unit = _observation_value(observation)
        value = _number(raw)
        if value is None:
            result.issues.append(f"observation {code} carried no readable number")
            continue
        if unit and (target, unit) in UNIT_CONVERSIONS:
            value = value * UNIT_CONVERSIONS[(target, unit)]
        elif unit and unit not in UNIT_ALIASES[target]:
            result.issues.append(
                f"observation {code} reported unit {unit}, expected {CANONICAL_UNITS[target]}; it was not used"
            )
            continue
        if _in_range(target, value, result):
            record[target] = value

    # Naming the missing required fields lets schema validation reject the
    # record, instead of a scorer reading absence as a normal finding.
    for required in STROKE_FEATURE_CONTRACT.required_fields():
        if required not in record:
            result.issues.append(f"required contract field {required} was not present in the source")

    result.record = record
    return result


def map_bundle(bundle: dict[str, Any]) -> list[MappingResult]:
    """Map a FHIR-shaped bundle of encounters onto v3 records."""
    results: list[MappingResult] = []
    entries = bundle.get("entry", [])
    for entry in entries:
        resource = entry.get("resource") or {}
        if resource.get("resourceType") != "Encounter":
            continue
        observations = [
            item.get("resource", {})
            for item in entries
            if (item.get("resource") or {}).get("resourceType") == "Observation"
            and (item.get("resource") or {}).get("encounter", {}).get("reference")
            == f"Encounter/{resource.get('id')}"
        ]
        results.append(map_encounter(resource, observations))
    return results


def as_fhir_bundle(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Express v3 records as a FHIR-shaped bundle.

    The tests use it to run the mapper on the demo's own records, so the mapping
    is checked against real rows rather than a mock.
    """
    entries: list[dict[str, Any]] = []
    for record in records:
        encounter_id = record["encounter_id"]
        resource: dict[str, Any] = {"resourceType": "Encounter", "id": encounter_id}
        if "age" in record:
            resource["age"] = record["age"]
        if "hours_since_onset" in record:
            resource["hoursSinceOnset"] = record["hours_since_onset"]
        for source, target in {**_CHOICE_FIELDS, **_YES_NO_FIELDS}.items():
            if target in record:
                resource[source] = record[target]
        entries.append({"resource": resource})
        if "systolic_bp" in record:
            entries.append(
                {
                    "resource": {
                        "resourceType": "Observation",
                        "encounter": {"reference": f"Encounter/{encounter_id}"},
                        "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                        "valueQuantity": {"value": record["systolic_bp"], "unit": "mmHg", "code": "mm[Hg]"},
                    }
                }
            )
    return {"resourceType": "Bundle", "type": "collection", "entry": entries}
