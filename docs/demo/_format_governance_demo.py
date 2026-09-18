"""Formatters for run_governance_demo.sh.

A separate file rather than shell one-liners: the JSON these print is nested
enough that inline formatting becomes unreadable, and unreadable demo output is
worse than no demo output.

Usage: <json on stdin> | python _format_governance_demo.py <restriction|plan|release>
"""

from __future__ import annotations

import json
import sys


def restriction(data: dict) -> None:
    field = data["fieldRestrictions"][0] if data["fieldRestrictions"] else {"strategy": "-"}
    trust = data["assessment"]["trust"]
    print(
        f"    trust={trust:<9} decision={data['decision']:<7} "
        f"strategy={field['strategy']:<22} filtering_score={data['filteringScore']:.2f}"
    )
    for violation in data["violations"]:
        print(f"    violation: {violation}")


def plan(data: dict) -> None:
    print(
        f"  quorum={data['quorumMet']} "
        f"required={data['requiredParticipants']} "
        f"selected={', '.join(data['selectedSites']) or 'none'}"
    )
    for exclusion in data["excludedSites"]:
        print(f"  excluded {exclusion['siteId']}: {exclusion['reason']}")


def release(data: dict) -> None:
    for gate in data["gateResults"]:
        print(f"  [{gate['outcome'].upper():<13}] {gate['gateId']:<22} {gate['detail']}")
    print()
    rollback = data.get("rollbackVersion")
    suffix = f"  (keeping {rollback})" if rollback and data["action"] != "release" else ""
    print(f"  DECISION: {data['action'].upper()}{suffix}")


FORMATTERS = {"restriction": restriction, "plan": plan, "release": release}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in FORMATTERS:
        print(f"usage: {sys.argv[0]} {'|'.join(FORMATTERS)}", file=sys.stderr)
        return 1
    FORMATTERS[sys.argv[1]](json.load(sys.stdin))
    return 0


if __name__ == "__main__":
    sys.exit(main())
