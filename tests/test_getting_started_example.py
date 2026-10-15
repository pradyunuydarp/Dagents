"""The guide in docs/guide and the example it is built on stay in step.

The guide teaches by showing code from `examples/getting-started/` and the
output of its scripts. A copy in a document drifts as soon as the code
changes, so this file checks both directions:

- A code block marked `<!-- source: PATH -->` must appear, line for line, in
  that file. Every Python block in the guide must carry such a marker, so no
  Python in the guide is untested.
- An output block marked `<!-- output: PATH -->` must equal what that script
  prints when it runs against the real planner.
- Every module and script of the example appears in the guide at least once.

The scripts run against the real `dagentsc`, not a stub, because the Guard
fails closed: without the planner every request is denied and the output
would show nothing the guide describes. Those tests skip when the planner is
missing; the CI job that runs this file forbids that skip.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE_DIR = REPO_ROOT / "docs" / "guide"
EXAMPLE_DIR = REPO_ROOT / "examples" / "getting-started"
DUNE_BUILT_BINARY = REPO_ROOT / "bindings" / "ocaml" / "_build" / "default" / "bin" / "dagentsc.exe"

#: A marker on the line directly above a fenced code block.
MARKER = re.compile(r"^<!--\s*(source|output):\s*(\S+)\s*-->$")
FENCE = re.compile(r"^(`{3,})(\w*)\s*$")

#: Identifiers that belong to the example's domain. None may appear in agents/.
DOMAIN_WORDS = ("example-retail-returns", "retail-orders", "order_returns", "store-north", "store-south")


class Block:
    """One fenced code block in a guide page."""

    def __init__(self, page: Path, line: int, language: str, lines: list[str], marker: tuple[str, str] | None):
        self.page = page
        self.line = line
        self.language = language
        self.lines = lines
        self.marker = marker

    def where(self) -> str:
        return f"{self.page.relative_to(REPO_ROOT)}:{self.line}"


def guide_blocks() -> list[Block]:
    """Every fenced code block in docs/guide, with the marker above it, if any."""
    blocks: list[Block] = []
    for page in sorted(GUIDE_DIR.glob("*.md")):
        lines = page.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            opening = FENCE.match(lines[index])
            if not opening:
                index += 1
                continue
            fence, language = opening.groups()
            marker = MARKER.match(lines[index - 1].strip()) if index > 0 else None
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index].rstrip() != fence:
                body.append(lines[index])
                index += 1
            blocks.append(
                Block(page, index - len(body), language, body, marker.groups() if marker else None)  # type: ignore[arg-type]
            )
            index += 1
    return blocks


def contains_lines(text: str, block: list[str]) -> bool:
    """Whether `block` appears in `text` as a run of whole, consecutive lines."""
    lines = text.splitlines()
    size = len(block)
    return size > 0 and any(lines[start : start + size] == block for start in range(len(lines) - size + 1))


def find_planner() -> str | None:
    """The planner the scripts should use: DAGENTSC_BIN, then PATH, then the dune build."""
    configured = os.environ.get("DAGENTSC_BIN")
    if configured and os.path.isfile(configured):
        return configured
    on_path = shutil.which("dagentsc")
    if on_path:
        return on_path
    if DUNE_BUILT_BINARY.is_file():
        return str(DUNE_BUILT_BINARY)
    return None


PLANNER = find_planner()


def run_script(script: str, planner: str) -> subprocess.CompletedProcess[str]:
    """Run one example script from the repository root, as the guide does."""
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT), DAGENTSC_BIN=planner)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class GuideBlockTests(unittest.TestCase):
    """The code in the guide is the code in the repository."""

    blocks = guide_blocks()

    def test_the_guide_has_pages_and_marked_blocks(self) -> None:
        self.assertTrue(list(GUIDE_DIR.glob("*.md")), "docs/guide has no pages")
        self.assertTrue([block for block in self.blocks if block.marker], "no block in the guide carries a marker")

    def test_every_python_block_names_its_source(self) -> None:
        for block in self.blocks:
            if block.language == "python":
                with self.subTest(block=block.where()):
                    self.assertIsNotNone(block.marker, "add <!-- source: PATH --> on the line above this block")
                    self.assertEqual("source", block.marker[0])  # type: ignore[index]

    def test_source_blocks_match_their_files(self) -> None:
        for block in self.blocks:
            if not block.marker or block.marker[0] != "source":
                continue
            path = (REPO_ROOT / block.marker[1]).resolve()
            with self.subTest(block=block.where(), source=block.marker[1]):
                self.assertTrue(path.is_relative_to(REPO_ROOT), "a source must be inside the repository")
                self.assertTrue(path.is_file(), "the source file does not exist")
                self.assertTrue(
                    contains_lines(path.read_text(encoding="utf-8"), block.lines),
                    "this block differs from its source file; copy it again from the file",
                )

    def test_output_blocks_name_an_example_script(self) -> None:
        for block in self.blocks:
            if block.marker and block.marker[0] == "output":
                with self.subTest(block=block.where()):
                    path = REPO_ROOT / block.marker[1]
                    self.assertTrue(path.is_file(), f"{block.marker[1]} does not exist")
                    self.assertEqual(EXAMPLE_DIR, path.parent, "output blocks come from the example's scripts")

    def test_every_example_file_is_shown_in_the_guide(self) -> None:
        shown = {block.marker[1] for block in self.blocks if block.marker and block.marker[0] == "source"}
        files = sorted(EXAMPLE_DIR.glob("shop/*.py")) + sorted(EXAMPLE_DIR.glob("step*.py"))
        self.assertTrue(files)
        for path in files:
            if path.name == "__init__.py":
                continue
            name = str(path.relative_to(REPO_ROOT))
            with self.subTest(file=name):
                self.assertIn(name, shown, "the guide never shows this file")

    def test_the_example_adds_no_domain_word_to_the_framework(self) -> None:
        """CLAUDE.md: nothing in agents/ knows the application's domain."""
        for path in sorted((REPO_ROOT / "agents").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for word in DOMAIN_WORDS:
                with self.subTest(file=str(path.relative_to(REPO_ROOT)), word=word):
                    self.assertNotIn(word, text)


class FailClosedTests(unittest.TestCase):
    """Without a planner the example's Guard denies every read."""

    def test_the_guard_denies_every_read_without_the_planner(self) -> None:
        result = run_script("examples/getting-started/step2_guard_check.py", "/nonexistent/dagentsc")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(3, result.stdout.count("The planner could not be reached, so the Guard denied the request."))
        self.assertNotIn("permitted True", result.stdout)


@unittest.skipUnless(PLANNER, "dagentsc binary is not available")
class ExampleRunTests(unittest.TestCase):
    """The example runs against the real planner and prints what the guide shows."""

    outputs: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.outputs = {}
        for script in sorted(EXAMPLE_DIR.glob("step*.py")):
            name = str(script.relative_to(REPO_ROOT))
            result = run_script(name, PLANNER)  # type: ignore[arg-type]
            if result.returncode != 0:
                raise AssertionError(f"{name} failed:\n{result.stdout}\n{result.stderr}")
            cls.outputs[name] = result.stdout

    def test_every_step_runs(self) -> None:
        self.assertEqual(5, len(self.outputs))

    def test_the_guide_shows_the_real_output(self) -> None:
        marked = [block for block in guide_blocks() if block.marker and block.marker[0] == "output"]
        self.assertEqual(
            sorted(self.outputs), sorted({block.marker[1] for block in marked}),  # type: ignore[index]
            "the guide shows the output of every step, once or more",
        )
        for block in marked:
            with self.subTest(block=block.where()):
                actual = [line.rstrip() for line in self.outputs[block.marker[1]].splitlines()]  # type: ignore[index]
                self.assertEqual(actual, [line.rstrip() for line in block.lines], "run the script and paste its output")

    def test_the_extension_registers_and_the_orders_match_the_contract(self) -> None:
        output = self.outputs["examples/getting-started/step1_register.py"]
        self.assertIn("example-retail-returns 1.0.0", output)
        self.assertIn("store-north: 60 orders, matches the contract: True", output)
        self.assertIn("store-south: 45 orders, matches the contract: True", output)

    def test_the_guard_narrows_and_denies_by_trust(self) -> None:
        output = self.outputs["examples/getting-started/step2_guard_check.py"]
        self.assertIn("analyst-anna: trust high, decision narrow, permitted True", output)
        self.assertIn("new-hire-ben: trust moderate, decision narrow, permitted True", output)
        self.assertIn("contractor-cy: trust low, decision deny, permitted False", output)
        self.assertIn("chain intact: True", output)

    def test_stores_report_summaries_not_orders(self) -> None:
        output = self.outputs["examples/getting-started/step3_profile.py"]
        self.assertIn("order_value aggregate_only:20", output)
        self.assertNotIn("order_id", output)
        self.assertIn("Combined: 105 orders", output)

    def test_the_round_reaches_a_release_decision(self) -> None:
        output = self.outputs["examples/getting-started/step4_federated_round.py"]
        self.assertIn("store-north: completed, 60 examples", output)
        self.assertIn("store-south: completed, 45 examples", output)
        self.assertIn("Aggregation permitted: True", output)
        self.assertRegex(output, r"Decision: (release|another_round|reject)")

    def test_the_agents_run_the_same_round_over_their_apis(self) -> None:
        output = self.outputs["examples/getting-started/step5_agents_over_http.py"]
        in_process = self.outputs["examples/getting-started/step4_federated_round.py"]
        # Each store's LMA runs the same rule on the same guarded orders, so
        # the results equal the in-process round of step 4.
        for site_id in ("store-north", "store-south"):
            with self.subTest(site=site_id):
                ran_here = [line for line in in_process.splitlines() if line.startswith(f"  {site_id}: completed")]
                self.assertEqual(1, len(ran_here))
                self.assertIn(ran_here[0], output.splitlines())
                self.assertRegex(output, rf"{site_id} offer 200 accepted, execute 200 completed")
        self.assertIn("Aggregation permitted: True", output)
        self.assertIn("store-east, which the round did not select: 403 forbidden", output)
        # The GMA and the caller see counts and metrics, never an order.
        for word in ("order_id", "customer_postcode", "order_value"):
            self.assertNotIn(word, output)
        # The call log holds route templates, never an address.
        self.assertIn("GMA POST /api/v1/federation/rounds 202", output)
        self.assertNotIn("://", output)
        self.assertNotIn(".internal", output)


if __name__ == "__main__":
    unittest.main()
