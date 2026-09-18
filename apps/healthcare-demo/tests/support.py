"""Shared test helpers for the healthcare demo suite."""

from __future__ import annotations

import os
import pathlib
import shutil

from agents.common.infrastructure.dagents_runner import dagentsc_binary

#: Where ``dune build`` puts the planner, relative to the Dagents checkout.
#:
#: Resolved from this file so the suite works whether the demo is run inside the
#: Dagents repository or extracted into its own, provided ``DAGENTS_HOME`` or a
#: sibling checkout supplies the framework.
_CANDIDATES = [
    pathlib.Path(__file__).resolve().parents[3] / "bindings/ocaml/_build/default/bin/dagentsc.exe",
    pathlib.Path(os.getenv("DAGENTS_HOME", "")) / "bindings/ocaml/_build/default/bin/dagentsc.exe",
]


def dagentsc_available() -> bool:
    """Whether the OCaml planner can be reached from this environment.

    The governed tests need the real planner: stubbing it would prove the app
    calls something, not that the framework's governance holds.
    """
    binary = dagentsc_binary()
    if os.path.isfile(binary) or shutil.which(binary) is not None:
        return True
    for candidate in _CANDIDATES:
        if candidate.is_file():
            os.environ.setdefault("DAGENTSC_BIN", str(candidate))
            return True
    return False
