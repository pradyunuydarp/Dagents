"""The framework code names no word of the stroke demo's domain.

CLAUDE.md: nothing in ``dagents/`` knows what a stroke or a hospital is. The
framework says site, LMA and service; the application chooses its own words.
Tests may use the demo's words in their fixtures, so they are not checked.
"""

from __future__ import annotations

from pathlib import Path
import re
import unittest

AGENTS = Path(__file__).resolve().parents[1]

#: Words of the stroke demo's domain, matched without regard to case.
DOMAIN_WORDS = re.compile(r"hospital|stroke", re.IGNORECASE)


class FrameworkWordTests(unittest.TestCase):
    def test_no_framework_file_names_the_demos_domain(self) -> None:
        for path in sorted(AGENTS.rglob("*.py")):
            if AGENTS / "tests" in path.parents:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                with self.subTest(file=str(path.relative_to(AGENTS.parent)), line=number):
                    self.assertIsNone(DOMAIN_WORDS.search(line), line.strip())


if __name__ == "__main__":
    unittest.main()
