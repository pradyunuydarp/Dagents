"""Forward each hospital's public routes to that hospital's LMA.

In a split run (``HEALTHCARE_DEMO_ROLE=app``) the API the frontend calls holds
no hospital records. Each hospital runs as its own LMA process. The app still
answers every consortium route itself, and :func:`install_forwarding` sends
the routes that belong to one hospital to that hospital's LMA:

- ``/api/v1/hospitals/{site_id}`` and every path below it, any method.
- ``POST /api/v1/governance:probe``, to the hospital the body's ``site_id``
  names, or the first hospital when the body names none or is not JSON.
- ``GET /api/v1/framework/trace``, to the hospital the ``site_id`` query
  names, or the first hospital.

``GET /api/v1/hospitals`` (the list) is not forwarded. An unknown hospital is
the same 404 the combined app gives. A hospital that cannot be reached, or
does not answer in time, is a 503 whose message names the hospital, never an
address.

The app is the public edge, so it keeps the per-client rate limits for data
entry and probes, spent exactly where the combined app's route dependencies
spend them. The LMAs run with those limits off, because every request reaches
them from the app. The hourly probe cap stays in the LMA that runs the probe.

The forwarding routes are inserted ahead of the app's own routes and are
left out of the OpenAPI document, so the document still lists the routes the
app declares. They run inside the app's middleware, so every forwarded answer
carries the app's CORS headers and ``X-Dagents-App``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping
from urllib.parse import parse_qs, quote

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.api.limits import RateLimiter
from app.domain.dataset import UnknownSiteError, any_site_for

logger = logging.getLogger("healthcare_demo.forward")

#: Request headers passed to the LMA. The others describe the visitor's
#: connection to the app (cookies, X-Forwarded-For, Origin) and stay here.
FORWARDED_REQUEST_HEADERS = ("content-type", "accept", "if-none-match", "idempotency-key")

#: Response headers passed back to the visitor. The app adds its own CORS
#: headers and ``X-Dagents-App``.
FORWARDED_RESPONSE_HEADERS = (
    "content-type",
    "etag",
    "retry-after",
    "cache-control",
    "x-content-type-options",
    "content-security-policy",
)

#: Characters left as they are when the decoded path is encoded again. The
#: chart routes have ``.`` in their paths and the probe has ``:``.
_PATH_SAFE = "/:@!$&'()*+,;=-._~"

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _short(value: object, limit: int = 100) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def unknown_hospital(site_id: str) -> JSONResponse:
    """The 404 the combined app gives for a hospital it does not have."""
    return JSONResponse(status_code=404, content={"detail": f"There is no hospital with the id {_short(site_id)!r}."})


def hospital_name(site_id: str) -> str:
    """The name a visitor knows the hospital by, for error messages."""
    try:
        return any_site_for(site_id).display_name
    except UnknownSiteError:
        return f"Hospital {_short(site_id, 64)}"


def is_clean(path: str) -> bool:
    """Whether the decoded path has no empty, ``.`` or ``..`` segment.

    The route a request matches is decided on the decoded path. An HTTP client
    removes ``..`` segments before it sends a request, so
    ``/api/v1/hospitals/uk-134/../../federation/jobs:execute`` would reach the
    LMA's internal round route. Such a path is never forwarded. A trailing
    slash is allowed.
    """
    segments = path.split("/")[1:]
    if segments and segments[-1] == "":
        segments = segments[:-1]
    return path.startswith("/") and "\\" not in path and all(part not in ("", ".", "..") for part in segments)


def body_site_id(body: bytes) -> str | None:
    """The ``site_id`` of a probe body, or ``None`` when it names none or is not JSON.

    A body that gives ``site_id`` in another type goes to the first hospital,
    whose validation answers 422 as the combined app does.
    """
    try:
        payload = json.loads(body or b"null")
    except ValueError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("site_id"), str):
        return payload["site_id"]
    return None


class Forwarder:
    """Sends a request to one hospital's LMA and returns its answer.

    Params:
    - `lma_urls`: hospital id to its LMA's base address, in display order.
      The first is the hospital a probe or a trace uses when none is named.
    - `timeout`: seconds to wait for the LMA.
    - `transport`, `mounts`: passed to ``httpx.AsyncClient``, for tests.
    """

    def __init__(
        self,
        lma_urls: Mapping[str, str],
        *,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
        mounts: Mapping[str, httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        if not lma_urls:
            raise ValueError("forwarding needs at least one hospital in HEALTHCARE_DEMO_LMA_URLS")
        self.lma_urls = {site_id: url.rstrip("/") for site_id, url in lma_urls.items()}
        self.first_site = next(iter(self.lma_urls))
        self.timeout = timeout
        self._transport = transport
        self._mounts = dict(mounts or {})
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _client_for_this_loop(self) -> httpx.AsyncClient:
        # A client's connections belong to the event loop that opened them.
        # The server runs one loop; a test client may start a new one per
        # request, so the client is opened again when the loop changes.
        loop = asyncio.get_running_loop()
        if self._client is None or self._client.is_closed or self._loop is not loop:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                mounts=self._mounts or None,
                timeout=self.timeout,
                follow_redirects=False,
            )
            self._loop = loop
        return self._client

    async def aclose(self) -> None:
        """Close the connections to the LMAs. The next request opens them again."""
        if self._client is not None and self._loop is asyncio.get_running_loop():
            await self._client.aclose()
        self._client = None
        self._loop = None

    async def send(self, site_id: str, request: Request, body: bytes) -> Response:
        """Forward ``request`` to ``site_id``'s LMA, at the same path and query."""
        if site_id not in self.lma_urls:
            return unknown_hospital(site_id)
        path = request.url.path
        url = httpx.URL(
            self.lma_urls[site_id] + quote(path, safe=_PATH_SAFE),
            query=request.scope.get("query_string", b""),
        )
        headers = {key: request.headers[key] for key in FORWARDED_REQUEST_HEADERS if key in request.headers}
        name = hospital_name(site_id)
        try:
            answer = await self._client_for_this_loop().request(
                request.method, url, content=body or None, headers=headers
            )
        except httpx.TimeoutException:
            logger.warning("%s did not answer %s %s in time", site_id, request.method, path)
            raise HTTPException(
                status_code=503, detail=f"{name} did not answer in time. Try again in a minute."
            ) from None
        except httpx.TransportError as error:
            logger.warning("%s cannot be reached for %s %s: %s", site_id, request.method, path, error)
            raise HTTPException(
                status_code=503, detail=f"{name} cannot be reached right now. Try again in a minute."
            ) from None
        passed = {key: answer.headers[key] for key in FORWARDED_RESPONSE_HEADERS if key in answer.headers}
        return Response(content=answer.content, status_code=answer.status_code, headers=passed)


def is_entry(rest: str) -> bool:
    """Whether a path below a hospital is one of the two data-entry routes.

    ``encounters`` stores a record and ``encounters/{id}/outcome`` records its
    outcome. Both spend the ``entries`` limit when posted.
    """
    parts = rest.split("/")
    return parts == ["encounters"] or (len(parts) == 3 and parts[0] == "encounters" and parts[2] == "outcome")


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


def install_forwarding(
    app: FastAPI,
    settings: Any,
    rate_limits: RateLimiter,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    mounts: Mapping[str, httpx.AsyncBaseTransport] | None = None,
) -> Forwarder:
    """Send ``app``'s hospital routes to the hospitals' LMAs.

    Params:
    - `app`: the FastAPI app. Its middleware applies to the forwarded routes.
    - `settings`: the app's settings. Reads ``lma_url_map()`` and
      ``upstream_timeout_seconds``.
    - `rate_limits`: the app's per-client limits. Data entry spends
      ``entries`` and a probe spends ``probes``, before the hospital is
      checked, as the combined app's route dependencies do.
    - `transport`, `mounts`: passed to ``httpx.AsyncClient``, for tests.

    Returns the :class:`Forwarder`, also kept in ``app.state.forwarder``.
    Call its ``aclose()`` when the app shuts down.
    """
    forwarder = Forwarder(
        settings.lma_url_map(),
        timeout=settings.upstream_timeout_seconds,
        transport=transport,
        mounts=mounts,
    )
    app.state.forwarder = forwarder

    async def hospital_route(request: Request) -> Response:
        path = request.url.path
        if not is_clean(path):
            return _not_found()
        site_id = request.path_params["site_id"]
        rest = request.path_params.get("rest", "")
        if request.method == "POST" and is_entry(rest):
            rate_limits.dependency("entries")(request)
        return await forwarder.send(site_id, request, await request.body())

    async def probe_route(request: Request) -> Response:
        rate_limits.dependency("probes")(request)
        body = await request.body()
        return await forwarder.send(body_site_id(body) or forwarder.first_site, request, body)

    async def trace_route(request: Request) -> Response:
        values = parse_qs(request.url.query).get("site_id")
        site_id = values[0] if values and values[0] else forwarder.first_site
        return await forwarder.send(site_id, request, b"")

    routes = [
        APIRoute("/api/v1/hospitals/{site_id}", hospital_route, methods=_METHODS, include_in_schema=False),
        APIRoute("/api/v1/hospitals/{site_id}/{rest:path}", hospital_route, methods=_METHODS, include_in_schema=False),
        APIRoute("/api/v1/governance:probe", probe_route, methods=["POST"], include_in_schema=False),
        APIRoute("/api/v1/framework/trace", trace_route, methods=["GET"], include_in_schema=False),
    ]
    # Ahead of the app's own routes, which match the same paths.
    app.router.routes[0:0] = routes
    return forwarder
