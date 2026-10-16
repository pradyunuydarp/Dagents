"""How the app reaches the Dagents agents, and how the app's services are put together.

The demo runs on four Dagents agents: one LMA (local monitoring agent) per
hospital and one GMA (global monitoring agent) for the study. The app calls
them only through their HTTP APIs, with the framework's agent client
(``dagents.common.infrastructure.agent_client``):

- **In process** (``HEALTHCARE_DEMO_AGENTS=in-process``, the default): the app
  builds the four agents with their app factories and calls them through
  ``httpx.ASGITransport``. A call goes through the agents' real routes and
  request validation, without a socket. Each LMA reads its hospital's store
  through :class:`~app.services.site_source.StoreSiteSource` and keeps its
  audit chain and job log on that store's connection; the GMA keeps the model
  registry and its audit chain on the coordinator store's connection, so the
  app still holds one database connection per hospital and one for the
  coordinator. The tokens are generated with ``secrets.token_urlsafe`` when the
  agents are first built, on the first request that needs them, and never
  logged.
- **Over the network** (``HEALTHCARE_DEMO_AGENTS=network``): the agents run as
  their own processes. Their addresses and the app's tokens come from the
  environment (see ``app.core.config``). The same client code calls them.

Who calls whom, and with which token:

| Caller | Calls | Token |
|---|---|---|
| A hospital view in the app | That hospital's LMA | The LMA's ``site`` token |
| The study operator in the app | The GMA | The GMA's ``admin`` token |
| The GMA | Each LMA's site, summary, audit summary, registration and job routes | That LMA's ``coordinator`` token |
| An LMA | The GMA's update upload and released model | A GMA ``site`` token bound to that hospital |

:func:`lma_environment` and :func:`gma_environment` give the ``LMA_*`` and
``GMA_*`` settings (without secrets) that the in-process agents run with, so
agents started as separate processes run with the same ones.

:class:`FrameworkCalls` records the agent calls behind one public request, for
the ``framework_calls`` field of every successful answer whose body is a JSON
object.

This is the only app module that imports the agent client and the agents' app
factories. The rest of the app takes the clients, the error types
(``AgentError``, ``AgentUnavailableError``) and :func:`loggable` from here.
"""

from __future__ import annotations

import contextvars
import logging
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from dagents.common.domain.federation import DispatchCall
from dagents.common.extensions import ExtensionRegistry
from dagents.common.infrastructure.agent_client import (
    GMA_AGENT,
    AgentCall,
    AgentError,
    AgentNetwork,
    AgentUnavailableError,
    CallLog,
    GmaClient,
    LmaClient,
    lma_agent,
    without_addresses,
)

from app.core.config import Settings, lma_token_variable, lma_url_variable, settings as app_settings, site_value
from app.domain.conditions import STROKE_CONDITION, STROKE_STUDY_ID
from app.domain.dataset import SITES, Site, UnknownSiteError, load_site_records
from app.extension import HealthcareStrokeExtension
from app.services.hospital_views import CAPABILITIES, POLICY_VERSION, PROFILE_FIELDS, REPORTING_PURPOSE, HospitalView
from app.services.site_source import SITE_SOURCE_NAME, StoreSiteSource
from app.services.site_store import (
    COORDINATOR_MODELS_TABLE,
    COORDINATOR_ROLE,
    SITE_JOB_LOG_TABLE,
    CoordinatorStore,
    MemoryCoordinatorStore,
    MemorySiteStore,
    PostgresCoordinatorStore,
    PostgresSiteStore,
    SiteStore,
    site_database_url,
)

logger = logging.getLogger(__name__)

#: How an LMA started on its own loads the app's extension.
EXTENSION_PATH = "app.extension:HealthcareStrokeExtension"

#: Random bytes in each generated token, before it is encoded.
TOKEN_BYTES = 32


def create_lma_app(settings: Any, deps: Any) -> Any:
    """One LMA app, from ``dagents.lma.app_factory``.

    The agents' modules are imported here, when the agents are first built,
    not when the app starts: the health check answers before they are
    imported, which takes seconds at a fraction of a CPU.
    """
    from dagents.lma.app_factory import create_app

    return create_app(settings, deps)


def create_gma_app(settings: Any, deps: Any) -> Any:
    """The GMA app, from ``dagents.gma.app_factory``. Imported on first use, as for :func:`create_lma_app`."""
    from dagents.gma.app_factory import create_app

    return create_app(settings, deps)


def loggable(error: BaseException) -> str:
    """An error's message for the log, with every ``scheme://...`` address replaced by ``[address]``.

    An agent's error detail can quote a store error that names a database
    host. The browser never gets the detail; the log gets it without the
    address.
    """
    return without_addresses(str(error))


# --- The agents' settings -------------------------------------------------------------------


def lma_environment(site: Site, *, database: bool) -> dict[str, str]:
    """The ``LMA_*`` settings one hospital's LMA runs with, without secrets or addresses.

    Params:
    - `site`: the hospital.
    - `database`: whether the hospital's records are in Postgres. Then the LMA
      reads them as the hospital's role and keeps its audit chain and job log
      in the hospital's tables.

    A separate LMA process also needs ``LMA_DATABASE_URL``,
    ``LMA_GMA_ENDPOINT``, ``LMA_GMA_TOKEN``, ``LMA_SITE_TOKEN`` and
    ``LMA_COORDINATOR_TOKEN``, which never appear here.
    """
    env = {
        "LMA_APP_NAME": f"dagents-lma-{site.site_id}",
        "LMA_SITE_ID": site.site_id,
        "LMA_CONDITION_ID": STROKE_CONDITION.condition_id,
        "LMA_STUDY_ID": STROKE_STUDY_ID,
        "LMA_REPORTING_PURPOSE": REPORTING_PURPOSE,
        "LMA_PROFILE_FIELDS": ",".join(PROFILE_FIELDS),
        "LMA_CAPABILITIES": ",".join(CAPABILITIES),
        "LMA_POLICY_VERSION": POLICY_VERSION,
        "LMA_EXTENSIONS": EXTENSION_PATH,
        "LMA_SITE_SOURCE": SITE_SOURCE_NAME,
        "LMA_REQUIRE_TOKENS": "1",
    }
    if database:
        env.update(
            {
                "LMA_SITE_ROLE": site.role,
                "LMA_AUDIT_TABLE": f"{site.schema}.guard_audit",
                "LMA_AUDIT_ANCHOR_TABLE": f"{site.schema}.guard_audit_anchor",
                "LMA_JOB_LOG_TABLE": SITE_JOB_LOG_TABLE,
            }
        )
    return env


def gma_environment(*, database: bool, dispatch_concurrency: int = 3) -> dict[str, str]:
    """The ``GMA_*`` settings the study's GMA runs with, without secrets or addresses.

    Params:
    - `database`: whether the coordinator's tables are in Postgres. Then the
      GMA keeps the model registry and its audit chain there, acting as
      ``dagents_coordinator``, which has no grant on any hospital schema.
    - `dispatch_concurrency`: how many hospitals a round's jobs go to at once.

    The GMA's site summaries are not kept between calls
    (``GMA_SUMMARY_TTL_SECONDS=0``), so a page shows a new entry at once, and
    it waits up to 10 seconds for each LMA's summary
    (``GMA_SUMMARY_TIMEOUT_SECONDS``), so one LMA that does not answer cannot
    hold a page for the agent network's longer timeout. Its source routes are
    off (``GMA_ALLOW_SOURCE_READS=0``): it reads no record.
    A separate GMA process also needs ``GMA_DATABASE_URL``,
    ``GMA_SITE_ENDPOINTS``, ``GMA_LMA_TOKENS``, ``GMA_ADMIN_TOKEN`` and
    ``GMA_SITE_TOKENS``, which never appear here.
    """
    env = {
        "GMA_APP_NAME": "dagents-gma",
        "GMA_ENGINE": "http",
        "GMA_EXTENSIONS": EXTENSION_PATH,
        "GMA_DISPATCH_CONCURRENCY": str(dispatch_concurrency),
        "GMA_SUMMARY_TTL_SECONDS": "0",
        "GMA_SUMMARY_TIMEOUT_SECONDS": "10",
        "GMA_ALLOW_SOURCE_READS": "0",
        "GMA_REQUIRE_TOKENS": "1",
    }
    if database:
        env.update(
            {
                "GMA_DATABASE_ROLE": COORDINATOR_ROLE,
                "GMA_MODEL_TABLE": COORDINATOR_MODELS_TABLE,
                "GMA_AUDIT_TABLE": "coordinator.guard_audit",
                "GMA_AUDIT_ANCHOR_TABLE": "coordinator.guard_audit_anchor",
            }
        )
    return env


def _site_map(values: Mapping[str, str]) -> str:
    """``SITE=VALUE,...``, the form the GMA's site settings take."""
    return ",".join(f"{site_id}={value}" for site_id, value in sorted(values.items()))


# --- Tokens ----------------------------------------------------------------------------------


def _token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


@dataclass(frozen=True)
class AgentTokens:
    """The tokens of an in-process deployment. ``repr`` shows none of them.

    - `gma_admin`: the study operator's token for the GMA.
    - `lma_site`: each LMA's ``site`` token, for the hospital views.
    - `lma_coordinator`: each LMA's ``coordinator`` token, for the GMA.
    - `gma_site`: each hospital's GMA ``site`` token, bound to that hospital,
      for its LMA's update uploads and the released model.
    """

    gma_admin: str = field(repr=False)
    lma_site: Mapping[str, str] = field(repr=False)
    lma_coordinator: Mapping[str, str] = field(repr=False)
    gma_site: Mapping[str, str] = field(repr=False)

    @classmethod
    def generate(cls, site_ids: Sequence[str]) -> AgentTokens:
        """New random tokens, one of each kind per hospital."""
        return cls(
            gma_admin=_token(),
            lma_site={site_id: _token() for site_id in site_ids},
            lma_coordinator={site_id: _token() for site_id in site_ids},
            gma_site={site_id: _token() for site_id in site_ids},
        )


# --- The agents ------------------------------------------------------------------------------


class Agents:
    """The agents the app calls, and the tokens it calls them with.

    :meth:`gma` returns a client with the study operator's token and
    :meth:`lma` one with a hospital's ``site`` token. :meth:`close` stops the
    in-process agents' event loop, or closes the HTTP clients.
    """

    def __init__(
        self,
        network: AgentNetwork,
        *,
        site_ids: Sequence[str],
        gma_token: str | None,
        lma_tokens: Mapping[str, str],
        closers: Sequence[Callable[[], Any]] = (),
    ) -> None:
        self.network = network
        self.site_ids = tuple(site_ids)
        self._gma_token = gma_token
        self._lma_tokens = dict(lma_tokens)
        self._closers = list(closers)
        self._closed = False
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"Agents(mode={self.mode!r}, site_ids={list(self.site_ids)!r})"

    @property
    def mode(self) -> str:
        """``in-process`` or ``network``."""
        return self.network.mode

    def gma(self) -> GmaClient:
        """The GMA, with the study operator's ``admin`` token."""
        return self.network.gma(self._gma_token)

    def lma(self, site_id: str) -> LmaClient:
        """One hospital's LMA, with its ``site`` token. Raises ``UnknownSiteError`` for another id."""
        if site_id not in self._lma_tokens:
            raise UnknownSiteError(site_id)
        return self.network.lma(site_id, self._lma_tokens[site_id])

    def health_checks(self, timeout_s: float) -> list[dict[str, str]]:
        """Each agent's health check, GMA first: ``[{name, status, detail}]``.

        ``name`` is ``gma`` or ``lma-<site_id>``; ``status`` is ``ok`` or
        ``unavailable``. The checks run at the same time, each with a copy of
        the caller's context, so a request's framework calls list them. No
        address is returned.
        """
        checks: list[tuple[str, Callable[[], Any]]] = [("gma", self.gma)]
        checks += [(f"lma-{site_id}", (lambda site_id=site_id: self.lma(site_id))) for site_id in self.site_ids]

        def check(name: str, client: Callable[[], Any]) -> dict[str, str]:
            try:
                client().request("GET", "/api/v1/health", timeout_s=timeout_s)
            except AgentUnavailableError:
                return {"name": name, "status": "unavailable", "detail": "did not answer"}
            except AgentError as error:
                return {"name": name, "status": "unavailable", "detail": f"answered {error.status} ({error.code})"}
            return {"name": name, "status": "ok", "detail": "answered its health check"}

        with ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix="healthcare-health") as pool:
            futures = [pool.submit(contextvars.copy_context().run, check, name, client) for name, client in checks]
            return [future.result() for future in futures]

    def close(self) -> None:
        """Stop the agents. A second call does nothing."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self.network.close()
        finally:
            for close in reversed(self._closers):
                try:
                    close()
                except Exception:  # noqa: BLE001 - closing one agent must not stop the others
                    logger.warning("an agent could not be closed", exc_info=True)


def build_in_process_agents(
    settings: Settings,
    sites: Sequence[Site],
    stores: Mapping[str, SiteStore],
    sources: Mapping[str, StoreSiteSource],
    coordinator_store: CoordinatorStore,
    *,
    tokens: AgentTokens | None = None,
    extension: Any | None = None,
) -> Agents:
    """Build one LMA per hospital and a GMA in this process, on one in-process agent network.

    Each LMA reads its hospital's store through ``sources[site_id]``, keeps its
    audit chain in the store's audit sink and its job log on the store's
    connection. The GMA runs the rounds over the network (``GMA_ENGINE=http``)
    and keeps the model registry and its audit chain on the coordinator
    store's connection. Every agent requires tokens.

    ``extension`` is what the agents' extension registry holds; it defaults to
    the app's :class:`~app.extension.HealthcareStrokeExtension`. Raises
    ``ValueError`` when the settings could leave the agents without threads
    (:meth:`Settings.check_agent_capacity`). Nothing it built is left running
    when it raises.
    """
    from dagents.gma.app_factory import GmaDeps
    from dagents.gma.config import Settings as GmaSettings
    from dagents.lma.app_factory import LmaDeps
    from dagents.lma.config import Settings as LmaSettings

    settings.check_agent_capacity()
    site_ids = [site.site_id for site in sites]
    tokens = tokens or AgentTokens.generate(site_ids)
    registry = ExtensionRegistry()
    registry.register(extension or HealthcareStrokeExtension())

    network = AgentNetwork.in_process(thread_tokens=settings.agent_thread_tokens)
    closers: list[Callable[[], Any]] = []
    try:
        for site in sites:
            store = stores[site.site_id]
            database = store.lease is not None
            env = {
                **lma_environment(site, database=database),
                "LMA_SITE_TOKEN": tokens.lma_site[site.site_id],
                "LMA_COORDINATOR_TOKEN": tokens.lma_coordinator[site.site_id],
                "LMA_GMA_TOKEN": tokens.gma_site[site.site_id],
            }
            job_log = None
            if database:
                from dagents.lma.infrastructure.job_log import PostgresJobLog

                job_log = PostgresJobLog(store.lease, site.role, SITE_JOB_LOG_TABLE)
            app = create_lma_app(
                LmaSettings.from_env(env),
                LmaDeps(
                    registry=registry,
                    source=sources[site.site_id],
                    audit=store.audit_sink(),
                    job_log=job_log,
                    network=network,
                ),
            )
            closers.append(app.state.lma.close)
            network.add_app(lma_agent(site.site_id), app)

        database = coordinator_store.lease is not None
        gma_env = {
            **gma_environment(database=database, dispatch_concurrency=settings.dispatch_concurrency),
            "GMA_ADMIN_TOKEN": tokens.gma_admin,
            "GMA_LMA_TOKENS": _site_map(tokens.lma_coordinator),
            "GMA_SITE_TOKENS": _site_map(tokens.gma_site),
        }
        gma_app = create_gma_app(
            GmaSettings.from_env(gma_env),
            GmaDeps(
                registry=registry,
                network=network,
                coordinator_lease=coordinator_store.lease,
                audit=coordinator_store.audit_sink(),
            ),
        )
        closers.append(gma_app.state.gma.close)
        network.add_app(GMA_AGENT, gma_app)
    except BaseException:
        network.close()
        for close in reversed(closers):
            close()
        raise
    return Agents(
        network, site_ids=site_ids, gma_token=tokens.gma_admin, lma_tokens=tokens.lma_site, closers=closers
    )


def connect_network_agents(
    settings: Settings, sites: Sequence[Site], environ: Mapping[str, str] | None = None
) -> Agents:
    """Connect to agents that run as their own processes.

    Reads ``HEALTHCARE_DEMO_GMA_URL``, ``HEALTHCARE_DEMO_GMA_TOKEN`` and, for
    each hospital, ``HEALTHCARE_DEMO_LMA_<SITE>_URL`` and
    ``HEALTHCARE_DEMO_LMA_<SITE>_TOKEN``. Raises ``ValueError`` naming the
    first variable that is missing, never its value.
    """
    settings.check_agent_capacity()
    if not settings.gma_url.strip():
        raise ValueError("HEALTHCARE_DEMO_AGENTS=network needs HEALTHCARE_DEMO_GMA_URL")
    if not settings.gma_token.strip():
        raise ValueError("HEALTHCARE_DEMO_AGENTS=network needs HEALTHCARE_DEMO_GMA_TOKEN")
    urls = {GMA_AGENT: settings.gma_url.strip()}
    tokens: dict[str, str] = {}
    for site in sites:
        url = site_value(lma_url_variable(site.site_id), environ)
        token = site_value(lma_token_variable(site.site_id), environ)
        if not url:
            raise ValueError(f"HEALTHCARE_DEMO_AGENTS=network needs {lma_url_variable(site.site_id)}")
        if not token:
            raise ValueError(f"HEALTHCARE_DEMO_AGENTS=network needs {lma_token_variable(site.site_id)}")
        urls[lma_agent(site.site_id)] = url
        tokens[site.site_id] = token
    network = AgentNetwork.over_http(urls)
    return Agents(
        network, site_ids=[site.site_id for site in sites], gma_token=settings.gma_token.strip(), lma_tokens=tokens
    )


# --- The calls behind one public request ------------------------------------------------------

_CURRENT_CALLS: contextvars.ContextVar[FrameworkCalls | None] = contextvars.ContextVar(
    "healthcare_framework_calls", default=None
)


def call_entry(call: AgentCall) -> dict[str, Any]:
    """One call the app made to an agent: ``{service, site_id, method, path, status, ms, via}``.

    ``path`` is the route template, never a host or an address. ``via`` is
    ``app``.
    """
    return {**call.as_dict(), "via": "app"}


#: The route each dispatch step calls on an LMA.
_DISPATCH_PATHS = {
    "offer": "/api/v1/federation/jobs:offer",
    "execute": "/api/v1/federation/jobs:execute",
    "run": "/api/v1/federation/jobs:run",
}


def dispatch_entry(call: DispatchCall) -> dict[str, Any]:
    """One call the GMA made to an LMA while it dispatched a round, in the same shape. ``via`` is ``gma``."""
    return {
        "service": "lma",
        "site_id": call.site_id,
        "method": "POST",
        "path": _DISPATCH_PATHS[call.step],
        "status": call.status_code,
        "ms": call.ms,
        "via": "gma",
    }


class FrameworkCalls:
    """The LMA and GMA calls behind one public request.

    Use it as a context manager around the request. Every call the app makes
    to an agent in that context is recorded (``via: "app"``). The calls the GMA
    made to the LMAs while it dispatched a round are added with
    :func:`note_dispatch` and follow the GMA call that made them
    (``via: "gma"``). No entry holds a host, a token or a body.
    """

    def __init__(self) -> None:
        self._log = CallLog()
        self._lock = threading.Lock()
        self._dispatch: list[tuple[int, list[dict[str, Any]]]] = []
        self._token: contextvars.Token[FrameworkCalls | None] | None = None

    def __enter__(self) -> FrameworkCalls:
        self._log.__enter__()
        self._token = _CURRENT_CALLS.set(self)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _CURRENT_CALLS.reset(self._token)
            self._token = None
        self._log.__exit__(*exc_info)

    @staticmethod
    def current() -> FrameworkCalls | None:
        """The recorder of the request this code runs for, or ``None``."""
        return _CURRENT_CALLS.get()

    def add_dispatch(self, calls: Sequence[DispatchCall]) -> None:
        """Add the GMA's calls to the LMAs for one round, after the calls recorded so far."""
        rows = [dispatch_entry(call) for call in calls]
        with self._lock:
            self._dispatch.append((len(self._log.calls), rows))

    def entries(self) -> list[dict[str, Any]]:
        """Every call, in the order it finished; a round's dispatch rows follow the GMA call that made them."""
        calls = [call_entry(call) for call in self._log.calls]
        with self._lock:
            notes = list(self._dispatch)
        merged: list[dict[str, Any]] = []
        position = 0
        for index, rows in notes:
            merged.extend(calls[position:index])
            merged.extend(rows)
            position = max(position, index)
        merged.extend(calls[position:])
        return merged


def note_dispatch(calls: Sequence[DispatchCall]) -> None:
    """Add a round's dispatch calls to the current request's framework calls, if a request is recorded."""
    recorder = FrameworkCalls.current()
    if recorder is not None:
        recorder.add_dispatch(calls)


# --- The app's services ------------------------------------------------------------------------


def build_site_store(
    site: Site, database_url: str, *, max_entered: int, environ: Mapping[str, str] | None = None
) -> SiteStore:
    """The store for one hospital.

    With a database URL (or the hospital's own
    ``HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL``), the hospital's schema in
    Postgres. Without one, an in-memory store loaded from the committed extract.
    """
    if database_url.strip() or site_database_url(site, "", environ):
        return PostgresSiteStore.from_environment(site, database_url.strip(), environ, max_entered=max_entered)
    return MemorySiteStore(site, load_site_records(site.site_id), max_entered=max_entered)


@dataclass
class _Built:
    stores: dict[str, SiteStore]
    coordinator_store: CoordinatorStore
    agents: Agents
    views: dict[str, Any]
    overview: Any
    pilot: Any
    owned: list[Any]
    models_registered: bool = False


class HealthcareDemo:
    """The app's services: the hospitals' stores, the agents, the hospital views, the study and the overview.

    Params:
    - `sites`: the hospitals, in display order. Defaults to the three IST
      hospitals.
    - `settings`: the app's settings. Defaults to the process settings.
    - `database_url`: ``HEALTHCARE_DEMO_DATABASE_URL``. Empty means in-memory
      stores loaded from the committed extract. Defaults to the settings.
    - `secure_aggregation_threshold`, `max_entered_per_site`: default to the
      settings.
    - `environ`: where per-hospital connection strings and network-mode
      addresses are read from. Defaults to ``os.environ``.
    - `site_stores`, `coordinator_store`: stores to use instead of building
      them, for tests. The caller closes them.
    - `extension`: what the in-process agents' extension registry holds, for
      tests that need, say, other release gates. Defaults to the app's
      extension.

    Building it reads no records, opens no connection and starts no agent.
    The first use builds the stores and the agents. The first call that needs
    a model version (a hospital's dashboard, worklist or entry, the models, an
    approval, a pilot) registers the app's model versions with the GMA, once
    per build; :meth:`ready` does it at once. The status report, a hospital's
    summary, its audit trail and the Guard probe do not need them, so they
    answer while the planner or the coordinator's store is down. :meth:`close`
    stops the agents and closes the stores it built; a later use builds them
    again.
    """

    def __init__(
        self,
        sites: Sequence[Site] | None = None,
        *,
        settings: Settings | None = None,
        database_url: str | None = None,
        secure_aggregation_threshold: int | None = None,
        max_entered_per_site: int | None = None,
        environ: Mapping[str, str] | None = None,
        site_stores: Mapping[str, SiteStore] | None = None,
        coordinator_store: CoordinatorStore | None = None,
        extension: Any | None = None,
    ) -> None:
        self.settings = settings or app_settings
        self._extension = extension
        self.sites: tuple[Site, ...] = tuple(sites or SITES)
        self.database_url = (self.settings.database_url if database_url is None else database_url).strip()
        self.secure_aggregation_threshold = (
            self.settings.secure_aggregation_threshold
            if secure_aggregation_threshold is None
            else secure_aggregation_threshold
        )
        self.max_entered_per_site = (
            self.settings.max_entered_per_site if max_entered_per_site is None else max_entered_per_site
        )
        self._environ = environ
        self._injected_stores = dict(site_stores or {})
        self._injected_coordinator = coordinator_store
        self._lock = threading.Lock()
        self._built: _Built | None = None

    # --- Building --------------------------------------------------------------------

    def _built_services(self) -> _Built:
        with self._lock:
            if self._built is None:
                self._built = self._assemble()
            return self._built

    def _assemble(self) -> _Built:
        # Imported here: the overview and the pilot import this module for its
        # error types and clients.
        from dagents.common.infrastructure.charts import MAX_SERIES

        from app.services.overview import StudyOverview
        from app.services.pilot import PilotService

        owned: list[Any] = []
        try:
            stores: dict[str, SiteStore] = {}
            for site in self.sites:
                store = self._injected_stores.get(site.site_id)
                if store is None:
                    store = build_site_store(
                        site, self.database_url, max_entered=self.max_entered_per_site, environ=self._environ
                    )
                    owned.append(store)
                stores[site.site_id] = store
            coordinator_store = self._injected_coordinator
            if coordinator_store is None:
                coordinator_store = (
                    PostgresCoordinatorStore(self.database_url) if self.database_url else MemoryCoordinatorStore()
                )
                owned.append(coordinator_store)
            sources = {site_id: StoreSiteSource(store) for site_id, store in stores.items()}

            if self.settings.agents_mode == "network":
                memory = [site_id for site_id, store in stores.items() if store.lease is None]
                if memory:
                    raise ValueError(
                        "HEALTHCARE_DEMO_AGENTS=network needs HEALTHCARE_DEMO_DATABASE_URL: agents in other "
                        "processes cannot read this process's in-memory stores"
                    )
                agents = connect_network_agents(self.settings, self.sites, self._environ)
            else:
                agents = build_in_process_agents(
                    self.settings, self.sites, stores, sources, coordinator_store, extension=self._extension
                )

            # A hospital's chart colour is its place in the consortium, as on
            # the consortium charts. More hospitals than colours means none has one.
            slots = len(self.sites) <= MAX_SERIES
            views = {
                site.site_id: HospitalView(
                    site,
                    stores[site.site_id],
                    agents,
                    source=sources[site.site_id],
                    series_slot=position if slots else None,
                    models_ready=self._ensure_models,
                )
                for position, site in enumerate(self.sites, start=1)
            }
            overview = StudyOverview(
                self.sites,
                views,
                agents,
                coordinator_store,
                secure_aggregation_threshold=self.secure_aggregation_threshold,
                status_timeout_seconds=self.settings.request_timeout_seconds,
                models_ready=self._ensure_models,
            )
            pilot = PilotService(
                self.sites,
                agents,
                coordinator_store,
                secure_aggregation_threshold=self.secure_aggregation_threshold,
                on_report=overview.remember_pilot,
                note_dispatch=note_dispatch,
                models_ready=self._ensure_models,
            )
        except BaseException:
            for resource in reversed(owned):
                resource.close()
            raise
        return _Built(stores, coordinator_store, agents, views, overview, pilot, owned)

    def ready(self) -> HealthcareDemo:
        """Build the services if needed and make sure the GMA holds the app's model versions."""
        self._ensure_models()
        return self

    def _ensure_models(self) -> None:
        """Register the app's model versions with the GMA, once per build.

        No lock is held during the GMA's call. Two first requests may both
        register; the GMA stores a version sent twice with the same content
        once. A failed registration is tried again on the next call.
        """
        built = self._built_services()
        if built.models_registered:
            return
        built.pilot.register_models()
        built.models_registered = True

    def status(self) -> dict[str, Any]:
        """The status report (:meth:`StudyOverview.status`). It registers no model, so it answers while the GMA's store is down."""
        return self._built_services().overview.status()

    # --- The services ----------------------------------------------------------------
    # Each is built on first use. The views, the overview and the pilot
    # register the app's model versions before the first call that needs one.

    @property
    def agents(self) -> Agents:
        return self._built_services().agents

    @property
    def stores(self) -> dict[str, SiteStore]:
        return self._built_services().stores

    @property
    def coordinator_store(self) -> CoordinatorStore:
        return self._built_services().coordinator_store

    @property
    def views(self) -> dict[str, Any]:
        """Each hospital's view (:class:`~app.services.hospital_views.HospitalView`), by site id."""
        return self._built_services().views

    @property
    def overview(self) -> Any:
        """The study overview (:class:`~app.services.overview.StudyOverview`)."""
        return self._built_services().overview

    @property
    def pilot(self) -> Any:
        """The study operator (:class:`~app.services.pilot.PilotService`)."""
        return self._built_services().pilot

    def hospital(self, site_id: str) -> Any:
        """The view of the hospital with this id. Raises ``UnknownSiteError`` (a ``KeyError``) first for another id."""
        if site_id not in {site.site_id for site in self.sites}:
            raise UnknownSiteError(site_id)
        return self.views[site_id]

    def close(self) -> None:
        """Stop the agents and close the stores this object built. A later use builds them again."""
        with self._lock:
            built, self._built = self._built, None
        if built is None:
            return
        try:
            built.agents.close()
        finally:
            for resource in reversed(built.owned):
                try:
                    resource.close()
                except Exception:  # noqa: BLE001 - closing one store must not stop the others
                    logger.warning("a store could not be closed", exc_info=True)


__all__ = [
    "AgentError",
    "AgentTokens",
    "AgentUnavailableError",
    "Agents",
    "EXTENSION_PATH",
    "FrameworkCalls",
    "GmaClient",
    "HealthcareDemo",
    "LmaClient",
    "build_in_process_agents",
    "build_site_store",
    "call_entry",
    "connect_network_agents",
    "dispatch_entry",
    "gma_environment",
    "lma_environment",
    "loggable",
    "note_dispatch",
]


def main(argv: Sequence[str] | None = None) -> int:
    """Print the settings an agent started on its own runs with, one ``NAME=value`` per line.

    ``python -m app.services.agents lma uk-134 --database`` prints a
    hospital's ``LMA_*`` settings and ``python -m app.services.agents gma
    --database`` the GMA's. Secrets and addresses are never printed; a start
    script adds them from the environment.
    """
    import argparse

    from app.domain.dataset import site_for

    parser = argparse.ArgumentParser(prog="python -m app.services.agents", description=main.__doc__)
    parser.add_argument("agent", choices=("lma", "gma"))
    parser.add_argument("site_id", nargs="?", help="the hospital, for an LMA")
    parser.add_argument("--database", action="store_true", help="the records and tables are in Postgres")
    args = parser.parse_args(argv)
    if args.agent == "lma":
        if not args.site_id:
            parser.error("an LMA needs a site id")
        env = lma_environment(site_for(args.site_id), database=args.database)
    else:
        env = gma_environment(database=args.database, dispatch_concurrency=app_settings.dispatch_concurrency)
    for name, value in env.items():
        print(f"{name}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
