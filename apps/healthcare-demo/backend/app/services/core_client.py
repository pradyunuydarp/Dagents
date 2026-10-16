"""The app's client of the Dagents core service.

When a hospital joins while the demo runs, core-service renders the new LMA's
Compose file (``POST /api/v1/manifests/compose``), with the OCaml manifest
compiler. The app never writes Compose. Only the deployer
(``app.services.deployment``) uses this client.

No error repeats the service's address: an error is logged with
:func:`~app.services.agents.loggable`, and the page gets a fixed sentence.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.services.agents import loggable

logger = logging.getLogger(__name__)

#: The route that renders a Compose file.
COMPOSE_PATH = "/api/v1/manifests/compose"


class CoreServiceUnavailable(RuntimeError):
    """core-service did not answer, or answered with something other than a Compose plan."""


class CoreServiceClient:
    """Calls core-service at ``base_url``.

    Params:
    - `base_url`: ``HEALTHCARE_DEMO_CORE_SERVICE_URL``.
    - `timeout_s`: how long a render may take.
    - `health_timeout_s`: how long the health check may take.
    - `transport`: an ``httpx`` transport, for tests.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 30.0,
        health_timeout_s: float = 2.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._health_timeout_s = health_timeout_s
        self._client = httpx.Client(base_url=self._base_url, transport=transport, trust_env=False)

    def __repr__(self) -> str:
        return "CoreServiceClient()"

    def health(self) -> bool:
        """Whether ``GET /api/v1/health`` answers 200 in time."""
        try:
            response = self._client.get("/api/v1/health", timeout=self._health_timeout_s)
        except httpx.HTTPError as error:
            logger.warning("core-service did not answer its health check: %s", loggable(error))
            return False
        return response.status_code == 200

    def render_compose(self, request: dict[str, Any]) -> dict[str, Any]:
        """core-service's answer to ``request``: ``{plan_id, services, issues, compose_yaml}``.

        Raises :class:`CoreServiceUnavailable` when no answer comes, or the
        answer is not a 200 with a JSON object.
        """
        try:
            response = self._client.post(COMPOSE_PATH, json=request, timeout=self._timeout_s)
        except httpx.HTTPError as error:
            raise CoreServiceUnavailable(f"no answer from {COMPOSE_PATH}: {loggable(error)}") from None
        if response.status_code != 200:
            raise CoreServiceUnavailable(f"{COMPOSE_PATH} answered {response.status_code}")
        try:
            answer = response.json()
        except ValueError:
            raise CoreServiceUnavailable(f"{COMPOSE_PATH} answered with a body that is not JSON") from None
        if not isinstance(answer, dict):
            raise CoreServiceUnavailable(f"{COMPOSE_PATH} answered with JSON that is not an object")
        return answer

    def close(self) -> None:
        self._client.close()


__all__ = ["COMPOSE_PATH", "CoreServiceClient", "CoreServiceUnavailable"]
