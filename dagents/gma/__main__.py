"""Serve a GMA: ``python -m dagents.gma``, or the ``dagents-gma`` command.

The same as ``uvicorn dagents.gma.main:app``, with the host and port taken
from ``--host`` and ``--port`` or ``GMA_API_HOST`` and ``GMA_API_PORT``
(default ``127.0.0.1:8020``). The GMA reads its other settings from the
``GMA_*`` variables (:meth:`dagents.gma.config.Settings.from_env`).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from dagents.common.serve import serve


def main(argv: Sequence[str] | None = None) -> int:
    return serve(agent="GMA", app_path="dagents.gma.main:app", env_prefix="GMA", default_port=8020, argv=argv)


if __name__ == "__main__":
    sys.exit(main())
