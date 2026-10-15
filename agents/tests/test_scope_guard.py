"""Nothing in agents/ knows the healthcare demo's domain.

CLAUDE.md: "Nothing in agents/ knows what a stroke is." The framework runs the
demo through its extension (a condition pack, model kinds, a site source); if
a capability needs a domain word in agents/, it belongs in the application.
This test reads every Python file under agents/, except the tests, and fails
on the demo's words and site ids. Prose that names the healthcare demo as a
consumer is allowed; its identifiers are not.

The site ids are read from the demo's own site list, so a hospital added there
is checked here too, in each spelling the demo uses: ``uk-134``, the schema and
role ``site_uk_134``, and the setting suffix ``UK_134``.
"""

from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_DIR = REPO_ROOT / "agents"
TESTS_DIR = AGENTS_DIR / "tests"
DEMO_SITES_FILE = REPO_ROOT / "apps" / "healthcare-demo" / "backend" / "app" / "domain" / "dataset.py"

#: Words of the demo's domain, matched without regard to case.
DOMAIN_WORDS = ("stroke", "poor_outcome")

_SITE_ENTRY = re.compile(r'Site\(\s*"([a-z]{2}-\d{3})"')


def demo_site_ids(path: Path = DEMO_SITES_FILE) -> list[str]:
    """The site ids in the demo's ``SITES`` list, such as ``uk-134``."""
    return sorted(set(_SITE_ENTRY.findall(path.read_text(encoding="utf-8"))))


def forbidden_terms(site_ids: list[str]) -> list[str]:
    """Every word and site id spelling the framework must not contain, in lower case."""
    terms = list(DOMAIN_WORDS)
    for site_id in site_ids:
        underscored = site_id.replace("-", "_")
        terms += [site_id, f"site_{underscored}", underscored]
    return sorted(set(term.lower() for term in terms))


def framework_files(root: Path = AGENTS_DIR) -> list[Path]:
    """Every Python file under ``root``, except those under its ``tests`` folder."""
    tests = root / "tests"
    return sorted(path for path in root.rglob("*.py") if tests not in path.parents)


def findings(files: list[Path], terms: list[str]) -> list[tuple[Path, int, str]]:
    """``(path, line number, term)`` for each line of ``files`` that holds a term."""
    found = []
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            lowered = line.lower()
            for term in terms:
                if term in lowered:
                    found.append((path, number, term))
    return found


class ScopeGuardTests(unittest.TestCase):
    def test_the_demo_site_list_is_read(self) -> None:
        # If the list moved or changed shape, the scan below would check no site id.
        self.assertTrue(DEMO_SITES_FILE.is_file(), f"{DEMO_SITES_FILE} is missing; point the scope guard at the site list")
        self.assertGreaterEqual(len(demo_site_ids()), 3)
        self.assertIn("uk-134", demo_site_ids())

    def test_the_scan_covers_the_framework_and_skips_the_tests(self) -> None:
        files = framework_files()
        relative = {str(path.relative_to(REPO_ROOT)) for path in files}
        for expected in (
            "agents/lma/app_factory.py",
            "agents/gma/app_factory.py",
            "agents/common/extensions/registry.py",
            "agents/common/infrastructure/agent_client.py",
        ):
            self.assertIn(expected, relative)
        self.assertFalse(any(path.startswith("agents/tests/") for path in relative))

    def test_no_framework_file_names_the_demo_domain(self) -> None:
        found = findings(framework_files(), forbidden_terms(demo_site_ids()))
        self.assertEqual(
            [],
            [f"{path.relative_to(REPO_ROOT)}:{number}: {term}" for path, number, term in found],
            "move this to the application, through agents/common/extensions",
        )

    def test_the_scan_finds_each_spelling(self) -> None:
        """A planted file with each term is reported, and a file under tests/ is not."""
        terms = forbidden_terms(["uk-134"])
        self.assertEqual(
            sorted(["stroke", "poor_outcome", "uk-134", "site_uk_134", "uk_134"]), terms
        )
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "agents"
            (root / "lma").mkdir(parents=True)
            (root / "tests").mkdir()
            planted = {
                "lma/a.py": 'PACK = "Suspected_Stroke"\n',
                "lma/b.py": "label = 'poor_outcome'\n",
                "lma/c.py": "SITE = 'uk-134'\n",
                "lma/d.py": "ROLE = 'site_uk_134'\n",
                "lma/e.py": "URL = os.environ['HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL']\n",
            }
            for name, text in planted.items():
                (root / name).write_text(text, encoding="utf-8")
            (root / "lma" / "clean.py").write_text("SITE = 'site-a'\n", encoding="utf-8")
            (root / "tests" / "test_x.py").write_text("SITE = 'uk-134'  # stroke\n", encoding="utf-8")

            found = findings(framework_files(root), terms)
            reported = {path.relative_to(root).as_posix() for path, _, _ in found}
            self.assertEqual(set(planted), reported)


if __name__ == "__main__":
    unittest.main()
