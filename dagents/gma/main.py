"""FastAPI entrypoint for the Global Monitoring Agent.

``uvicorn dagents.gma.main:app`` serves a GMA built from the environment
(:meth:`dagents.gma.config.Settings.from_env`). An application that runs a GMA
in its own process calls :func:`dagents.gma.app_factory.create_app` instead.
The routes are in :mod:`dagents.gma.routes`.
"""

from dagents.common.domain.federation import ReleaseEvaluationRequest
from dagents.gma.app_factory import create_app
from dagents.gma.config import Settings
from dagents.gma.routes import GuardEnforcementRequest

app = create_app(Settings.from_env())

__all__ = ["GuardEnforcementRequest", "ReleaseEvaluationRequest", "app"]
