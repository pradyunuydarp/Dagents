"""A header on every response this app sends, so a client can tell it from the platform's.

The hosting platform answers 502, 503 or 504 while the container starts, and
the app answers 503 itself when a hospital's store cannot be reached. The
frontend retries the first kind and must show the second at once. The status
alone cannot tell them apart; this header can, because only the app sets it.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

#: The header and its value. The frontend's `liveFetch` reads the name, and the
#: CORS settings expose it to the published page.
APP_HEADER = "X-Dagents-App"
APP_HEADER_VALUE = "healthcare-demo"

_HEADER = (APP_HEADER.lower().encode("latin-1"), APP_HEADER_VALUE.encode("latin-1"))


class AppHeader:
    """ASGI middleware that adds :data:`APP_HEADER` to every HTTP response.

    It wraps ``send``, so it marks every response that passes through it:
    successes, the app's own errors, and responses that middleware inside it
    sends, such as the 413 from :class:`app.api.limits.BodySizeLimit`.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def marked(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = [item for item in message.get("headers", []) if item[0].lower() != _HEADER[0]]
                message = {**message, "headers": [*headers, _HEADER]}
            await send(message)

        await self.app(scope, receive, marked)
