#!/usr/bin/env python3
"""Make the PDFs and preview images for the site's Resources page, and check them.

The Resources page of the framework site (`#/resources`) offers the decks we
used to teach Dagents. Each deck is offered as a PowerPoint file and as a PDF
that opens in the browser. The list is `docs/presentation/resources.json`.

This script owns the `facts` field of every item in that list: sizes, SHA-256
digests, slide and page counts, the last-changed date, and the paths of the
PDF and preview image it makes in `docs/presentation/exports/`.

The PDFs are committed, not made in CI. Converting a deck needs LibreOffice
Impress, and the result depends on the fonts installed. In CI nobody would look
at the pages, so a broken conversion would be published unseen. A person runs
this script, looks at the result and commits it. `--check` (and
`tests/test_site_resources.py`) fails when a listed file changes after that,
so a deck cannot change without its PDF being made again.

Usage::

    python scripts/export_presentations.py --write   # convert changed decks, refresh the facts
    python scripts/export_presentations.py --check   # fail if a listed file changed since --write

`--write` needs LibreOffice Impress, PyMuPDF and Pillow::

    apt install libreoffice-impress fonts-liberation fonts-dejavu
    pip install pymupdf pillow

It converts only the decks whose content or preview slide changed, so editing a
Markdown file needs none of these. `--check` uses only the standard library.
"""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]
LIST = REPO_ROOT / "docs" / "presentation" / "resources.json"
EXPORTS = Path("docs/presentation/exports")

#: An item id becomes a published file name (`resources/<id>.pdf`), so it must
#: stay a plain, stable slug.
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

KINDS = ("deck", "pdf", "markdown")

PREVIEW_WIDTH = 480

#: The decks use Aptos, Calibri, Georgia, Menlo and Cascadia Mono, which
#: LibreOffice on Linux does not have. Without these aliases fontconfig picks
#: DejaVu Sans, which is wider than Aptos, and more lines wrap or clip. Liberation
#: Sans has Arial's widths, which are close to Aptos. Georgia maps to Liberation
#: Serif (Times widths): DejaVu Serif is about 28% wider, and slide titles in
#: Georgia wrapped onto their subtitles.
FONT_ALIASES = {
    "Aptos": "Liberation Sans",
    "Calibri": "Liberation Sans",
    "Calibri Light": "Liberation Sans",
    "Georgia": "Liberation Serif",
    "Menlo": "DejaVu Sans Mono",
    "Cascadia Mono": "DejaVu Sans Mono",
}

#: Bump when the conversion changes in a way the deck's digest does not show
#: (for example the PDF metadata step), so --write makes every PDF again.
EXPORT_STEPS_VERSION = 2


def export_settings() -> str:
    """A digest of everything besides the deck that decides how its PDF looks."""
    settings = {"fonts": FONT_ALIASES, "steps": EXPORT_STEPS_VERSION}
    return hashlib.sha256(json.dumps(settings, sort_keys=True).encode("utf-8")).hexdigest()[:16]


# --- reading the list and the files ------------------------------------------


def load() -> dict:
    return json.loads(LIST.read_text(encoding="utf-8"))


def save(document: dict) -> None:
    LIST.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def items(document: dict) -> list[dict]:
    return [item for group in document["groups"] for item in group["items"]]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def slide_count(pptx: Path) -> int:
    """Slides in the deck's show order, from `ppt/presentation.xml`."""
    with zipfile.ZipFile(pptx) as archive:
        xml = archive.read("ppt/presentation.xml").decode("utf-8")
    return len(re.findall(r"<p:sldId\b", xml))


def pdf_page_count(pdf: Path) -> int:
    """Pages in a PDF written by LibreOffice.

    LibreOffice writes each page object as plain text, so counting
    `/Type /Page` (and not `/Pages`) is exact for these files. It is not exact
    for PDFs that pack objects into compressed streams, which is why the
    reports' page counts come from PyMuPDF at `--write` time instead.
    """
    return len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", pdf.read_bytes()))


def webp_size(path: Path) -> tuple[int, int] | None:
    """Width and height of a lossy or lossless WebP, or None if it is not one."""
    data = path.read_bytes()[:30]
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8 ":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


def last_changed(relative: str) -> str:
    """The date of the last commit that changed a file, or today if it has
    uncommitted changes or git is not available."""
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--", relative],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        if dirty:
            return date.today().isoformat()
        stamp = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", relative],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return stamp or date.today().isoformat()
    except (OSError, subprocess.CalledProcessError):
        return date.today().isoformat()


# --- checking ----------------------------------------------------------------


def problems(document: dict | None = None) -> list[str]:
    """Every way the list and the files on disk disagree. Empty means in step."""
    document = document if document is not None else load()
    found: list[str] = []
    seen: set[str] = set()
    hint = "run `python scripts/export_presentations.py --write`"

    for item in items(document):
        item_id = item.get("id", "")
        where = f"{item_id or '?'}:"
        if not ID_PATTERN.match(item_id):
            found.append(f"{where} id must be a lower-case slug, because it names the published files")
        if item_id in seen:
            found.append(f"{where} id is used twice")
        seen.add(item_id)
        for field in ("title", "description", "audience", "source"):
            if not str(item.get(field, "")).strip():
                found.append(f"{where} `{field}` is empty")
        kind = item.get("kind")
        if kind not in KINDS:
            found.append(f"{where} kind {kind!r} is not one of {', '.join(KINDS)}")
            continue

        source = REPO_ROOT / item["source"]
        facts = item.get("facts") or {}
        if not source.is_file():
            found.append(f"{where} {item['source']} does not exist")
            continue
        if facts.get("sha256") != sha256(source) or facts.get("bytes") != source.stat().st_size:
            found.append(f"{where} {item['source']} changed since its facts were recorded; {hint}")
            continue
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(facts.get("updated", ""))):
            found.append(f"{where} `facts.updated` is not a date; {hint}")

        if kind == "deck":
            found += deck_problems(item, source, facts, hint)
        elif kind == "pdf":
            if not source.read_bytes().startswith(b"%PDF"):
                found.append(f"{where} {item['source']} is not a PDF")
            if not isinstance(facts.get("pages"), int) or facts["pages"] < 1:
                found.append(f"{where} `facts.pages` is missing; {hint}")

    for entry in document.get("excluded", []):
        path = entry.get("path", "")
        if not (REPO_ROOT / path).exists():
            found.append(f"excluded {path} does not exist; remove the entry")
        if not str(entry.get("reason", "")).strip():
            found.append(f"excluded {path} needs a reason")
        if path in {item.get("source") for item in items(document)}:
            found.append(f"{path} is both listed and excluded")
    for path in unaccounted(document):
        found.append(f"{path} is neither listed nor excluded in {LIST.relative_to(REPO_ROOT)}")
    return found


#: Files that could belong on the Resources page. Each one must be listed or
#: excluded with a reason, so a new deck cannot be added without a decision.
#: Decks count in any format, anywhere under docs/ or at the root; the PDFs this
#: script writes to EXPORTS are not candidates.
CANDIDATES = (
    *(f"{folder}*.{suffix}" for folder in ("docs/**/", "") for suffix in ("pptx", "ppt", "potx", "odp", "key")),
    "docs/presentation/**/*.md",
    "docs/presentation/**/*.pdf",
    "docs/reports/*.pdf",
    "*.pdf",
)


def unaccounted(document: dict, root: Path = REPO_ROOT) -> list[str]:
    """Candidate files under `root` that the list neither offers nor excludes."""
    known = {item.get("source") for item in items(document)}
    known |= {entry.get("path") for entry in document.get("excluded", [])}
    found = set()
    for pattern in CANDIDATES:
        for path in root.glob(pattern):
            relative = path.relative_to(root).as_posix()
            # Office lock files (~$deck.pptx), hidden files and this script's own
            # exports are not documents to list.
            if path.is_file() and not path.name.startswith(("~$", ".")) and not relative.startswith(f"{EXPORTS.as_posix()}/"):
                found.add(relative)
    return sorted(found - known)


def deck_problems(item: dict, source: Path, facts: dict, hint: str) -> list[str]:
    where = f"{item['id']}:"
    found = []
    if not source.read_bytes().startswith(b"PK"):
        found.append(f"{where} {item['source']} is not a .pptx (zip) file")
        return found
    slides = slide_count(source)
    if facts.get("slides") != slides:
        found.append(f"{where} the deck has {slides} slides but the list says {facts.get('slides')}; {hint}")

    pdf_facts = facts.get("pdf") or {}
    pdf = REPO_ROOT / str(pdf_facts.get("path", ""))
    if not pdf_facts.get("path") or not pdf.is_file():
        found.append(f"{where} its PDF is missing; {hint}")
    elif pdf_facts.get("sha256") != sha256(pdf) or pdf_facts.get("bytes") != pdf.stat().st_size:
        found.append(f"{where} {pdf_facts['path']} is not the file this script wrote; {hint}")
    elif not pdf.read_bytes().startswith(b"%PDF"):
        found.append(f"{where} {pdf_facts['path']} is not a PDF")
    elif pdf_page_count(pdf) != slides:
        found.append(f"{where} {pdf_facts['path']} has {pdf_page_count(pdf)} pages for {slides} slides; {hint}")
    elif pdf_facts.get("settings") != export_settings():
        found.append(f"{where} {pdf_facts['path']} was made with other fonts or export steps; {hint}")

    preview_facts = facts.get("preview") or {}
    preview = REPO_ROOT / str(preview_facts.get("path", ""))
    slide = item.get("previewSlide")
    if not isinstance(slide, int) or not 1 <= slide <= slides:
        found.append(f"{where} previewSlide must be a slide number from 1 to {slides}")
    if not preview_facts.get("path") or not preview.is_file():
        found.append(f"{where} its preview image is missing; {hint}")
    elif preview_facts.get("sha256") != sha256(preview) or preview_facts.get("slide") != slide:
        found.append(f"{where} its preview image is out of date; {hint}")
    elif webp_size(preview) != (preview_facts.get("width"), preview_facts.get("height")):
        found.append(f"{where} {preview_facts['path']} is not the WebP image the list describes")
    return found


# --- writing -----------------------------------------------------------------


def soffice() -> str:
    for name in ("soffice", "libreoffice"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("LibreOffice is not installed: apt install libreoffice-impress")


def fontconfig_file(directory: Path) -> Path:
    aliases = "\n".join(
        f'  <alias binding="same"><family>{name}</family>'
        f"<prefer><family>{substitute}</family></prefer></alias>"
        for name, substitute in FONT_ALIASES.items()
    )
    path = directory / "fonts.conf"
    path.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
        '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
        f"{aliases}\n</fontconfig>\n",
        encoding="utf-8",
    )
    return path


def convert(pptx: Path, target: Path, title: str) -> None:
    """Write `target` as LibreOffice's PDF export of `pptx`, titled `title`. Notes pages are not exported."""
    with tempfile.TemporaryDirectory() as scratch:
        scratch_dir = Path(scratch)
        env = dict(os.environ, FONTCONFIG_FILE=str(fontconfig_file(scratch_dir)))
        # A private profile, so a desktop LibreOffice that is already open does
        # not swallow the request.
        profile = (scratch_dir / "profile").as_uri()
        subprocess.run(
            [soffice(), "--headless", f"-env:UserInstallation={profile}",
             "--convert-to", "pdf", "--outdir", str(scratch_dir), str(pptx)],
            env=env, check=True, capture_output=True, timeout=600,
        )
        produced = scratch_dir / f"{pptx.stem}.pdf"
        if not produced.is_file():
            sys.exit(f"LibreOffice did not convert {pptx}; is libreoffice-impress installed?")
        target.parent.mkdir(parents=True, exist_ok=True)
        set_title(produced, target, title)


def set_title(source: Path, target: Path, title: str) -> None:
    """Copy the PDF to `target` with the item's title, which the browser shows on the tab.

    The decks' own metadata says "Presentation" and names the tool that made
    them as the author, so every PDF tab would read "Presentation".
    """
    try:
        import pymupdf
    except ImportError:
        sys.exit("--write needs PyMuPDF to set each PDF's title: pip install pymupdf")
    with pymupdf.open(source) as document:
        metadata = dict(document.metadata or {})
        metadata.update({"title": title, "author": "", "subject": "", "keywords": ""})
        document.set_metadata(metadata)
        document.save(target, garbage=1, deflate=True)


def render_preview(pdf: Path, slide: int, target: Path) -> tuple[int, int]:
    """Save one page of `pdf` as a small WebP and return its size."""
    import io

    try:
        import pymupdf
        from PIL import Image
    except ImportError:
        sys.exit("--write needs PyMuPDF and Pillow: pip install pymupdf pillow")

    page = pymupdf.open(pdf)[slide - 1]
    zoom = PREVIEW_WIDTH / page.rect.width
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
    image = Image.open(io.BytesIO(pixmap.tobytes("png"))).convert("RGB")
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, "WEBP", quality=80, method=6)
    return image.size


def pdf_pages_any(pdf: Path) -> int:
    try:
        import pymupdf
    except ImportError:
        sys.exit("--write needs PyMuPDF to count a report's pages: pip install pymupdf")
    return pymupdf.open(pdf).page_count


def write() -> int:
    document = load()
    for item in items(document):
        source = REPO_ROOT / item["source"]
        if not source.is_file():
            sys.exit(f"{item['id']}: {item['source']} does not exist")
        old = item.get("facts") or {}
        digest = sha256(source)
        unchanged = old.get("sha256") == digest
        facts = {
            "updated": old["updated"] if unchanged and old.get("updated") else last_changed(item["source"]),
            "bytes": source.stat().st_size,
            "sha256": digest,
        }
        if item["kind"] == "deck":
            facts.update(write_deck(item, source, old, unchanged))
        elif item["kind"] == "pdf":
            facts["pages"] = old["pages"] if unchanged and old.get("pages") else pdf_pages_any(source)
        item["facts"] = facts
    save(document)
    remaining = problems(document)
    for problem in remaining:
        print(f"  {problem}", file=sys.stderr)
    print(f"{LIST.relative_to(REPO_ROOT)}: {len(items(document))} items" + (" — problems remain" if remaining else ""))
    return 1 if remaining else 0


def write_deck(item: dict, source: Path, old: dict, unchanged: bool) -> dict:
    slides = slide_count(source)
    pdf_rel = EXPORTS / f"{item['id']}.pdf"
    preview_rel = EXPORTS / f"{item['id']}.webp"
    pdf = REPO_ROOT / pdf_rel
    preview = REPO_ROOT / preview_rel
    old_pdf = old.get("pdf") or {}
    old_preview = old.get("preview") or {}

    pdf_current = (
        unchanged
        and old_pdf.get("path") == pdf_rel.as_posix()
        and pdf.is_file()
        and old_pdf.get("sha256") == sha256(pdf)
        and old_pdf.get("settings") == export_settings()
    )
    if not pdf_current:
        print(f"{item['id']}: converting {item['source']}")
        convert(source, pdf, item["title"])
    pages = pdf_page_count(pdf)
    if pages != slides:
        sys.exit(f"{item['id']}: the PDF has {pages} pages for {slides} slides")

    slide = item["previewSlide"]
    preview_current = (
        pdf_current
        and old_preview.get("path") == preview_rel.as_posix()
        and old_preview.get("slide") == slide
        and preview.is_file()
        and old_preview.get("sha256") == sha256(preview)
    )
    if preview_current:
        width, height = old_preview["width"], old_preview["height"]
    else:
        print(f"{item['id']}: preview from slide {slide}")
        width, height = render_preview(pdf, slide, preview)

    return {
        "slides": slides,
        "pdf": {
            "path": pdf_rel.as_posix(),
            "bytes": pdf.stat().st_size,
            "sha256": sha256(pdf),
            "pages": pages,
            "settings": export_settings(),
        },
        "preview": {
            "path": preview_rel.as_posix(),
            "bytes": preview.stat().st_size,
            "sha256": sha256(preview),
            "slide": slide,
            "width": width,
            "height": height,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="convert changed decks and refresh the facts")
    mode.add_argument("--check", action="store_true", help="fail if a listed file changed since --write")
    args = parser.parse_args(argv)
    if args.write:
        return write()
    found = problems()
    for problem in found:
        print(problem)
    print(f"{len(found)} problem(s)" if found else "resources.json matches the files")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
