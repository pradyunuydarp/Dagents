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
| Round records, site enrolments, update store | ``deps.rounds``, ``deps.enrolments`` and ``deps.updates``; otherwise the Postgres tables in ``GMA_ROUND_TABLE``, ``GMA_ENROLMENT_TABLE`` and ``GMA_UPDATE_TABLE`` (the last acting as ``GMA_UPDATE_ROLE``); otherwise in memory. The update store is write-only |
| Coordinator connection | ``deps.coordinator_lease``, or one kept connection to ``GMA_DATABASE_URL``. Every store built from a table setting uses it, and building the app opens no connection |
| Agent network | ``deps.network``, or one built over HTTP from ``GMA_SITE_ENDPOINTS`` (empty when only ``GMA_JOINABLE_SITE_ENDPOINTS`` is set) |
| Round engine | The HTTP engine (``GMA_ENGINE=http``) or the in-process simulator |
| Site directory, release service | The parts above |
| Site addresses | ``GMA_SITE_ENDPOINTS`` and ``GMA_JOINABLE_SITE_ENDPOINTS``; a joined address goes into the agent network |
| Tokens | ``GMA_ADMIN_TOKEN`` (scope ``admin``) and ``GMA_SITE_TOKENS`` (scope ``site``, each bound to its site) |

Recovery after a restart (:meth:`GmaState.ensure_recovered`) runs once: in
the lifespan when uvicorn starts the GMA, and otherwise on the first round
write. uvicorn serves only after the lifespan returns, so the lifespan waits
for the recovery at most :data:`START_RECOVERY_WAIT_SECONDS` and logs a
failure instead of raising it. The round write routes wait for the recovery
and answer 503 ``recovery_failed`` while it fails. Reads, ``/health`` and
approval do not wait for it once the GMA serves.

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
import time
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
    InMemoryRoundRepository,
    InMemorySiteRegistry,
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
from dagents.common.infrastructure.dagents_runner import (
    PlannerUnavailableError,
    check_planner_settings,
    warn_if_no_planner,
)
from dagents.common.infrastructure.postgres_roles import RoleScopeError
from dagents.gma.adapters.http_federation_engine import HttpFederationEngine
from dagents.gma.application.aggregation_service import AggregationService
from dagents.gma.application.model_registry import ModelRegistry, ModelStore
from dagents.gma.application.release_service import ReleaseService
from dagents.gma.application.site_addresses import SiteAddressBook
from dagents.gma.application.site_directory import SiteDirectory
from dagents.gma.config import Settings
from dagents.gma.di import build_aggregation_service, build_governance_service, build_round_controller
from dagents.gma.domain.errors import RecoveryFailed
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

#: The name each of the GMA's own handler threads starts with.
HANDLER_THREAD_NAME = "gma-handler"

#: How long, in seconds, the lifespan waits for the recovery after a restart
#: before uvicorn serves. A recovery normally takes well under a second. A
#: database that stops answering would otherwise keep ``/health`` down.
START_RECOVERY_WAIT_SECONDS = 10.0


class HandlerThreads:
    """A small thread pool of the GMA's own, for short handlers that must not wait for the server's thread tokens.

    A training dispatch holds one of the server's thread tokens while the LMAs
    run their jobs, and each LMA uploads its update to the GMA in that time. If
    the upload took a server token too, enough dispatches at once would hold
    every token, and each upload would wait until its LMA gave up. So the
    upload route is async and runs its short, blocking work (a model store
    read and the model kind's check) here. The recovery after a restart runs
    here too (:meth:`GmaState.ensure_recovered`), so a request that waits for
    it holds no server thread. The pool starts its threads on first use, and
    :meth:`close` stops them.
    """

    def __init__(self, workers: int = UPLOAD_THREADS, name: str = HANDLER_THREAD_NAME) -> None:
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

    A store handed here takes the place of the one its setting would build
    (``GMA_MODEL_TABLE``, ``GMA_AUDIT_TABLE``, ``GMA_ROUND_TABLE``,
    ``GMA_ENROLMENT_TABLE``, ``GMA_UPDATE_TABLE``), and that setting is not
    used. The GMA builds what is left out: from its table setting, on the one
    coordinator connection, or in memory, new for each app.
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
    #: The round controller.
    rounds: FederatedRoundController
    models: ModelRegistry
    updates: UpdateStore
    directory: SiteDirectory
    releases: ReleaseService
    engine: FederationEngine
    network: AgentNetwork | None
    access: AgentAccess
    addresses: SiteAddressBook
    #: Where the controller keeps round records.
    round_store: RoundRepository
    #: Where the controller keeps site enrolments.
    enrolments: SiteRegistry
    #: The kept connection the coordinator's Postgres stores share, or ``None``
    #: when no store was built from a table setting and none was handed in.
    coordinator_lease: KeptConnection | None = None
    #: The age limit for stored updates, in seconds, that the recovery and the
    #: check before each dispatch use; ``None`` runs no age check (see
    #: :func:`update_age_limit`).
    update_age_limit_seconds: float | None = None
    #: How long the lifespan waits for the recovery before uvicorn serves.
    start_recovery_wait_seconds: float = START_RECOVERY_WAIT_SECONDS
    #: The GMA's own handler threads: update uploads and the recovery after a restart.
    upload_threads: HandlerThreads = field(default_factory=HandlerThreads)
    _closers: list[Callable[[], None]] = field(default_factory=list, repr=False)
    _recovered: bool = field(default=False, init=False, repr=False)
    _recovery_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _start_recovery: asyncio.Future[None] | None = field(default=None, init=False, repr=False)

    @property
    def recovered(self) -> bool:
        """Whether this GMA has recovered the rounds a restart left unfinished."""
        return self._recovered

    @property
    def updates_survive_restart(self) -> bool:
        """Whether the update store keeps its updates after the process stops (its ``survives_restart``)."""
        return getattr(self.updates, "survives_restart", False) is True

    async def ensure_recovered(self) -> None:
        """Recover the rounds a restart left unfinished, once. The round write routes wait for it.

        After the first success this only reads a flag. Before that, it runs
        :meth:`FederatedRoundController.recover` on the GMA's handler threads
        (:attr:`upload_threads`) and takes its once-lock there, never on the
        event loop or a server thread, so a request that waits for it holds no
        server thread. Requests that arrive while it runs wait for the same
        recovery. The recovery is told whether the update store survives a
        restart and the age limit (:attr:`update_age_limit_seconds`).

        Raises :class:`RecoveryFailed` (503, ``recovery_failed``) when the
        recovery raised. The next call tries again.
        """
        if self._recovered:
            return
        try:
            await self.upload_threads.run(self._recover_once)
        except Exception as error:
            logger.warning(
                "the GMA could not recover the rounds a restart left unfinished; round writes answer 503 until a "
                "recovery succeeds: %s",
                _described(error),
            )
            raise RecoveryFailed(_cause(error)) from error

    async def recover_at_start(self) -> None:
        """Run :meth:`ensure_recovered` when the server starts, for at most :attr:`start_recovery_wait_seconds`.

        A lifespan that raises stops uvicorn, and with it ``/health``, so a
        failure is logged instead. After a failure the GMA serves reads and
        ``/health``, and each round write tries the recovery again.

        uvicorn serves only after the lifespan returns, so the wait is
        bounded. When the recovery takes longer, for example because the
        database stopped answering, it continues on its handler thread and
        the GMA serves. Round writes wait for that recovery, or try again if
        it fails.
        """
        task = asyncio.ensure_future(self.ensure_recovered())
        # A failure after the wait is logged by ensure_recovered; reading it here
        # stops asyncio from reporting an exception that was never retrieved.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        self._start_recovery = task
        try:
            await asyncio.wait_for(asyncio.shield(task), self.start_recovery_wait_seconds)
        except asyncio.TimeoutError:
            logger.error(
                "the GMA waited %s seconds and started before its recovery finished; round writes wait for that "
                "recovery, and reads and /health answer meanwhile",
                self.start_recovery_wait_seconds,
            )
        except Exception as error:  # noqa: BLE001 - logged, so the server still starts
            cause = error.cause if isinstance(error, RecoveryFailed) else _cause(error)
            logger.error(
                "the GMA started without recovering the rounds a restart left unfinished (%s); it serves reads and "
                "/health, and round writes answer 503 until a recovery succeeds",
                cause,
            )

    def stop_start_recovery(self) -> None:
        """Stop waiting for a recovery the lifespan started and that has not finished. The server is stopping."""
        task, self._start_recovery = self._start_recovery, None
        if task is not None and not task.done():
            task.cancel()

    def _recover_once(self) -> None:
        """Run the recovery unless it has run. Called on a handler thread."""
        with self._recovery_lock:
            if self._recovered:
                return
            report = self.rounds.recover(
                updates_kept=self.updates_survive_restart,
                now=time.time(),
                update_max_age_seconds=self.update_age_limit_seconds,
            )
            self._recovered = True
        if report.rounds or report.deleted_updates:
            logger.info(
                "recovery asked the planner about %d unfinished rounds and closed %d; it deleted the stored updates "
                "of %d rounds that were finished or not stored",
                len(report.rounds),
                sum(1 for item in report.rounds if item.verdict == "close"),
                len(report.deleted_updates),
            )

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


def _cause(error: BaseException) -> str:
    """The code an agent would answer ``error`` with: what a :class:`RecoveryFailed` names as its cause."""
    if isinstance(error, DagentsError):
        return error.code
    if isinstance(error, PlannerUnavailableError):
        return "planner_unavailable"
    if isinstance(error, RoleScopeError):
        return "store_not_set_up"
    return "unexpected_error"


def _described(error: BaseException) -> str:
    """``error`` for a log line: its message when its kind never holds a connection string, else its type."""
    if isinstance(error, (DagentsError, PlannerUnavailableError, RoleScopeError)):
        return f"{type(error).__name__}: {error}"
    return type(error).__name__


#: Each table setting, the ``Settings`` field that holds it, and the
#: ``GmaDeps`` field whose store takes its place.
_TABLE_SETTINGS = (
    ("GMA_MODEL_TABLE", "model_table", "model_store"),
    ("GMA_AUDIT_TABLE", "audit_table", "audit"),
    ("GMA_ROUND_TABLE", "round_table", "rounds"),
    ("GMA_ENROLMENT_TABLE", "enrolment_table", "enrolments"),
    ("GMA_UPDATE_TABLE", "update_table", "updates"),
)


def _tables_to_build(settings: Settings, deps: GmaDeps) -> list[str]:
    """The table settings the GMA builds a Postgres store from: those that are set and whose store ``deps`` lacks."""
    return [name for name, setting, dep in _TABLE_SETTINGS if getattr(settings, setting) and getattr(deps, dep) is None]


def _names(names: list[str]) -> str:
    """``A``, ``A and B``, ``A, B and C``, followed by ``needs`` or ``need``."""
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return f"{listed} {'needs' if len(names) == 1 else 'need'}"


def _check(settings: Settings, deps: GmaDeps) -> None:
    """Refuse settings that cannot work, before anything is built.

    A message names the settings, never a value: no connection string, role
    password or token.
    """
    if settings.audit_anchor_table and not settings.audit_table:
        raise ValueError("GMA_AUDIT_ANCHOR_TABLE needs GMA_AUDIT_TABLE")
    if settings.update_role and not settings.update_table:
        raise ValueError("GMA_UPDATE_ROLE needs GMA_UPDATE_TABLE")
    if settings.update_max_age_seconds is not None and not settings.update_table and deps.updates is None:
        raise ValueError("GMA_UPDATE_MAX_AGE_SECONDS needs GMA_UPDATE_TABLE, the table whose updates it limits")
    built = _tables_to_build(settings, deps)
    if "GMA_ROUND_TABLE" in built:
        missing = [
            name
            for name, kept in (
                ("GMA_MODEL_TABLE", bool(settings.model_table) or deps.model_store is not None),
                ("GMA_AUDIT_TABLE", bool(settings.audit_table) or deps.audit is not None),
            )
            if not kept
        ]
        if missing:
            raise ValueError(
                f"GMA_ROUND_TABLE needs {' and '.join(missing)}: a round kept across a restart needs its candidate "
                "model version and the release Guard's audit chain kept as well, or its release can be decided "
                "differently"
            )
    if settings.engine != "http":
        # The simulator neither reads nor deletes stored updates, so nothing
        # would ever delete the ones a store kept.
        reason = "the engine that reads and deletes stored updates"
        if "GMA_UPDATE_TABLE" in built:
            raise ValueError(f"GMA_UPDATE_TABLE needs GMA_ENGINE=http, {reason}")
        if getattr(deps.updates, "survives_restart", False) is True:
            raise ValueError(f"an update store that survives a restart needs GMA_ENGINE=http, {reason}")
        if settings.update_max_age_seconds is not None:
            raise ValueError(f"GMA_UPDATE_MAX_AGE_SECONDS needs GMA_ENGINE=http, {reason}")
    if "GMA_UPDATE_TABLE" in built:
        if deps.rounds is not None and getattr(deps.rounds, "survives_restart", False) is not True:
            raise ValueError(
                "GMA_UPDATE_TABLE needs a round store that survives a restart; the round store handed to create_app "
                "does not, so its stored updates would outlive their rounds"
            )
        if deps.rounds is None and not settings.round_table:
            raise ValueError(
                "GMA_UPDATE_TABLE needs GMA_ROUND_TABLE: without it a restart loses the rounds the stored updates "
                "belong to"
            )
        if not settings.update_role:
            raise ValueError("GMA_UPDATE_TABLE needs GMA_UPDATE_ROLE, the role that owns the updates table")
    if built:
        if not settings.database_url and deps.coordinator_lease is None:
            raise ValueError(f"{_names(built)} GMA_DATABASE_URL")
        if not settings.database_role:
            raise ValueError(f"{_names(built)} GMA_DATABASE_ROLE, the role every coordinator query runs as")
    if "GMA_UPDATE_TABLE" in built and settings.update_role == settings.database_role:
        raise ValueError(
            "GMA_UPDATE_ROLE must differ from GMA_DATABASE_ROLE, so that nothing acting as the coordinator role can "
            "read an update"
        )
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


def update_age_limit(settings: Settings, updates: UpdateStore) -> float | None:
    """The age limit for the stored updates of ``updates``, in seconds, or ``None`` for no age check.

    The limit applies when the update store keeps its updates after a restart
    (``GMA_UPDATE_TABLE``, or a store in ``GmaDeps`` whose
    ``survives_restart`` is true), or when ``GMA_UPDATE_MAX_AGE_SECONDS`` is
    set. It is ``GMA_UPDATE_MAX_AGE_SECONDS``, or 24 hours when that is not
    set. A GMA that keeps its updates in memory and sets no limit runs no age
    check: a restart already ends those updates.
    """
    if getattr(updates, "survives_restart", False) is True or settings.update_max_age_seconds is not None:
        return settings.update_age_limit_seconds
    return None


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


def _round_state_stores(
    settings: Settings, deps: GmaDeps, lease: KeptConnection | None
) -> tuple[RoundRepository, SiteRegistry, UpdateStore]:
    """The round, enrolment and update stores.

    Each comes from ``deps``, else from its table setting on ``lease``, else
    from memory. ``_check`` has made sure that ``lease``,
    ``GMA_DATABASE_ROLE`` and, for the update table, ``GMA_UPDATE_ROLE`` are
    there for every store built from a setting. Building a store opens no
    connection.
    """
    role = str(settings.database_role)
    round_store = deps.rounds
    enrolments = deps.enrolments
    updates = deps.updates
    if (round_store is None and settings.round_table) or (enrolments is None and settings.enrolment_table) or (
        updates is None and settings.update_table
    ):
        from dagents.gma.infrastructure.postgres_rounds import (
            PostgresEnrolmentStore,
            PostgresRoundRepository,
            PostgresUpdateStore,
        )

        assert lease is not None  # checked by _check
        if round_store is None and settings.round_table:
            round_store = PostgresRoundRepository(lease, role, settings.round_table)
        if enrolments is None and settings.enrolment_table:
            enrolments = PostgresEnrolmentStore(lease, role, settings.enrolment_table)
        if updates is None and settings.update_table:
            updates = PostgresUpdateStore(
                lease, str(settings.update_role), settings.update_table, coordinator_role=role
            )
    return (
        round_store if round_store is not None else InMemoryRoundRepository(),
        enrolments if enrolments is not None else InMemorySiteRegistry(),
        updates if updates is not None else InMemoryUpdateStore(),
    )


def create_app(settings: Settings | None = None, deps: GmaDeps | None = None) -> FastAPI:
    """Build a GMA app. ``create_app(Settings())`` is a GMA with nothing configured.

    Raises ``ValueError`` for settings that cannot work: an HTTP engine with no
    LMA to call, tables without a database or a role, a round table without
    the model and audit tables, an update table without a round table, its
    own role or the HTTP engine, an update role that is the coordinator role,
    an update age limit without an update table or the HTTP engine, an update
    store that survives a restart without the HTTP engine, a table name that
    is not ``schema.table``,
    required tokens that are not set, a token that is too short, a joinable
    site that is also configured, has a bad address, lacks its tokens, or has
    no HTTP network to join, or a planner setting that makes no sense
    (``DAGENTSC_TIMEOUT_SECONDS``, ``DAGENTSC_MAX_INPUT_BYTES``,
    ``DAGENTSC_MAX_OUTPUT_BYTES``, ``DAGENTSC_SHA256``). A role name the
    stores cannot act as raises
    :class:`~dagents.common.infrastructure.postgres_roles.RoleScopeError`.

    Building the app opens no database connection: each Postgres store
    connects on its first operation.
    """
    settings = settings or Settings()
    deps = deps or GmaDeps()
    _check(settings, deps)
    check_planner_settings()
    access = _access(settings)
    extensions, configured = _extensions(settings, deps)
    closers: list[Callable[[], None]] = []
    try:
        lease = deps.coordinator_lease
        if lease is None and _tables_to_build(settings, deps):
            from dagents.common.infrastructure.postgres_lease import KeptConnection

            lease = KeptConnection(str(settings.database_url), name="coordinator")
            closers.append(lease.close)
        role = str(settings.database_role)

        audit = deps.audit
        if audit is None and settings.audit_table:
            from dagents.common.infrastructure.postgres_audit import PostgresAuditSink

            audit = PostgresAuditSink(
                lease.lease,
                role,
                settings.audit_table,
                settings=lease.settings,
                anchor_table=settings.audit_anchor_table,
            )
        model_store = deps.model_store
        if model_store is None and settings.model_table:
            from dagents.gma.infrastructure.postgres_models import PostgresModelStore

            model_store = PostgresModelStore(lease, role, settings.model_table)
        model_store = model_store or InMemoryModelStore()
        round_store, enrolments, updates = _round_state_stores(settings, deps, lease)

        strict = configured and bool(extensions.list_condition_packs())
        governance = build_governance_service(registry=extensions, audit=audit, strict_classifications=strict)
        models = ModelRegistry(model_store, extensions)

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
                round_ids_survive_restart=getattr(round_store, "survives_restart", False) is True,
            )
        else:
            engine = InProcessFederationEngine()
        age_limit = update_age_limit(settings, updates)
        rounds = build_round_controller(engine, sites=enrolments, rounds=round_store, update_max_age_seconds=age_limit)
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
        round_store=round_store,
        enrolments=enrolments,
        coordinator_lease=lease,
        update_age_limit_seconds=age_limit,
        _closers=closers,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Under uvicorn the GMA recovers before it serves, waiting at most
        # START_RECOVERY_WAIT_SECONDS. A failure is logged, never raised, so
        # /health still answers. In process no lifespan runs, and the first
        # round write recovers instead.
        try:
            await state.recover_at_start()
            yield
        finally:
            state.stop_start_recovery()
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


__all__ = [
    "AGENT_NAME",
    "HANDLER_THREAD_NAME",
    "START_RECOVERY_WAIT_SECONDS",
    "GmaDeps",
    "GmaState",
    "HandlerThreads",
    "create_app",
    "update_age_limit",
]
