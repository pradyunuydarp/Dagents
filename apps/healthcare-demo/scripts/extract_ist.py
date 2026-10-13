#!/usr/bin/env python3
"""Regenerate the committed IST-1 extract for the three demo hospitals.

Source: the International Stroke Trial database, version 2 (IST-1), file
``IST_corrected.csv``. It is available under the ODC Attribution License 1.0.
See ``data/ist/NOTICE.md`` for the attribution and the citation.

What the script does:

1. Reads the source, either from the pinned URL or from a local copy.
2. Checks its SHA-256 against the pinned value, and stops if it differs.
3. Decodes it as Windows-1250, the encoding of the published file.
4. Checks the hospital selection rule (the largest hospital in each of the three
   countries with the most patients) still picks the hospitals in
   ``app.domain.dataset.SITES``.
5. Maps the selected rows onto the demo's record schema (feature contract v3)
   and checks the row count per hospital.
6. Writes ``data/ist/ist_three_hospitals.csv`` as UTF-8 with LF line endings.

The script reads only the columns it maps. It never writes the trial's own
outcome predictions, free-text comments, dates or times, or the trial treatment
allocation; ``NEVER_EXTRACT`` lists them and the reasons.

Usage, from the repository root:

    .venv/bin/python apps/healthcare-demo/scripts/extract_ist.py               # download
    .venv/bin/python apps/healthcare-demo/scripts/extract_ist.py --source PATH # local copy

Tests never run the download.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import sys
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parents[1]
# The site list and the age bands live in the backend package, which is not
# installed. Importing them keeps one definition of each.
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT))

from app.domain.dataset import (  # noqa: E402
    CSV_COLUMNS,
    SITES,
    age_band_for,
    encounter_id_for,
)

#: The pinned source: a byte-identical mirror of the Edinburgh DataShare file,
#: fixed to one commit so the URL cannot change under the checksum.
SOURCE_URL = (
    "https://raw.githubusercontent.com/ebergel/ist-1-database/"
    "e9ebea9d6202bed786abc89785740e13ac087fed/data/IST_corrected.csv"
)

#: A second, independent copy with the same bytes.
MIRROR_URL = (
    "https://raw.githubusercontent.com/AtlantaL/Survival-Analysis-International-Strokes-Trial/"
    "ffff8832ea4d01c436e44209422a0dca1f30db84/Dataset/IST_corrected.csv"
)

SOURCE_SHA256 = "a350aed2329bbcde23c05caaa96b9275b44b017adecc2fd02231d590454695d5"
SOURCE_BYTES = 4_798_288
SOURCE_ROWS = 19_435
SOURCE_COLUMNS = 112
SOURCE_ENCODING = "cp1250"

DEFAULT_OUTPUT = APP_ROOT / "data" / "ist" / "ist_three_hospitals.csv"

#: IST columns that must never reach the extract, with the reason for each.
NEVER_EXTRACT: dict[str, str] = {
    "EXPDD": "the trial's predicted probability of death or dependency; it leaks the outcome",
    "EXPD6": "the trial's predicted probability of death at 6 months; it leaks the outcome",
    "EXPD14": "the trial's predicted probability of death at 14 days; it leaks the outcome",
    "DMAJNCHX": "free-text comment",
    "DSIDEX": "free-text comment",
    "DNOSTRKX": "free-text comment",
    "DDEADX": "free-text comment",
    "FDEADX": "free-text comment",
    "RDATE": "date of randomisation, a quasi-identifier",
    "RTIME": "time of randomisation, a quasi-identifier",
    "HOURLOCAL": "local time of randomisation, a quasi-identifier",
    "MINLOCAL": "local time of randomisation, a quasi-identifier",
    "DAYLOCAL": "day of the week of randomisation, a quasi-identifier",
    "DMAJNCHD": "follow-up date",
    "DSIDED": "follow-up date",
    "DRSISCD": "follow-up date",
    "DRSHD": "follow-up date",
    "DRSUNKD": "follow-up date",
    "DPED": "follow-up date",
    "DALIVED": "follow-up date",
    "DDEADD": "follow-up date",
    "FLASTD": "follow-up date",
    "FDEADD": "follow-up date",
    "FU1_RECD": "follow-up date",
    "FU2_DONE": "follow-up date",
    "FU1_COMP": "follow-up date",
    "RXASP": "trial aspirin allocation, not a patient characteristic",
    "RXHEP": "trial heparin allocation, not a patient characteristic",
}

#: The IST columns the mapping reads. Everything else in the source is ignored.
SOURCE_FIELDS = (
    "HOSPNUM",
    "COUNTRY",
    "AGE",
    "SEX",
    "RCONSC",
    "RDEF1",
    "RDEF2",
    "RDEF3",
    "RDEF4",
    "RDEF5",
    "RDELAY",
    "RSBP",
    "RATRIAL",
    "RSLEEP",
    "STYPE",
    "OCCODE",
)

_YES_NO = {"Y": True, "N": False}
# RDEF1-RDEF5 use C for "can't assess", which becomes a missing value.
_DEFICIT = {"Y": True, "N": False, "C": None}
# RATRIAL is blank for the 984 pilot-phase patients, where it was not coded.
_ATRIAL = {"Y": True, "N": False, "": None}
_SEX = {"M": "male", "F": "female"}
_CONSCIOUS = {"F": "alert", "D": "drowsy", "U": "unconscious"}
_SUBTYPE = {"TACS": "TACS", "PACS": "PACS", "LACS": "LACS", "POCS": "POCS", "OTH": "other"}
# OCCODE: 1 dead, 2 dependent, 3 not recovered, 4 recovered; 0, 8, 9 and blank
# mean the six-month status is unknown.
_OUTCOME = {"1": True, "2": True, "3": False, "4": False, "0": None, "8": None, "9": None, "": None}


class ExtractionError(RuntimeError):
    """The source or the extract does not match what the script expects."""


def verify_source(data: bytes) -> None:
    """Stop unless ``data`` is the pinned ``IST_corrected.csv``."""
    digest = hashlib.sha256(data).hexdigest()
    if digest != SOURCE_SHA256:
        raise ExtractionError(
            f"the source has SHA-256 {digest}, expected {SOURCE_SHA256} "
            f"({len(data)} bytes, expected {SOURCE_BYTES}); it is not the pinned IST_corrected.csv"
        )


def read_source_rows(data: bytes) -> list[dict[str, str]]:
    """Decode the verified source and return its rows as dictionaries."""
    verify_source(data)
    reader = csv.DictReader(io.StringIO(data.decode(SOURCE_ENCODING), newline=""))
    rows = list(reader)
    if len(reader.fieldnames or []) != SOURCE_COLUMNS or len(rows) != SOURCE_ROWS:
        raise ExtractionError(
            f"expected {SOURCE_ROWS} rows x {SOURCE_COLUMNS} columns, "
            f"found {len(rows)} x {len(reader.fieldnames or [])}"
        )
    return rows


def selected_hospitals(rows: Iterable[dict[str, str]]) -> list[int]:
    """Apply the selection rule and return the chosen hospital numbers.

    The rule: take the three countries with the most patients; in each, take the
    hospital with the most patients, breaking ties by the lowest number. The
    result is in country order, largest country first.
    """
    by_country: Counter[str] = Counter()
    by_hospital: dict[str, Counter[int]] = {}
    for row in rows:
        country = row["COUNTRY"]
        by_country[country] += 1
        by_hospital.setdefault(country, Counter())[int(row["HOSPNUM"])] += 1
    countries = sorted(by_country, key=lambda name: (-by_country[name], name))[:3]
    chosen = []
    for country in countries:
        counts = by_hospital[country]
        chosen.append(min(counts, key=lambda hospital: (-counts[hospital], hospital)))
    return chosen


def _lookup(table: dict[str, Any], column: str, value: str, source_row: int) -> Any:
    key = value.strip().upper()
    if key not in table:
        raise ExtractionError(f"row {source_row}: unexpected {column} value {value!r}")
    return table[key]


def map_row(row: dict[str, str], source_row: int, site_id: str) -> dict[str, Any]:
    """Map one IST row onto the v3 record schema.

    ``source_row`` is the 1-based position of the row among the data rows of
    ``IST_corrected.csv`` (the header is not counted). A missing value is
    ``None`` here and an empty cell in the CSV.
    """
    age = int(row["AGE"])
    hours = int(row["RDELAY"])
    systolic = int(row["RSBP"])
    return {
        "encounter_id": encounter_id_for(site_id, source_row),
        "site_id": site_id,
        "source_row": source_row,
        "age": age,
        "age_band": age_band_for(age),
        "sex": _lookup(_SEX, "SEX", row["SEX"], source_row),
        "conscious_level": _lookup(_CONSCIOUS, "RCONSC", row["RCONSC"], source_row),
        "face_weakness": _lookup(_DEFICIT, "RDEF1", row["RDEF1"], source_row),
        "arm_weakness": _lookup(_DEFICIT, "RDEF2", row["RDEF2"], source_row),
        "leg_weakness": _lookup(_DEFICIT, "RDEF3", row["RDEF3"], source_row),
        "speech_difficulty": _lookup(_DEFICIT, "RDEF4", row["RDEF4"], source_row),
        "hours_since_onset": hours,
        "systolic_bp": systolic,
        "visual_field_loss": _lookup(_DEFICIT, "RDEF5", row["RDEF5"], source_row),
        "atrial_fibrillation": _lookup(_ATRIAL, "RATRIAL", row["RATRIAL"], source_row),
        "onset_on_waking": _lookup(_YES_NO, "RSLEEP", row["RSLEEP"], source_row),
        "stroke_subtype": _lookup(_SUBTYPE, "STYPE", row["STYPE"], source_row),
        "poor_outcome_6m": _lookup(_OUTCOME, "OCCODE", row["OCCODE"], source_row),
    }


def extract(data: bytes) -> list[dict[str, Any]]:
    """Verify the source and return the mapped rows for the three hospitals."""
    rows = read_source_rows(data)
    by_hospnum = {site.hospnum: site for site in SITES}
    chosen = selected_hospitals(rows)
    if sorted(chosen) != sorted(by_hospnum):
        raise ExtractionError(
            f"the selection rule picks hospitals {chosen}, but the demo defines {sorted(by_hospnum)}"
        )

    mapped: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        site = by_hospnum.get(int(row["HOSPNUM"]))
        if site is not None:
            mapped.append(map_row(row, index, site.site_id))

    counts = Counter(record["site_id"] for record in mapped)
    for site in SITES:
        if counts[site.site_id] != site.expected_rows:
            raise ExtractionError(
                f"{site.site_id} has {counts[site.site_id]} rows, expected {site.expected_rows}"
            )
    # Grouped by hospital, in the order of SITES, then by source row.
    order = {site.site_id: position for position, site in enumerate(SITES)}
    return sorted(mapped, key=lambda record: (order[record["site_id"]], record["source_row"]))


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_csv(records: list[dict[str, Any]], path: Path) -> None:
    """Write the extract as UTF-8 with LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(CSV_COLUMNS)
        for record in records:
            writer.writerow([_cell(record[column]) for column in CSV_COLUMNS])


def download(url: str = SOURCE_URL, timeout: float = 60.0) -> bytes:
    """Download the source. Only the command line calls this."""
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - pinned https URL
        return response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regenerate the IST-1 extract for the three demo hospitals.")
    parser.add_argument("--source", type=Path, help="a local copy of IST_corrected.csv (default: download)")
    parser.add_argument("--url", default=SOURCE_URL, help="the URL to download when --source is not given")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="where to write the extract")
    args = parser.parse_args(argv)

    try:
        data = args.source.read_bytes() if args.source else download(args.url)
        records = extract(data)
    except ExtractionError as error:
        print(f"extract_ist: {error}", file=sys.stderr)
        return 1

    write_csv(records, args.output)
    counts = Counter(record["site_id"] for record in records)
    for site in SITES:
        print(f"{site.site_id} (IST hospital {site.hospnum}, {site.country}): {counts[site.site_id]} rows")
    print(f"wrote {len(records)} rows to {args.output}")
    print(f"source SHA-256 {SOURCE_SHA256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
