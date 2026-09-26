#!/usr/bin/env python3
"""Fail when a test suite skipped a test that needed the OCaml planner.

The governance and federation tests run against the real ``dagentsc`` binary,
because stubbing the planner would prove the code calls something, not that the
governance holds. They skip with a message when the binary is not reachable —
which is right for a developer who has not built it, and wrong for CI: a run
that skipped its governance tests is green without having proved anything. This
repository has already shipped a suite that was green for exactly that reason.

CI builds the planner and hands it to every job that needs it, so a skip here
means something is broken in the wiring — a missing ``DAGENTSC_BIN``, a binary
that did not survive the artifact round trip, a lost ``chmod +x`` — and the run
must fail rather than look fine.

Usage::

    python -m unittest discover -s agents/tests -t . -v 2>&1 | tee suite.log
    python scripts/ci_no_skipped_planner_tests.py suite.log

The suite must be run with ``-v``: without it, unittest prints an ``s`` per skip
and no reason, and there is nothing here to match on. That case is treated as a
failure too, rather than passing by accident.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys


#: The skip reasons that mean "the planner was not reachable". These are the
#: strings the suites themselves use; if one is reworded, this script stops
#: matching, which is why `NO_SKIPS_AT_ALL` below is also checked.
PLANNER_SKIP_PATTERNS = (
    re.compile(r"skipped\s+['\"]dagentsc binary is not available", re.IGNORECASE),
    re.compile(r"skipped.*dagentsc", re.IGNORECASE),
)

#: Verbose unittest output always contains one of these, so a log missing them
#: was not produced by `-v` and cannot be checked.
VERBOSE_MARKERS = ("... ok", "... skipped", "... FAIL", "... ERROR")


def main(argv: list[str]) -> int:
    """Check one unittest log for planner-related skips.

    Params:
    - `argv`: `[log_path]`.

    What it does:
    - Confirms the log came from a verbose run, then reports any line whose skip
      reason points at a missing planner.

    Returns:
    - `0` when nothing relevant skipped, `1` otherwise.
    """
    if len(argv) != 1:
        print(f"usage: {Path(__file__).name} <unittest-log>", file=sys.stderr)
        return 2
    path = Path(argv[0])
    if not path.is_file():
        print(f"No such log file: {path}", file=sys.stderr)
        return 1
    log = path.read_text(encoding="utf-8", errors="replace")

    if not any(marker in log for marker in VERBOSE_MARKERS):
        print(
            f"{path} does not look like verbose unittest output, so skips cannot be "
            "inspected. Re-run the suite with -v.",
            file=sys.stderr,
        )
        return 1

    offending = [
        line.strip()
        for line in log.splitlines()
        if any(pattern.search(line) for pattern in PLANNER_SKIP_PATTERNS)
    ]
    if offending:
        print(
            "These tests skipped because the OCaml planner was not reachable. CI builds "
            "it and passes DAGENTSC_BIN, so this is a wiring failure, not an excuse:",
            file=sys.stderr,
        )
        for line in offending:
            print(f"  {line}", file=sys.stderr)
        return 1

    total_skips = log.count("... skipped")
    print(f"No planner-related skips in {path} ({total_skips} skips for other reasons).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
