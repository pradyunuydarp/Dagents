"""Tests for the CI guards.

A guard against a false green is only worth having if it still matches. If
`ci_no_skipped_planner_tests.py` quietly stopped recognising the skip message,
CI would go back to passing with the governance tests skipped — the exact
failure the guard exists to prevent, restored silently. So the guard is tested
against the real messages the suites emit.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ci_no_skipped_planner_tests as guard  # noqa: E402  (needs the sys.path entry)


#: The exact line `agents/tests/test_governance.py` produces under `-v` when the
#: planner is missing. Copied verbatim from a real run: if the suites reword
#: their skip reason, this test fails and the guard gets updated with it.
REAL_SKIP_LINE = (
    "A field the request never asked for must not survive enforcement. ... "
    "skipped 'dagentsc binary is not available'"
)


def run_guard(log: str) -> int:
    """Write `log` to a temporary file and return the guard's exit code."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "suite.log"
        path.write_text(log, encoding="utf-8")
        return guard.main([str(path)])


class PlannerSkipGuardTests(unittest.TestCase):
    def test_it_fails_on_the_real_skip_message(self) -> None:
        log = f"test_one (x.Y.test_one) ... ok\n{REAL_SKIP_LINE}\n"
        self.assertEqual(1, run_guard(log))

    def test_it_passes_a_run_with_no_planner_skips(self) -> None:
        log = (
            "test_one (x.Y.test_one) ... ok\n"
            "test_two (x.Y.test_two) ... ok\n"
            "----------------------------------------------------------------------\n"
            "Ran 2 tests in 0.01s\n\nOK\n"
        )
        self.assertEqual(0, run_guard(log))

    def test_it_tolerates_skips_for_other_reasons(self) -> None:
        """Postgres fixtures and missing browsers are legitimately absent here."""
        log = (
            "test_one (x.Y.test_one) ... ok\n"
            "test_two (x.Y.test_two) ... skipped 'local pagila postgres dataset is required'\n"
        )
        self.assertEqual(0, run_guard(log))

    def test_a_non_verbose_log_is_a_failure_not_a_pass(self) -> None:
        """Without `-v` there is no skip reason to inspect, so it cannot be cleared.

        Treating an uncheckable log as clean is how a guard becomes decoration.
        """
        log = "..sss\n----\nRan 5 tests in 0.01s\n\nOK (skipped=3)\n"
        self.assertEqual(1, run_guard(log))

    def test_a_missing_log_is_a_failure(self) -> None:
        self.assertEqual(1, guard.main([str(REPO_ROOT / "no-such-file.log")]))

    def test_wrong_usage_is_reported(self) -> None:
        self.assertEqual(2, guard.main([]))


def run_blocks(workflow: str) -> list[str]:
    """Return the body of every `run:` step in a workflow file.

    A small hand parser, because PyYAML is not a dependency of this suite. It
    handles the two forms the workflows use: `run: command` on one line, and
    `run: |` followed by an indented block.
    """
    lines = workflow.splitlines()
    blocks: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        # A step can start with `run:` itself, as in `- run: make test`.
        if stripped.startswith("- "):
            stripped = stripped[2:].lstrip()
        if not stripped.startswith("run:"):
            continue
        rest = stripped[len("run:"):].strip()
        if rest not in ("|", ">"):
            blocks.append(rest)
            continue
        indent = len(line) - len(line.lstrip()) + 2
        body = []
        for following in lines[index + 1:]:
            if following.strip() and not following.startswith(" " * indent):
                break
            body.append(following)
        blocks.append("\n".join(body))
    return blocks


class PipefailTests(unittest.TestCase):
    """A step that pipes a test run into `tee` must also set `pipefail`.

    GitHub runs `run:` steps with `bash -e`, without `pipefail`. In
    `unittest ... | tee log`, the step's exit code is `tee`'s, which is 0, so
    a failing suite passes. This hid two real test failures in the healthcare
    demo for several commits.
    """

    WORKFLOWS = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))

    def test_every_tee_pipeline_sets_pipefail(self) -> None:
        self.assertTrue(self.WORKFLOWS, "no workflows found")
        for path in self.WORKFLOWS:
            for block in run_blocks(path.read_text(encoding="utf-8")):
                if "| tee" not in block:
                    continue
                with self.subTest(workflow=path.name, step=block.strip().splitlines()[-1][:60]):
                    self.assertIn(
                        "set -o pipefail",
                        block,
                        f"{path.name}: a step pipes into tee without `set -o pipefail`, "
                        "so a failing command would pass",
                    )

    def test_the_parser_finds_both_step_forms(self) -> None:
        sample = (
            "    steps:\n"
            "      - run: echo one\n"
            "      - run: |\n"
            "          set -o pipefail\n"
            "          make test | tee out.log\n"
            "      - name: after\n"
        )
        self.assertEqual(
            ["echo one", "          set -o pipefail\n          make test | tee out.log"],
            run_blocks(sample),
        )


if __name__ == "__main__":
    unittest.main()
