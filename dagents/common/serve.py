"""Start an agent's FastAPI app with uvicorn, for ``python -m dagents.lma`` and ``python -m dagents.gma``.

The installed commands ``dagents-lma`` and ``dagents-gma`` call the same code.
The app reads its own settings from the environment when uvicorn imports it,
as it does under ``uvicorn dagents.lma.main:app``. This module only chooses
where uvicorn listens: ``--host`` and ``--port``, else the agent's
``*_API_HOST`` and ``*_API_PORT`` variables, else ``127.0.0.1`` and the
agent's documented port.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence


def serve(
    *,
    agent: str,
    app_path: str,
    env_prefix: str,
    default_port: int,
    argv: Sequence[str] | None = None,
) -> int:
    """Parse the command line and run uvicorn until it stops.

    Params:
    - `agent`: the name shown in help and errors, such as ``LMA``.
    - `app_path`: the ``module:attribute`` uvicorn imports.
    - `env_prefix`: ``LMA`` or ``GMA``; the host and port come from
      ``<prefix>_API_HOST`` and ``<prefix>_API_PORT``.
    - `default_port`: the port when neither the flag nor the variable is set.

    Returns the exit status. Uvicorn is in the ``lma`` and ``gma`` extras, so
    without it this prints how to install it and returns 2.
    """
    parser = argparse.ArgumentParser(
        prog=f"python -m dagents.{env_prefix.lower()}",
        description=f"Serve the Dagents {agent} with uvicorn. Settings come from {env_prefix}_* variables.",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get(f"{env_prefix}_API_HOST") or "127.0.0.1",
        help=f"address to listen on (default: {env_prefix}_API_HOST, else 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=_port(os.environ.get(f"{env_prefix}_API_PORT"), default_port),
        help=f"port to listen on (default: {env_prefix}_API_PORT, else {default_port})",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug"],
        help="uvicorn log level (default: info)",
    )
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ImportError:
        print(
            f"The {agent} needs uvicorn and FastAPI. Install them with: "
            f"pip install 'dagents[{env_prefix.lower()}]'",
            file=sys.stderr,
        )
        return 2
    uvicorn.run(app_path, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def _port(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    if not value.strip().isdigit():
        raise SystemExit("the API port must be a whole number")
    return int(value.strip())
