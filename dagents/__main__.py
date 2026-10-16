"""Print the installed version and which planner the runner would start.

Run it as ``python -m dagents``. It starts nothing and reads no data. The
exit status is 0 when the runner can start a planner, and 1 when it finds
none or ``DAGENTSC_BIN`` names a file it cannot run, because then the Ethical
Guard denies every request.
"""

from __future__ import annotations

import sys

from dagents import __version__
from dagents.common.infrastructure.dagents_runner import find_planner, planner_problem

#: What each lookup rule means, in the order the runner tries them.
SOURCES = {
    "DAGENTSC_BIN": "the DAGENTSC_BIN setting",
    "bundled": "the planner bundled with this package",
    "PATH": "dagentsc on PATH",
}


def main() -> int:
    path, source = find_planner()
    print(f"dagents {__version__}")
    if path is None:
        print("planner: none found. Set DAGENTSC_BIN, install a platform wheel, or put dagentsc on PATH.")
        print("Without a planner the Ethical Guard denies every request.")
        return 1
    problem = planner_problem((path, source))
    if problem is not None:
        print(f"planner: {problem}.")
        print("Without a planner the Ethical Guard denies every request.")
        return 1
    print(f"planner: {path} ({SOURCES[source]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
