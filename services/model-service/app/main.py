"""FastAPI application factory for the model service."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import settings
from app.ml.inventory import UnsupportedModelError


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="0.1.0")
    app.include_router(router)

    @app.exception_handler(UnsupportedModelError)
    def unsupported_model(_: Request, exc: UnsupportedModelError) -> JSONResponse:
        """Answer an unrunnable model family with 400 rather than 500.

        Asking for a family this deployment cannot execute is a bad request, not
        a server fault — and the OCaml router can legitimately select one, so
        this is a reachable path rather than a defensive one. The exception's
        message already names what is available and what a `planned` family
        would need, so it is passed through as the detail.
        """
        return JSONResponse(
            status_code=400,
            content={
                "detail": str(exc),
                "model_family": exc.family,
                "task": exc.task,
            },
        )

    return app


app = create_app()
