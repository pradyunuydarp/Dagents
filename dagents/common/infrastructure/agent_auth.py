"""Bearer tokens for the LMA and GMA routes.

An agent accepts a set of tokens, each granted one **scope**. A route names the
scopes that may call it with :func:`require_scope`. The two agents use these
scopes:

| Agent | Scope | Held by |
|---|---|---|
| LMA | `site` | the site's own staff and product views |
| LMA | `coordinator` | the GMA, for the registration and job routes |
| GMA | `admin` | the study operator |
| GMA | `site` | one LMA; the token names its site, so it can upload only that site's update |

A token bound to a site (``TokenGrant.site_id``) may call a route that names a
site in its path only for its own site.

With no token configured an agent runs open, as before tokens existed: every
caller may call every route, and :func:`install_agent_access` logs a warning.
An agent started with ``require_tokens=True`` (``LMA_REQUIRE_TOKENS=1``,
``GMA_REQUIRE_TOKENS=1``) refuses to start without tokens.

Tokens are compared with ``hmac.compare_digest``, against every grant, so the
time a check takes does not depend on which grant matched. No message, log
line or ``repr`` holds a token.
"""

from __future__ import annotations

import hmac
import logging
import re
from dataclasses import dataclass, field
from typing import Annotated, Awaitable, Callable, ClassVar, Iterable, Mapping

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from dagents.common.domain.errors import DagentsError

logger = logging.getLogger(__name__)

#: The fewest characters a token may have. ``secrets.token_urlsafe()`` gives 43.
MIN_TOKEN_LENGTH = 16

# RFC 6750 b64token: the characters a bearer token may hold.
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9\-._~+/]+=*")
_SCOPE_PATTERN = re.compile(r"[a-z][a-z0-9_-]*")

_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="AgentToken",
    description=(
        "A token the agent was started with. Each token is granted one scope. An agent started without "
        "tokens accepts every caller."
    ),
)


class AccessConfigurationError(ValueError):
    """The agent's token settings are not valid, or tokens are required and none is set.

    The message never holds a token.
    """


class AgentAccessError(DagentsError, PermissionError):
    """A caller may not call the route. The agent answers ``{detail, code}``."""


class AuthenticationRequired(AgentAccessError):
    """The request carries no bearer token, or one the agent does not accept."""

    code: ClassVar[str] = "unauthorized"
    http_status: ClassVar[int] = 401


class AccessDenied(AgentAccessError):
    """The token is valid, but its scope or site may not call this route."""

    code: ClassVar[str] = "forbidden"
    http_status: ClassVar[int] = 403


@dataclass(frozen=True)
class TokenGrant:
    """One token an agent accepts, and what it may do.

    ``site_id`` binds the token to one site: on a route that names a site in its
    path, the token may act only for that site.
    """

    scope: str
    token: str = field(repr=False)
    site_id: str | None = None


@dataclass(frozen=True)
class Caller:
    """Who called a route, as :func:`require_scope` found from the token.

    ``scope`` is ``None`` when the agent runs open (no tokens configured).
    """

    scope: str | None
    site_id: str | None = None

    @property
    def is_open(self) -> bool:
        """True when the agent runs without tokens, so the caller is not known."""
        return self.scope is None

    def may_act_for(self, site_id: str) -> bool:
        """Whether the caller may act for ``site_id``: true unless its token is bound to another site."""
        return self.site_id is None or self.site_id == site_id


class AgentAccess:
    """The tokens one agent accepts. ``AgentAccess.open()`` accepts every caller."""

    def __init__(self, grants: Iterable[TokenGrant] = ()) -> None:
        checked: list[tuple[TokenGrant, bytes]] = []
        seen: set[bytes] = set()
        for index, grant in enumerate(grants, start=1):
            where = f"token {index} (scope {grant.scope!r})" if grant.scope else f"token {index}"
            if not isinstance(grant.scope, str) or not _SCOPE_PATTERN.fullmatch(grant.scope):
                raise AccessConfigurationError(f"{where}: a scope must be lower-case letters, digits, '-' or '_'")
            if grant.site_id is not None and not grant.site_id.strip():
                raise AccessConfigurationError(f"{where}: a site id must not be empty")
            if not isinstance(grant.token, str) or len(grant.token) < MIN_TOKEN_LENGTH:
                raise AccessConfigurationError(f"{where}: a token must have at least {MIN_TOKEN_LENGTH} characters")
            if not _TOKEN_PATTERN.fullmatch(grant.token):
                raise AccessConfigurationError(
                    f"{where}: a token may hold only letters, digits and '-._~+/', with '=' at the end"
                )
            secret = grant.token.encode("ascii")
            if secret in seen:
                raise AccessConfigurationError(f"{where}: the same token is granted twice; give each grant its own")
            seen.add(secret)
            checked.append((grant, secret))
        self._grants = tuple(checked)

    @classmethod
    def open(cls) -> AgentAccess:
        """Access with no tokens: every caller may call every route."""
        return cls(())

    @property
    def is_open(self) -> bool:
        """True when no token is configured."""
        return not self._grants

    @property
    def scopes(self) -> frozenset[str]:
        """The scopes that have at least one token."""
        return frozenset(grant.scope for grant, _ in self._grants)

    def authenticate(self, token: str) -> TokenGrant | None:
        """Return the grant whose token equals ``token``, or ``None``.

        Every grant is compared, also after a match, so the time taken does not
        depend on which grant matched.
        """
        candidate = token.encode("utf-8")
        match: TokenGrant | None = None
        for grant, secret in self._grants:
            if hmac.compare_digest(candidate, secret) and match is None:
                match = grant
        return match

    def __repr__(self) -> str:
        if self.is_open:
            return "AgentAccess(open)"
        return f"AgentAccess(scopes={sorted(self.scopes)})"


def agent_error_response(
    status: int, code: str, detail: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """The JSON error body both agents answer with: ``{"detail": ..., "code": ...}``.

    The agent client reads ``code`` to raise a typed error. ``detail`` must
    never hold a connection string, a host or a credential.
    """
    return JSONResponse({"detail": detail, "code": code}, status_code=status, headers=dict(headers or {}))


async def _access_error_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, AgentAccessError)
    headers = {"WWW-Authenticate": "Bearer"} if error.http_status == 401 else None
    return agent_error_response(error.http_status, error.code, str(error), headers)


def install_agent_access(
    app: FastAPI,
    access: AgentAccess,
    *,
    agent_name: str,
    require_tokens: bool = False,
    expected_scopes: Iterable[str] = (),
    log: logging.Logger | None = None,
) -> None:
    """Give ``app`` its tokens, and answer refused calls with ``{detail, code}``.

    Call it once in the agent's app factory, before the app serves a request.
    :func:`require_scope` reads the access from ``app.state.agent_access``.

    - With no tokens, it logs one warning, or raises
      :class:`AccessConfigurationError` when ``require_tokens`` is true.
    - For each scope in ``expected_scopes`` that has no token, it logs a
      warning: that scope's routes refuse every caller.
    """
    log = log or logger
    if access.is_open:
        if require_tokens:
            raise AccessConfigurationError(f"{agent_name} requires tokens, but no token is configured")
        log.warning("%s runs without tokens: every caller may call every route", agent_name)
    else:
        for scope in sorted(set(expected_scopes) - access.scopes):
            log.warning("%s has no token for the %r scope, so the routes of that scope refuse every caller", agent_name, scope)
    app.state.agent_access = access
    app.add_exception_handler(AgentAccessError, _access_error_handler)


def require_scope(*scopes: str, bind_site: str | None = None) -> Callable[..., Awaitable[Caller]]:
    """A FastAPI dependency that lets in only a token of one of ``scopes``.

    Use it as ``Depends(require_scope("site", "coordinator"))``, as a route
    parameter (to read the :class:`Caller`) or in ``dependencies=[...]``.

    ``bind_site`` names the path parameter that holds a site id. A token bound
    to a site may then call the route only when that parameter is its site.

    Answers 401 (``unauthorized``) for a missing or unknown token and 403
    (``forbidden``) for a token whose scope or site may not call the route. An
    app that was not given its access with :func:`install_agent_access`
    answers 500, so a route never runs open by mistake.
    """
    if not scopes:
        raise ValueError("require_scope needs at least one scope")
    allowed = frozenset(scopes)

    async def dependency(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
    ) -> Caller:
        access = getattr(request.app.state, "agent_access", None)
        if not isinstance(access, AgentAccess):
            raise RuntimeError("install_agent_access was not called for this app, so its routes refuse every call")
        if access.is_open:
            return Caller(scope=None)
        if credentials is None:
            raise AuthenticationRequired("a bearer token is required")
        grant = access.authenticate(credentials.credentials)
        if grant is None:
            raise AuthenticationRequired("the token is not valid")
        if grant.scope not in allowed:
            raise AccessDenied("this token may not call this route")
        if bind_site is not None and grant.site_id is not None:
            if request.path_params.get(bind_site) != grant.site_id:
                raise AccessDenied("this token may act only for its own site")
        return Caller(scope=grant.scope, site_id=grant.site_id)

    dependency.__name__ = f"require_scope_{'_'.join(sorted(allowed))}"
    return dependency


def parse_site_map(raw: str | None, *, setting: str) -> dict[str, str]:
    """Read ``SITE=VALUE,SITE=VALUE`` from a setting, such as tokens or addresses by site.

    Blank or ``None`` gives an empty map. Only the first ``=`` of an entry
    separates the site from its value. Messages name the setting and the
    entry's position, never a value, because values may be tokens.
    """
    if raw is None or not raw.strip():
        return {}
    result: dict[str, str] = {}
    for index, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        site_id, separator, value = entry.partition("=")
        site_id, value = site_id.strip(), value.strip()
        if not separator or not site_id or not value:
            raise AccessConfigurationError(f"{setting}: entry {index} must look like SITE=VALUE")
        if site_id in result:
            raise AccessConfigurationError(f"{setting}: site {site_id!r} is named twice")
        result[site_id] = value
    return result


__all__ = [
    "MIN_TOKEN_LENGTH",
    "AccessConfigurationError",
    "AccessDenied",
    "AgentAccess",
    "AgentAccessError",
    "AuthenticationRequired",
    "Caller",
    "TokenGrant",
    "agent_error_response",
    "install_agent_access",
    "parse_site_map",
    "require_scope",
]
