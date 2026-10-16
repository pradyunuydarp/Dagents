"""List an agent app's routes on every FastAPI version the ``lma`` and ``gma`` extras allow.

FastAPI 0.115, which CI pins, copies the routes of an included router into
``app.routes``. Newer versions keep an included router as one entry in
``app.routes``, so a test that reads ``app.routes`` finds none of the agent's
routes and checks nothing. These helpers return the same routes on both.

The LMA and GMA include their routers without a prefix, so each route keeps
the path it was declared with. ``flat_routes`` raises when that stops being
true, because a test would then see the wrong paths.
"""

from __future__ import annotations

from typing import Any

from fastapi import routing
from fastapi.routing import APIRoute


def flat_routes(app: Any) -> list[Any]:
    """Every route of ``app``, with the routes of included routers in place of the routers."""
    iter_route_contexts = getattr(routing, "iter_route_contexts", None)
    if iter_route_contexts is None:
        return list(app.routes)
    routes = []
    for context in iter_route_contexts(app.routes):
        route = context.original_route
        if getattr(route, "path", None) != context.path:
            raise AssertionError(
                f"route {context.name!r} is served at {context.path!r} but declared at "
                f"{getattr(route, 'path', None)!r}; flat_routes expects routers included without a prefix"
            )
        routes.append(route)
    return routes


def api_routes(app: Any) -> list[APIRoute]:
    """The FastAPI routes of ``app``: the ones with a handler, leaving out the docs pages."""
    return [route for route in flat_routes(app) if isinstance(route, APIRoute)]
