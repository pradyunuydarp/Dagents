"""Build a Local Monitoring Agent app.

``create_app(settings, deps)`` builds one LMA. A process may build several,
one per site: each app keeps its services in ``app.state.lma`` and shares
nothing with the others. Everything is set up when the app is created,
because an app called in process through ``httpx.ASGITransport`` receives no
lifespan events. When the app runs under uvicorn, its shutdown closes what the
LMA built from its settings (an HTTP client for the GMA, a site source, a
database connection); objects passed in ``deps`` belong to the caller.

Errors are answered as JSON ``{detail, code}``:

| Error | Status | `code` |
|---|---|---|
| A missing or unknown token / a token of the wrong scope | 401 / 403 | `unauthorized` / `forbidden` |
| The planner could not be run | 503 | `planner_unavailable` |
| The site's store could not be reached / is not set up | 503 | `store_unavailable` / `store_not_set_up` |
| A role-scoped transaction could not be opened | 503 | `store_not_set_up` |
| A classification conflict, a round conflict, a model that does not verify | 409 | `conflict` |
| A request that names a source only configuration may set | 403 | `forbidden` |
| No site configured, no released model | 404 | `not_found` |
| The GMA did not answer / answered with an error | 503 / 502 | `agent_unavailable` / `bad_gateway` |

No ``detail`` holds a host, a token or a connection string.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dagents.common.domain.errors import DagentsError
from dagents.common.infrastructure.agent_auth import agent_error_response, install_agent_access
from dagents.common.infrastructure.dagents_runner import PlannerUnavailableError
from dagents.common.infrastructure.postgres_roles import RoleScopeError
from dagents.lma.config import Settings
from dagents.lma.di import LmaDeps, LmaRuntime, build_runtime
from dagents.lma.routes import router

__all__ = ["LmaDeps", "create_app"]


async def _dagents_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, DagentsError)
    return agent_error_response(error.http_status, error.code, str(error))


async def _planner_unavailable(request: Request, error: Exception) -> JSONResponse:
    # The planner's own message can name the binary's path, so it is not forwarded.
    return agent_error_response(503, "planner_unavailable", "the planner could not be run")


async def _role_scope(request: Request, error: Exception) -> JSONResponse:
    return agent_error_response(503, "store_not_set_up", str(error))


def install_error_handlers(app: FastAPI) -> None:
    """Answer the framework's typed errors with ``{detail, code}`` and their status."""
    app.add_exception_handler(DagentsError, _dagents_error)
    app.add_exception_handler(PlannerUnavailableError, _planner_unavailable)
    app.add_exception_handler(RoleScopeError, _role_scope)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    runtime: LmaRuntime = app.state.lma
    runtime.close()


def create_app(settings: Settings | None = None, deps: LmaDeps | None = None) -> FastAPI:
    """Build one LMA from its settings and the objects an application hands it.

    Params:
    - `settings`: the LMA's settings. ``None`` uses the defaults: an LMA with
      no site and no tokens, as before sites existed.
    - `deps`: objects the application owns, such as its own site source, audit
      sink, job log or agent network (see :class:`LmaDeps`).

    Raises ``ValueError`` or ``ExtensionError`` for settings that do not fit
    together, and ``AccessConfigurationError`` when tokens are required and
    none is set. Nothing it built is left open when it raises.
    """
    settings = settings or Settings()
    runtime = build_runtime(settings, deps)
    try:
        app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=_lifespan)
        app.state.lma = runtime
        install_agent_access(
            app,
            runtime.access,
            agent_name=settings.app_name,
            require_tokens=settings.require_tokens,
            expected_scopes=("site", "coordinator") if runtime.site is not None else (),
        )
        install_error_handlers(app)
        app.include_router(router)
    except BaseException:
        runtime.close()
        raise
    return app
