"""Build a GMA app from its settings and, optionally, objects an application hands it.

``create_app(settings, deps)`` returns a complete FastAPI app. Everything is
built when the app is created, not in a lifespan handler, because an app run
in process behind ``httpx.ASGITransport`` receives no lifespan events. Several
GMAs can be built in one process; none of them uses a module-level singleton.

What the app holds (``app.state.gma``, a :class:`GmaState`):

| Part | Built from |
|---|---|
| Extension registry | ``deps.registry``; otherwise a new registry with ``GMA_EXTENSIONS``; otherwise the default registry |
| Governance | A Guard over ``deps.audit``, the Postgres audit chain in ``GMA_AUDIT_TABLE``, or memory. Strict about classifications when a configured registry has condition packs |
| Model registry | ``deps.model_store``, the Postgres table in ``GMA_MODEL_TABLE``, or memory |
| Round records, site enrolments, update store | ``deps.rounds``, ``deps.enrolments`` and ``deps.updates``; otherwise in memory. The update store is write-only |
| Agent network | ``deps.network``, or one built over HTTP from ``GMA_SITE_ENDPOINTS`` (empty when only ``GMA_JOINABLE_SITE_ENDPOINTS`` is set) |
| Round engine | The HTTP engine (``GMA_ENGINE=http``) or the in-process simulator |
| Site directory, release service | The parts above |
| Site addresses | ``GMA_SITE_ENDPOINTS`` and ``GMA_JOINABLE_SITE_ENDPOINTS``; a joined address goes into the agent network |
| Tokens | ``GMA_ADMIN_TOKEN`` (scope ``admin``) and ``GMA_SITE_TOKENS`` (scope ``site``, each bound to its site) |

Errors are answered as ``{"detail": ..., "code": ...}``: a framework error
with its own code and status, a planner that cannot be run with 503
``planner_unavailable``, a coordinator store that refuses its role with 503
``store_not_set_up``. No answer holds a connection string or a token.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import contextvars
from dataclasses import dataclass, field
import functools
import logging
import math
import threading
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, TypeVar

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from dagents.common.application.ethical_guard import AuditSink
from dagents.common.application.federation import (
    FederatedRoundController,
    FederationEngine,
    InProcessFederationEngine,
    RoundRepository,
    SiteRegistry,
)
from dagents.common.application.governance_service import GovernanceService
from dagents.common.domain.errors import DagentsError
from dagents.common.extensions import ExtensionRegistry, default_registry
from dagents.common.extensions.registry import load_extensions
from dagents.common.infrastructure.agent_auth import (
    AccessConfigurationError,
    AgentAccess,
    TokenGrant,
    agent_error_response,
    install_agent_access,
)
from dagents.common.infrastructure.agent_client import (
    AgentError,
    AgentNetwork,
    AgentUnavailableError,
    lma_agent,
    normalize_base_url,
)
from dagents.common.infrastructure.dagents_runner import PlannerUnavailableError, warn_if_no_planner
from dagents.common.infrastructure.postgres_roles import RoleScopeError
from dagents.gma.adapters.http_federation_engine import HttpFederationEngine
from dagents.gma.application.aggregation_service import AggregationService
from dagents.gma.application.model_registry import ModelRegistry, ModelStore
from dagents.gma.application.release_service import ReleaseService
from dagents.gma.application.site_addresses import SiteAddressBook
from dagents.gma.application.site_directory import SiteDirectory
from dagents.gma.config import Settings
from dagents.gma.di import build_aggregation_service, build_governance_service, build_round_controller
from dagents.gma.infrastructure.postgres_models import InMemoryModelStore
from dagents.gma.infrastructure.update_store import InMemoryUpdateStore, UpdateStore

if TYPE_CHECKING:  # pragma: no cover - for annotations only
    from dagents.common.infrastructure.postgres_lease import KeptConnection

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: The name used in the GMA's log lines and token warnings.
AGENT_NAME = "GMA"

#: How many update uploads the GMA works on at once, on its own threads.
UPLOAD_THREADS = 8


class HandlerThreads:
    """A small thread pool of the GMA's own, for short handlers that must not wait for the server's thread tokens.

    A training dispatch holds one of the server's thread tokens while the LMAs
    run their jobs, and each LMA uploads its update to the GMA in that time. If
    the upload took a server token too, enough dispatches at once would hold
    every token, and each upload would wait until its LMA gave up. So the
    upload route is async and runs its short, blocking work (a model store
    read and the model kind's check) here. The pool starts its threads on first
    use, and :meth:`close` stops them.
    """

    def __init__(self, workers: int = UPLOAD_THREADS, name: str = "gma-upload") -> None:
        self._workers = workers
        self._name = name
        self._lock = threading.Lock()
        self._pool: ThreadPoolExecutor | None = None

    async def run(self, work: Callable[[], T]) -> T:
        """Run ``work`` on one of the pool's threads, with the caller's context, and return its result."""
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix=self._name)
            pool = self._pool
        context = contextvars.copy_context()
        return await asyncio.get_running_loop().run_in_executor(pool, functools.partial(context.run, work))

    def close(self) -> None:
        """Stop the threads. A later :meth:`run` starts new ones."""
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


@dataclass
class GmaDeps:
    """Objects an application hands its GMA instead of building them from settings.

    - `registry`: the extension registry; condition packs and model kinds come from it.
    - `network`: the agent network the GMA calls the LMAs through. The caller
      closes it.
    - `model_store`: where model versions are kept.
    - `coordinator_lease`: a kept Postgres connection for the coordinator's
      tables. The caller closes it.
    - `audit`: the release Guard's audit sink.
    - `rounds`: where round records are kept (a ``RoundRepository``).
    - `enrolments`: where site enrolments are kept (a ``SiteRegistry``).
    - `updates`: where the clipped updates of running training rounds are
      kept until aggregation or a discard (an ``UpdateStore``).

    The GMA builds what is left out: the three round stores in memory, new
    for each app.
    """

    registry: ExtensionRegistry | None = None
    network: AgentNetwork | None = None
    model_store: ModelStore | None = None
    coordinator_lease: KeptConnection | None = None
    audit: AuditSink | None = None
    rounds: RoundRepository | None = None
    enrolments: SiteRegistry | None = None
    updates: UpdateStore | None = None


@dataclass
class GmaState:
    """What one GMA app runs on. Routes read it from ``app.state.gma``."""

    settings: Settings
    extensions: ExtensionRegistry
    service: AggregationService
    governance: GovernanceService
    rounds: FederatedRoundController
    models: ModelRegistry
    updates: UpdateStore
    directory: SiteDirectory
    releases: ReleaseService
    engine: FederationEngine
    network: AgentNetwork | None
    access: AgentAccess
    addresses: SiteAddressBook
    upload_threads: HandlerThreads = field(default_factory=HandlerThreads)
    _closers: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def close(self) -> None:
        """Close what the app built itself: its HTTP agent network, its Postgres lease and its upload threads.

        A second call does nothing more.
        """
        self.upload_threads.close()
        closers, self._closers = self._closers, []
        for close in reversed(closers):
            try:
                close()
            except Exception:  # noqa: BLE001 - closing must not hide the caller's own error
                logger.warning("the GMA could not close one of its resources", exc_info=True)


def _check(settings: Settings, deps: GmaDeps) -> None:
    """Refuse settings that cannot work, before anything is built."""
    tables = [
        name
        for name, value in (("GMA_MODEL_TABLE", settings.model_table), ("GMA_AUDIT_TABLE", settings.audit_table))
        if value
    ]
    if settings.audit_anchor_table and not settings.audit_table:
        raise ValueError("GMA_AUDIT_ANCHOR_TABLE needs GMA_AUDIT_TABLE")
    needs_database = (bool(settings.model_table) and deps.model_store is None) or (
        bool(settings.audit_table) and deps.audit is None
    )
    if needs_database:
        if not settings.database_url and deps.coordinator_lease is None:
            raise ValueError(f"{' and '.join(tables)} need GMA_DATABASE_URL")
        if not settings.database_role:
            raise ValueError("GMA_DATABASE_URL needs GMA_DATABASE_ROLE, the role every coordinator query runs as")
    joinable = settings.joinable_site_endpoints
    if settings.engine == "http" and deps.network is None and not settings.site_endpoints and not joinable:
        raise ValueError(
            "GMA_ENGINE=http needs GMA_SITE_ENDPOINTS or GMA_JOINABLE_SITE_ENDPOINTS, or an agent network handed "
            "to create_app"
        )
    if not joinable:
        return
    both = sorted(set(joinable) & set(settings.site_endpoints))
    if both:
        raise ValueError(
            f"a site cannot be in both GMA_SITE_ENDPOINTS and GMA_JOINABLE_SITE_ENDPOINTS: {', '.join(both)}"
        )
    for site_id, url in sorted(joinable.items()):
        try:
            normalize_base_url(lma_agent(site_id), url)
        except ValueError as error:
            raise ValueError(f"GMA_JOINABLE_SITE_ENDPOINTS: {error}") from None
    if settings.engine != "http":
        raise ValueError("GMA_JOINABLE_SITE_ENDPOINTS needs GMA_ENGINE=http: the simulator calls no LMA")
    if deps.network is not None and deps.network.mode != "network":
        raise ValueError(
            "GMA_JOINABLE_SITE_ENDPOINTS needs an agent network over HTTP; an in-process network takes no address"
        )
    has_tokens = settings.require_tokens or bool(settings.admin_token or settings.site_tokens or settings.lma_tokens)
    if has_tokens:
        for setting, tokens in (("GMA_LMA_TOKENS", settings.lma_tokens), ("GMA_SITE_TOKENS", settings.site_tokens)):
            missing = sorted(set(joinable) - set(tokens))
            if missing:
                raise ValueError(
                    f"GMA_JOINABLE_SITE_ENDPOINTS: {setting} has no entry for {', '.join(missing)}; the GMA could "
                    "not call that LMA or accept its update"
                )


def _access(settings: Settings) -> AgentAccess:
    grants = []
    if settings.admin_token:
        grants.append(TokenGrant("admin", settings.admin_token))
    grants.extend(TokenGrant("site", token, site_id=site_id) for site_id, token in sorted(settings.site_tokens.items()))
    access = AgentAccess(grants)
    if settings.require_tokens and access.is_open:
        raise AccessConfigurationError(f"{AGENT_NAME} requires tokens, but no token is configured")
    return access


def _extensions(settings: Settings, deps: GmaDeps) -> tuple[ExtensionRegistry, bool]:
    """The registry to use, and whether it was configured for this GMA."""
    if deps.registry is not None:
        return deps.registry, True
    if settings.extensions:
        registry = ExtensionRegistry()
        load_extensions(settings.extensions, registry)
        return registry, True
    return default_registry, False


def create_app(settings: Settings | None = None, deps: GmaDeps | None = None) -> FastAPI:
    """Build a GMA app. ``create_app(Settings())`` is a GMA with nothing configured.

    Raises ``ValueError`` for settings that cannot work: an HTTP engine with no
    LMA to call, tables without a database or a role, required tokens that are
    not set, a token that is too short, or a joinable site that is also
    configured, has a bad address, lacks its tokens, or has no HTTP network to
    join.
    """
    settings = settings or Settings()
    deps = deps or GmaDeps()
    _check(settings, deps)
    access = _access(settings)
    extensions, configured = _extensions(settings, deps)
    closers: list[Callable[[], None]] = []
    try:
        lease = deps.coordinator_lease
        needs_lease = (settings.model_table and deps.model_store is None) or (settings.audit_table and deps.audit is None)
        if lease is None and needs_lease:
            from dagents.common.infrastructure.postgres_lease import KeptConnection

            lease = KeptConnection(str(settings.database_url), name="coordinator")
            closers.append(lease.close)

        audit = deps.audit
        if audit is None and settings.audit_table:
            from dagents.common.infrastructure.postgres_audit import PostgresAuditSink

            audit = PostgresAuditSink(
                lease.lease,
                str(settings.database_role),
                settings.audit_table,
                settings=lease.settings,
                anchor_table=settings.audit_anchor_table,
            )
        model_store = deps.model_store
        if model_store is None and settings.model_table:
            from dagents.gma.infrastructure.postgres_models import PostgresModelStore

            model_store = PostgresModelStore(lease, str(settings.database_role), settings.model_table)
        model_store = model_store or InMemoryModelStore()

        strict = configured and bool(extensions.list_condition_packs())
        governance = build_governance_service(registry=extensions, audit=audit, strict_classifications=strict)
        models = ModelRegistry(model_store, extensions)
        updates = deps.updates if deps.updates is not None else InMemoryUpdateStore()

        network = deps.network
        if network is None and (settings.site_endpoints or settings.joinable_site_endpoints):
            network = AgentNetwork.over_http(
                {lma_agent(site_id): url for site_id, url in settings.site_endpoints.items()}
            )
            closers.append(network.close)

        engine: FederationEngine
        if settings.engine == "http":
            assert network is not None  # checked by _check
            engine = HttpFederationEngine(
                network,
                models=models,
                updates=updates,
                lma_tokens=settings.lma_tokens,
                concurrency=settings.dispatch_concurrency,
                round_ids_survive_restart=getattr(deps.rounds, "survives_restart", False) is True,
            )
        else:
            engine = InProcessFederationEngine()
        rounds = build_round_controller(engine, sites=deps.enrolments, rounds=deps.rounds)
        service = build_aggregation_service()
        directory = SiteDirectory(
            network,
            rounds,
            lma_tokens=settings.lma_tokens,
            concurrency=settings.dispatch_concurrency,
            ttl_seconds=settings.summary_ttl_seconds,
            timeout_s=settings.summary_timeout_seconds,
        )
        releases = ReleaseService(rounds, models, governance, extensions)
        addresses = SiteAddressBook(
            network,
            configured=settings.site_endpoints,
            joinable=settings.joinable_site_endpoints,
            on_change=directory.forget,
        )
    except Exception:
        for close in reversed(closers):
            close()
        raise

    state = GmaState(
        settings=settings,
        extensions=extensions,
        service=service,
        governance=governance,
        rounds=rounds,
        models=models,
        updates=updates,
        directory=directory,
        releases=releases,
        engine=engine,
        network=network,
        access=access,
        addresses=addresses,
        _closers=closers,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            state.close()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.gma = state
    install_agent_access(
        app,
        access,
        agent_name=AGENT_NAME,
        require_tokens=settings.require_tokens,
        expected_scopes=("admin", "site") if settings.engine == "http" else ("admin",),
        log=logger,
    )
    _install_error_handlers(app)

    from dagents.gma.routes import router

    app.include_router(router)
    warn_if_no_planner(logger, AGENT_NAME)
    return app


def _non_finite(value: Any) -> bool:
    return isinstance(value, float) and not math.isfinite(value)


def _finite_only(value: Any) -> Any:
    """``value`` with every NaN or infinite number left out: a dict drops the key, a list the item."""
    if isinstance(value, dict):
        return {key: _finite_only(item) for key, item in value.items() if not _non_finite(item)}
    if isinstance(value, list):
        return [_finite_only(item) for item in value if not _non_finite(item)]
    return value


def _install_error_handlers(app: FastAPI) -> None:
    """Answer framework, planner, store and agent errors as ``{detail, code}``, and invalid requests with 422."""

    async def invalid_request(request: Request, error: Exception) -> JSONResponse:
        """FastAPI's own 422 answer, with any NaN or infinite input left out of it.

        Python reads ``NaN`` and ``Infinity`` in a JSON body, and FastAPI's
        answer repeats each refused input. A JSON answer cannot hold either
        number, so without this the request would get a 500 instead of 422.
        """
        assert isinstance(error, RequestValidationError)
        try:
            return await request_validation_exception_handler(request, error)
        except ValueError:
            return JSONResponse({"detail": _finite_only(jsonable_encoder(error.errors()))}, status_code=422)

    async def framework_error(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, DagentsError)
        body: dict[str, Any] = {"detail": str(error), "code": error.code}
        refusal = getattr(error, "refusal", None)
        if isinstance(refusal, str):
            body["refusal"] = refusal
        return JSONResponse(body, status_code=error.http_status)

    async def planner_error(request: Request, error: Exception) -> JSONResponse:
        logger.warning("the GMA could not use the planner for %s %s: %s", request.method, request.url.path, error)
        return agent_error_response(
            503, "planner_unavailable", "the planner could not be run, or its answer could not be used"
        )

    async def role_error(request: Request, error: Exception) -> JSONResponse:
        logger.warning("the coordinator store refused a role-scoped operation: %s", error)
        return agent_error_response(
            503, "store_not_set_up", "the coordinator store is not set up for its role; apply the migrations"
        )

    async def agent_error(request: Request, error: Exception) -> JSONResponse:
        assert isinstance(error, AgentError)
        if isinstance(error, AgentUnavailableError):
            return agent_error_response(503, "agent_unavailable", str(error))
        return agent_error_response(502, "agent_error", str(error))

    app.add_exception_handler(RequestValidationError, invalid_request)
    app.add_exception_handler(DagentsError, framework_error)
    app.add_exception_handler(PlannerUnavailableError, planner_error)
    app.add_exception_handler(RoleScopeError, role_error)
    app.add_exception_handler(AgentError, agent_error)


__all__ = ["AGENT_NAME", "GmaDeps", "GmaState", "HandlerThreads", "create_app"]
