"""FastAPI entrypoint for the Local Monitoring Agent.

``uvicorn dagents.lma.main:app`` serves one LMA built from the ``LMA_*``
settings (:meth:`dagents.lma.config.Settings.from_env`). An application that
runs LMAs in its own process builds them with
:func:`dagents.lma.app_factory.create_app` instead.
"""

from dagents.lma.app_factory import create_app
from dagents.lma.config import Settings

# The API reference names this request body by its path in this module.
from dagents.lma.routes import GuardEnforcementRequest

__all__ = ["GuardEnforcementRequest", "app"]

app = create_app(Settings.from_env())
