"""Tests for the link previews and icons of the three published sites.

A link to a site shared on LinkedIn, WhatsApp, Slack or X shows the image named
by the page's `og:image` tag. The cards and the logo mark are drawn in
`design/social/`, and `scripts/render_social_cards.mjs` renders them into each
frontend's `public/` folder. The PNGs are committed, so publishing needs no
browser and no fonts. These tests check:

- each `index.html` has every link preview tag, names its card through the
  `%PUBLIC_SITE_URL%` placeholder, and links its icons;
- the Vite plugin (`social-meta.mjs`) turns the placeholder into the published
  address from `env/.env.published`, and refuses an address a crawler cannot
  use;
- the Pages workflow gives each build its published address, and its three
  checks (`scripts/check_link_previews.py`) fail on broken addresses, broken
  built pages and a published image that does not load;
- each card is 1200 x 630 and under 300 KB, each icon has its size, and every
  output is current with the files it was made from (`design/social/rendered.json`);
- the cards and descriptions show no number that can change, as a digit or as
  a word. The stroke triage card lists the hospitals the demo defines, its
  texts give their count as the demo defines it, and the NL2SQL card's question
  is one of the demo's samples.

Each record check is also run against the problem it is meant to catch.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
import hashlib
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SOCIAL = REPO_ROOT / "design" / "social"
RECORD = SOCIAL / "rendered.json"
PUBLISHED = REPO_ROOT / "env" / ".env.published"
PAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"
RENDER_SCRIPT = "scripts/render_social_cards.mjs"
CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_link_previews.py"
DATASET = REPO_ROOT / "apps" / "healthcare-demo" / "backend" / "app" / "domain" / "dataset.py"

PLACEHOLDER = "%PUBLIC_SITE_URL%"
CARD = "social-preview.png"
CARD_SIZE = (1200, 630)
#: WhatsApp drops a preview image above about 300 KB.
MAX_CARD_BYTES = 300 * 1024
#: Each icon and its size in pixels; None for the SVG.
ICONS = {"favicon.svg": None, "favicon-32.png": 32, "apple-touch-icon.png": 180, "icon-512.png": 512}
FONT_PACKAGES = ("@fontsource/archivo", "@fontsource/ibm-plex-mono")


@dataclass(frozen=True)
class Site:
    name: str
    #: The frontend's folder: index.html, vite.config.ts, social-meta.mjs, public/.
    folder: str
    #: Its published address in env/.env.published.
    address_key: str
    #: Its card in design/social/.
    card: str

    @property
    def public(self) -> str:
        return f"{self.folder}/public"


SITES = (
    Site("framework site", "site", "DAGENTS_PUBLIC_SITE_URL", "framework.html"),
    Site("stroke triage demo", "apps/healthcare-demo/frontend", "HEALTHCARE_DEMO_PUBLIC_SITE_URL", "healthcare.html"),
    Site("NL2SQL demo", "services/nl2sql-demo/frontend", "NL2SQL_DEMO_PUBLIC_SITE_URL", "nl2sql.html"),
)

#: Every `<meta>` each page must have, by its `property` or `name`.
REQUIRED_META = (
    "description",
    "og:type",
    "og:site_name",
    "og:title",
    "og:description",
    "og:url",
    "og:image",
    "og:image:secure_url",
    "og:image:type",
    "og:image:width",
    "og:image:height",
    "og:image:alt",
    "twitter:card",
    "twitter:title",
    "twitter:description",
    "twitter:image",
    "twitter:image:alt",
    "theme-color",
)

#: Filler and emphasis words the writing skill rules out.
FILLER = re.compile(
    r"\b(simply|just|really|actually|very|quite|honest(ly)?|crucially|seamless(ly)?|powerful|"
    r"cutting-edge|revolutionary|effortless(ly)?|robust|leverage)\b",
    re.IGNORECASE,
)

#: A count written as a word changes as a digit does ("forty endpoints").
COUNT_WORDS = {2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
NUMBER_WORD = (
    r"zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
    r"hundreds?|thousands?|millions?|billions?|dozens?"
)


# --- helpers ----------------------------------------------------------------


class HeadParser(HTMLParser):
    """The title, every `<meta>` and every `<link>` of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, list[dict[str, str]]] = {}
        self.links: list[dict[str, str]] = []
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key:
                self.meta.setdefault(key, []).append(values)
        elif tag == "link":
            self.links.append(values)
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data

    def content(self, key: str) -> str:
        """The content of the one tag with this key; fails when there are none or several."""
        found = self.meta.get(key, [])
        if len(found) != 1:
            raise AssertionError(f"expected one {key} tag, found {len(found)}")
        return found[0].get("content", "")


def parse_head(html: str) -> HeadParser:
    parser = HeadParser()
    parser.feed(html)
    return parser


class CardText(HTMLParser):
    """The visible text of a card, split into what sits inside named classes and the rest."""

    def __init__(self, allowed_classes: set[str]) -> None:
        super().__init__(convert_charrefs=True)
        self.allowed_classes = allowed_classes
        self.stack: list[tuple[str, bool]] = []
        self.free_text: list[str] = []
        self.allowed_text: list[str] = []
        self.skip = 0

    VOID = {"meta", "link", "img", "br", "hr", "input", "path"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.VOID:
            return
        classes = set((dict(attrs).get("class") or "").split())
        inside = bool(classes & self.allowed_classes) or (bool(self.stack) and self.stack[-1][1])
        self.stack.append((tag, inside))
        if tag in ("style", "title", "script"):
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.VOID:
            return
        while self.stack:
            open_tag, _ = self.stack.pop()
            if open_tag in ("style", "title", "script"):
                self.skip -= 1
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        if self.skip or not data.strip():
            return
        inside = bool(self.stack) and self.stack[-1][1]
        (self.allowed_text if inside else self.free_text).append(data.strip())


def demo_hospitals() -> list[tuple[str, str]]:
    """Each hospital in the stroke demo's `SITES`, as (name, schema) the way the card prints them."""
    sites = []
    for node in ast.walk(ast.parse(DATASET.read_text(encoding="utf-8"))):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "SITES":
            for call in node.value.elts:
                values = [ast.literal_eval(argument) for argument in call.args]
                sites.append((f"{values[1]} · {values[2]}", f"schema {values[4]}"))
    if not sites:
        raise AssertionError(f"SITES not found in {DATASET.relative_to(REPO_ROOT)}")
    return sites


def changing_numbers(text: str, hospitals: int | None = None) -> list[str]:
    """Every number in a link preview text, as a digit or a word, that can change.

    "NL2SQL" is a name. With `hospitals`, "<that count> hospitals" is allowed:
    it is the stroke demo's structure, and the test ties it to `SITES`. Any
    other count of hospitals is returned.
    """
    text = text.replace("NL2SQL", "NLSQL")
    found = []
    for match in re.finditer(rf"\b({NUMBER_WORD})\s+hospitals\b", text, re.IGNORECASE):
        if hospitals is None or match.group(1).lower() != COUNT_WORDS.get(hospitals):
            found.append(match.group(0))
    text = re.sub(rf"\b({NUMBER_WORD})\s+hospitals\b", " ", text, flags=re.IGNORECASE)
    found += re.findall(r"\d+", text)
    found += [match.group(0) for match in re.finditer(rf"\b({NUMBER_WORD})\b", text, re.IGNORECASE)]
    return found


def published_addresses() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in PUBLISHED.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def png_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()[:24]
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


def load_record() -> dict:
    return json.loads(RECORD.read_text(encoding="utf-8"))


def locked_font_versions() -> dict[str, str]:
    """The @fontsource versions the framework site's lockfile installs."""
    lock = json.loads((REPO_ROOT / "site" / "package-lock.json").read_text(encoding="utf-8"))
    return {name: lock["packages"][f"node_modules/{name}"]["version"] for name in FONT_PACKAGES}


def expected_outputs() -> list[str]:
    return sorted(f"{site.public}/{file}" for site in SITES for file in (CARD, *ICONS))


def stale_outputs(record: dict, root: Path, fonts: dict[str, str], published: dict[str, str]) -> list[str]:
    """Every way the record and the files disagree. An empty list means the outputs are current."""
    problems = []
    outputs = record.get("outputs", {})
    for path in expected_outputs():
        if path not in outputs:
            problems.append(f"{path} is not in {RECORD.name}")
    for path, entry in outputs.items():
        output = root / path
        if not output.is_file():
            problems.append(f"{path} is missing")
        elif sha256(output) != entry["sha256"]:
            problems.append(f"{path} changed after the render")
        for source, digest in entry["sources"].items():
            if not (root / source).is_file():
                problems.append(f"{path} was made from {source}, which is missing")
            elif sha256(root / source) != digest:
                problems.append(f"{source} changed after {path} was rendered")
        for name, version in entry.get("fonts", {}).items():
            if fonts.get(name) != version:
                problems.append(f"{path} was rendered with {name} {version}; the lockfile has {fonts.get(name)}")
        for key, value in entry.get("published", {}).items():
            if published.get(key) != value:
                problems.append(f"{path} shows {key}={value}; {PUBLISHED.name} has {published.get(key)}")
    return problems


def run_plugin(cases: list[dict]) -> list[dict]:
    """Run each frontend's social-meta.mjs on the given cases with Node and return the results."""
    node = shutil.which("node")
    if node is None:
        raise AssertionError("node is not installed; the link preview plugin cannot run")
    script = r"""
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const cases = JSON.parse(readFileSync(0, "utf8"));
const results = [];
for (const item of cases) {
  const plugin = await import(pathToFileURL(item.plugin).href);
  try {
    const { prefix, absolute } = plugin.publicSitePrefix(item.value ?? undefined, item.base);
    const html = item.html === undefined ? null : plugin.fillPublicSiteUrl(item.html, prefix);
    results.push({ ok: true, prefix, absolute, html });
  } catch (error) {
    results.push({ ok: false, error: String(error.message) });
  }
}
process.stdout.write(JSON.stringify(results));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    if completed.returncode != 0:
        raise AssertionError(f"node failed: {completed.stderr}")
    return json.loads(completed.stdout)


def site_index(site: Site) -> str:
    return (REPO_ROOT / site.folder / "index.html").read_text(encoding="utf-8")


# --- the tags in each index.html ------------------------------------------


class PageTagTests(unittest.TestCase):
    def test_every_page_has_every_tag(self) -> None:
        for site in SITES:
            head = parse_head(site_index(site))
            with self.subTest(site=site.name):
                self.assertTrue(head.title.strip(), "the page has no title")
                missing = [key for key in REQUIRED_META if not any(m.get("content") for m in head.meta.get(key, []))]
                self.assertEqual([], missing, f"{site.folder}/index.html lacks these tags")

    def test_the_tags_name_the_card_through_the_placeholder(self) -> None:
        """The absolute address is written at build time, never in the HTML."""
        for site in SITES:
            head = parse_head(site_index(site))
            with self.subTest(site=site.name):
                self.assertEqual(PLACEHOLDER, head.content("og:url"))
                for key in ("og:image", "og:image:secure_url", "twitter:image"):
                    self.assertEqual(f"{PLACEHOLDER}{CARD}", head.content(key), key)
                self.assertEqual("website", head.content("og:type"))
                self.assertEqual("Dagents", head.content("og:site_name"))
                self.assertEqual("image/png", head.content("og:image:type"))
                self.assertEqual(str(CARD_SIZE[0]), head.content("og:image:width"))
                self.assertEqual(str(CARD_SIZE[1]), head.content("og:image:height"))
                self.assertEqual("summary_large_image", head.content("twitter:card"))

    def test_no_page_holds_a_published_address(self) -> None:
        addresses = [value for key, value in published_addresses().items() if key.endswith("_PUBLIC_SITE_URL")]
        self.assertTrue(addresses)
        for site in SITES:
            html = site_index(site)
            for address in addresses:
                with self.subTest(site=site.name, address=address):
                    self.assertNotIn(urlsplit(address).netloc, html, "the address must come from the build")

    def test_the_texts_agree_and_follow_the_writing_rules(self) -> None:
        for site in SITES:
            head = parse_head(site_index(site))
            with self.subTest(site=site.name):
                description = head.content("description")
                self.assertEqual(description, head.content("og:description"))
                self.assertEqual(description, head.content("twitter:description"))
                self.assertEqual(head.content("og:title"), head.content("twitter:title"))
                self.assertEqual(head.content("og:image:alt"), head.content("twitter:image:alt"))
                self.assertGreaterEqual(len(description), 100, "a description of about 150 characters")
                self.assertLessEqual(len(description), 200, "a description of about 150 characters")
                sentences = [part for part in re.split(r"(?<=\.)\s+", description) if part]
                self.assertLessEqual(len(sentences), 2, "a description is one or two sentences")
                # A count, a score or a date changes; the tags describe structure and
                # purpose. The stroke demo may say how many hospitals it has, as SITES does.
                hospitals = len(demo_hospitals()) if site.card == "healthcare.html" else None
                for text in (head.title, description, head.content("og:title"), head.content("og:image:alt")):
                    self.assertEqual([], changing_numbers(text, hospitals), f"a number in link preview text: {text!r}")
                    self.assertIsNone(FILLER.search(text), f"filler word in {text!r}")

    def test_the_number_check_catches_digits_and_words(self) -> None:
        three = len(demo_hospitals())
        caught = {
            "Typed planners decide what each of forty endpoints may see.": None,
            "Records from 19435 patients.": None,
            "Two hundred records at each hospital.": None,
            f"{COUNT_WORDS[three + 1].capitalize()} hospitals keep their records apart.": three,
            "Hospitals keep their records apart.  Three hospitals train a model.": None,
        }
        for text, hospitals in caught.items():
            with self.subTest(text=text):
                self.assertNotEqual([], changing_numbers(text, hospitals))
        allowed = {
            "An NL2SQL demo.": None,
            f"{COUNT_WORDS[three].capitalize()} hospitals keep their records apart.": three,
        }
        for text, hospitals in allowed.items():
            with self.subTest(text=text):
                self.assertEqual([], changing_numbers(text, hospitals))

    def test_the_stroke_demo_texts_give_the_hospital_count_from_sites(self) -> None:
        """If a hospital is added, the description and the alt text must change with the card."""
        site = next(site for site in SITES if site.card == "healthcare.html")
        head = parse_head(site_index(site))
        phrase = f"{COUNT_WORDS[len(demo_hospitals())]} hospitals"
        for key in ("description", "og:image:alt"):
            with self.subTest(tag=key):
                self.assertIn(phrase, head.content(key).lower())

    def test_every_page_links_its_icons_and_theme_colours(self) -> None:
        for site in SITES:
            head = parse_head(site_index(site))
            links = {(link.get("rel"), link.get("href")): link for link in head.links}
            with self.subTest(site=site.name):
                self.assertEqual("image/svg+xml", links[("icon", "/favicon.svg")].get("type"))
                png = links[("icon", "/favicon-32.png")]
                self.assertEqual(("image/png", "32x32"), (png.get("type"), png.get("sizes")))
                self.assertIn(("apple-touch-icon", "/apple-touch-icon.png"), links)
                self.assertIn(("manifest", "/site.webmanifest"), links)
                schemes = sorted(tag.get("media", "") for tag in head.meta["theme-color"])
                self.assertEqual(["(prefers-color-scheme: dark)", "(prefers-color-scheme: light)"], schemes)

    def test_each_vite_config_runs_the_plugin_from_its_own_folder(self) -> None:
        """The healthcare demo must stay extractable, so each frontend imports its own copy."""
        for site in SITES:
            config = (REPO_ROOT / site.folder / "vite.config.ts").read_text(encoding="utf-8")
            with self.subTest(site=site.name):
                self.assertIn('from "./social-meta.mjs"', config)
                self.assertRegex(config, r"plugins:\s*\[[^\]]*socialMeta\(\)")
                copy_text = (REPO_ROOT / site.folder / "social-meta.mjs").read_text(encoding="utf-8")
                self.assertIn("GENERATED FILE", copy_text)
                self.assertTrue(copy_text.endswith((SOCIAL / "social-meta.mjs").read_text(encoding="utf-8")))


# --- the plugin writes the published address --------------------------------


class PublishedAddressTests(unittest.TestCase):
    def test_the_published_addresses_are_usable(self) -> None:
        published = published_addresses()
        root = published[SITES[0].address_key]
        for site in SITES:
            address = published.get(site.address_key, "")
            with self.subTest(key=site.address_key):
                parts = urlsplit(address)
                self.assertEqual("https", parts.scheme, "crawlers need an https og:image")
                self.assertTrue(address.endswith("/"))
                self.assertFalse(parts.query or parts.fragment or parts.username, address)
                # The Pages workflow publishes the demos inside the framework site's tree.
                self.assertTrue(address.startswith(root), f"{address} is not under {root}")



class SocialMetaPluginTests(unittest.TestCase):
    """The Vite plugin, run with Node. The CI contracts job sets up Node and requires this class to run."""

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("node") is None:
            raise unittest.SkipTest("node is not installed; the link preview plugin cannot run")

    def test_the_build_writes_each_published_address_into_the_tags(self) -> None:
        published = published_addresses()
        cases = [
            {
                "plugin": str(REPO_ROOT / site.folder / "social-meta.mjs"),
                "value": published[site.address_key],
                "base": urlsplit(published[site.address_key]).path,
                "html": site_index(site),
            }
            for site in SITES
        ]
        for site, result in zip(SITES, run_plugin(cases)):
            address = published[site.address_key]
            with self.subTest(site=site.name):
                self.assertTrue(result["ok"], result.get("error"))
                self.assertTrue(result["absolute"])
                head = parse_head(result["html"])
                self.assertNotIn(PLACEHOLDER, result["html"])
                self.assertEqual(address, head.content("og:url"))
                for key in ("og:image", "og:image:secure_url", "twitter:image"):
                    self.assertEqual(f"{address}{CARD}", head.content(key), key)

    def test_a_build_without_the_address_uses_the_base_path(self) -> None:
        """A local or CI build still works; only the Pages build needs the address."""
        plugin = str(REPO_ROOT / SITES[0].folder / "social-meta.mjs")
        result = run_plugin([{"plugin": plugin, "value": None, "base": "/", "html": site_index(SITES[0])}])[0]
        self.assertTrue(result["ok"], result.get("error"))
        self.assertFalse(result["absolute"])
        self.assertEqual(f"/{CARD}", parse_head(result["html"]).content("og:image"))

    def test_the_plugin_refuses_an_address_a_crawler_cannot_use(self) -> None:
        plugin = str(REPO_ROOT / SITES[0].folder / "social-meta.mjs")
        refused = {
            "http://example.org/site/": "/site/",
            "https://example.org/site/?ref=1": "/site/",
            "https://user:secret@example.org/site/": "/site/",
            "https://example.org/site": "/site/",
            "https://example.org/other/": "/site/",
            "not an address": "/site/",
        }
        results = run_plugin([{"plugin": plugin, "value": value, "base": base} for value, base in refused.items()])
        for (value, _), result in zip(refused.items(), results):
            with self.subTest(value=value):
                self.assertFalse(result["ok"], f"{value} was accepted")


# --- the Pages workflow -----------------------------------------------------


class PagesWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = yaml.safe_load(PAGES_WORKFLOW.read_text(encoding="utf-8"))
        self.build = self.workflow["jobs"]["build"]["steps"]
        self.deploy = self.workflow["jobs"]["deploy"]["steps"]

    def step_index(self, steps: list[dict], name: str) -> int:
        for index, step in enumerate(steps):
            if step.get("name") == name:
                return index
        self.fail(f"pages.yml has no step named {name!r}")

    def test_the_check_script_covers_every_site(self) -> None:
        self.assertEqual([site.address_key for site in SITES], list(load_check_script().KEYS))

    def test_each_build_gets_its_published_address(self) -> None:
        resolve = self.step_index(self.build, "Resolve the published addresses")
        self.assertEqual(
            'python scripts/check_link_previews.py addresses --github-env "$GITHUB_ENV"',
            self.build[resolve]["run"].strip(),
        )
        for site in SITES:
            with self.subTest(site=site.name):
                build = next(
                    (index for index, step in enumerate(self.build) if step.get("working-directory") == site.folder
                     and "npm run build" in step.get("run", "")),
                    None,
                )
                self.assertIsNotNone(build, f"no build step for {site.folder}")
                self.assertLess(resolve, build, "the addresses must be resolved before the build")
                self.assertEqual(
                    f"${{{{ env.{site.address_key} }}}}", self.build[build]["env"].get("PUBLIC_SITE_URL")
                )

    def test_the_built_pages_are_checked_before_the_upload(self) -> None:
        check = self.step_index(self.build, "Every page has a link preview at its published address")
        assemble = self.step_index(self.build, "Assemble the published tree")
        upload = next(index for index, step in enumerate(self.build) if "upload-pages-artifact" in step.get("uses", ""))
        self.assertLess(assemble, check)
        self.assertLess(check, upload)
        self.assertEqual("python scripts/check_link_previews.py built --tree _site", self.build[check]["run"].strip())
        self.assertEqual("_site", self.build[upload]["with"]["path"])

    def test_the_deploy_requests_each_published_image(self) -> None:
        deploy = next(index for index, item in enumerate(self.deploy) if "deploy-pages" in item.get("uses", ""))
        checkout = next(index for index, item in enumerate(self.deploy) if "actions/checkout" in item.get("uses", ""))
        step = self.step_index(self.deploy, "Each published page's link preview image loads")
        self.assertLess(deploy, step, "the step checks the deployed pages")
        self.assertLess(checkout, step, "the step runs a script from the repository")
        # The deploy job installs nothing, so the script needs only the runner's python3.
        self.assertEqual("python3 scripts/check_link_previews.py published", self.deploy[step]["run"].strip())

    def test_no_new_step_pastes_an_expression_into_its_script(self) -> None:
        """Values reach a script through env:, never through ${{ }} inside run: (the ci skill)."""
        names = (
            "Resolve the published addresses",
            "Every page has a link preview at its published address",
        )
        for name in names:
            with self.subTest(step=name):
                self.assertNotIn("${{", self.build[self.step_index(self.build, name)]["run"])
        deploy = self.deploy[self.step_index(self.deploy, "Each published page's link preview image loads")]
        self.assertNotIn("${{", deploy["run"])


def load_check_script():
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_link_previews", CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Its dataclasses look their module up in sys.modules while the module loads.
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def run_check(*args: str, published: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run scripts/check_link_previews.py as the Pages workflow does, with its own env file."""
    with tempfile.TemporaryDirectory() as directory:
        env_file = Path(directory) / ".env.published"
        values = published_addresses() if published is None else published
        env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
        environment = dict(os.environ)
        # The stand-in server is local; a proxy in the environment must not intercept it.
        for name in ("NO_PROXY", "no_proxy"):
            environment[name] = ",".join(filter(None, [environment.get(name, ""), "127.0.0.1", "localhost"]))
        return subprocess.run(
            [sys.executable, str(CHECK_SCRIPT), "--published", str(env_file), *args],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
            env=environment,
        )


def write_built_tree(tree: Path, published: dict[str, str], prefix_for=None) -> None:
    """Lay out each site as the Pages build does: its index.html with the address filled in, and its public files."""
    root = published[SITES[0].address_key]
    for site in SITES:
        address = published[site.address_key]
        folder = tree / address[len(root):]
        folder.mkdir(parents=True, exist_ok=True)
        for item in (REPO_ROOT / site.public).iterdir():
            if item.is_file() and item.name != "recording.json":
                shutil.copyfile(item, folder / item.name)
        prefix = prefix_for(site, address) if prefix_for else address
        (folder / "index.html").write_text(site_index(site).replace(PLACEHOLDER, prefix), encoding="utf-8")


class LinkPreviewCheckTests(unittest.TestCase):
    """The three checks in scripts/check_link_previews.py, run on good inputs and on each breakage."""

    def assertFails(self, completed: subprocess.CompletedProcess, needle: str) -> None:  # noqa: N802
        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("FAIL", completed.stdout)
        self.assertIn(needle, completed.stdout)

    # --- addresses

    def test_the_published_addresses_pass_and_reach_the_builds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            github_env = Path(directory) / "github_env"
            completed = run_check("addresses", "--github-env", str(github_env))
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            published = published_addresses()
            expected = "".join(f"{site.address_key}={published[site.address_key]}\n" for site in SITES)
            self.assertEqual(expected, github_env.read_text(encoding="utf-8"))

    def test_a_broken_address_stops_the_deploy(self) -> None:
        good = published_addresses()
        healthcare = SITES[1].address_key
        cases = {
            "missing": ({key: value for key, value in good.items() if key != healthcare}, "is missing"),
            "http": ({**good, healthcare: good[healthcare].replace("https://", "http://")}, "https address"),
            "no trailing slash": ({**good, healthcare: good[healthcare].rstrip("/")}, "must end in /"),
            "query string": ({**good, healthcare: good[healthcare] + "?ref=x"}, "plain address"),
            "credentials": ({**good, healthcare: good[healthcare].replace("https://", "https://u:p@")}, "plain address"),
            "trailing space": ({**good, healthcare: good[healthcare] + " "}, "plain address"),
            "outside the site": ({**good, healthcare: "https://example.org/healthcare-demo/"}, "not under"),
        }
        for name, (published, needle) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                github_env = Path(directory) / "github_env"
                completed = run_check("addresses", "--github-env", str(github_env), published=published)
                self.assertFails(completed, needle)
                self.assertFalse(github_env.exists(), "a failed check must not hand addresses to the builds")

    # --- built pages

    def test_a_good_built_tree_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            write_built_tree(Path(directory), published_addresses())
            completed = run_check("built", "--tree", directory)
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            self.assertEqual(len(SITES), completed.stdout.count("ok  "))

    def test_each_broken_built_page_fails(self) -> None:
        published = published_addresses()
        root = published[SITES[0].address_key]
        folder_of = {site.name: published[site.address_key][len(root):] for site in SITES}
        demo = SITES[2]

        def drop_tag(tree: Path) -> None:
            page = tree / folder_of[demo.name] / "index.html"
            html = page.read_text(encoding="utf-8")
            page.write_text(re.sub(r'\s*<meta property="og:image" [^>]*>', "", html), encoding="utf-8")

        def leave_placeholder(tree: Path) -> None:
            page = tree / folder_of[demo.name] / "index.html"
            page.write_text(page.read_text(encoding="utf-8") + f"<!-- {PLACEHOLDER} -->", encoding="utf-8")

        def wrong_size(tree: Path) -> None:
            folder = tree / folder_of[demo.name]
            shutil.copyfile(folder / "icon-512.png", folder / CARD)

        def missing_icon(tree: Path) -> None:
            (tree / folder_of[demo.name] / "apple-touch-icon.png").unlink()

        def missing_card(tree: Path) -> None:
            (tree / folder_of[demo.name] / CARD).unlink()

        cases = {
            "a missing og:image": (drop_tag, "og:image is missing"),
            "a leftover placeholder": (leave_placeholder, "placeholder"),
            "a card of the wrong size": (wrong_size, "512 x 512"),
            "a missing icon": (missing_icon, "apple-touch-icon.png is missing"),
            "a missing card": (missing_card, f"{CARD} is missing"),
        }
        for name, (break_tree, needle) in cases.items():
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                write_built_tree(Path(directory), published)
                break_tree(Path(directory))
                completed = run_check("built", "--tree", directory)
                self.assertFails(completed, needle)
                self.assertIn(f"FAIL  {demo.address_key}", completed.stdout)

    def test_a_page_built_without_the_published_address_fails(self) -> None:
        """Without PUBLIC_SITE_URL the plugin writes the base path, which crawlers ignore."""
        with tempfile.TemporaryDirectory() as directory:
            write_built_tree(
                Path(directory),
                published_addresses(),
                prefix_for=lambda site, address: urlsplit(address).path if site is SITES[1] else address,
            )
            completed = run_check("built", "--tree", directory)
            self.assertFails(completed, "had no PUBLIC_SITE_URL")
            self.assertIn(f"FAIL  {SITES[1].address_key}", completed.stdout)
            self.assertIn(f"ok    {SITES[0].address_key}", completed.stdout)

    # --- after the deploy

    def serve(self, answers: dict[str, tuple[int, str, bytes]]) -> str:
        """A stand-in for GitHub Pages that answers each path as given and anything else with 404."""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
                status, content_type, body = answers.get(self.path, (404, "text/html", b"<h1>404</h1>"))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def published_site(self, change=None) -> tuple[dict[str, str], dict[str, tuple[int, str, bytes]]]:
        """A stand-in of the three published pages and cards; `change(answers, paths)` breaks one."""
        answers: dict[str, tuple[int, str, bytes]] = {}
        base = self.serve(answers)
        published = {}
        paths = {}
        for site, folder in zip(SITES, ("/Dagents/", "/Dagents/healthcare-demo/", "/Dagents/nl2sql-demo/")):
            address = f"{base}{folder}"
            published[site.address_key] = address
            paths[site.address_key] = folder
            html = site_index(site).replace(PLACEHOLDER, address).encode()
            answers[folder] = (200, "text/html; charset=utf-8", html)
            answers[f"{folder}{CARD}"] = (200, "image/png", (REPO_ROOT / site.public / CARD).read_bytes())
        if change:
            change(answers, paths)
        return published, answers

    def test_published_images_that_load_pass(self) -> None:
        published, _ = self.published_site()
        completed = run_check("published", "--attempts", "1", "--delay", "0", published=published)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(len(SITES), completed.stdout.count("200 image/png"))

    def test_a_published_image_that_does_not_load_fails(self) -> None:
        key = SITES[1].address_key

        def not_found(answers, paths):
            del answers[f"{paths[key]}{CARD}"]

        def html_page(answers, paths):
            # A server that answers a missing file with its index page.
            answers[f"{paths[key]}{CARD}"] = answers[paths[key]]

        def empty(answers, paths):
            answers[f"{paths[key]}{CARD}"] = (200, "image/png", b"")

        def no_tag(answers, paths):
            status, content_type, body = answers[paths[key]]
            html = re.sub(rb'\s*<meta property="og:image" [^>]*>', b"", body)
            answers[paths[key]] = (status, content_type, html)

        def page_down(answers, paths):
            answers[paths[key]] = (503, "text/html", b"unavailable")

        cases = {
            "a 404 image": (not_found, "404 text/html"),
            "an HTML answer": (html_page, "200 text/html"),
            "an empty image": (empty, "0 bytes"),
            "no og:image tag": (no_tag, "names og:image nowhere"),
            "a page that does not answer": (page_down, "returned 503"),
        }
        for name, (change, needle) in cases.items():
            with self.subTest(case=name):
                published, _ = self.published_site(change)
                completed = run_check("published", "--attempts", "1", "--delay", "0", published=published)
                self.assertFails(completed, needle)
                self.assertIn(f"FAIL  {key}", completed.stdout)


# --- the rendered images and icons ------------------------------------------


class RenderedOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = load_record()
        self.fonts = locked_font_versions()
        self.published = published_addresses()

    def test_every_output_is_current_with_its_sources(self) -> None:
        self.assertEqual(
            [],
            stale_outputs(self.record, REPO_ROOT, self.fonts, self.published),
            f"run `node {RENDER_SCRIPT}`, look at every changed PNG, and commit it with design/social/rendered.json",
        )

    def test_the_record_check_catches_each_kind_of_drift(self) -> None:
        card = f"{SITES[0].public}/{CARD}"
        cases = {}
        changed_source = copy.deepcopy(self.record)
        changed_source["outputs"][card]["sources"]["design/social/card.css"] = "0" * 64
        cases["a changed source"] = changed_source
        changed_output = copy.deepcopy(self.record)
        changed_output["outputs"][card]["sha256"] = "0" * 64
        cases["a changed output"] = changed_output
        missing = copy.deepcopy(self.record)
        del missing["outputs"][card]
        cases["a missing output"] = missing
        old_font = copy.deepcopy(self.record)
        old_font["outputs"][card]["fonts"]["@fontsource/archivo"] = "0.0.1"
        cases["another font version"] = old_font
        moved = copy.deepcopy(self.record)
        moved["outputs"][card]["published"][SITES[0].address_key] = "https://example.org/"
        cases["a moved site"] = moved
        for name, record in cases.items():
            with self.subTest(drift=name):
                self.assertNotEqual([], stale_outputs(record, REPO_ROOT, self.fonts, self.published))

    def test_each_card_was_made_from_its_own_source_and_the_shared_files(self) -> None:
        for site in SITES:
            entry = self.record["outputs"][f"{site.public}/{CARD}"]
            with self.subTest(site=site.name):
                self.assertEqual(f"design/social/{site.card}", entry["from"])
                for source in (
                    f"design/social/{site.card}",
                    "design/social/card.css",
                    "design/social/mark.svg",
                    "design/dagents-design-system.css",
                    RENDER_SCRIPT,
                ):
                    self.assertIn(source, entry["sources"])
                self.assertEqual({site.address_key: self.published[site.address_key]}, entry["published"])

    def test_each_card_is_1200_by_630_and_small_enough_to_share(self) -> None:
        for site in SITES:
            path = REPO_ROOT / site.public / CARD
            with self.subTest(site=site.name):
                self.assertEqual(CARD_SIZE, png_size(path))
                self.assertLess(path.stat().st_size, MAX_CARD_BYTES, "WhatsApp drops previews over about 300 KB")

    def test_each_icon_has_its_size_and_is_the_same_in_every_site(self) -> None:
        for file, size in ICONS.items():
            first = REPO_ROOT / SITES[0].public / file
            for site in SITES:
                path = REPO_ROOT / site.public / file
                with self.subTest(site=site.name, icon=file):
                    self.assertTrue(path.is_file())
                    if size is not None:
                        self.assertEqual((size, size), png_size(path))
                    self.assertEqual(first.read_bytes(), path.read_bytes())

    def test_the_tab_icon_follows_the_colour_scheme(self) -> None:
        source = (SOCIAL / "favicon.svg").read_text(encoding="utf-8")
        self.assertIn("@media (prefers-color-scheme: dark)", source)
        for site in SITES:
            with self.subTest(site=site.name):
                self.assertEqual(source, (REPO_ROOT / site.public / "favicon.svg").read_text(encoding="utf-8"))

    def test_each_web_manifest_lists_icons_that_exist(self) -> None:
        for site in SITES:
            manifest = json.loads((REPO_ROOT / site.public / "site.webmanifest").read_text(encoding="utf-8"))
            with self.subTest(site=site.name):
                self.assertTrue(manifest["name"])
                self.assertTrue(manifest["icons"])
                for icon in manifest["icons"]:
                    # Relative to the manifest, so it works under any base path.
                    self.assertFalse(icon["src"].startswith(("/", "http")), icon["src"])
                    self.assertTrue((REPO_ROOT / site.public / icon["src"]).is_file(), icon["src"])


# --- what the cards say -----------------------------------------------------


class CardContentTests(unittest.TestCase):
    def test_no_card_shows_a_number_that_can_change(self) -> None:
        """Hospital numbers, schema names, step numbers and the dataset's name are identifiers, not results."""
        allowed = {"site-name", "site-schema", "step-number", "address"}
        for site in SITES:
            parser = CardText(allowed)
            parser.feed((SOCIAL / site.card).read_text(encoding="utf-8"))
            text = " ".join(parser.free_text).replace("IST-1", "IST")
            with self.subTest(card=site.card):
                self.assertTrue(text)
                self.assertIsNone(re.search(r"\d", text), f"a number on the card: {text}")
                self.assertIsNone(FILLER.search(text), f"filler word on the card: {text}")

    def test_the_stroke_triage_card_lists_the_demo_hospitals(self) -> None:
        sites = demo_hospitals()
        card = (SOCIAL / "healthcare.html").read_text(encoding="utf-8")
        names = re.findall(r'<span class="site-name">([^<]*)</span>', card)
        schemas = re.findall(r'<span class="site-schema">([^<]*)</span>', card)
        self.assertEqual(sites, list(zip(names, schemas)))
        self.assertIn(f"across {COUNT_WORDS[len(sites)]} hospitals", card)

    def test_the_nl2sql_question_is_one_of_the_demo_samples(self) -> None:
        card = (SOCIAL / "nl2sql.html").read_text(encoding="utf-8")
        question = re.search(r"data-sample-question>([^<]*)<", card).group(1)
        samples = (REPO_ROOT / "services" / "nl2sql-demo" / "backend" / "app" / "services" / "samples.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(f'question="{question}"', samples)

    def test_each_card_shows_the_address_from_the_env_file(self) -> None:
        for site in SITES:
            card = (SOCIAL / site.card).read_text(encoding="utf-8")
            with self.subTest(card=site.card):
                self.assertIn(f'data-published-url="{site.address_key}"', card)
                self.assertNotIn("github.io", card, "the render script fills the address in")


if __name__ == "__main__":
    unittest.main()
