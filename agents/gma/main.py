"""FastAPI entrypoint for the Global Monitoring Agent.

``uvicorn agents.gma.main:app`` serves a GMA built from the environment
(:meth:`agents.gma.config.Settings.from_env`). An application that runs a GMA
in its own process calls :func:`agents.gma.app_factory.create_app` instead.
The routes are in :mod:`agents.gma.routes`.
"""

from agents.common.domain.federation import ReleaseEvaluationRequest
from agents.gma.app_factory import create_app
from agents.gma.config import Settings
from agents.gma.routes import GuardEnforcementRequest

app = create_app(Settings.from_env())

__all__ = ["GuardEnforcementRequest", "ReleaseEvaluationRequest", "app"]
