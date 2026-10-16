"""Dependency container for the Local Monitoring Agent.

:func:`build_runtime` builds everything one LMA needs from its settings and
the objects an application hands it (:class:`LmaDeps`). An object passed in
``deps`` belongs to the caller; an object the runtime builds from settings
belongs to the runtime, and :meth:`LmaRuntime.close` closes it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any, Callable

from dagents.common.application.ethical_guard import AuditSink, InMemoryAuditLog
from dagents.common.application.governance_service import GovernanceService
from dagents.common.application.ports import JobLog, SiteRecordSource
from dagents.common.extensions import ExtensionRegistry, default_registry, load_extensions
from dagents.common.infrastructure.agent_auth import AgentAccess, TokenGrant
from dagents.common.infrastructure.agent_client import GMA_AGENT, AgentNetwork
from dagents.common.infrastructure.sources import DefaultSourceResolver, default_adapters
from dagents.lma.adapters.gma_link import GmaLink
from dagents.lma.adapters.http_update_channel import HttpUpdateChannel
from dagents.lma.adapters.runner import InMemoryMonitoringRunner
from dagents.lma.application.federation_jobs import FederationJobService
from dagents.lma.application.monitoring_service import MonitoringService
from dagents.lma.application.site_service import SiteService
from dagents.lma.config import DEFAULT_STATEMENT_TIMEOUT_MS, Settings
from dagents.lma.domain.site_config import DEFAULT_POLICY_VERSION, SiteConfig
from dagents.lma.infrastructure.job_log import InMemoryJobLog, PostgresJobLog
from dagents.lma.infrastructure.messaging import InMemoryTelemetryPublisher
from dagents.lma.infrastructure.state import (
    InMemoryBundleRepository,
    InMemoryModelRunRepository,
    InMemoryRunHistoryRepository,
)

logger = logging.getLogger(__name__)


@dataclass
class LmaDeps:
    """Objects an application hands an LMA instead of letting it build them from settings.

    - ``registry``: the extensions. Without it, the LMA loads
      ``settings.extensions`` into a registry of its own, or uses the default
      registry when no extension is named.
    - ``site``: the site the LMA serves. Without it, the site comes from
      ``settings.site_id`` and the other site settings.
    - ``source``: the site's records, such as the application's own store.
      Without it, the LMA builds one with the site source factory named in
      ``settings.site_source``.
    - ``audit``: the site's audit chain. Without it, a Postgres chain when the
      settings name a database and an audit table, otherwise in memory.
    - ``job_log``: the site's job log, chosen the same way.
    - ``network``: the agent network the GMA is on. Without it, the LMA
      reaches ``settings.gma_endpoint`` over HTTP.
    """

    registry: ExtensionRegistry | None = None
    site: SiteConfig | None = None
    source: SiteRecordSource | None = None
    audit: AuditSink | None = None
    job_log: JobLog | None = None
    network: AgentNetwork | None = None


@dataclass
class LmaRuntime:
    """Everything one LMA serves from: its services, its tokens, and what it must close."""

    settings: Settings
    registry: ExtensionRegistry
    monitoring: MonitoringService
    governance: GovernanceService
    access: AgentAccess
    site: SiteService | None = None
    jobs: FederationJobService | None = None
    _closers: list[Callable[[], Any]] = field(default_factory=list, repr=False)

    def close(self) -> None:
        """Close what the runtime built itself: its network, its site source and its database connection."""
        closers, self._closers = self._closers, []
        for close in reversed(closers):
            try:
                close()
            except Exception:  # noqa: BLE001 - closing one resource must not stop the others
                logger.warning("closing an LMA resource failed", exc_info=True)


def build_monitoring_service(*, site_mode: bool = False, statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS) -> MonitoringService:
    return MonitoringService(
        runner=InMemoryMonitoringRunner(),
        publisher=InMemoryTelemetryPublisher(),
        bundles=InMemoryBundleRepository(),
        runs=InMemoryRunHistoryRepository(),
        model_runs=InMemoryModelRunRepository(),
        source_resolver=DefaultSourceResolver(adapters=default_adapters(statement_timeout_ms=statement_timeout_ms)),
        site_mode=site_mode,
    )


def build_governance_service(
    *, audit: AuditSink | None = None, registry: ExtensionRegistry | None = None, strict: bool = False
) -> GovernanceService:
    """Build the LMA's Ethical Guard and its audit log.

    The LMA is where three of the four guard boundaries stand: before local data
    is read, before training touches it, and before anything leaves the site.
    An LMA started for a site runs with strict classifications, so a caller
    cannot plan or enforce against a policy of its own.
    """
    return GovernanceService(audit=audit, registry=registry, strict_classifications=strict)


def build_access(settings: Settings) -> AgentAccess:
    """The tokens the LMA accepts: one for the ``site`` scope and one for the ``coordinator`` scope."""
    grants = []
    if settings.site_token:
        grants.append(TokenGrant("site", settings.site_token))
    if settings.coordinator_token:
        grants.append(TokenGrant("coordinator", settings.coordinator_token))
    return AgentAccess(grants)


def _site_from_settings(settings: Settings) -> SiteConfig | None:
    if settings.site_id is None:
        return None
    if settings.condition_id is None:
        raise ValueError("an LMA started for a site needs LMA_CONDITION_ID as well as LMA_SITE_ID")
    return SiteConfig(
        site_id=settings.site_id,
        condition_id=settings.condition_id,
        study_id=settings.study_id,
        reporting_purpose=settings.reporting_purpose,
        profile_fields=settings.profile_fields,
        capabilities=settings.capabilities,
        policy_version=settings.policy_version or DEFAULT_POLICY_VERSION,
    )


def build_runtime(settings: Settings, deps: LmaDeps | None = None) -> LmaRuntime:
    """Build one LMA's services from its settings and the objects it was handed.

    Raises ``ValueError`` for settings that do not fit together, and
    ``ExtensionError`` for an extension, condition pack or site source that is
    not registered, so a misconfigured LMA fails at startup. Nothing it built
    is left open when it raises.
    """
    deps = deps or LmaDeps()
    closers: list[Callable[[], Any]] = []
    try:
        return _build(settings, deps, closers)
    except BaseException:
        for close in reversed(closers):
            try:
                close()
            except Exception:  # noqa: BLE001 - the original error matters more
                pass
        raise


def _build(settings: Settings, deps: LmaDeps, closers: list[Callable[[], Any]]) -> LmaRuntime:
    if deps.registry is not None:
        registry = deps.registry
    elif settings.extensions:
        registry = ExtensionRegistry()
        load_extensions(settings.extensions, registry)
    else:
        registry = default_registry

    site = deps.site or _site_from_settings(settings)
    access = build_access(settings)
    if site is None:
        return LmaRuntime(
            settings=settings,
            registry=registry,
            monitoring=build_monitoring_service(
                statement_timeout_ms=settings.statement_timeout_ms or DEFAULT_STATEMENT_TIMEOUT_MS
            ),
            governance=build_governance_service(audit=deps.audit, registry=registry),
            access=access,
            _closers=closers,
        )

    # The pack must exist before anything opens a connection.
    registry.condition_pack(site.condition_id)

    needs_database = (
        (deps.source is None and settings.site_source is not None)
        or (deps.audit is None and settings.audit_table is not None)
        or (deps.job_log is None and settings.job_log_table is not None)
    )
    lease = None
    role = settings.site_role
    if needs_database and settings.database_url is None:
        if deps.audit is None and settings.audit_table is not None:
            raise ValueError("LMA_AUDIT_TABLE needs LMA_DATABASE_URL")
        if deps.job_log is None and settings.job_log_table is not None:
            raise ValueError("LMA_JOB_LOG_TABLE needs LMA_DATABASE_URL")
    if needs_database and settings.database_url is not None:
        if not role:
            raise ValueError("LMA_DATABASE_URL needs LMA_SITE_ROLE, the role every operation acts as")
        from dagents.common.infrastructure.postgres_lease import KeptConnection

        lease = KeptConnection(
            settings.database_url,
            name=f"site {site.site_id}",
            statement_timeout_ms=settings.statement_timeout_ms or DEFAULT_STATEMENT_TIMEOUT_MS,
        )
        closers.append(lease.close)

    source = deps.source
    if source is None:
        if settings.site_source is None:
            raise ValueError("an LMA started for a site needs a site source: set LMA_SITE_SOURCE")
        factory = registry.site_source(settings.site_source)
        source = factory(lease, role, site.site_id)
        closers.append(source.close)

    audit = deps.audit
    if audit is None:
        if lease is not None and settings.audit_table is not None:
            from dagents.common.infrastructure.postgres_audit import PostgresAuditSink

            audit = PostgresAuditSink(
                lease.lease,
                role,
                settings.audit_table,
                settings=lease.settings,
                anchor_table=settings.audit_anchor_table,
            )
        else:
            audit = InMemoryAuditLog()

    job_log = deps.job_log
    if job_log is None:
        if lease is not None and settings.job_log_table is not None:
            job_log = PostgresJobLog(lease, role, settings.job_log_table)
        else:
            job_log = InMemoryJobLog()

    network = deps.network
    if network is None and settings.gma_endpoint:
        network = AgentNetwork.over_http({GMA_AGENT: settings.gma_endpoint})
        closers.append(network.close)
    gma = GmaLink(network, settings.gma_token) if network is not None else None
    if gma is None:
        logger.warning(
            "LMA for site %s has no GMA: the model routes answer 503 and training jobs are refused", site.site_id
        )

    governance = build_governance_service(audit=audit, registry=registry, strict=True)
    site_service = SiteService(
        site,
        registry=registry,
        governance=governance,
        source=source,
        models=gma,
        max_staleness_ms=settings.source_max_staleness_ms,
    )
    jobs = FederationJobService(
        site_service,
        job_log=job_log,
        channel_factory=(lambda kind: HttpUpdateChannel(gma, kind)) if gma is not None else None,
    )
    return LmaRuntime(
        settings=settings,
        registry=registry,
        monitoring=build_monitoring_service(
            site_mode=True, statement_timeout_ms=settings.statement_timeout_ms or DEFAULT_STATEMENT_TIMEOUT_MS
        ),
        governance=governance,
        access=access,
        site=site_service,
        jobs=jobs,
        _closers=closers,
    )
