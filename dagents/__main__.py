"""Print the installed version, which planner the runner would start, its SHA-256 and the limits of each call.

Run it as ``python -m dagents``. It starts nothing and reads no data except
the planner binary, which it hashes. The exit status is 0 when the runner can
start a planner, and 1 when it finds none, when ``DAGENTSC_BIN`` names a file
it cannot run, when the binary's SHA-256 is not the one ``DAGENTSC_SHA256``
requires, or when a planner setting (``DAGENTSC_SHA256``,
``DAGENTSC_TIMEOUT_SECONDS``, ``DAGENTSC_MAX_INPUT_BYTES``,
``DAGENTSC_MAX_OUTPUT_BYTES``) makes no sense, because then the Ethical Guard
denies every request.
"""

from __future__ import annotations

import sys

from dagents import __version__
from dagents.common.infrastructure.dagents_runner import (
    check_planner_hash,
    find_planner,
    planner_limits,
    planner_problem,
    seconds_text,
)

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
    problem = planner_problem((path, source), check_hash=False)
    if problem is not None:
        print(f"planner: {problem}.")
        print("Without a planner the Ethical Guard denies every request.")
        return 1
    print(f"planner: {path} ({SOURCES[source]})")
    try:
        check = check_planner_hash(path)
    except ValueError as error:
        print(f"planner sha256: {error}.")
        print("With this setting the runner starts no planner, so the Ethical Guard denies every request.")
        return 1
    if check.problem is not None:
        print(f"planner sha256: {check.problem}.")
        print("The runner does not start this planner, so the Ethical Guard denies every request.")
        return 1
    if check.sha256 is None:
        print("planner sha256: unknown, because the file could not be read")
    elif check.pinned is not None:
        print(f"planner sha256: {check.sha256}, as DAGENTSC_SHA256 requires")
    else:
        print(f"planner sha256: {check.sha256}")
    try:
        limits = planner_limits()
    except ValueError as error:
        print(f"planner limits: {error}.")
        print("With this setting the runner starts no planner, so the Ethical Guard denies every request.")
        return 1
    print(
        f"planner limits: {seconds_text(limits.timeout_seconds)} per call, {limits.max_input_bytes} bytes of input, "
        f"{limits.max_output_bytes} bytes of output"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
