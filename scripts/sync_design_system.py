#!/usr/bin/env python3
"""Copy the Dagents design system into each frontend, and check the copies.

Three frontends have to read as one system: the framework site and the two demo
apps. Sharing one file by import is not available to all of them —
`apps/healthcare-demo/` is extractable into its own repository via
`scripts/extract_repo.sh`, so it must not import across the tree, and the
frontend skill forbids it.

So the system is generated into each app instead, and the copies are
drift-checked, which is the same shape the service inventory uses: one source,
generated artifacts, and a test that fails when they diverge. Editing a copy is
a mistake the check catches.

Usage::

    scripts/sync_design_system.py --write   # refresh the copies
    scripts/sync_design_system.py --check   # fail if a copy is stale
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "design" / "dagents-design-system.css"

#: Where each frontend keeps its copy. The path is relative to the repo root and
#: each app imports it from its own `src/`, so nothing reaches outside its tree.
COPIES = (
    Path("site/src/design-system.css"),
    Path("apps/healthcare-demo/frontend/src/design-system.css"),
    Path("services/nl2sql-demo/frontend/src/design-system.css"),
)

BANNER = """/* GENERATED FILE — DO NOT EDIT.
 *
 * Copied from design/dagents-design-system.css by scripts/sync_design_system.py.
 * Edit the source and re-run:
 *
 *     python scripts/sync_design_system.py --write
 *
 * `tests/test_design_system.py` fails when a copy drifts from the source.
 */
"""


def rendered() -> str:
    """Return the file content each copy must have, banner included."""
    return BANNER + "\n" + SOURCE.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Write or check the design-system copies.

    Params:
    - `argv`: argument list, defaulting to `sys.argv[1:]`.

    Returns:
    - `0` on success, `1` when `--check` finds a stale or missing copy.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="refresh every copy")
    group.add_argument("--check", action="store_true", help="fail if a copy is stale")
    args = parser.parse_args(argv)

    if not SOURCE.is_file():
        print(f"Missing design system source: {SOURCE}", file=sys.stderr)
        return 1
    expected = rendered()

    if args.write:
        for relative in COPIES:
            target = REPO_ROOT / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(expected, encoding="utf-8")
        print(f"wrote the design system into {len(COPIES)} frontends")
        return 0

    stale = [
        str(relative)
        for relative in COPIES
        if not (REPO_ROOT / relative).is_file()
        or (REPO_ROOT / relative).read_text(encoding="utf-8") != expected
    ]
    if stale:
        print(
            "These design-system copies are stale or missing: " + ", ".join(stale) + "\n"
            "Refresh them with: python scripts/sync_design_system.py --write\n"
            "If you edited a copy directly, move the change into "
            "design/dagents-design-system.css instead.",
            file=sys.stderr,
        )
        return 1
    print(f"the design system is in sync across {len(COPIES)} frontends")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
