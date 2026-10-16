"""Limits that keep the public demo usable for everyone.

The demo has no accounts. Five limits protect it:

- :class:`RateLimiter`: a token bucket per client and per group of POST
  endpoints. A client may send ``per_minute`` requests at once, then one more
  every ``60 / per_minute`` seconds. A request over the limit gets 429 with a
  ``Retry-After`` header.
- :class:`HourlyCap`: at most a set number of an action in any 60 minutes,
  whoever asks: Guard probes per hospital, and pilots. Each probe and pilot
  adds audit records and model versions that stay in the database, so this
  bounds how fast the stored chains grow. Beyond the cap the API answers 429
  with ``Retry-After``.
- :class:`BodySizeLimit`: ASGI middleware that refuses a POST, PUT or PATCH
  body larger than a set number of bytes with 413, and a request that has both
  ``Content-Length`` and ``Transfer-Encoding`` with 400.
- :class:`SingleFlight`: one long action at a time: a pilot, or adding a
  hospital. A second request while one runs gets 409 instead of waiting. The
  flight names its holder, so the 409 says what is running.
- :class:`InFlightCap`: ASGI middleware that lets a set number of requests run
  at once. A request waits up to a set time for a slot, then gets the app's 503
  "busy" answer. The Dagents agents run in this process and share one pool of
  threads, and a request that makes nested agent calls holds several of them,
  so the cap keeps the pool from running out.

The limits live in this process. With several processes each keeps its own
count, which is enough for a demo on one instance.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from fastapi import HTTPException, Request

#: Buckets kept before idle ones are dropped, so many clients cannot fill memory.
MAX_BUCKETS = 10_000


def forwarded_for_entries(request: Request) -> list[str]:
    """The entries of every ``X-Forwarded-For`` header, left to right."""
    joined = ",".join(request.headers.getlist("x-forwarded-for"))
    return [entry.strip() for entry in joined.split(",")] if joined.strip() else []


def client_key(request: Request, forwarded_for_hops: int = 0) -> str:
    """The address a limit counts requests against.

    Each proxy in front of the app appends to ``X-Forwarded-For`` the address
    it received the request from. Everything to the left of what the proxies
    appended came from the client, which can write any address there. So with
    ``forwarded_for_hops`` proxies in front of the app, the client is the entry
    that many places from the right, and the entries further left are ignored.

    ``0`` means no proxy is trusted, and the TCP peer is used. A header with
    fewer entries than ``forwarded_for_hops`` did not pass through every proxy,
    so the TCP peer is used for it too.
    """
    if forwarded_for_hops > 0:
        entries = forwarded_for_entries(request)
        if len(entries) >= forwarded_for_hops:
            chosen = entries[-forwarded_for_hops]
            if chosen:
                return chosen[:100]
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    """Token buckets per ``(group, client)``.

    Params:
    - `per_minute`: group name to requests per minute. ``0`` or a missing
      group means no limit.
    - `forwarded_for_hops`: the number of proxies in front of the app that
      append to ``X-Forwarded-For``. See :func:`client_key`.
    - `clock`: seconds, for tests. Defaults to ``time.monotonic``.
    """

    def __init__(
        self,
        per_minute: Mapping[str, int],
        *,
        forwarded_for_hops: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self.forwarded_for_hops = 0
        self.per_minute: dict[str, int] = {}
        self.configure(per_minute, forwarded_for_hops=forwarded_for_hops)

    def configure(self, per_minute: Mapping[str, int], *, forwarded_for_hops: int | None = None) -> None:
        """Replace the limits and forget every client's count."""
        for group, limit in per_minute.items():
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError(f"rate limit for {group} must be a whole number of 0 or more, got {limit!r}")
        if forwarded_for_hops is not None and (
            isinstance(forwarded_for_hops, bool) or not isinstance(forwarded_for_hops, int) or forwarded_for_hops < 0
        ):
            raise ValueError(f"forwarded_for_hops must be a whole number of 0 or more, got {forwarded_for_hops!r}")
        with self._lock:
            self.per_minute = dict(per_minute)
            if forwarded_for_hops is not None:
                self.forwarded_for_hops = forwarded_for_hops
            self._buckets.clear()

    def reset(self) -> None:
        """Forget every client's count."""
        with self._lock:
            self._buckets.clear()

    def take(self, group: str, client: str) -> float:
        """Spend one request. Returns 0, or the seconds to wait before retrying."""
        limit = self.per_minute.get(group, 0)
        if limit <= 0:
            return 0.0
        rate = limit / 60.0
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get((group, client))
            if bucket is None:
                if len(self._buckets) >= MAX_BUCKETS:
                    self._drop_full(now)
                bucket = self._buckets[(group, client)] = _Bucket(tokens=float(limit), updated=now)
            bucket.tokens = min(float(limit), bucket.tokens + (now - bucket.updated) * rate)
            bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return 0.0
            return (1.0 - bucket.tokens) / rate

    def _drop_full(self, now: float) -> None:
        """Drop buckets that have refilled; they hold nothing a new bucket would not."""
        for key, bucket in list(self._buckets.items()):
            limit = self.per_minute.get(key[0], 0)
            if limit <= 0 or bucket.tokens + (now - bucket.updated) * limit / 60.0 >= limit:
                del self._buckets[key]
        if len(self._buckets) >= MAX_BUCKETS:
            oldest = sorted(self._buckets, key=lambda key: self._buckets[key].updated)
            for key in oldest[: len(oldest) // 2]:
                del self._buckets[key]

    def dependency(self, group: str) -> Callable[[Request], None]:
        """A FastAPI dependency that spends one request from ``group``."""

        def limit(request: Request) -> None:
            wait = self.take(group, client_key(request, self.forwarded_for_hops))
            if wait > 0:
                seconds = max(1, math.ceil(wait))
                raise HTTPException(
                    status_code=429,
                    detail=f"Too many requests. Try again in {seconds} seconds.",
                    headers={"Retry-After": str(seconds)},
                )

        return limit


class HourlyCap:
    """At most ``per_hour[group]`` actions per key in any 60 minutes, counted in this process.

    Params:
    - `per_hour`: group name to actions per hour. ``0`` or a missing group
      means no cap.
    - `clock`: seconds, for tests. Defaults to ``time.monotonic``.

    The key is what the cap is counted against, for example a hospital id. The
    caller passes only keys it has checked, such as a known hospital, so the
    number of keys stays small. A refused action is not counted.
    """

    WINDOW_SECONDS = 3600.0

    def __init__(self, per_hour: Mapping[str, int], *, clock: Callable[[], float] = time.monotonic) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._taken: dict[tuple[str, str], deque[float]] = {}
        self.per_hour: dict[str, int] = {}
        self.configure(per_hour)

    def configure(self, per_hour: Mapping[str, int]) -> None:
        """Replace the caps and forget every count."""
        for group, cap in per_hour.items():
            if isinstance(cap, bool) or not isinstance(cap, int) or cap < 0:
                raise ValueError(f"hourly cap for {group} must be a whole number of 0 or more, got {cap!r}")
        with self._lock:
            self.per_hour = dict(per_hour)
            self._taken.clear()

    def take(self, group: str, key: str = "all") -> float:
        """Count one action. Returns 0, or the seconds until the cap allows another."""
        cap = self.per_hour.get(group, 0)
        if cap <= 0:
            return 0.0
        now = self._clock()
        with self._lock:
            taken = self._taken.setdefault((group, key), deque())
            while taken and now - taken[0] >= self.WINDOW_SECONDS:
                taken.popleft()
            if len(taken) >= cap:
                return taken[0] + self.WINDOW_SECONDS - now
            taken.append(now)
            return 0.0


def retry_after(seconds: float) -> tuple[str, str]:
    """``(Retry-After value, "about N minutes")`` for a wait in seconds."""
    whole = max(1, math.ceil(seconds))
    minutes = max(1, math.ceil(whole / 60))
    return str(whole), f"about {minutes} minute{'s' if minutes != 1 else ''}"


class SingleFlight:
    """Lets one caller in at a time and turns the others away at once.

    The caller that enters names itself (``holder``), such as ``"pilot"`` or
    ``"deployment"``, so a refused caller can say what is running. A pilot and
    the addition of a hospital share one flight: both change which hospitals
    take part, so they never run at the same time.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holder: str | None = None

    def try_enter(self, holder: str = "") -> bool:
        """Enter and record ``holder``, or return ``False`` at once when the flight is held."""
        if not self._lock.acquire(blocking=False):
            return False
        self._holder = holder
        return True

    def leave(self) -> None:
        """Clear the holder, then let the next caller in."""
        self._holder = None
        self._lock.release()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    @property
    def holder(self) -> str | None:
        """Who holds the flight now, or ``None`` when nobody does.

        A refused caller reads it after ``try_enter`` failed. The holder may
        have left in between, so the caller treats ``None`` as its own kind.
        """
        return self._holder


ASGIApp = Callable[[dict, Callable[[], Awaitable[dict]], Callable[[dict], Awaitable[None]]], Awaitable[None]]

_LIMITED_METHODS = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimit:
    """Refuses a request body larger than ``max_bytes`` with 413.

    A declared ``Content-Length`` over the limit is refused before anything is
    read. Every other body is read and counted up to one byte past the limit,
    then passed on whole, whatever its headers say. The server's HTTP parser
    frames a request that has both ``Content-Length`` and
    ``Transfer-Encoding`` by the chunked encoding, so a small declared length
    can come with a large body. Such a request is refused with 400 before the
    body is read.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = int(max_bytes)

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") not in _LIMITED_METHODS or self.max_bytes <= 0:
            await self.app(scope, receive, send)
            return
        names = {name.lower() for name, _ in scope.get("headers") or []}
        if b"content-length" in names and b"transfer-encoding" in names:
            await self._respond(send, 400, "The request has both Content-Length and Transfer-Encoding.")
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = False
            if too_large:
                await self._refuse(send)
                return

        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await self._refuse(send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        sent = False

        async def replay() -> dict:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    async def _refuse(self, send: Any) -> None:
        kilobytes = max(1, self.max_bytes // 1024)
        await self._respond(send, 413, f"The request body is larger than {kilobytes} KB.")

    @staticmethod
    async def _respond(send: Any, status: int, detail: str) -> None:
        payload = json.dumps({"detail": detail}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": payload})


#: What a visitor sees when every slot stays taken for the whole wait.
BUSY_MESSAGE = "The demo server is busy. Try again in a minute."

#: Paths the in-flight cap lets through without a slot: the health check reads
#: no store and calls no agent, and the platform must reach it under load.
UNCAPPED_PATHS = frozenset({"/health", "/api/v1/health"})


class InFlightCap:
    """Lets at most ``max_in_flight`` requests run at once; the others wait for a slot.

    A request waits up to ``wait_seconds`` for a slot, then gets 503 with
    :data:`BUSY_MESSAGE` and ``Retry-After: 60``. The health paths
    (:data:`UNCAPPED_PATHS`) and non-HTTP traffic pass without a slot.

    The count is shared by every event loop in the process, so it holds under a
    server and in tests alike. A waiting request checks for a free slot after
    5 ms, then waits twice as long each time up to 100 ms; it holds no thread
    while it waits.

    Params:
    - `max_in_flight`: the number of slots, at least 1.
    - `wait_seconds`: how long a request waits for a slot.
    - `clock`: seconds, for tests. Defaults to ``time.monotonic``.
    """

    #: Seconds between checks for a free slot: the first wait, and the longest.
    FIRST_POLL_SECONDS = 0.005
    LONGEST_POLL_SECONDS = 0.1

    def __init__(
        self,
        app: ASGIApp,
        max_in_flight: int,
        wait_seconds: float = 20.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(max_in_flight, bool) or not isinstance(max_in_flight, int) or max_in_flight < 1:
            raise ValueError(f"max_in_flight must be a whole number of 1 or more, got {max_in_flight!r}")
        self.app = app
        self.max_in_flight = max_in_flight
        self.wait_seconds = float(wait_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._active = 0
        self.refused = 0

    @property
    def active(self) -> int:
        """How many requests hold a slot now."""
        with self._lock:
            return self._active

    def _try_take(self) -> bool:
        with self._lock:
            if self._active < self.max_in_flight:
                self._active += 1
                return True
            return False

    def _give_back(self) -> None:
        with self._lock:
            self._active -= 1

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Any) -> None:
        if scope["type"] != "http" or scope.get("path") in UNCAPPED_PATHS:
            await self.app(scope, receive, send)
            return
        deadline = self._clock() + self.wait_seconds
        pause = self.FIRST_POLL_SECONDS
        while not self._try_take():
            if self._clock() >= deadline:
                with self._lock:
                    self.refused += 1
                await self._busy(send)
                return
            await asyncio.sleep(pause)
            pause = min(pause * 2, self.LONGEST_POLL_SECONDS)
        try:
            await self.app(scope, receive, send)
        finally:
            self._give_back()

    @staticmethod
    async def _busy(send: Any) -> None:
        payload = json.dumps({"detail": BUSY_MESSAGE}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"retry-after", b"60"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
