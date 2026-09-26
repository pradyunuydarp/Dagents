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


if __name__ == "__main__":
    unittest.main()
