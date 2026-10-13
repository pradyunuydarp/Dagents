"""A JSON 500 for any error the app's handlers do not cover.

Starlette answers an unhandled error from its outermost layer, outside the CORS
middleware and :class:`app.api.app_header.AppHeader`. That 500 is plain text
without CORS headers, so a page on another origin cannot read it: the browser
reports a network error, and the page retries for 90 seconds before it says
the server is down. This middleware sits inside both, so its 500 carries the
CORS headers and ``X-Dagents-App``, and the page shows the error at once.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

#: What a visitor sees. The error itself goes to the server log.
UNEXPECTED_ERROR_MESSAGE = "The server could not complete this request."


class UnhandledErrors:
    """ASGI middleware that turns an unhandled exception into a JSON 500.

    If the response has not started, it sends
    ``{"detail": UNEXPECTED_ERROR_MESSAGE}`` with status 500. Then it raises
    the exception again, as Starlette's own error middleware does, so the
    server logs it with its traceback.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def tracked(message: dict) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracked)
        except Exception:
            if not started:
                payload = json.dumps({"detail": UNEXPECTED_ERROR_MESSAGE}).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 500,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": payload})
            raise
