"""Internal routes of the app process (``HEALTHCARE_DEMO_ROLE=app``) for the hospitals' LMAs.

The LMAs call these; the frontend never does. They are served by a second
listener of the app process (:func:`internal_listener`), on
``HEALTHCARE_DEMO_INTERNAL_HOST`` and ``HEALTHCARE_DEMO_INTERNAL_PORT``, never
on the public port. The public port is the one a compose file publishes and the
frontend's dev server proxies, and these routes have no authentication, so a
visitor could otherwise store an update for a hospital or read every model's
parameters.

- ``GET /api/v1/registry/model-in-use`` and
  ``GET /api/v1/registry/models/{version}``: one model with its parameters,
  which a hospital checks against the digests a round names. 404 for an
  unknown version.
- ``POST /api/v1/federation/rounds/{round_id}/updates``: one hospital's
  bounded update (``UpdateSubmission``), stored in the app's runtime until the
  round is aggregated. 409 when the consortium refuses it (see
  ``Consortium.accept_update``).

A pilot needs these routes to answer while it runs, so their work runs in a
thread pool of their own (``INTERNAL_THREADS``). The public routes share
AnyIO's default pool of 40 threads, and requests that wait for the running
pilot, such as approvals, can fill it.
"""

from __future__ import annotations

import asyncio
import contextlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any, AsyncIterator, Callable, Iterator, Mapping, TypeVar

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException

from agents.common.infrastructure.federation_http import UPDATE_PATH, UpdateReceipt, UpdateSubmission

from app.api.errors import UnhandledErrors
from app.api.limits import BodySizeLimit
from app.services.consortium import Consortium, UpdateRefused
from app.services.remote import MODEL_IN_USE_PATH, MODEL_PATH
from app.services.site_store import UnknownModelError

#: Threads the internal routes run their work in. A round sends the app at
#: most one call from each hospital at a time.
INTERNAL_THREADS = 8

_T = TypeVar("_T")


def build_coordinator_router(consortium: Callable[[], Consortium]) -> APIRouter:
    """The app's internal routes. ``consortium`` returns the process's consortium."""
    router = APIRouter(include_in_schema=False)
    threads = ThreadPoolExecutor(max_workers=INTERNAL_THREADS, thread_name_prefix="internal-routes")

    async def run(work: Callable[[], _T]) -> _T:
        return await asyncio.get_running_loop().run_in_executor(threads, work)

    def model_row(version: str) -> dict[str, Any]:
        model = consortium().registry.get(version)
        if model is None:
            raise UnknownModelError(version)
        return model.row()

    @router.get(MODEL_IN_USE_PATH)
    async def registry_model_in_use() -> dict[str, Any]:
        return await run(lambda: consortium().registry.model_in_use().row())

    @router.get(MODEL_PATH)
    async def registry_model(version: str) -> dict[str, Any]:
        return await run(lambda: model_row(version))

    @router.post(UPDATE_PATH)
    async def submit_update(round_id: str, submission: UpdateSubmission) -> dict[str, Any]:
        try:
            digest = await run(lambda: consortium().accept_update(round_id, submission.job, submission.update))
        except UpdateRefused as error:
            raise HTTPException(status_code=409, detail=f"The update was not stored: {error}.") from None
        return UpdateReceipt(update_digest=digest).model_dump(mode="json")

    return router


def build_coordinator_app(
    consortium: Callable[[], Consortium],
    *,
    exception_handlers: Mapping[Any, Callable[..., Any]],
    max_body_bytes: int,
) -> FastAPI:
    """The ASGI app of the internal listener: the internal routes and nothing else.

    ``exception_handlers`` are the public app's, so an error gets the same
    answer it got when these routes were on the public app. It has no OpenAPI
    document and no CORS headers: no browser calls it.
    """
    internal = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    internal.include_router(build_coordinator_router(consortium))
    for exception, handler in exception_handlers.items():
        internal.add_exception_handler(exception, handler)
    internal.add_middleware(BodySizeLimit, max_bytes=max_body_bytes)
    internal.add_middleware(UnhandledErrors)
    return internal


class _InternalServer(uvicorn.Server):
    """A uvicorn server that leaves the process's signals to the public server.

    Its own handlers would replace the public server's, so Ctrl-C would stop
    only this listener. The public server stops it when it shuts down.
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@contextlib.asynccontextmanager
async def internal_listener(app: FastAPI, host: str, port: int) -> AsyncIterator[None]:
    """Serve ``app`` on ``host:port`` in this event loop for the length of the block.

    The block starts once the listener accepts connections. If the port cannot
    be bound, uvicorn logs the error and exits the process, as it does for the
    public port.
    """
    server = _InternalServer(uvicorn.Config(app, host=host, port=port, lifespan="off", log_config=None))
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            await task
            raise RuntimeError(f"the internal listener on {host}:{port} stopped before it started")
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        server.should_exit = True
        await task
