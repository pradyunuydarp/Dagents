"""Serve the demo API with one release gate a trained candidate passes. Tests start it; nothing else does.

The demo's own release gates decide whatever the records say, so a pilot may
never reach ``release`` and the "Approve release" button may never be enabled.
Two checks need a pilot whose gates decide ``release``:

- ``frontend/approve.mjs``, the browser check of the approval, which
  ``tests/run_approval_check.sh`` runs against this server;
- ``tests/test_restart_processes.py``, which restarts this server on Postgres
  after an approval.

This module serves ``app.main:app`` with the app's services replaced by
services whose condition pack has one gate, AUC at least 0.5. That is the swap
``tests.test_api.ApprovalTests`` makes with ``mock.patch``. The planner still
makes every decision: the gates, the release Guard and the approval.

The gates are set here, in the test folder, and not by an app setting. So no
caller of a running demo can choose the policy a request is checked against.

Usage::

    python tests/lenient_gates_server.py --port-file PATH  # a free loopback port, written to PATH
    python tests/lenient_gates_server.py --fd N            # a listening socket the parent passed down

Everything else comes from the environment, as for ``app.main``. The agents run
inside this process. Without a database setting, each hospital's records are
in memory. With ``HEALTHCARE_DEMO_DATABASE_URL`` set, the hospitals' records,
the model registry and the audit chains are in Postgres.

A release this server approves passed only the test gate. So it refuses to
start when ``HEALTHCARE_DEMO_DATABASE_URL`` or any
``HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`` names a database whose name does
not start with ``dagents_scratch_``, the prefix of the scratch databases that
``tests.test_site_isolation.SupabaseLikeDatabase`` makes. It checks after the
app has read its env files. Uvicorn logs at ``warning``, so the log names no
address.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import socket
import sys
from typing import Mapping

APP_ROOT = Path(__file__).resolve().parents[1]
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1])


#: The start of every scratch database name a test makes (``SupabaseLikeDatabase``).
SCRATCH_DATABASE_PREFIX = "dagents_scratch_"
#: The settings that point the app at a database: the shared one and each hospital's own.
DATABASE_SETTING = re.compile(r"^HEALTHCARE_DEMO_(SITE_[A-Z0-9_]+_)?DATABASE_URL$")


def _database_name(dsn: str) -> str:
    """The database a connection string names, or an empty string when it names none or cannot be read."""
    try:
        from psycopg.conninfo import conninfo_to_dict
    except ModuleNotFoundError:  # pragma: no cover - psycopg is in the app's requirements
        return ""
    try:
        return str(conninfo_to_dict(dsn).get("dbname") or "")
    except Exception:  # psycopg raises ProgrammingError for a string it cannot parse
        return ""


def database_refusal(environ: Mapping[str, str]) -> str | None:
    """Why this server must not start with these settings, or None when it may.

    Every database setting must be empty or name a scratch database a test
    made. The reason names the setting, never its value, because the value can
    hold a password.
    """
    for name in sorted(environ):
        value = environ[name].strip()
        if not value or not DATABASE_SETTING.match(name):
            continue
        if not _database_name(value).startswith(SCRATCH_DATABASE_PREFIX):
            return (
                f"{name} names a database whose name does not start with {SCRATCH_DATABASE_PREFIX}. This server "
                "approves releases that passed only a test gate, so it runs only on in-memory stores or on a "
                "scratch database that a test made. Unset the setting to run on in-memory stores."
            )
    return None


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--port-file", type=Path, help="bind a free loopback port and write its number to this file")
    where.add_argument("--fd", type=int, help="serve on this listening socket, inherited from the parent process")
    return parser.parse_args(argv)


def _listening_socket(arguments: argparse.Namespace) -> socket.socket:
    """The socket to serve on. A new one is bound to a free loopback port and its number written down."""
    if arguments.fd is not None:
        return socket.socket(fileno=arguments.fd)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    # Written in one step, so a reader never sees half a number.
    partial = arguments.port_file.with_name(arguments.port_file.name + ".partial")
    partial.write_text(str(sock.getsockname()[1]), encoding="utf-8")
    partial.replace(arguments.port_file)
    return sock


def main(argv: list[str] | None = None) -> None:
    arguments = _arguments(argv)
    # Bound before the app is imported, which takes a few seconds, so no other
    # process can take the port in between.
    sock = _listening_socket(arguments)

    # The app's `tests` package comes first: the Dagents checkout has a
    # `tests` package of its own.
    for path in (FRAMEWORK_ROOT, APP_ROOT / "backend", APP_ROOT):
        sys.path.insert(0, str(path))

    import uvicorn

    from dagents.common.domain.federation import GateComparison, ReleaseGate

    import app.main as app_main
    from app.services.agents import HealthcareDemo
    from tests.support import gated_extension

    # Checked after the import, which loads the app's env files into the environment.
    refusal = database_refusal(os.environ)
    if refusal is not None:
        sock.close()
        raise SystemExit(f"lenient_gates_server: {refusal}")

    gate = ReleaseGate(gate_id="discrimination", metric="auc", comparison=GateComparison(kind="at_least", value=0.5))
    # The routes and the lifespan read `app.main.demo` when they run, so the
    # replacement serves every request and is closed when the server stops.
    app_main.demo = HealthcareDemo(extension=gated_extension([gate]))
    uvicorn.Server(uvicorn.Config(app_main.app, log_level="warning")).run(sockets=[sock])


if __name__ == "__main__":
    main()
