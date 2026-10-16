"""Call an LMA or a GMA through its HTTP API, in process or over the network.

An application, and one agent calling another, reaches the agents through an
:class:`AgentNetwork`. The network has two transports, and the client code
above them is the same:

- :meth:`AgentNetwork.in_process` runs every agent app on one event-loop
  thread and calls it through ``httpx.ASGITransport``. A call goes through the
  real routes, request validation and response models, without a socket.
- :meth:`AgentNetwork.over_http` calls agents served by uvicorn, with one
  kept-alive ``httpx.Client`` per agent. A new client per call took 648 to
  705 ms at 0.1 CPU in the deploy measurements, so clients are always reused.

Agents are named :data:`GMA_AGENT` and ``lma_agent(site_id)``.
``network.gma(token)`` and ``network.lma(site_id, token)`` return typed
clients with one method per route; each returns the domain model the route
answers with.

Errors. A response that is not 2xx (or 304) raises :class:`AgentError` with
``status``, ``code`` and ``detail``. The ``code`` comes from the agent's
``{detail, code}`` body, or from the status when the body has none
(``not_found``, ``conflict``, ``forbidden`` and so on).
:class:`AgentUnavailableError` is raised when no answer came (a connection
failure, a timeout, an agent with no address) and for a 502, 503 or 504
without a JSON ``code``. Neither error ever holds a host, a port or the body of
an answer that is not JSON. The ``detail`` of a JSON error body is the agent's
own text, and a store error's text can name a database host, so text that
callers or users see uses :meth:`AgentError.without_detail`, and a log line
uses :func:`without_addresses`.

Call log. Inside ``with CallLog() as log:``, every call made from that context
is recorded as ``{service, site_id, method, path, status, ms}``, where
``path`` is the route template (``/api/v1/federation/rounds/{round_id}``),
never a real path or a host. The calls an agent makes while it answers are not
recorded in the caller's log.

Threads. The clients are synchronous: call them from a thread, such as a sync
FastAPI handler, never from the in-process network's own event loop. Every
in-process agent's sync handlers share the loop's thread limiter
(``thread_tokens``). A handler that calls another agent holds its thread while
it waits, so an application must cap the requests it lets in at once: with 40
tokens, 60 concurrent outer requests that each made a nested call deadlocked
in the deploy measurements; with 64 tokens they finished. Every call has a
timeout, so a starved call ends with :class:`AgentUnavailableError` instead of
waiting forever.

``ASGITransport`` sends no lifespan events, so an agent app built for in-process
use must set itself up when it is created, not in a lifespan handler.

Addresses at run time. An HTTP network can take an agent's address after it
was built (:meth:`AgentNetwork.add_address`) and give it up again
(:meth:`AgentNetwork.remove_address`). The map of clients is replaced, never
changed in place, so a call that has started keeps the client it found. A GMA
uses this for a site that joins a study while it runs; the GMA decides which
address it accepts.

Call :meth:`AgentNetwork.close` when you are done; it closes the clients and
stops the event-loop thread, cancelling calls still waiting. The loop thread is
a daemon thread, and the worker threads anyio starts from it are too, so a
process whose network was not closed still exits.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import json as jsonlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, ClassVar, Generic, Literal, Mapping, TypeVar
from urllib.parse import quote, urlsplit

import anyio
import anyio.to_thread
import httpx
from anyio.from_thread import BlockingPortal
from pydantic import TypeAdapter, ValidationError

from dagents.common.domain.federation import (
    AggregationReadiness,
    ApprovalResult,
    CandidateModel,
    FederatedJob,
    JobAcceptance,
    JobRun,
    ModelVersion,
    ModelVersionList,
    ModelVersionRegistration,
    ReleaseEvaluation,
    ReleaseEvaluationRequest,
    RoundManifest,
    RoundPlan,
    RoundRecord,
    SiteAddressList,
    SiteAddressRequest,
    SiteAddressState,
    SiteRegistration,
    SiteResult,
    UpdateReceipt,
    UpdateUpload,
)
from dagents.common.domain.site import (
    AuditSummary,
    JobLogEntry,
    LocalEvaluation,
    ModelInUse,
    ProbeRequest,
    ProbeResponse,
    SiteDirectoryEntry,
    SiteInfo,
    SiteSummary,
    SourceState,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The name of the GMA in an :class:`AgentNetwork`.
GMA_AGENT = "gma"

_LMA_PREFIX = "lma:"
_LOOP_THREAD_NAME = "dagents-agent-network"
_STOP_TIMEOUT_S = 10.0

Service = Literal["lma", "gma"]


def lma_agent(site_id: str) -> str:
    """The name of the LMA that serves ``site_id`` in an :class:`AgentNetwork`."""
    if not isinstance(site_id, str) or not site_id.strip():
        raise ValueError("an LMA needs a site id")
    return f"{_LMA_PREFIX}{site_id}"


def _parse_agent_name(name: str) -> tuple[Service, str | None]:
    if name == GMA_AGENT:
        return "gma", None
    if isinstance(name, str) and name.startswith(_LMA_PREFIX) and name[len(_LMA_PREFIX) :].strip():
        return "lma", name[len(_LMA_PREFIX) :]
    raise ValueError(f"unknown agent name {name!r}: use {GMA_AGENT!r} or lma_agent(site_id)")


def _label(service: Service, site_id: str | None) -> str:
    return "the GMA" if service == "gma" else f"the LMA of site {site_id}"


_ADDRESS = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")


def without_addresses(text: str) -> str:
    """``text`` with every ``scheme://...`` address replaced by ``[address]``.

    Use it before logging an agent's error: a store error's detail can quote a
    connection string. An answer to a caller should not carry the detail at
    all (see :meth:`AgentError.without_detail`).
    """
    return _ADDRESS.sub("[address]", text)


# ---------------------------------------------------------------------------
# Call log
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgentCall:
    """One call to an agent, as a :class:`CallLog` records it."""

    service: Service
    site_id: str | None
    method: str
    path: str
    status: int | None
    ms: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "site_id": self.site_id,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "ms": self.ms,
        }


_ACTIVE_LOG: contextvars.ContextVar[CallLog | None] = contextvars.ContextVar("dagents_agent_call_log", default=None)


class CallLog:
    """The agent calls made while the log is active, in the order they finished.

    Use it as a context manager. A log entered inside another records its
    calls in both. The active log is a context variable, so a sync FastAPI
    handler, which runs in a thread with a copy of the request's context,
    records into a log that middleware entered for the request.
    """

    def __init__(self) -> None:
        self._calls: list[AgentCall] = []
        self._lock = threading.Lock()
        self._parent: CallLog | None = None
        self._token: contextvars.Token[CallLog | None] | None = None

    def __enter__(self) -> CallLog:
        if self._token is not None:
            raise RuntimeError("this call log is already active")
        self._parent = _ACTIVE_LOG.get()
        self._token = _ACTIVE_LOG.set(self)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _ACTIVE_LOG.reset(self._token)
            self._token = None

    @staticmethod
    def current() -> CallLog | None:
        """The innermost active log in this context, or ``None``."""
        return _ACTIVE_LOG.get()

    def record(self, call: AgentCall) -> None:
        log: CallLog | None = self
        while log is not None:
            with log._lock:
                log._calls.append(call)
            log = log._parent

    @property
    def calls(self) -> list[AgentCall]:
        with self._lock:
            return list(self._calls)

    def as_list(self) -> list[dict[str, Any]]:
        """The calls as ``{service, site_id, method, path, status, ms}`` dictionaries."""
        return [call.as_dict() for call in self.calls]


# ---------------------------------------------------------------------------
# Errors and responses
# ---------------------------------------------------------------------------


class AgentError(Exception):
    """An agent answered with an error, or did not answer.

    ``status`` is the HTTP status, or ``None`` when no answer came. ``code`` is
    the agent's error code (see the module docstring). ``detail`` is the
    agent's message for a JSON error body, or the status phrase otherwise.
    """

    def __init__(
        self,
        *,
        service: Service,
        site_id: str | None,
        path: str,
        status: int | None,
        code: str,
        detail: str,
    ) -> None:
        self.service = service
        self.site_id = site_id
        self.path = path
        self.status = status
        self.code = code
        self.detail = detail
        super().__init__(self._message())

    def _message(self) -> str:
        return f"{_label(self.service, self.site_id)} answered {self.status} ({self.code}): {self.detail}"

    def without_detail(self) -> str:
        """Who answered, with which status and code, without the agent's detail.

        The detail is the agent's own text. A store error's detail can name a
        database host or a hosting vendor, so text that callers or users see
        should use this, and the detail should go only to a log, through
        :func:`without_addresses`.
        """
        return f"{_label(self.service, self.site_id)} answered {self.status} ({self.code})"


class AgentUnavailableError(AgentError):
    """No usable answer came: a connection failure, a timeout, no address, or a 502-504 without a code."""

    def _message(self) -> str:
        return f"{_label(self.service, self.site_id)} is unavailable: {self.detail}"

    def without_detail(self) -> str:
        # The client wrote this detail itself ("no answer within 30 s"), so it holds no host.
        return self._message()


@dataclass(frozen=True)
class AgentResponse:
    """A successful answer: a 2xx status, or 304. ``body`` is the parsed JSON, or ``None`` when empty."""

    status: int
    headers: httpx.Headers
    body: Any


_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "invalid_request",
    500: "server_error",
}


class _NotJson:
    """A body that could not be parsed as JSON. Its text is never forwarded."""


_NOT_JSON = _NotJson()


def _decode(content: bytes) -> Any:
    if not content:
        return None
    try:
        return jsonlib.loads(content)
    except ValueError:
        return _NOT_JSON


def _validation_summary(errors: list[Any]) -> str:
    parts = []
    for error in errors[:5]:
        if not isinstance(error, dict):
            continue
        location = ".".join(str(part) for part in error.get("loc", []) if part not in ("body", "query", "path"))
        message = str(error.get("msg", "is not valid"))
        parts.append(f"{location}: {message}" if location else message)
    text = "; ".join(parts) or "the request is not valid"
    if len(errors) > 5:
        text += f"; and {len(errors) - 5} more"
    return text


def _status_phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return f"HTTP {status}"


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Reply:
    status: int
    headers: httpx.Headers
    content: bytes


class _UnknownAgent(Exception):
    pass


class _NoAnswer(Exception):
    def __init__(self, timed_out: bool) -> None:
        super().__init__()
        self.timed_out = timed_out


class _NetworkClosed(Exception):
    pass


def _internal_base_url(name: str) -> str:
    service, site_id = _parse_agent_name(name)
    if service == "gma":
        return "http://gma.agents.internal"
    label = re.sub(r"[^a-z0-9-]+", "-", str(site_id).lower()).strip("-") or "site"
    return f"http://lma-{label}.agents.internal"


def _logged(app: Any, label: str) -> Any:
    """Log an app's unhandled error. ``ASGITransport`` turns it into a 500 without logging it."""

    async def logged_app(scope: Any, receive: Any, send: Any) -> None:
        try:
            await app(scope, receive, send)
        except Exception:
            logger.exception("%s raised an error while answering %s %s", label, scope.get("method"), scope.get("path"))
            raise

    return logged_app


class _LoopThread:
    """An event loop on a daemon thread, reached through an anyio blocking portal.

    Threads inherit daemon status from the thread that starts them, so the
    worker threads that run the agents' sync handlers are daemon threads too.
    """

    def __init__(self, thread_tokens: int) -> None:
        self._stop_lock = threading.Lock()
        self._stopped = False
        ready: concurrent.futures.Future[BlockingPortal] = concurrent.futures.Future()
        self._thread = threading.Thread(
            target=self._run, args=(ready, thread_tokens), name=_LOOP_THREAD_NAME, daemon=True
        )
        self._thread.start()
        self.portal = ready.result(timeout=30)

    @staticmethod
    def _run(ready: concurrent.futures.Future[BlockingPortal], thread_tokens: int) -> None:
        async def main() -> None:
            # FastAPI runs sync handlers and dependencies with this limiter.
            anyio.to_thread.current_default_thread_limiter().total_tokens = thread_tokens
            async with BlockingPortal() as portal:
                ready.set_result(portal)
                await portal.sleep_until_stopped()

        try:
            anyio.run(main)
        except BaseException as error:  # noqa: BLE001 - reported to the starting thread or logged
            if not ready.done():
                ready.set_exception(error)
            else:
                logger.exception("the agent network's event loop stopped with an error")

    @property
    def thread(self) -> threading.Thread:
        return self._thread

    def stop(self) -> None:
        with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
        if threading.get_ident() == self._thread.ident:
            raise RuntimeError("the agent network cannot be closed from its own event loop")
        try:
            # Cancel what is still running: a caller that waits gets "closed".
            self.portal.call(self.portal.stop, True)
        except RuntimeError:
            pass  # the portal has already stopped
        self._thread.join(_STOP_TIMEOUT_S)
        if self._thread.is_alive():
            logger.warning("the agent network's event loop did not stop within %s s", _STOP_TIMEOUT_S)


class _InProcessTransport:
    mode: ClassVar[str] = "in-process"

    def __init__(self, thread_tokens: int) -> None:
        self._clients: dict[str, httpx.AsyncClient] = {}
        self._lock = threading.Lock()
        self._closed = False
        self._loop = _LoopThread(thread_tokens)

    @property
    def agents(self) -> list[str]:
        with self._lock:
            return sorted(self._clients)

    def add_address(self, name: str, url: str) -> None:
        raise TypeError("an in-process network takes apps, not addresses; use add_app")

    def remove_address(self, name: str) -> None:
        raise TypeError("an in-process network takes apps, not addresses")

    def add(self, name: str, app: Any) -> None:
        service, site_id = _parse_agent_name(name)
        with self._lock:
            if self._closed:
                raise RuntimeError("the agent network is closed")
            if name in self._clients:
                raise ValueError(f"an app is already added as {name!r}")
            transport = httpx.ASGITransport(app=_logged(app, _label(service, site_id)), raise_app_exceptions=False)
            self._clients[name] = httpx.AsyncClient(
                transport=transport, base_url=_internal_base_url(name), timeout=None, trust_env=False
            )

    def send(
        self,
        name: str,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        body: Any,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> _Reply:
        with self._lock:
            if self._closed:
                raise _NetworkClosed()
            client = self._clients.get(name)
        if client is None:
            raise _UnknownAgent()
        try:
            future = self._loop.portal.start_task_soon(_send_async, client, method, path, params, body, dict(headers))
        except RuntimeError:
            if self._closed or not self._loop.thread.is_alive():
                raise _NetworkClosed() from None
            raise
        try:
            # Wait here, not inside the loop. anyio cannot cancel a task while
            # it waits for a sync handler's thread: anyio.fail_after(0.5)
            # around a handler that slept 1.5 s returned its answer after
            # 1.5 s, with no timeout. The handler's thread runs on after this
            # wait ends, as an in-process call always did.
            return future.result(timeout=timeout_s)
        except concurrent.futures.CancelledError:
            raise _NetworkClosed() from None
        except TimeoutError:
            if future.done():
                raise _NoAnswer(timed_out=True) from None
            future.cancel()
            raise _NoAnswer(timed_out=True) from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                self._loop.portal.call(client.aclose)
            except Exception:  # noqa: BLE001 - closing an in-memory client cannot lose data
                logger.debug("could not close an in-process agent client", exc_info=True)
        self._loop.stop()


async def _send_async(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    params: Mapping[str, Any] | None,
    body: Any,
    headers: dict[str, str],
) -> _Reply:
    # This task runs with a copy of the caller's context. The calls the agent
    # makes while it answers belong to the agent, not to the caller's log.
    _ACTIVE_LOG.set(None)
    response = await client.request(method, path, params=params, json=body, headers=headers)
    return _Reply(response.status_code, response.headers, response.content)


def normalize_base_url(name: str, url: str) -> str:
    """``url`` without a trailing ``/``, after the checks :meth:`AgentNetwork.over_http` makes.

    Raises ``ValueError`` for an address that is not http or https with a
    host, or that holds a user name, a password, a query or a fragment. The
    message names ``name``, never the address.
    """
    return _check_base_url(name, url)


def _check_base_url(name: str, url: str) -> str:
    try:
        parts = urlsplit(url)
        port_ok = parts.port is None or parts.port > 0
    except (ValueError, TypeError):
        raise ValueError(f"the address for {name} is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname or not port_ok:
        raise ValueError(f"the address for {name} must be an http or https URL with a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"the address for {name} must not hold a user name or a password")
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError(f"the address for {name} must not hold a query or a fragment")
    return url.rstrip("/")


def _unanswered_on_kept_connection(error: httpx.TransportError) -> bool:
    """True for the errors of a request the server closed or reset without answering.

    A server that closes a kept-alive connection with an unread request in it
    resets the connection, so the request was not processed. A processed
    request gets this error only when the server's process dies after the
    work and before the answer. The agents' routes that change state refuse a
    repeat with 409 (a reused round id, a second update for a site, a version
    that is no longer a candidate), so such a repeat fails closed.
    """
    if isinstance(error, (httpx.ReadError, httpx.WriteError)):
        return True
    return isinstance(error, httpx.RemoteProtocolError) and "without sending a response" in str(error)


class _HttpTransport:
    mode: ClassVar[str] = "network"

    def __init__(self, base_urls: Mapping[str, str], timeout_s: float) -> None:
        checked = {}
        for name, url in base_urls.items():
            _parse_agent_name(name)
            checked[name] = _check_base_url(name, url)
        self._lock = threading.Lock()
        self._closed = False
        self._timeout_s = timeout_s
        self._urls: dict[str, str] = dict(checked)
        self._clients = {name: self._client(url) for name, url in checked.items()}

    def _client(self, url: str) -> httpx.Client:
        # trust_env=False: a token must never go through a proxy that an
        # environment variable happens to name.
        return httpx.Client(base_url=url, timeout=httpx.Timeout(self._timeout_s), trust_env=False, follow_redirects=False)

    @property
    def agents(self) -> list[str]:
        return sorted(self._clients)

    def add(self, name: str, app: Any) -> None:
        raise TypeError("add_app is only for an in-process network; use add_address for an agent served over HTTP")

    def add_address(self, name: str, url: str) -> None:
        service, site_id = _parse_agent_name(name)
        checked = _check_base_url(name, url)
        with self._lock:
            if self._closed:
                raise RuntimeError("the agent network is closed")
            known = self._urls.get(name)
            if known == checked:
                return
            if known is not None:
                raise ValueError(f"{_label(service, site_id)} already has another address in this network")
            # Replace the maps, so a call that is reading them keeps the old one.
            self._clients = {**self._clients, name: self._client(checked)}
            self._urls = {**self._urls, name: checked}

    def remove_address(self, name: str) -> None:
        _parse_agent_name(name)
        with self._lock:
            client = self._clients.get(name)
            if client is None:
                return
            self._clients = {key: value for key, value in self._clients.items() if key != name}
            self._urls = {key: value for key, value in self._urls.items() if key != name}
        client.close()

    def send(
        self,
        name: str,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        body: Any,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> _Reply:
        if self._closed:
            raise _NetworkClosed()
        client = self._clients.get(name)
        if client is None:
            raise _UnknownAgent()
        for attempt in (1, 2):
            try:
                response = client.request(
                    method, path, params=params, json=body, headers=dict(headers), timeout=httpx.Timeout(timeout_s)
                )
            except httpx.TimeoutException:
                raise _NoAnswer(timed_out=True) from None
            except httpx.TransportError as error:
                # uvicorn closes a connection after an app error and after
                # 5 s idle. A call sent on such a kept-alive connection just
                # before it closed is reset unread, so it is sent once more
                # on a new connection. A second failure is reported.
                if attempt == 1 and _unanswered_on_kept_connection(error):
                    continue
                raise _NoAnswer(timed_out=False) from None
            except RuntimeError:
                if self._closed:
                    raise _NetworkClosed() from None
                if self._clients.get(name) is not client:
                    # The address was removed while this call was starting.
                    raise _UnknownAgent() from None
                raise
            return _Reply(response.status_code, response.headers, response.content)
        raise AssertionError("unreachable")  # pragma: no cover

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = list(self._clients.values())
        for client in clients:
            client.close()


# ---------------------------------------------------------------------------
# The network
# ---------------------------------------------------------------------------


def _positive(value: float, what: str) -> float:
    if not value > 0:
        raise ValueError(f"{what} must be greater than 0")
    return float(value)


class AgentNetwork:
    """The agents an application or an agent can call, in process or over HTTP.

    Build one with :meth:`in_process` or :meth:`over_http`, and close it with
    :meth:`close` (or use it as a context manager).

    ``timeout_s`` bounds every call. ``dispatch_timeout_s`` bounds the calls
    that make an agent call other agents: round dispatch, site refresh, site
    summaries and job execution.
    """

    def __init__(self, transport: _InProcessTransport | _HttpTransport, *, timeout_s: float, dispatch_timeout_s: float) -> None:
        self._transport = transport
        self.timeout_s = _positive(timeout_s, "timeout_s")
        self.dispatch_timeout_s = _positive(dispatch_timeout_s, "dispatch_timeout_s")

    @classmethod
    def in_process(
        cls,
        apps: Mapping[str, Any] | None = None,
        *,
        thread_tokens: int = 64,
        timeout_s: float = 30.0,
        dispatch_timeout_s: float = 120.0,
    ) -> AgentNetwork:
        """Run agent apps in this process, on one event-loop thread.

        ``apps`` maps agent names (:data:`GMA_AGENT`, ``lma_agent(site_id)``)
        to ASGI apps. Apps can also be added later with :meth:`add_app`, which
        an agent that calls other agents needs: it is built with the network.
        ``thread_tokens`` is the number of sync handlers that may run at once,
        across all the agents.
        """
        if not isinstance(thread_tokens, int) or thread_tokens < 1:
            raise ValueError("thread_tokens must be a whole number of at least 1")
        timeout_s = _positive(timeout_s, "timeout_s")
        dispatch_timeout_s = _positive(dispatch_timeout_s, "dispatch_timeout_s")
        network = cls(_InProcessTransport(thread_tokens), timeout_s=timeout_s, dispatch_timeout_s=dispatch_timeout_s)
        try:
            for name, app in (apps or {}).items():
                network.add_app(name, app)
        except Exception:
            network.close()
            raise
        return network

    @classmethod
    def over_http(
        cls,
        base_urls: Mapping[str, str],
        *,
        timeout_s: float = 30.0,
        dispatch_timeout_s: float = 120.0,
    ) -> AgentNetwork:
        """Call agents served over HTTP.

        ``base_urls`` maps agent names to base addresses, such as
        ``http://127.0.0.1:8081``. An address must be http or https with no
        user name, password, query or fragment; it may have a path prefix.
        Errors never repeat an address.
        """
        timeout_s = _positive(timeout_s, "timeout_s")
        dispatch_timeout_s = _positive(dispatch_timeout_s, "dispatch_timeout_s")
        return cls(_HttpTransport(base_urls, timeout_s), timeout_s=timeout_s, dispatch_timeout_s=dispatch_timeout_s)

    @property
    def mode(self) -> str:
        """``in-process`` or ``network``."""
        return self._transport.mode

    @property
    def agents(self) -> list[str]:
        """The names of the agents this network can call, sorted."""
        return self._transport.agents

    def add_app(self, name: str, app: Any) -> None:
        """Add an agent app to an in-process network. Raises ``TypeError`` for an HTTP network."""
        self._transport.add(name, app)

    def add_address(self, name: str, url: str) -> None:
        """Add an agent served over HTTP to this network while it runs.

        The address is checked as :meth:`over_http` checks one. The same name
        with the same address again does nothing; the same name with another
        address raises ``ValueError``. Raises ``TypeError`` for an in-process
        network and ``RuntimeError`` for a closed one. No message repeats the
        address.
        """
        self._transport.add_address(name, url)

    def remove_address(self, name: str) -> None:
        """Remove an agent's address and close its client. An unknown name does nothing.

        A later call to that agent raises :class:`AgentUnavailableError`.
        Raises ``TypeError`` for an in-process network.
        """
        self._transport.remove_address(name)

    def has_lma(self, site_id: str) -> bool:
        return lma_agent(site_id) in self._transport.agents

    def lma(self, site_id: str, token: str | None = None) -> LmaClient:
        """A client for the LMA of ``site_id``, sending ``token`` as a bearer token when given."""
        return LmaClient(self, lma_agent(site_id), token)

    def gma(self, token: str | None = None) -> GmaClient:
        """A client for the GMA, sending ``token`` as a bearer token when given."""
        return GmaClient(self, GMA_AGENT, token)

    def close(self) -> None:
        """Stop the event-loop thread (in process) or close the HTTP clients. A second call does nothing."""
        self._transport.close()

    def __enter__(self) -> AgentNetwork:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _send(
        self,
        name: str,
        method: str,
        path: str,
        params: Mapping[str, Any] | None,
        body: Any,
        headers: Mapping[str, str],
        timeout_s: float,
    ) -> _Reply:
        return self._transport.send(name, method, path, params, body, headers, timeout_s)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

_PATH_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _fill(template: str, values: Mapping[str, Any] | None) -> str:
    if not template.startswith("/"):
        raise ValueError("a route template must start with '/'")
    values = values or {}
    return _PATH_FIELD.sub(lambda match: quote(str(values[match.group(1)]), safe=""), template)


class AgentClient:
    """Calls one agent. :class:`LmaClient` and :class:`GmaClient` add a typed method per route."""

    def __init__(self, network: AgentNetwork, name: str, token: str | None) -> None:
        self.service, self.site_id = _parse_agent_name(name)
        self._network = network
        self._name = name
        self._token = token

    def __repr__(self) -> str:
        site = f", site_id={self.site_id!r}" if self.site_id else ""
        return f"{type(self).__name__}(service={self.service!r}{site})"

    def request(
        self,
        method: str,
        template: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> AgentResponse:
        """Call a route and return its answer, or raise :class:`AgentError`.

        ``template`` is the route as the agent declares it, such as
        ``/api/v1/federation/rounds/{round_id}``. Each ``{name}`` is replaced
        by ``path_params[name]``, percent-encoded. The call log records the
        template. ``json`` is the request body; ``params`` is the query.
        """
        method = method.upper()
        path = _fill(template, path_params)
        sent_headers = {"Accept": "application/json"}
        if self._token:
            sent_headers["Authorization"] = f"Bearer {self._token}"
        sent_headers.update(headers or {})
        timeout = timeout_s if timeout_s is not None else self._network.timeout_s
        status: int | None = None
        started = time.perf_counter()
        try:
            reply = self._network._send(self._name, method, path, params, json, sent_headers, timeout)
            status = reply.status
        except _UnknownAgent:
            raise self._unavailable(template, None, f"no address is configured for {_label(self.service, self.site_id)}") from None
        except _NoAnswer as error:
            detail = f"no answer within {timeout:g} s" if error.timed_out else "the connection failed"
            raise self._unavailable(template, None, detail) from None
        except _NetworkClosed:
            raise self._unavailable(template, None, "the agent network is closed") from None
        finally:
            log = CallLog.current()
            if log is not None:
                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                log.record(AgentCall(self.service, self.site_id, method, template, status, elapsed_ms))
        return self._read(reply, template)

    def _unavailable(self, template: str, status: int | None, detail: str) -> AgentUnavailableError:
        return AgentUnavailableError(
            service=self.service, site_id=self.site_id, path=template, status=status, code="agent_unavailable", detail=detail
        )

    def _read(self, reply: _Reply, template: str) -> AgentResponse:
        body = _decode(reply.content)
        if 200 <= reply.status < 300 or reply.status == 304:
            if body is _NOT_JSON:
                raise AgentError(
                    service=self.service,
                    site_id=self.site_id,
                    path=template,
                    status=reply.status,
                    code="invalid_response",
                    detail="the answer is not JSON",
                )
            return AgentResponse(reply.status, reply.headers, body)
        code: str | None = None
        detail: str | None = None
        if isinstance(body, dict):
            if isinstance(body.get("code"), str) and body["code"]:
                code = body["code"]
            raw_detail = body.get("detail")
            if isinstance(raw_detail, str):
                detail = raw_detail
            elif isinstance(raw_detail, list):
                detail = _validation_summary(raw_detail)
        if code is None and reply.status in (502, 503, 504):
            # A gateway or a starting server, not the agent's own answer: its
            # body is not forwarded, because it can name hosts.
            raise self._unavailable(template, reply.status, f"answered {reply.status} without an error code")
        raise AgentError(
            service=self.service,
            site_id=self.site_id,
            path=template,
            status=reply.status,
            code=code or _STATUS_CODES.get(reply.status, f"http_{reply.status}"),
            detail=detail if detail is not None else _status_phrase(reply.status),
        )

    def _parse(self, response: AgentResponse, shape: _Shape[T], template: str) -> T:
        try:
            return shape.adapter.validate_python(response.body)
        except ValidationError:
            raise AgentError(
                service=self.service,
                site_id=self.site_id,
                path=template,
                status=response.status,
                code="invalid_response",
                detail=f"the answer does not match {shape.name}",
            ) from None

    def _call(
        self,
        method: str,
        template: str,
        shape: _Shape[T],
        *,
        path_params: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        timeout_s: float | None = None,
    ) -> T:
        response = self.request(method, template, path_params=path_params, params=params, json=json, timeout_s=timeout_s)
        return self._parse(response, shape, template)

    def health(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """``GET /api/v1/health``. ``timeout_s`` replaces the network's timeout for this call."""
        return self._call("GET", "/api/v1/health", _DICT, timeout_s=timeout_s)


def _body(model: Any) -> Any:
    return model.model_dump(mode="json")


class _Shape(Generic[T]):
    """A type the client checks an answer against, with a name for error messages."""

    def __init__(self, tp: Any, name: str) -> None:
        self.adapter: TypeAdapter[T] = TypeAdapter(tp)
        self.name = name


_DICT: _Shape[dict[str, Any]] = _Shape(dict[str, Any], "a JSON object")
_SITE_INFO: _Shape[SiteInfo] = _Shape(SiteInfo, "SiteInfo")
_SITE_SUMMARY: _Shape[SiteSummary] = _Shape(SiteSummary, "SiteSummary")
_SOURCE_STATE: _Shape[SourceState] = _Shape(SourceState, "SourceState")
_MODEL_IN_USE: _Shape[ModelInUse] = _Shape(ModelInUse, "ModelInUse")
_LOCAL_EVALUATION: _Shape[LocalEvaluation] = _Shape(LocalEvaluation, "LocalEvaluation")
_PROBE_RESPONSE: _Shape[ProbeResponse] = _Shape(ProbeResponse, "ProbeResponse")
_AUDIT_SUMMARY: _Shape[AuditSummary] = _Shape(AuditSummary, "AuditSummary")
_REGISTRATION: _Shape[SiteRegistration] = _Shape(SiteRegistration, "SiteRegistration")
_REGISTRATIONS: _Shape[list[SiteRegistration]] = _Shape(list[SiteRegistration], "a list of SiteRegistration")
_ACCEPTANCE: _Shape[JobAcceptance] = _Shape(JobAcceptance, "JobAcceptance")
_SITE_RESULT: _Shape[SiteResult] = _Shape(SiteResult, "SiteResult")
_JOB_RUN: _Shape[JobRun] = _Shape(JobRun, "JobRun")
_JOB_LOG: _Shape[list[JobLogEntry]] = _Shape(list[JobLogEntry], "a list of JobLogEntry")
_DIRECTORY: _Shape[list[SiteDirectoryEntry]] = _Shape(list[SiteDirectoryEntry], "a list of SiteDirectoryEntry")
_ROUND_PLAN: _Shape[RoundPlan] = _Shape(RoundPlan, "RoundPlan")
_ROUND_RECORD: _Shape[RoundRecord] = _Shape(RoundRecord, "RoundRecord")
_ROUND_RECORDS: _Shape[list[RoundRecord]] = _Shape(list[RoundRecord], "a list of RoundRecord")
_READINESS: _Shape[AggregationReadiness] = _Shape(AggregationReadiness, "AggregationReadiness")
_CANDIDATE: _Shape[CandidateModel] = _Shape(CandidateModel, "CandidateModel")
_RECEIPT: _Shape[UpdateReceipt] = _Shape(UpdateReceipt, "UpdateReceipt")
_RELEASE_EVALUATION: _Shape[ReleaseEvaluation] = _Shape(ReleaseEvaluation, "ReleaseEvaluation")
_MODEL_VERSION: _Shape[ModelVersion] = _Shape(ModelVersion, "ModelVersion")
_MODEL_VERSIONS: _Shape[ModelVersionList] = _Shape(ModelVersionList, "ModelVersionList")
_APPROVAL: _Shape[ApprovalResult] = _Shape(ApprovalResult, "ApprovalResult")
_ADDRESS_STATE: _Shape[SiteAddressState] = _Shape(SiteAddressState, "SiteAddressState")
_ADDRESS_LIST: _Shape[SiteAddressList] = _Shape(SiteAddressList, "SiteAddressList")


class LmaClient(AgentClient):
    """The routes of one site's LMA (plan section 6.8).

    The site routes take the LMA's ``site`` token (some also its
    ``coordinator`` token); the registration and job routes take its
    ``coordinator`` token.
    """

    def site_info(self, *, timeout_s: float | None = None) -> SiteInfo:
        """``GET /api/v1/site``: who the site is and what its LMA serves."""
        return self._call("GET", "/api/v1/site", _SITE_INFO, timeout_s=timeout_s)

    def site_summary(self, *, timeout_s: float | None = None) -> SiteSummary:
        """``GET /api/v1/site/summary``: counts and the label rate the site's planner permits."""
        return self._call("GET", "/api/v1/site/summary", _SITE_SUMMARY, timeout_s=timeout_s)

    def site_profile(self) -> dict[str, Any]:
        """``GET /api/v1/site/profile``: the site's running profile, as ``RunningProfile.snapshot()``."""
        return self._call("GET", "/api/v1/site/profile", _DICT)

    def refresh_source(self) -> SourceState:
        """``POST /api/v1/site/source:refresh``: read the source's count and version again, now."""
        return self._call("POST", "/api/v1/site/source:refresh", _SOURCE_STATE)

    def model_in_use(self) -> ModelInUse:
        """``GET /api/v1/site/model``: the released model, as the LMA reads it from the GMA."""
        return self._call("GET", "/api/v1/site/model", _MODEL_IN_USE)

    def evaluate_model(self) -> LocalEvaluation:
        """``POST /api/v1/site/model:evaluate``: the released model measured on the site's labelled records."""
        return self._call("POST", "/api/v1/site/model:evaluate", _LOCAL_EVALUATION)

    def probe(self, request: ProbeRequest) -> ProbeResponse:
        """``POST /api/v1/governance/restrictions:probe``: what the site's Guard would release. Never rows."""
        return self._call("POST", "/api/v1/governance/restrictions:probe", _PROBE_RESPONSE, json=_body(request))

    def audit_summary(self, *, timeout_s: float | None = None) -> AuditSummary:
        """``GET /api/v1/governance/audit:summary``: the audit chain's state and length."""
        return self._call("GET", "/api/v1/governance/audit:summary", _AUDIT_SUMMARY, timeout_s=timeout_s)

    def registration(self) -> SiteRegistration:
        """``GET /api/v1/federation/registration``: the site's enrolment, with its current cohort size."""
        return self._call("GET", "/api/v1/federation/registration", _REGISTRATION)

    def offer_job(self, job: FederatedJob) -> JobAcceptance:
        """``POST /api/v1/federation/jobs:offer``: the site checks the job before it reads any data."""
        return self._call("POST", "/api/v1/federation/jobs:offer", _ACCEPTANCE, json=_body(job))

    def execute_job(self, job: FederatedJob) -> SiteResult:
        """``POST /api/v1/federation/jobs:execute``: run an accepted job. 409 for a job the site did not accept."""
        return self._call(
            "POST",
            "/api/v1/federation/jobs:execute",
            _SITE_RESULT,
            json=_body(job),
            timeout_s=self._network.dispatch_timeout_s,
        )

    def run_job(self, job: FederatedJob) -> JobRun:
        """``POST /api/v1/federation/jobs:run``: offer the job and, when the site accepts it, run it, in one call."""
        return self._call(
            "POST",
            "/api/v1/federation/jobs:run",
            _JOB_RUN,
            json=_body(job),
            timeout_s=self._network.dispatch_timeout_s,
        )

    def job_log(self, limit: int = 20) -> list[JobLogEntry]:
        """``GET /api/v1/federation/jobs?limit=N``: the site's job log, newest first."""
        return self._call("GET", "/api/v1/federation/jobs", _JOB_LOG, params={"limit": limit})


class GmaClient(AgentClient):
    """The routes of the GMA (plan section 6.9).

    Most routes take the GMA's ``admin`` token. The update upload takes a
    ``site`` token bound to the uploading site, and the released model takes
    either.
    """

    # Sites

    def refresh_sites(self) -> list[SiteRegistration]:
        """``POST /api/v1/federation/sites:refresh``: enrol each configured LMA from its own registration."""
        return self._call(
            "POST", "/api/v1/federation/sites:refresh", _REGISTRATIONS, timeout_s=self._network.dispatch_timeout_s
        )

    def site_summaries(self, include_audit: bool = False, *, timeout_s: float | None = None) -> list[SiteDirectoryEntry]:
        """``GET /api/v1/federation/sites/summaries``: what each site reports, with its audit state when asked.

        The GMA calls every LMA, so the call waits up to the network's
        dispatch timeout unless ``timeout_s`` is given.
        """
        params = {"include": "audit"} if include_audit else None
        return self._call(
            "GET",
            "/api/v1/federation/sites/summaries",
            _DIRECTORY,
            params=params,
            timeout_s=timeout_s if timeout_s is not None else self._network.dispatch_timeout_s,
        )

    def list_sites(self) -> list[SiteRegistration]:
        """``GET /api/v1/federation/sites``: every enrolment, sorted by site id."""
        return self._call("GET", "/api/v1/federation/sites", _REGISTRATIONS)

    def register_site(self, registration: SiteRegistration) -> SiteRegistration:
        """``PUT /api/v1/federation/sites/{site_id}``: enrol or update one site."""
        return self._call(
            "PUT",
            "/api/v1/federation/sites/{site_id}",
            _REGISTRATION,
            path_params={"site_id": registration.site_id},
            json=_body(registration),
        )

    def register_site_address(self, site_id: str, url: str) -> SiteAddressState:
        """``PUT /api/v1/federation/sites/{site_id}/address``: give the GMA a joinable site's LMA address.

        The GMA accepts only the address listed for the site in its
        ``GMA_JOINABLE_SITE_ENDPOINTS`` (403 ``not_joinable`` or
        ``address_differs``; 409 ``configured_elsewhere``). It does not enrol
        the site: :meth:`refresh_sites` does.
        """
        return self._call(
            "PUT",
            "/api/v1/federation/sites/{site_id}/address",
            _ADDRESS_STATE,
            path_params={"site_id": site_id},
            json=_body(SiteAddressRequest(url=url)),
        )

    def remove_site_address(self, site_id: str) -> None:
        """``DELETE /api/v1/federation/sites/{site_id}/address``: forget a joined site's address."""
        self.request("DELETE", "/api/v1/federation/sites/{site_id}/address", path_params={"site_id": site_id})

    def site_addresses(self) -> SiteAddressList:
        """``GET /api/v1/federation/site-addresses``: the joinable sites and where each held address came from."""
        return self._call("GET", "/api/v1/federation/site-addresses", _ADDRESS_LIST)

    # Rounds

    def plan_round(self, manifest: RoundManifest) -> RoundPlan:
        """``POST /api/v1/federation/rounds:plan``: select the eligible sites, without dispatching."""
        return self._call("POST", "/api/v1/federation/rounds:plan", _ROUND_PLAN, json=_body(manifest))

    def dispatch_round(self, manifest: RoundManifest) -> RoundRecord:
        """``POST /api/v1/federation/rounds``: plan the round and dispatch its jobs. 409 for a reused round id."""
        return self._call(
            "POST",
            "/api/v1/federation/rounds",
            _ROUND_RECORD,
            json=_body(manifest),
            timeout_s=self._network.dispatch_timeout_s,
        )

    def list_rounds(self, limit: int = 20) -> list[RoundRecord]:
        """``GET /api/v1/federation/rounds?limit=N``: the stored rounds, newest first."""
        return self._call("GET", "/api/v1/federation/rounds", _ROUND_RECORDS, params={"limit": limit})

    def get_round(self, round_id: str) -> RoundRecord:
        """``GET /api/v1/federation/rounds/{round_id}``: one round's lineage. 404 for an unknown round."""
        return self._call("GET", "/api/v1/federation/rounds/{round_id}", _ROUND_RECORD, path_params={"round_id": round_id})

    def submit_result(self, round_id: str, result: SiteResult) -> RoundRecord:
        """``POST /api/v1/federation/rounds/{round_id}/results``: record one site's result."""
        return self._call(
            "POST",
            "/api/v1/federation/rounds/{round_id}/results",
            _ROUND_RECORD,
            path_params={"round_id": round_id},
            json=_body(result),
        )

    def readiness(self, round_id: str) -> AggregationReadiness:
        """``GET /api/v1/federation/rounds/{round_id}/readiness``: whether the results may be aggregated."""
        return self._call(
            "GET", "/api/v1/federation/rounds/{round_id}/readiness", _READINESS, path_params={"round_id": round_id}
        )

    def aggregate(self, round_id: str) -> CandidateModel:
        """``POST /api/v1/federation/rounds/{round_id}:aggregate``: make a candidate. 409 ``aggregation_refused``."""
        return self._call(
            "POST", "/api/v1/federation/rounds/{round_id}:aggregate", _CANDIDATE, path_params={"round_id": round_id}
        )

    def upload_update(self, round_id: str, site_id: str, upload: UpdateUpload) -> UpdateReceipt:
        """``PUT /api/v1/federation/rounds/{round_id}/updates/{site_id}``: store one site's clipped update."""
        return self._call(
            "PUT",
            "/api/v1/federation/rounds/{round_id}/updates/{site_id}",
            _RECEIPT,
            path_params={"round_id": round_id, "site_id": site_id},
            json=_body(upload),
        )

    def discard_round(self, round_id: str, reason: str) -> None:
        """``POST /api/v1/federation/rounds/{round_id}:discard``: delete the round's updates and record why."""
        self.request(
            "POST", "/api/v1/federation/rounds/{round_id}:discard", path_params={"round_id": round_id}, json={"reason": reason}
        )

    def evaluate_release(self, round_id: str, request: ReleaseEvaluationRequest) -> ReleaseEvaluation:
        """``POST /api/v1/federation/rounds/{round_id}:evaluate-release``: the release Guard, the gates and the lifecycle rule."""
        return self._call(
            "POST",
            "/api/v1/federation/rounds/{round_id}:evaluate-release",
            _RELEASE_EVALUATION,
            path_params={"round_id": round_id},
            json=_body(request),
        )

    # Governance

    def audit_summary(self) -> AuditSummary:
        """``GET /api/v1/governance/audit:summary``: the coordinator audit chain's state and length."""
        return self._call("GET", "/api/v1/governance/audit:summary", _AUDIT_SUMMARY)

    # Model versions

    def list_model_versions(self, limit: int = 20) -> ModelVersionList:
        """``GET /api/v1/model-versions?limit=N``: the model in use and the newest versions, without parameters."""
        return self._call("GET", "/api/v1/model-versions", _MODEL_VERSIONS, params={"limit": limit})

    def released_model(self, known_digest: str | None = None) -> ModelVersion | None:
        """``GET /api/v1/model-versions:released``: the released version, with its parameters.

        With ``known_digest``, it sends ``If-None-Match: "<digest>"`` and
        returns ``None`` when the GMA answers 304, because the released model
        still has that digest.
        """
        headers = {"If-None-Match": f'"{known_digest}"'} if known_digest else None
        response = self.request("GET", "/api/v1/model-versions:released", headers=headers)
        if response.status == 304:
            return None
        return self._parse(response, _MODEL_VERSION, "/api/v1/model-versions:released")

    def get_model_version(self, version: str) -> ModelVersion:
        """``GET /api/v1/model-versions/{version}``: one version, without parameters. 404 when unknown."""
        return self._call("GET", "/api/v1/model-versions/{version}", _MODEL_VERSION, path_params={"version": version})

    def register_model_version(self, version: str, registration: ModelVersionRegistration) -> ModelVersion:
        """``PUT /api/v1/model-versions/{version}``: register a version. Registering the same content again changes nothing."""
        return self._call(
            "PUT",
            "/api/v1/model-versions/{version}",
            _MODEL_VERSION,
            path_params={"version": version},
            json=_body(registration),
        )

    def approve(self, version: str) -> ApprovalResult:
        """``POST /api/v1/model-versions/{version}:approve``: release a candidate. 409 with the lifecycle refusal."""
        return self._call("POST", "/api/v1/model-versions/{version}:approve", _APPROVAL, path_params={"version": version})


__all__ = [
    "GMA_AGENT",
    "AgentCall",
    "AgentClient",
    "AgentError",
    "AgentNetwork",
    "AgentResponse",
    "AgentUnavailableError",
    "CallLog",
    "GmaClient",
    "LmaClient",
    "lma_agent",
    "normalize_base_url",
    "without_addresses",
]
