"""Tests for the teaching resources the framework site offers.

The Resources page (`#/resources`) reads `docs/presentation/resources.json`, and
the site build copies each listed deck, its PDF and its preview image into
`dist/resources/`. The PDFs are made by `scripts/export_presentations.py` and
committed, so these tests keep the list, the decks and the PDFs in step:

- every listed file exists, and its recorded size, SHA-256 digest and slide
  count match the file, so a deck cannot change without its PDF being made
  again;
- every deck and note under `docs/presentation/`, every report PDF and every
  PDF at the repository root is listed or excluded with a reason, so the page
  cannot fall behind the folder;
- no published file contains the deployed API's host. The guard in
  `test_publishing_guards.py` skips binary files, and these are all binary.

Each check is also run against the problem it is meant to catch.
"""

from __future__ import annotations

import copy
from pathlib import Path
import re
import sys
import tempfile
import unittest
from urllib.parse import urlsplit
import zipfile
import zlib

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import export_presentations as resources  # noqa: E402  (needs the sys.path entry)


def deck(document: dict, item_id: str) -> dict:
    return next(item for item in resources.items(document) if item["id"] == item_id)


class ResourceListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = resources.load()

    def test_the_list_matches_the_files(self) -> None:
        self.assertEqual(
            [],
            resources.problems(self.document),
            "run `python scripts/export_presentations.py --write`, look at the new PDFs, "
            "and commit them with resources.json",
        )

    def test_every_group_has_items_and_every_deck_a_pdf_and_a_preview(self) -> None:
        for group in self.document["groups"]:
            with self.subTest(group=group["id"]):
                self.assertTrue(group["items"], "an empty group shows a heading with nothing under it")
        for item in resources.items(self.document):
            if item["kind"] == "deck":
                with self.subTest(item=item["id"]):
                    self.assertIn("pdf", item["facts"])
                    self.assertIn("preview", item["facts"])

    def test_a_changed_deck_is_refused(self) -> None:
        """A deck edited after its PDF was made: the digest no longer matches."""
        document = copy.deepcopy(self.document)
        deck(document, "dagents-framework")["facts"]["sha256"] = "0" * 64
        found = resources.problems(document)
        self.assertTrue(any("dagents-framework" in p and "changed since" in p for p in found), found)

    def test_a_deck_pointing_at_another_file_is_refused(self) -> None:
        document = copy.deepcopy(self.document)
        deck(document, "case-study-stroke")["source"] = deck(document, "case-study-sepsis")["source"]
        found = resources.problems(document)
        self.assertTrue(any(p.startswith("case-study-stroke:") for p in found), found)

    def test_a_deck_without_its_pdf_is_refused(self) -> None:
        document = copy.deepcopy(self.document)
        deck(document, "federated-stroke")["facts"]["pdf"]["path"] = "docs/presentation/exports/missing.pdf"
        found = resources.problems(document)
        self.assertTrue(any("federated-stroke" in p and "PDF is missing" in p for p in found), found)

    def test_a_pdf_from_another_deck_is_refused(self) -> None:
        """The PDF exists and is unchanged, but it was made from a different deck."""
        document = copy.deepcopy(self.document)
        target = deck(document, "case-study-sepsis")["facts"]
        target["pdf"] = copy.deepcopy(deck(document, "federated-stroke")["facts"]["pdf"])
        found = resources.problems(document)
        self.assertTrue(any("case-study-sepsis" in p and "pages for" in p for p in found), found)

    def test_a_stale_preview_is_refused(self) -> None:
        document = copy.deepcopy(self.document)
        deck(document, "dagents-framework")["previewSlide"] = 3
        found = resources.problems(document)
        self.assertTrue(any("dagents-framework" in p and "preview" in p for p in found), found)

    def test_ids_name_files_so_they_must_be_slugs_and_unique(self) -> None:
        document = copy.deepcopy(self.document)
        items = resources.items(document)
        items[1]["id"] = items[0]["id"]
        items[2]["id"] = "Not A Slug"
        found = resources.problems(document)
        self.assertTrue(any("used twice" in p for p in found), found)
        self.assertTrue(any("lower-case slug" in p for p in found), found)

    def test_an_exclusion_needs_a_reason(self) -> None:
        document = copy.deepcopy(self.document)
        document["excluded"][0]["reason"] = " "
        self.assertTrue(any("needs a reason" in p for p in resources.problems(document)))


class CoverageTests(unittest.TestCase):
    """Every deck, note and report is offered on the page or excluded with a reason."""

    def test_nothing_is_unaccounted_for(self) -> None:
        self.assertEqual([], resources.unaccounted(resources.load()))

    def test_a_new_deck_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for name in (
                "docs/presentation/new-deck.pptx",
                "docs/presentation/case/notes.md",
                "docs/reports/report.pdf",
                "export.pdf",
                "docs/presentation/~$new-deck.pptx",
            ):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b"x")
            document = {"groups": [], "excluded": [{"path": "export.pdf", "reason": "an old copy"}]}
            self.assertEqual(
                ["docs/presentation/case/notes.md", "docs/presentation/new-deck.pptx", "docs/reports/report.pdf"],
                resources.unaccounted(document, root),
            )

    def test_a_deck_in_another_format_or_folder_is_found(self) -> None:
        found = [
            "docs/presentation/export.pdf",
            "docs/presentation/old.ppt",
            "docs/presentation/talk.odp",
            "docs/presentation/talk.key",
            "docs/presentation/theme.potx",
            "docs/slides/x.pptx",
            "talk.pptx",
        ]
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            for name in [*found, "docs/presentation/exports/deck.pdf"]:
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_bytes(b"x")
            # The script's own exports are not candidates.
            self.assertEqual(sorted(found), resources.unaccounted({"groups": [], "excluded": []}, root))


class PublishedFileTests(unittest.TestCase):
    """The files copied into the site carry no deployed address."""

    PUBLISHED_ENV = REPO_ROOT / "env" / ".env.published"

    def api_host(self) -> str:
        for line in self.PUBLISHED_ENV.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == "HEALTHCARE_DEMO_PUBLIC_API_URL":
                host = (urlsplit(value.strip()).hostname or "").lower()
                if host:
                    return host
        self.fail("env/.env.published declares no HEALTHCARE_DEMO_PUBLIC_API_URL")

    @staticmethod
    def published_paths() -> list[Path]:
        paths = []
        for item in resources.items(resources.load()):
            if item["kind"] in ("deck", "pdf"):
                paths.append(REPO_ROOT / item["source"])
            facts = item.get("facts", {})
            for key in ("pdf", "preview"):
                if key in facts:
                    paths.append(REPO_ROOT / facts[key]["path"])
        return paths

    @staticmethod
    def readable_parts(path: Path) -> list[bytes]:
        """The bytes a reader could see: zip members of a deck; for a PDF, its
        inflated streams and the text, links and metadata PyMuPDF extracts.

        Text in a LibreOffice or LaTeX PDF is often stored as glyph numbers
        (CID fonts), so a byte search of its streams misses it. The two course
        reports have no deck behind them, so their text is read with PyMuPDF.
        PyMuPDF is required: without it this check would pass without reading
        the text, so the test fails instead of skipping.
        """
        data = path.read_bytes()
        parts = [data]
        if data.startswith(b"PK"):
            with zipfile.ZipFile(path) as archive:
                parts += [archive.read(name) for name in archive.namelist()]
        elif data.startswith(b"%PDF"):
            for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
                try:
                    parts.append(zlib.decompressobj().decompress(match.group(1)))
                except zlib.error:
                    continue
            try:
                import pymupdf
            except ImportError as error:  # pragma: no cover - CI installs it
                raise AssertionError(
                    "PyMuPDF is needed to read the text of the published PDFs: pip install pymupdf"
                ) from error
            with pymupdf.open(stream=data, filetype="pdf") as document:
                parts.append(" ".join(str(value) for value in (document.metadata or {}).values()).encode("utf-8"))
                for page in document:
                    parts.append(page.get_text().encode("utf-8"))
                    parts += [str(link.get("uri", "")).encode("utf-8") for link in page.get_links()]
        return parts

    def test_every_published_file_is_checked(self) -> None:
        paths = self.published_paths()
        decks = [item for item in resources.items(resources.load()) if item["kind"] == "deck"]
        self.assertGreaterEqual(len(paths), 3 * len(decks))

    def test_no_published_file_contains_the_api_host(self) -> None:
        host = self.api_host().encode("ascii")
        for path in self.published_paths():
            with self.subTest(path=path.relative_to(REPO_ROOT).as_posix()):
                found = any(host in part.lower() for part in self.readable_parts(path))
                self.assertFalse(found, "the deployed API's address must stay in env/ and .github/")

    def test_the_search_reads_text_a_byte_search_misses(self) -> None:
        """A host typed in a CID-encoded font, as LaTeX and LibreOffice write text, is found."""
        import pymupdf

        document = pymupdf.open()
        page = document.new_page()
        page.insert_font(fontname="F0", fontbuffer=pymupdf.Font("cjk").buffer)
        page.insert_text((72, 72), "Call https://api.example.test/v1 for the data", fontname="F0", fontsize=11)
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "report.pdf"
            path.write_bytes(document.tobytes(deflate=True, garbage=3))
            raw = [path.read_bytes()] + [
                zlib.decompressobj().decompress(match.group(1))
                for match in re.finditer(rb"stream\r?\n(.*?)endstream", path.read_bytes(), re.S)
            ]
            self.assertFalse(any(b"api.example.test" in part for part in raw), "the font must hide the text from a byte search")
            self.assertTrue(any(b"api.example.test" in part for part in self.readable_parts(path)))

    def test_the_search_reads_inside_a_deck(self) -> None:
        """A host written on a slide is found, although the .pptx is compressed."""
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch) / "deck.pptx"
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("ppt/slides/slide1.xml", "<a:t>https://api.example.test/v1</a:t>")
            self.assertFalse(b"api.example.test" in path.read_bytes())
            self.assertTrue(any(b"api.example.test" in part for part in self.readable_parts(path)))


if __name__ == "__main__":
    unittest.main()
