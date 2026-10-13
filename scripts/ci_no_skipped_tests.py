#!/usr/bin/env python3
"""Fail when a test suite skipped tests that CI is set up to run.

Several suites skip, with a message, when something they need is missing:

| Category | The tests need | Skip message |
|---|---|---|
| `planner` | the `dagentsc` binary | `dagentsc binary is not available` |
| `database` | a PostgreSQL 16 admin connection string | starts with `database test skipped` |
| `charts` | matplotlib | `matplotlib is not installed; ...` |

A skip is right on a developer's machine that lacks one of these. In CI it is
wrong: each job that runs these suites provides what they need, so a skip
means the job is wired wrongly, and the run would be green without testing
anything. This happened once with the governance tests.

If a suite rewords its skip message, the pattern here stops matching.
`--require-ran` covers that case: it names a test class that must have run at
least one test (passed, failed or errored, but not skipped).

Usage::

    python -m unittest discover -s agents/tests -t . -v 2>&1 | tee suite.log
    python scripts/ci_no_skipped_tests.py --forbid planner --forbid charts \\
        --require-ran agents.tests.test_charts.RenderTests suite.log

The suite must run with `-v`. Without it, unittest prints one `s` per skip and
no reason, so there is nothing to check, and the guard fails.

Exit codes: 0 when the log is clean, 1 when a forbidden skip was found, a
required class did not run or the log cannot be checked, 2 for wrong usage.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
import re
import sys


@dataclass(frozen=True)
class Category:
    """A kind of skip that CI does not accept."""

    #: Why the tests skipped, for the error message.
    reason: str
    #: What the CI job does to prevent it, and where to look.
    remedy: str
    #: Matched against each log line. They match the reasons the suites use;
    #: `tests/test_ci_guards.py` reads those reasons from the test files.
    patterns: tuple[re.Pattern[str], ...]


CATEGORIES: dict[str, Category] = {
    "planner": Category(
        reason="the OCaml planner (dagentsc) was not reachable",
        remedy="The job downloads the planner artifact and sets DAGENTSC_BIN, so check those steps.",
        patterns=(
            re.compile(r"skipped\s+['\"]dagentsc binary is not available", re.IGNORECASE),
            re.compile(r"skipped.*dagentsc", re.IGNORECASE),
        ),
    ),
    "database": Category(
        reason="no PostgreSQL database was reachable",
        remedy=(
            "The job runs a postgres:16 service container and sets the test connection string, "
            "so check the service and the environment variables."
        ),
        patterns=(re.compile(r"skipped\s+['\"]database test skipped", re.IGNORECASE),),
    ),
    "charts": Category(
        reason="matplotlib is not installed",
        remedy="The job installs matplotlib and numpy, so check the install step.",
        patterns=(re.compile(r"skipped\s+['\"][^'\"]*matplotlib", re.IGNORECASE),),
    ),
}

#: Verbose unittest output contains test names or one of these. A log with
#: neither was not produced with `-v` and cannot be checked.
VERBOSE_MARKERS = ("... ok", "... skipped", "... FAIL", "... ERROR")

#: `test_name (package.module.Class.test_name)` on Python 3.11 and later, and
#: `test_name (package.module.Class)` before that.
_TEST_LINE = re.compile(r"^(?P<method>test\w*) \((?P<qualified>[\w.]+)\)")
#: A class or module fixture that skipped. None of its tests run.
_FIXTURE_SKIP = re.compile(
    r"^(?:setUpClass|setUpModule) \((?P<qualified>[\w.]+)\) \.\.\. skipped (?P<reason>.*)$"
)
#: A test's result. It follows " ... " on the test line or on its docstring
#: line, or starts a line of its own when the test wrote output first.
_OUTCOME = re.compile(
    r"(?:^|\.\.\. )(?P<outcome>ok|skipped|FAIL|ERROR|expected failure|unexpected success)\b"
)
#: Any skip and its reason, for the summary of allowed skips.
_ANY_SKIP = re.compile(r"(?:^|\.\.\. )skipped (?P<reason>.*)$")
#: The failure details start with a line of 70 "=" followed by the failing
#: test's name, and end at "Ran N tests". Lines in between are tracebacks.
_DETAILS_RULE = "=" * 70
_DETAILS_HEADING = re.compile(r"^(?:ERROR|FAIL|UNEXPECTED SUCCESS): ")
_RAN_LINE = re.compile(r"^Ran \d+ tests? in ")


@dataclass
class LogSummary:
    """What one verbose unittest log says about each test class."""

    #: Class name -> the outcome of each of its tests, in log order. A test
    #: whose outcome is not in the log is recorded as `None`.
    outcomes: dict[str, list[str | None]] = field(default_factory=dict)
    #: Class or module name -> the reason its fixture skipped.
    fixture_skips: dict[str, str] = field(default_factory=dict)

    def ran(self, test_class: str) -> bool:
        """Whether at least one test of `test_class` ran and was not skipped."""
        return any(outcome not in (None, "skipped") for outcome in self.outcomes.get(test_class, []))


def parse_log(log: str) -> LogSummary:
    """Read the test classes and their outcomes from a verbose unittest log.

    Params:
    - `log`: the text of a `python -m unittest ... -v` run. One file may hold
      several runs.

    Returns:
    - a `LogSummary`.
    """
    summary = LogSummary()
    current: tuple[str, int] | None = None  # (class name, index into its outcomes)
    in_details = False
    lines = log.splitlines()
    for number, line in enumerate(lines):
        if in_details:
            in_details = not _RAN_LINE.match(line)
            continue
        following = lines[number + 1] if number + 1 < len(lines) else ""
        if line == _DETAILS_RULE and _DETAILS_HEADING.match(following):
            in_details, current = True, None
            continue
        fixture = _FIXTURE_SKIP.match(line)
        if fixture:
            summary.fixture_skips[fixture["qualified"]] = fixture["reason"]
            current = None
            continue
        test = _TEST_LINE.match(line)
        if test:
            qualified, method = test["qualified"], test["method"]
            suffix = "." + method
            test_class = qualified[: -len(suffix)] if qualified.endswith(suffix) else qualified
            outcomes = summary.outcomes.setdefault(test_class, [])
            outcomes.append(None)
            current = (test_class, len(outcomes) - 1)
            line = line[test.end():]
        if current is None:
            continue
        outcome = _OUTCOME.search(line)
        if outcome:
            test_class, index = current
            summary.outcomes[test_class][index] = outcome["outcome"]
            current = None
    return summary


def forbidden_skips(log: str, categories: list[str]) -> dict[str, list[str]]:
    """Return the log lines that skipped for each forbidden category.

    Params:
    - `log`: a verbose unittest log.
    - `categories`: names from `CATEGORIES`.

    Returns:
    - category name -> the matching lines, stripped. A category with no match
      is left out.
    """
    found: dict[str, list[str]] = {}
    for line in log.splitlines():
        for name in categories:
            if any(pattern.search(line) for pattern in CATEGORIES[name].patterns):
                found.setdefault(name, []).append(line.strip())
                break
    return found


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=Path(__file__).name,
        description="Fail when a verbose unittest log shows skips that CI does not accept.",
    )
    parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        choices=sorted(CATEGORIES),
        help="a skip category to fail on; repeat for several",
    )
    parser.add_argument(
        "--require-ran",
        action="append",
        default=[],
        metavar="MODULE.CLASS",
        help="a test class, named as unittest names it, that must have run at least one test; "
        "repeat for several",
    )
    parser.add_argument("log", help="the log of a unittest run made with -v")
    return parser


def main(argv: list[str]) -> int:
    """Check one unittest log.

    Params:
    - `argv`: the command-line arguments, without the program name.

    Returns:
    - `0` when the log is clean, `1` when it is not or cannot be checked, `2`
      for wrong usage.
    """
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:  # argparse exits on --help and on bad arguments
        return int(stop.code or 0)
    if not args.forbid and not args.require_ran:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: give at least one --forbid or --require-ran", file=sys.stderr)
        return 2

    path = Path(args.log)
    if not path.is_file():
        print(f"No such log file: {path}", file=sys.stderr)
        return 1
    log = path.read_text(encoding="utf-8", errors="replace")
    summary = parse_log(log)
    verbose = summary.outcomes or summary.fixture_skips or any(marker in log for marker in VERBOSE_MARKERS)
    if not verbose:
        print(
            f"{path} does not look like verbose unittest output, so its skips cannot be "
            "checked. Run the suite with -v.",
            file=sys.stderr,
        )
        return 1

    failed = False
    for name, lines in forbidden_skips(log, args.forbid).items():
        failed = True
        category = CATEGORIES[name]
        print(
            f"{path}: {len(lines)} {name} skips, because {category.reason}. {category.remedy}",
            file=sys.stderr,
        )
        for line in lines:
            print(f"  {line}", file=sys.stderr)

    for test_class in args.require_ran:
        if summary.ran(test_class):
            continue
        failed = True
        reason = summary.fixture_skips.get(test_class)
        if reason is not None:
            detail = f"its setUpClass skipped with {reason}"
        elif test_class in summary.outcomes:
            detail = "all of its tests skipped"
        else:
            detail = "none of its tests are in the log, so check the class name and the suite"
        print(f"{path}: no test from {test_class} ran; {detail}.", file=sys.stderr)

    if failed:
        return 1
    other = Counter(
        match["reason"] for match in map(_ANY_SKIP.search, log.splitlines()) if match is not None
    )
    checked = ", ".join(args.forbid) if args.forbid else "none"
    print(
        f"{path}: no forbidden skips (categories checked: {checked}). "
        f"Required test classes that ran: {len(args.require_ran)}. "
        f"{sum(other.values())} skips for other reasons."
    )
    for reason, count in sorted(other.items()):
        print(f"  {count} x {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
