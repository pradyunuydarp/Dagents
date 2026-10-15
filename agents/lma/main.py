"""FastAPI entrypoint for the Local Monitoring Agent.

``uvicorn agents.lma.main:app`` serves one LMA built from the ``LMA_*``
settings (:meth:`agents.lma.config.Settings.from_env`). An application that
runs LMAs in its own process builds them with
:func:`agents.lma.app_factory.create_app` instead.
"""

from agents.lma.app_factory import create_app
from agents.lma.config import Settings

# The API reference names this request body by its path in this module.
from agents.lma.routes import GuardEnforcementRequest

__all__ = ["GuardEnforcementRequest", "app"]

app = create_app(Settings.from_env())
