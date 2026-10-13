"""The demo's three hospitals and the committed IST-1 extract they start from.

The records are real, de-identified patient records from the International
Stroke Trial (IST-1), recruited in 1991-1996. Each demo hospital is one real IST
hospital: the hospital with the most patients in each of the three countries
with the most patients. ``scripts/extract_ist.py`` produced the extract, and
``data/ist/NOTICE.md`` records the source, the licence and what was left out.

This module reads one file, the committed extract, and nothing else. It does not
open a network connection or a database. A deployed site reads the same rows
from its own database schema, seeded from this file.

A missing value is an absent key in a record, never ``None``.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

# --- The dataset --------------------------------------------------------------

DATASET_NAME = "International Stroke Trial database (version 2)"
DATASET_CITATION = (
    "Sandercock PAG, Niewada M, Członkowska A; International Stroke Trial Collaborative Group. "
    "The International Stroke Trial database. Trials 2011;12:101 (erratum Trials 2012;13:24)."
)
DATASET_LICENCE = "ODC Attribution License (ODC-By) 1.0"
DATASET_LICENCE_URL = "https://opendatacommons.org/licenses/by/1-0/"
DATASET_SOURCE_URL = "https://doi.org/10.7488/ds/104"

#: The notice ODC-By section 4.3 asks for on every page and chart built from the data.
DATASET_NOTICE = (
    "Contains information from the International Stroke Trial database (version 2) "
    "(https://doi.org/10.7488/ds/104), made available under the ODC Attribution License "
    "(https://opendatacommons.org/licenses/by/1-0/)."
)

#: Credit for the people who collected the data and for the trial's main funders,
#: as the database paper lists them.
DATASET_CREDIT = (
    "The International Stroke Trial Collaborative Group collected the data from 19,435 patients "
    "in 1991-1996. The trial was funded mainly by the UK Medical Research Council, the UK Stroke "
    "Association and the European Union BIOMED-1 programme."
)

#: What the API reports about the dataset.
DATASET: dict[str, str] = {
    "name": DATASET_NAME,
    "citation": DATASET_CITATION,
    "licence": DATASET_LICENCE,
    "licence_url": DATASET_LICENCE_URL,
    "source_url": DATASET_SOURCE_URL,
    "notice": DATASET_NOTICE,
    "credit": DATASET_CREDIT,
}


# --- The hospitals ------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One demo hospital.

    ``schema`` is also the name of the database role that owns the schema, so a
    site's rows are read and written only as that role.
    """

    site_id: str
    display_name: str
    country: str
    hospnum: int
    schema: str
    expected_rows: int

    @property
    def role(self) -> str:
        """The database role for this site. It has the same name as the schema."""
        return self.schema


#: The three hospitals, largest first. ``hospnum`` is the IST ``HOSPNUM``.
SITES: tuple[Site, ...] = (
    Site("uk-134", "Hospital 134", "United Kingdom", 134, "site_uk_134", 524),
    Site("it-002", "Hospital 2", "Italy", 2, "site_it_002", 292),
    Site("ch-245", "Hospital 245", "Switzerland", 245, "site_ch_245", 198),
)

SITE_IDS: tuple[str, ...] = tuple(site.site_id for site in SITES)


class UnknownSiteError(KeyError):
    """The site id is not one of the demo hospitals."""


def site_for(site_id: str) -> Site:
    """Return the hospital with this id, or raise :class:`UnknownSiteError`."""
    for site in SITES:
        if site.site_id == site_id:
            return site
    raise UnknownSiteError(site_id)


# --- Records ------------------------------------------------------------------

#: Ages the record schema accepts, in years.
AGE_MIN = 16
AGE_MAX = 110

#: The age bands of the feature contract, youngest first.
AGE_BANDS: tuple[str, ...] = ("<50", "50-59", "60-69", "70-79", "80-89", "90+")


def age_band_for(age: int | float) -> str:
    """Return the contract's age band for an age in years.

    The exact age stays at the site. Only the band is in the feature contract.
    """
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        raise ValueError(f"age must be a number, got {age!r}")
    if not AGE_MIN <= age <= AGE_MAX:
        raise ValueError(f"age {age} is outside {AGE_MIN}-{AGE_MAX}")
    if age < 50:
        return "<50"
    if age >= 90:
        return "90+"
    lower = int(age // 10) * 10
    return f"{lower}-{lower + 9}"


def encounter_id_for(site_id: str, source_row: int) -> str:
    """The id of a dataset record: the site id and the 1-based source row."""
    return f"{site_id}-{source_row:05d}"


#: Columns of ``ist_three_hospitals.csv``, in file order.
CSV_COLUMNS: tuple[str, ...] = (
    "encounter_id",
    "site_id",
    "source_row",
    "age",
    "age_band",
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
    "poor_outcome_6m",
)

_INT_COLUMNS = frozenset({"source_row", "age"})
_FLOAT_COLUMNS = frozenset({"hours_since_onset", "systolic_bp"})
_BOOL_COLUMNS = frozenset(
    {
        "face_weakness",
        "arm_weakness",
        "leg_weakness",
        "speech_difficulty",
        "visual_field_loss",
        "atrial_fibrillation",
        "onset_on_waking",
        "poor_outcome_6m",
    }
)

#: The extract's path relative to the app root.
EXTRACT_PATH = Path("data") / "ist" / "ist_three_hospitals.csv"


@lru_cache(maxsize=1)
def dataset_path() -> Path:
    """Where the committed extract is read from.

    In the repository this module is ``apps/healthcare-demo/backend/app/domain``,
    so the app root is three levels up. In the container the package is
    ``/app/app`` and the image keeps the data in ``/app/data``. The path is
    found on first use, not at import.
    """
    here = Path(__file__).resolve()
    candidates = [here.parents[3] / EXTRACT_PATH, here.parents[2] / EXTRACT_PATH]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


class DatasetError(RuntimeError):
    """The extract is missing or does not have the expected shape."""


def _parse(column: str, value: str) -> Any:
    if column in _INT_COLUMNS:
        return int(value)
    if column in _FLOAT_COLUMNS:
        return float(value)
    if column in _BOOL_COLUMNS:
        if value not in {"true", "false"}:
            raise DatasetError(f"{column} has value {value!r}; expected true, false or empty")
        return value == "true"
    return value


@lru_cache(maxsize=4)
def _read_extract(path: str) -> tuple[tuple[str, dict[str, Any]], ...]:
    """Parse the extract once per path. Returns (site_id, record) pairs."""
    try:
        handle = open(path, encoding="utf-8", newline="")
    except OSError as error:
        raise DatasetError(f"the dataset extract could not be read from {path}: {error}") from error
    with handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
            raise DatasetError(f"{path} has columns {reader.fieldnames}, expected {list(CSV_COLUMNS)}")
        rows: list[tuple[str, dict[str, Any]]] = []
        for row in reader:
            record: dict[str, Any] = {}
            for column in CSV_COLUMNS:
                if column == "site_id" or row[column] == "":
                    continue
                record[column] = _parse(column, row[column])
                if column == "source_row":
                    record["origin"] = "dataset"
            rows.append((row["site_id"], record))
    return tuple(rows)


def load_site_records(site_id: str, path: Path | str | None = None) -> list[dict[str, Any]]:
    """Return one hospital's dataset records, in source order.

    Each record holds ``encounter_id``, ``source_row``, ``origin`` (always
    ``"dataset"``), ``age`` and the contract fields. A missing value is an
    absent key. The caller gets new dictionaries and may change them.
    """
    site_for(site_id)
    rows = _read_extract(str(path or dataset_path()))
    return [dict(record) for row_site, record in rows if row_site == site_id]
