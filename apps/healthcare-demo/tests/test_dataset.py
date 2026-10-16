"""Tests for the committed IST-1 extract and the module that reads it.

The extract is real trial data under a licence with conditions, so the tests
check what was left out and that the licence files travel with it, as well as
the shape of every row. Nothing here downloads anything.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import hashlib
import importlib.util
import io
import pathlib
import re
import tempfile
import unittest
from unittest import mock

from app.domain import dataset
from app.domain.conditions import STROKE_FEATURE_CONTRACT, STROKE_FIELD_DOMAINS, STROKE_LABEL_FIELD

APP_ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = APP_ROOT / "data" / "ist"
EXTRACT = DATA_DIR / "ist_three_hospitals.csv"
JOINING_EXTRACT = DATA_DIR / "ist_joining_hospitals.csv"
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


def check_records(test: unittest.TestCase, records: dict[str, list[dict]]) -> None:
    """The domain, type and id checks of ``ExtractTests``, for any extract.

    ``records`` maps each site id to its records, as ``load_site_records``
    returns them.
    """
    dtypes = {field.name: field.dtype for field in STROKE_FEATURE_CONTRACT.fields}
    python_types = {"bool": bool, "float": float, "string": str}
    seen_ids: set[str] = set()
    seen_rows: set[int] = set()
    total = 0
    for site_id, rows in records.items():
        for record in rows:
            total += 1
            label = record["encounter_id"]
            # Domains.
            test.assertLessEqual(set(record), RECORD_FIELDS, label)
            test.assertNotIn(None, record.values(), "a missing value must be an absent key")
            for name, value in record.items():
                domain = STROKE_FIELD_DOMAINS.get(name)
                if domain is None:
                    continue
                if domain.options:
                    test.assertIn(value, domain.options, f"{label} {name}")
                if domain.minimum is not None:
                    test.assertGreaterEqual(value, domain.minimum, f"{label} {name}")
                if domain.maximum is not None:
                    test.assertLessEqual(value, domain.maximum, f"{label} {name}")
            # Types.
            test.assertIs(type(record["age"]), int)
            test.assertIs(type(record["source_row"]), int)
            for name, dtype in dtypes.items():
                if name in record:
                    test.assertIs(type(record[name]), python_types[dtype], f"{label} {name}")
            # Ids, rows and bands.
            test.assertEqual(record["origin"], "dataset")
            test.assertEqual(record["encounter_id"], f"{site_id}-{record['source_row']:05d}")
            test.assertEqual(record["age_band"], dataset.age_band_for(record["age"]))
            test.assertTrue(1 <= record["source_row"] <= extract_ist.SOURCE_ROWS)
            seen_ids.add(record["encounter_id"])
            seen_rows.add(record["source_row"])
            # Required fields. The outcome may be missing (see the joining tests).
            for name in ("age_band", "sex", "conscious_level", "hours_since_onset", "systolic_bp"):
                test.assertIn(name, record)
    test.assertEqual(len(seen_ids), total)
    test.assertEqual(len(seen_rows), total)


def source_rows(layout: list[tuple[str, int, int]]) -> list[dict[str, str]]:
    """Synthetic IST rows: ``count`` patients of hospital ``hospnum`` in ``country``, per entry."""
    template = {
        "AGE": "74",
        "SEX": "M",
        "RCONSC": "F",
        "RDEF1": "Y",
        "RDEF2": "Y",
        "RDEF3": "N",
        "RDEF4": "N",
        "RDEF5": "N",
        "RDELAY": "6",
        "RSBP": "160",
        "RATRIAL": "N",
        "RSLEEP": "N",
        "STYPE": "PACS",
        "OCCODE": "3",
    }
    return [
        {**template, "COUNTRY": country, "HOSPNUM": str(hospnum)}
        for country, hospnum, count in layout
        for _ in range(count)
    ]


#: A synthetic source in which country D is the fourth largest (283 patients)
#: and hospital 290 is its largest (263). Country E has fewer patients (280),
#: although its hospital 14 is larger than hospital 290.
FOURTH_COUNTRY_LAYOUT = [
    ("A", 11, 400),
    ("D", 291, 10),
    ("B", 12, 350),
    ("D", 290, 200),
    ("C", 13, 300),
    ("E", 14, 280),
    ("D", 290, 63),
    ("D", 291, 10),
]


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


class JoiningExtractTests(unittest.TestCase):
    """The joining hospital: the site that is added while the demo runs."""

    def test_the_three_consortium_hospitals_are_unchanged(self) -> None:
        self.assertEqual(dataset.SITE_IDS, ("uk-134", "it-002", "ch-245"))
        self.assertEqual({site.extract for site in dataset.SITES}, {"ist_three_hospitals.csv"})
        # A site built with the six original fields reads the three hospitals' extract.
        self.assertEqual(dataset.Site("x-1", "X", "Nowhere", 0, "site_x_1", 3).extract, "ist_three_hospitals.csv")

    def test_the_joining_hospital_is_hospital_290_in_poland(self) -> None:
        self.assertEqual(
            dataset.JOINING_SITES,
            (
                dataset.Site(
                    "pl-290", "Hospital 290", "Poland", 290, "site_pl_290", 263, extract="ist_joining_hospitals.csv"
                ),
            ),
        )
        self.assertEqual(dataset.JOINING_SITE_IDS, ("pl-290",))
        self.assertFalse(set(dataset.JOINING_SITE_IDS) & set(dataset.SITE_IDS))
        self.assertNotIn(dataset.JOINING_SITES[0].schema, {site.schema for site in dataset.SITES})

    def test_site_for_finds_only_the_consortium_hospitals(self) -> None:
        self.assertEqual(dataset.site_for("uk-134").hospnum, 134)
        with self.assertRaises(dataset.UnknownSiteError):
            dataset.site_for("pl-290")

    def test_joining_site_for_finds_only_the_joining_hospital(self) -> None:
        self.assertEqual(dataset.joining_site_for("pl-290").hospnum, 290)
        for site_id in ("uk-134", "riverside-general"):
            with self.assertRaises(dataset.UnknownSiteError, msg=site_id):
                dataset.joining_site_for(site_id)

    def test_any_site_for_finds_both_kinds(self) -> None:
        self.assertIs(dataset.any_site_for("it-002"), dataset.SITES[1])
        self.assertIs(dataset.any_site_for("pl-290"), dataset.JOINING_SITES[0])
        with self.assertRaises(dataset.UnknownSiteError):
            dataset.any_site_for("riverside-general")

    def test_is_joining(self) -> None:
        self.assertTrue(dataset.is_joining("pl-290"))
        for site_id in ("uk-134", "it-002", "ch-245", "riverside-general", ""):
            self.assertFalse(dataset.is_joining(site_id), site_id)

    def test_records_source_and_expected_count(self) -> None:
        joining = dataset.joining_site_for("pl-290")
        self.assertEqual(dataset.records_source(joining), "dataset")
        self.assertEqual(dataset.expected_record_count(joining), 263)
        for site in dataset.SITES:
            self.assertEqual(dataset.records_source(site), "dataset")
            self.assertEqual(dataset.expected_record_count(site), site.expected_rows)

    def test_dataset_path_without_a_name_is_unchanged(self) -> None:
        self.assertEqual(dataset.dataset_path(), EXTRACT)
        self.assertEqual(dataset.dataset_path().relative_to(APP_ROOT), dataset.EXTRACT_PATH)
        self.assertEqual(dataset.dataset_path("ist_three_hospitals.csv"), EXTRACT)
        self.assertEqual(dataset.dataset_path("ist_joining_hospitals.csv"), DATA_DIR / "ist_joining_hospitals.csv")

    def test_dataset_path_takes_only_a_file_name(self) -> None:
        for name in ("", "../ist_three_hospitals.csv", "ist/ist_three_hospitals.csv", "/etc/passwd"):
            with self.assertRaises(ValueError, msg=name):
                dataset.dataset_path(name)

    def test_a_joining_hospital_without_an_extract_starts_empty(self) -> None:
        """The seam: switching the joining hospital to "start empty" is one entry."""
        empty = dataclasses.replace(dataset.JOINING_SITES[0], extract=None, expected_rows=0)
        with mock.patch.object(dataset, "JOINING_SITES", (empty,)):
            site = dataset.any_site_for("pl-290")
            self.assertIs(site, empty)
            self.assertTrue(dataset.is_joining("pl-290"))
            self.assertEqual(dataset.load_site_records("pl-290"), [])
            self.assertEqual(dataset.records_source(site), "empty")
            self.assertEqual(dataset.expected_record_count(site), 0)
            # The consortium hospitals still read their extract.
            self.assertEqual(len(dataset.load_site_records("ch-245")), 198)
        # A site that keeps its row count but has no extract still has no records.
        self.assertEqual(dataset.expected_record_count(dataclasses.replace(empty, expected_rows=5)), 0)

    def test_the_joining_file_is_utf8_with_lf_line_endings_and_the_documented_digest(self) -> None:
        raw = JOINING_EXTRACT.read_bytes()
        raw.decode("utf-8")
        self.assertNotIn(b"\r", raw)
        notice = (DATA_DIR / "NOTICE.md").read_text(encoding="utf-8")
        match = re.search(r"Joining extract SHA-256 \| `([0-9a-f]{64})`", notice)
        self.assertIsNotNone(match, "NOTICE.md must state the joining extract's SHA-256")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), match.group(1))

    def test_the_joining_file_holds_263_rows_all_for_pl_290(self) -> None:
        with JOINING_EXTRACT.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        self.assertEqual(tuple(reader.fieldnames or ()), dataset.CSV_COLUMNS)
        self.assertEqual(len(rows), 263)
        self.assertEqual({row["site_id"] for row in rows}, {"pl-290"})
        for column in extract_ist.NEVER_EXTRACT:
            self.assertNotIn(column, reader.fieldnames)
            self.assertNotIn(column.lower(), reader.fieldnames)
        for column in extract_ist.SOURCE_FIELDS:
            self.assertNotIn(column, reader.fieldnames)

    def test_load_site_records_reads_the_joining_hospital_from_its_own_file(self) -> None:
        records = dataset.load_site_records("pl-290")
        self.assertEqual(len(records), 263)
        # Three records have no six-month outcome (OCCODE 0, 8, 9 or blank), as NOTICE.md says.
        self.assertEqual(sum(isinstance(record.get(STROKE_LABEL_FIELD), bool) for record in records), 260)
        self.assertEqual([record["source_row"] for record in records], sorted(r["source_row"] for r in records))
        # Neither file holds the other's hospitals.
        self.assertEqual(dataset.load_site_records("pl-290", path=EXTRACT), [])
        self.assertEqual(dataset.load_site_records("uk-134", path=JOINING_EXTRACT), [])

    def test_the_record_checks_hold_for_both_files(self) -> None:
        files = {
            EXTRACT.name: {site.site_id: dataset.load_site_records(site.site_id) for site in dataset.SITES},
            JOINING_EXTRACT.name: {
                site.site_id: dataset.load_site_records(site.site_id) for site in dataset.JOINING_SITES
            },
        }
        for name, records in files.items():
            with self.subTest(file=name):
                check_records(self, records)
        three_rows = {record["source_row"] for rows in files[EXTRACT.name].values() for record in rows}
        joining_rows = {record["source_row"] for rows in files[JOINING_EXTRACT.name].values() for record in rows}
        self.assertFalse(three_rows & joining_rows, "a source row belongs to one hospital only")

    def test_the_script_uses_the_dataset_module_for_the_joining_hospital(self) -> None:
        self.assertIs(extract_ist.JOINING_SITES, dataset.JOINING_SITES)
        self.assertEqual(extract_ist.JOINING_OUTPUT, JOINING_EXTRACT)

    def test_the_selection_rule_extends_to_more_countries(self) -> None:
        rows = source_rows(FOURTH_COUNTRY_LAYOUT)
        self.assertEqual(extract_ist.selected_hospitals(rows), [11, 12, 13])
        self.assertEqual(extract_ist.selected_hospitals(rows, countries=4), [11, 12, 13, 290])
        self.assertEqual(extract_ist.selected_hospitals(rows, countries=5), [11, 12, 13, 290, 14])

    def test_extract_joining_picks_the_largest_hospital_of_the_fourth_country(self) -> None:
        rows = source_rows(FOURTH_COUNTRY_LAYOUT)
        with mock.patch.object(extract_ist, "read_source_rows", return_value=rows) as read:
            records = extract_ist.extract_joining(b"source bytes")
        read.assert_called_once_with(b"source bytes")
        expected_rows = [index for index, row in enumerate(rows, start=1) if row["HOSPNUM"] == "290"]
        self.assertEqual(len(records), 263)
        self.assertEqual({record["site_id"] for record in records}, {"pl-290"})
        self.assertEqual([record["source_row"] for record in records], expected_rows)
        self.assertEqual(records[0]["encounter_id"], f"pl-290-{expected_rows[0]:05d}")

    def test_extract_joining_stops_when_the_rule_picks_another_hospital(self) -> None:
        # Country D is still the fourth largest, but hospital 291 is now its largest.
        layout = [("A", 11, 900), ("B", 12, 800), ("C", 13, 700), ("D", 290, 263), ("D", 291, 270), ("E", 14, 100)]
        with mock.patch.object(extract_ist, "read_source_rows", return_value=source_rows(layout)):
            with self.assertRaisesRegex(extract_ist.ExtractionError, r"joining hospitals \[291\]"):
                extract_ist.extract_joining(b"source bytes")

    def test_extract_joining_stops_on_the_wrong_row_count(self) -> None:
        layout = [entry if entry != ("D", 290, 63) else ("D", 290, 62) for entry in FOURTH_COUNTRY_LAYOUT]
        with mock.patch.object(extract_ist, "read_source_rows", return_value=source_rows(layout)):
            with self.assertRaisesRegex(extract_ist.ExtractionError, "pl-290 has 262 rows, expected 263"):
                extract_ist.extract_joining(b"source bytes")

    def test_main_writes_both_extracts_from_one_source(self) -> None:
        three = [extract_ist.map_row(row, 1, "uk-134") for row in source_rows([("A", 134, 1)])]
        with tempfile.TemporaryDirectory() as folder:
            source = pathlib.Path(folder) / "IST_corrected.csv"
            source.write_bytes(b"source bytes")
            output = pathlib.Path(folder) / "three.csv"
            joining_output = pathlib.Path(folder) / "joining.csv"
            printed = io.StringIO()
            with mock.patch.object(extract_ist, "extract", return_value=three), mock.patch.object(
                extract_ist, "read_source_rows", return_value=source_rows(FOURTH_COUNTRY_LAYOUT)
            ), mock.patch.object(extract_ist, "download", side_effect=AssertionError("no download")):
                with contextlib.redirect_stdout(printed):
                    status = extract_ist.main(
                        ["--source", str(source), "--output", str(output), "--joining-output", str(joining_output)]
                    )
            self.assertEqual(status, 0)
            self.assertEqual(len(output.read_text(encoding="utf-8").splitlines()), 2)
            joining_lines = joining_output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(joining_lines[0], ",".join(dataset.CSV_COLUMNS))
            self.assertEqual(len(joining_lines), 264)
        self.assertIn("pl-290 (IST hospital 290, Poland): 263 rows", printed.getvalue())
        self.assertIn(f"wrote 263 rows to {joining_output}", printed.getvalue())

    def test_main_writes_neither_file_when_the_joining_check_fails(self) -> None:
        layout = [entry if entry != ("D", 290, 63) else ("D", 290, 62) for entry in FOURTH_COUNTRY_LAYOUT]
        with tempfile.TemporaryDirectory() as folder:
            source = pathlib.Path(folder) / "IST_corrected.csv"
            source.write_bytes(b"source bytes")
            output = pathlib.Path(folder) / "three.csv"
            joining_output = pathlib.Path(folder) / "joining.csv"
            errors = io.StringIO()
            with mock.patch.object(extract_ist, "extract", return_value=[]), mock.patch.object(
                extract_ist, "read_source_rows", return_value=source_rows(layout)
            ), contextlib.redirect_stderr(errors):
                status = extract_ist.main(
                    ["--source", str(source), "--output", str(output), "--joining-output", str(joining_output)]
                )
            self.assertEqual(status, 1)
            self.assertIn("pl-290 has 262 rows, expected 263", errors.getvalue())
            self.assertFalse(output.exists())
            self.assertFalse(joining_output.exists())

    def test_a_source_with_the_wrong_digest_writes_neither_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = pathlib.Path(folder) / "IST_corrected.csv"
            source.write_bytes(b"HOSPNUM,AGE\r\n290,70\r\n")
            output = pathlib.Path(folder) / "three.csv"
            joining_output = pathlib.Path(folder) / "joining.csv"
            with self.assertRaises(extract_ist.ExtractionError):
                extract_ist.extract_joining(source.read_bytes())
            with contextlib.redirect_stderr(io.StringIO()):
                status = extract_ist.main(
                    ["--source", str(source), "--output", str(output), "--joining-output", str(joining_output)]
                )
            self.assertEqual(status, 1)
            self.assertFalse(output.exists())
            self.assertFalse(joining_output.exists())


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
