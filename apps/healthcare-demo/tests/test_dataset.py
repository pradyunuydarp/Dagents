"""Tests for the committed IST-1 extract and the module that reads it.

The extract is real trial data under a licence with conditions, so the tests
check what was left out and that the licence files travel with it, as well as
the shape of every row. Nothing here downloads anything.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import pathlib
import re
import tempfile
import unittest

from app.domain import dataset
from app.domain.conditions import STROKE_FEATURE_CONTRACT, STROKE_FIELD_DOMAINS, STROKE_LABEL_FIELD

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = APP_ROOT / "data" / "ist"
EXTRACT = DATA_DIR / "ist_three_hospitals.csv"
SCRIPT = APP_ROOT / "scripts" / "extract_ist.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("extract_ist", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


extract_ist = _load_script()

#: Fields every record may carry: the extract columns other than site_id, plus origin.
RECORD_FIELDS = (set(dataset.CSV_COLUMNS) - {"site_id"}) | {"origin"}


class SiteTests(unittest.TestCase):
    def test_the_three_hospitals_are_the_ones_the_spec_names(self) -> None:
        self.assertEqual(
            [(site.site_id, site.hospnum, site.country, site.expected_rows) for site in dataset.SITES],
            [
                ("uk-134", 134, "United Kingdom", 524),
                ("it-002", 2, "Italy", 292),
                ("ch-245", 245, "Switzerland", 198),
            ],
        )

    def test_each_site_has_its_own_schema_named_like_its_role(self) -> None:
        schemas = [site.schema for site in dataset.SITES]
        self.assertEqual(schemas, ["site_uk_134", "site_it_002", "site_ch_245"])
        self.assertEqual(len(set(schemas)), len(schemas))
        for site in dataset.SITES:
            self.assertEqual(site.role, site.schema)
            self.assertRegex(site.schema, r"^site_[a-z]{2}_\d{3}$")

    def test_an_unknown_site_is_refused(self) -> None:
        with self.assertRaises(dataset.UnknownSiteError):
            dataset.load_site_records("riverside-general")


class ExtractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = {site.site_id: dataset.load_site_records(site.site_id) for site in dataset.SITES}

    def test_each_site_has_the_expected_number_of_rows(self) -> None:
        for site in dataset.SITES:
            self.assertEqual(len(self.records[site.site_id]), site.expected_rows, site.site_id)
        self.assertEqual(sum(len(rows) for rows in self.records.values()), 1014)

    def test_the_file_is_utf8_with_lf_line_endings_and_the_documented_digest(self) -> None:
        raw = EXTRACT.read_bytes()
        raw.decode("utf-8")
        self.assertNotIn(b"\r", raw)
        notice = (DATA_DIR / "NOTICE.md").read_text(encoding="utf-8")
        match = re.search(r"Extract SHA-256 \| `([0-9a-f]{64})`", notice)
        self.assertIsNotNone(match, "NOTICE.md must state the extract's SHA-256")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), match.group(1))

    def test_the_header_holds_no_column_that_must_never_be_extracted(self) -> None:
        header = EXTRACT.read_text(encoding="utf-8").splitlines()[0].split(",")
        self.assertEqual(tuple(header), dataset.CSV_COLUMNS)
        for column in extract_ist.NEVER_EXTRACT:
            self.assertNotIn(column, header)
            self.assertNotIn(column.lower(), header)
        # No raw IST column name at all: every column is mapped to a contract name.
        for column in extract_ist.SOURCE_FIELDS:
            self.assertNotIn(column, header)

    def test_every_value_is_in_its_domain(self) -> None:
        for rows in self.records.values():
            for record in rows:
                self.assertLessEqual(set(record), RECORD_FIELDS, record["encounter_id"])
                self.assertNotIn(None, record.values(), "a missing value must be an absent key")
                for name, value in record.items():
                    domain = STROKE_FIELD_DOMAINS.get(name)
                    if domain is None:
                        continue
                    if domain.options:
                        self.assertIn(value, domain.options, f"{record['encounter_id']} {name}")
                    if domain.minimum is not None:
                        self.assertGreaterEqual(value, domain.minimum, f"{record['encounter_id']} {name}")
                    if domain.maximum is not None:
                        self.assertLessEqual(value, domain.maximum, f"{record['encounter_id']} {name}")

    def test_values_have_the_contract_types(self) -> None:
        dtypes = {field.name: field.dtype for field in STROKE_FEATURE_CONTRACT.fields}
        python_types = {"bool": bool, "float": float, "string": str}
        for rows in self.records.values():
            for record in rows:
                self.assertIs(type(record["age"]), int)
                self.assertIs(type(record["source_row"]), int)
                for name, dtype in dtypes.items():
                    if name in record:
                        self.assertIs(type(record[name]), python_types[dtype], f"{record['encounter_id']} {name}")

    def test_ids_rows_and_bands_are_consistent(self) -> None:
        seen_ids: set[str] = set()
        seen_rows: set[int] = set()
        for site_id, rows in self.records.items():
            for record in rows:
                self.assertEqual(record["origin"], "dataset")
                self.assertEqual(record["encounter_id"], f"{site_id}-{record['source_row']:05d}")
                self.assertEqual(record["age_band"], dataset.age_band_for(record["age"]))
                self.assertTrue(1 <= record["source_row"] <= extract_ist.SOURCE_ROWS)
                seen_ids.add(record["encounter_id"])
                seen_rows.add(record["source_row"])
        self.assertEqual(len(seen_ids), 1014)
        self.assertEqual(len(seen_rows), 1014)

    def test_required_text_fields_and_the_outcome_are_never_missing(self) -> None:
        for rows in self.records.values():
            for record in rows:
                for name in ("age_band", "sex", "conscious_level", "hours_since_onset", "systolic_bp"):
                    self.assertIn(name, record)
                self.assertIsInstance(record[STROKE_LABEL_FIELD], bool)

    def test_cannot_assess_findings_stay_missing(self) -> None:
        """C (cannot assess) in the source must be a missing value, not "no"."""
        missing_visual = sum(
            1 for rows in self.records.values() for record in rows if "visual_field_loss" not in record
        )
        missing_af = sum(1 for record in self.records["it-002"] if "atrial_fibrillation" not in record)
        self.assertGreater(missing_visual, 0)
        self.assertGreater(missing_af, 0)

    def test_callers_get_their_own_copies(self) -> None:
        first = dataset.load_site_records("ch-245")
        first[0]["systolic_bp"] = 999.0
        self.assertNotEqual(dataset.load_site_records("ch-245")[0]["systolic_bp"], 999.0)


class AgeBandTests(unittest.TestCase):
    def test_band_edges(self) -> None:
        cases = {
            16: "<50", 49: "<50", 50: "50-59", 59: "50-59", 60: "60-69",
            79: "70-79", 89: "80-89", 90: "90+", 110: "90+",
        }
        for age, band in cases.items():
            self.assertEqual(dataset.age_band_for(age), band, age)
        self.assertEqual(dataset.age_band_for(59.9), "50-59")

    def test_ages_outside_the_schema_are_refused(self) -> None:
        for age in (15, 111, -1, True, None, "70"):
            with self.assertRaises(ValueError, msg=repr(age)):
                dataset.age_band_for(age)  # type: ignore[arg-type]


class LicenceTests(unittest.TestCase):
    def test_the_full_licence_text_travels_with_the_data(self) -> None:
        text = (DATA_DIR / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("ODC Attribution License (ODC-By)", text)
        self.assertIn("4.3 Notice for using output (Contents)", text)
        self.assertIn("10.4 Choice of law", text)

    def test_the_notice_names_the_source_citation_and_left_out_columns(self) -> None:
        notice = (DATA_DIR / "NOTICE.md").read_text(encoding="utf-8")
        self.assertIn(dataset.DATASET_NOTICE.split(" (https")[0], notice)
        self.assertIn("https://doi.org/10.7488/ds/104", notice)
        self.assertIn("Trials* 2011;12:101", notice)
        self.assertIn("UK Medical Research Council", notice)
        self.assertIn(extract_ist.SOURCE_SHA256, notice)
        self.assertIn(extract_ist.SOURCE_URL, notice)
        for column in extract_ist.NEVER_EXTRACT:
            self.assertIn(f"`{column}`", notice)

    def test_the_api_notice_is_the_one_odc_by_asks_for(self) -> None:
        self.assertEqual(
            dataset.DATASET_NOTICE,
            "Contains information from the International Stroke Trial database (version 2) "
            "(https://doi.org/10.7488/ds/104), made available under the ODC Attribution License "
            "(https://opendatacommons.org/licenses/by/1-0/).",
        )
        self.assertEqual(
            set(dataset.DATASET),
            {"name", "citation", "licence", "licence_url", "source_url", "notice", "credit"},
        )


class ExtractionScriptTests(unittest.TestCase):
    """The script itself, run on small inputs. It never downloads in a test."""

    def test_a_file_with_the_wrong_digest_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = pathlib.Path(folder) / "IST_corrected.csv"
            source.write_bytes(b"HOSPNUM,AGE\r\n134,70\r\n")
            output = pathlib.Path(folder) / "out.csv"
            with self.assertRaises(extract_ist.ExtractionError):
                extract_ist.extract(source.read_bytes())
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                status = extract_ist.main(["--source", str(source), "--output", str(output)])
            self.assertEqual(status, 1)
            self.assertIn("not the pinned IST_corrected.csv", errors.getvalue())
            self.assertFalse(output.exists())

    def test_the_script_uses_the_dataset_module_for_sites_and_columns(self) -> None:
        self.assertIs(extract_ist.SITES, dataset.SITES)
        self.assertIs(extract_ist.CSV_COLUMNS, dataset.CSV_COLUMNS)

    def _row(self, **changes: str) -> dict[str, str]:
        row = {
            "HOSPNUM": "245",
            "COUNTRY": "SWIT",
            "AGE": "81",
            "SEX": "F",
            "RCONSC": "D",
            "RDEF1": "Y",
            "RDEF2": "N",
            "RDEF3": "C",
            "RDEF4": "Y",
            "RDEF5": "C",
            "RDELAY": "12",
            "RSBP": "150",
            "RATRIAL": "",
            "RSLEEP": "N",
            "STYPE": "OTH",
            "OCCODE": "2",
        }
        row.update(changes)
        return row

    def test_a_row_is_mapped_by_the_documented_rules(self) -> None:
        mapped = extract_ist.map_row(self._row(), 7, "ch-245")
        self.assertEqual(mapped["encounter_id"], "ch-245-00007")
        self.assertEqual(mapped["age_band"], "80-89")
        self.assertEqual(mapped["sex"], "female")
        self.assertEqual(mapped["conscious_level"], "drowsy")
        self.assertIs(mapped["face_weakness"], True)
        self.assertIs(mapped["arm_weakness"], False)
        self.assertIsNone(mapped["leg_weakness"])
        self.assertIsNone(mapped["visual_field_loss"])
        self.assertIsNone(mapped["atrial_fibrillation"])
        self.assertEqual(mapped["stroke_subtype"], "other")
        self.assertIs(mapped["poor_outcome_6m"], True)
        for code, expected in {"1": True, "3": False, "4": False, "0": None, "9": None}.items():
            self.assertIs(extract_ist.map_row(self._row(OCCODE=code), 7, "ch-245")["poor_outcome_6m"], expected)

    def test_an_unknown_code_stops_the_extraction(self) -> None:
        with self.assertRaises(extract_ist.ExtractionError):
            extract_ist.map_row(self._row(RCONSC="X"), 7, "ch-245")

    def test_the_selection_rule_breaks_ties_by_the_lowest_hospital_number(self) -> None:
        rows = (
            [{"COUNTRY": "A", "HOSPNUM": "9"}] * 3
            + [{"COUNTRY": "A", "HOSPNUM": "4"}] * 3
            + [{"COUNTRY": "B", "HOSPNUM": "7"}] * 4
            + [{"COUNTRY": "C", "HOSPNUM": "1"}] * 2
            + [{"COUNTRY": "D", "HOSPNUM": "2"}] * 1
        )
        self.assertEqual(extract_ist.selected_hospitals(rows), [4, 7, 1])


if __name__ == "__main__":
    unittest.main()
