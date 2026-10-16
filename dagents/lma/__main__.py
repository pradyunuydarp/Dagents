"""Serve one LMA: ``python -m dagents.lma``, or the ``dagents-lma`` command.

The same as ``uvicorn dagents.lma.main:app``, with the host and port taken
from ``--host`` and ``--port`` or ``LMA_API_HOST`` and ``LMA_API_PORT``
(default ``127.0.0.1:8010``). The LMA reads its other settings from the
``LMA_*`` variables (:meth:`dagents.lma.config.Settings.from_env`).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from dagents.common.serve import serve


def main(argv: Sequence[str] | None = None) -> int:
    return serve(agent="LMA", app_path="dagents.lma.main:app", env_prefix="LMA", default_port=8010, argv=argv)


if __name__ == "__main__":
    sys.exit(main())
