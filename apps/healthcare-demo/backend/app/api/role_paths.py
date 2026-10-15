"""Which public paths each role serves.

A hospital's LMA (``lma`` role) serves its own hospital's routes, the health
check and its internal routes. The app process (``app`` role) serves the rest,
and sends each hospital-specific request to that hospital's LMA. The combined
process serves everything.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from agents.common.infrastructure.federation_http import EXECUTE_PATH, OFFER_PATH

from app.services.remote import SITE_REPORT_PATH

#: Hospital-specific routes outside ``/api/v1/hospitals/{site_id}``: each runs
#: at one hospital, named in the body (probe) or the query (trace).
HOSPITAL_ROUTES = ("/api/v1/governance:probe", "/api/v1/framework/trace")

#: The prefix of every route about one hospital.
HOSPITAL_PREFIX = "/api/v1/hospitals/"

#: Routes an LMA serves besides its hospital's routes.
LMA_ROUTES = ("/health", "/api/v1/health", SITE_REPORT_PATH, OFFER_PATH, EXECUTE_PATH)


def is_hospital_path(path: str) -> bool:
    """Whether a path is about one hospital, so it runs at that hospital's LMA in a split run."""
    return path.startswith(HOSPITAL_PREFIX) or path in HOSPITAL_ROUTES


def lma_serves(path: str) -> bool:
    """Whether an LMA serves this path.

    Every path below ``/api/v1/hospitals/`` is let through: the route answers
    404 for a hospital that is not this LMA's, with the same message as one
    process gives for an unknown hospital.
    """
    return is_hospital_path(path) or path in LMA_ROUTES


class OnlyPaths:
    """Answer 404 for every HTTP path ``serves`` does not accept.

    Added inside the CORS middleware and the app header, so the 404 carries
    both, like any other answer.
    """

    def __init__(self, app: Any, serves: Callable[[str], bool]) -> None:
        self.app = app
        self._serves = serves

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Any) -> None:
        if scope["type"] != "http" or self._serves(scope["path"]):
            await self.app(scope, receive, send)
            return
        body = json.dumps({"detail": "Not Found"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 404,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})
