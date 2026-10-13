#!/usr/bin/env python3
"""Fail when a test suite skipped a test that needed the OCaml planner.

This is the older command for one case of `ci_no_skipped_tests.py`, kept so
that existing instructions still work. It runs that guard with
`--forbid planner`. The CI workflow calls `ci_no_skipped_tests.py` directly.

Usage::

    python -m unittest discover -s agents/tests -t . -v 2>&1 | tee suite.log
    python scripts/ci_no_skipped_planner_tests.py suite.log

Exit codes: 0 when no planner test skipped, 1 when one did or the log cannot
be checked, 2 for wrong usage.
"""

from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ci_no_skipped_tests  # noqa: E402  (needs the sys.path entry)


def main(argv: list[str]) -> int:
    """Check one unittest log for planner-related skips.

    Params:
    - `argv`: `[log_path]`.

    Returns:
    - `0` when nothing relevant skipped, `1` otherwise, `2` for wrong usage.
    """
    if len(argv) != 1:
        print(f"usage: {Path(__file__).name} <unittest-log>", file=sys.stderr)
        return 2
    return ci_no_skipped_tests.main(["--forbid", "planner", "--", argv[0]])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
