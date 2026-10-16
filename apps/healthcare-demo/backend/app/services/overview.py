"""The coordinator's pages: the consortium overview, the models, approvals and the status report.

The study operator reads what the hospitals report through the GMA
(``GET /api/v1/federation/sites/summaries``): each hospital's LMA counts its
records and decides whether its poor outcome rate may leave. The GMA keeps the
model registry, so the model list and approvals are GMA calls too. The app
calls the GMA with its ``admin`` token and never reads a hospital's records.

Two numbers on a hospital's row come from the app at that hospital instead:
how many records were entered through the demo's form, and where its store is.
The app counts them in the hospital's own store, as the hospital's role,
because the LMA does not know which records were entered.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from agents.common.domain.site import SiteDirectoryEntry

from app.domain.conditions import (
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT,
    STROKE_FIELD_DOMAINS,
    STROKE_RELEASE_GATES,
)
from app.domain.dataset import DATASET, Site
from app.models import AgentsStatus
from app.services.agents import AgentError, loggable
from app.services.hospital_views import HospitalView, hospital_row
from app.services.pilot import STUDY_ID, version_summary
from app.services.site_store import CoordinatorStore

if TYPE_CHECKING:  # pragma: no cover - for annotations only
    from app.services.agents import Agents

logger = logging.getLogger(__name__)

#: Seconds an agent health check is reused. The status endpoint is called on
#: every page load.
STATUS_CACHE_SECONDS = 15.0

#: Seconds the status report waits for the GMA's site summaries. The GMA waits
#: up to 10 seconds for each LMA (``GMA_SUMMARY_TIMEOUT_SECONDS``), so a frozen
#: LMA still gives an answer in time; a frozen GMA gives "not reported".
STATUS_REPORT_TIMEOUT_SECONDS = 15.0

#: How the store layout is named for each kind of store.
_STORE_LAYOUT = {"postgres": "schema-per-site", "in-memory": "in-memory-per-site"}


class UnknownModelVersion(LookupError):
    """The GMA's registry holds no model version with this name. ``args[0]`` is the name."""


class ApprovalRefused(RuntimeError):
    """The GMA refused to release a model version. The message is the GMA's reason."""


class SiteReportMissing(RuntimeError):
    """A hospital's LMA answered without its summary, for example because it could not read its store."""


class SiteUnreachable(RuntimeError):
    """A hospital's LMA did not answer the GMA."""


class StudyOverview:
    """What the coordinator's pages show, read through the GMA.

    Params:
    - `sites`: the hospitals, in display order.
    - `views`: each hospital's view, for the entered count and the store's
      location, which the app counts at the hospital.
    - `agents`: how the app reaches the agents. The overview calls the GMA,
      and each agent's health route for the status report.
    - `coordinator_store`: where the pilot reports are stored.
    - `secure_aggregation_threshold`: shown with the study.
    - `status_timeout_seconds`: how long an agent's health check may take.
    - `models_ready`: called before anything that reads the model versions,
      so the GMA holds the app's versions first. The status report does not
      call it.
    """

    def __init__(
        self,
        sites: Sequence[Site],
        views: Mapping[str, HospitalView],
        agents: "Agents",
        coordinator_store: CoordinatorStore,
        *,
        secure_aggregation_threshold: int = 3,
        status_timeout_seconds: float = 2.0,
        models_ready: Callable[[], None] | None = None,
    ) -> None:
        self.sites = tuple(sites)
        self._views = dict(views)
        self._agents = agents
        self._store = coordinator_store
        self._secure_threshold = secure_aggregation_threshold
        self._status_timeout = status_timeout_seconds
        self._models_ready = models_ready
        self._lock = threading.Lock()
        self._last_pilot: dict[str, Any] | None = None
        self._engine: str | None = None
        self._services: tuple[float, list[dict[str, str]]] | None = None

    # --- What the hospitals report --------------------------------------------------------

    def _site_entries(self) -> dict[str, SiteDirectoryEntry]:
        return {entry.site_id: entry for entry in self._agents.gma().site_summaries()}

    def hospital_rows(self) -> list[dict[str, Any]]:
        """Each hospital's row, in display order: what its LMA reports, plus its entered count and store.

        Raises :class:`SiteUnreachable` when a hospital's LMA did not answer,
        and :class:`SiteReportMissing` when it answered without a summary. A
        store that cannot be read raises its own error first.
        """
        entries = self._site_entries()
        rows = []
        for site in self.sites:
            view = self._views[site.site_id]
            entered = view.counts()["entered"]
            entry = entries.get(site.site_id)
            if entry is None or not entry.reachable:
                raise SiteUnreachable(f"the LMA of {site.site_id} did not answer")
            if entry.summary is None:
                raise SiteReportMissing(f"the LMA of {site.site_id} sent no summary")
            rows.append(hospital_row(site, entry.summary, entered, view.store))
        return rows

    # --- Models ------------------------------------------------------------------------------

    def _need_models(self) -> None:
        if self._models_ready is not None:
            self._models_ready()

    def model_in_use(self) -> dict[str, Any]:
        """The released model's version, kind, digest, status, parent and release decision, from the GMA."""
        self._need_models()
        listing = self._agents.gma().list_model_versions(1)
        if listing.model_in_use is None:
            raise UnknownModelVersion("no model is released")
        return version_summary(listing.model_in_use)

    def models(self, limit: int) -> dict[str, Any]:
        """The model in use and the ``limit`` newest versions, oldest first, without parameters."""
        self._need_models()
        listing = self._agents.gma().list_model_versions(limit)
        return {
            "model_in_use": version_summary(listing.model_in_use) if listing.model_in_use is not None else None,
            "models": [version_summary(version) for version in reversed(listing.versions)],
        }

    def approve(self, version: str) -> dict[str, Any]:
        """Release a candidate, as the person who approves releases.

        The GMA's lifecycle rule decides: the version's last release decision
        must be ``release``, its release Guard check permitted, its metrics
        from the rounds, the version still a candidate, and the model its gates
        compared it with still the model in use. Returns ``{released,
        previous}``.

        Raises :class:`UnknownModelVersion` (404) and :class:`ApprovalRefused`
        (409) with the GMA's reason.
        """
        self._need_models()
        try:
            result = self._agents.gma().approve(version)
        except AgentError as error:
            if error.status in (404, 422):
                raise UnknownModelVersion(version) from None
            if error.status == 409:
                raise ApprovalRefused(error.detail) from None
            raise
        return {"released": version_summary(result.released), "previous": result.previous}

    # --- Pilots --------------------------------------------------------------------------------

    def remember_pilot(self, report: dict[str, Any]) -> None:
        """Keep the newest pilot report in memory, for the charts."""
        with self._lock:
            self._last_pilot = report

    def last_pilot(self) -> dict[str, Any] | None:
        """The report of the most recent pilot, or ``None``.

        A pilot this process ran is kept in memory. Otherwise the newest report
        in the coordinator store is read, so the charts that show the last
        pilot survive a restart. The store is asked again on each call until it
        holds a report.
        """
        with self._lock:
            pilot = self._last_pilot
        if pilot is not None:
            return pilot
        loaded = self._store.latest_pilot_report()
        with self._lock:
            # A pilot that finished while the store was read keeps its report.
            if self._last_pilot is None:
                self._last_pilot = loaded
            return self._last_pilot

    # --- The overview ----------------------------------------------------------------------------

    def engine(self, timeout_s: float | None = None) -> str | None:
        """The name of the GMA's round engine, from its health route. ``None`` while the GMA does not answer.

        The name is kept after the first answer. ``timeout_s`` bounds the
        health call; without it, the agent network's timeout applies.
        """
        with self._lock:
            if self._engine is not None:
                return self._engine
        try:
            engine = self._agents.gma().health(timeout_s=timeout_s).get("engine")
        except AgentError as error:
            logger.warning("the GMA did not report its engine: %s", loggable(error))
            return None
        if isinstance(engine, str) and engine:
            with self._lock:
                self._engine = engine
            return engine
        return None

    def overview(self) -> dict[str, Any]:
        """The consortium view. Hospital numbers are what each hospital reports.

        ``field_labels`` maps every record field to the label the entry form
        uses, so every page names a field the same way.
        """
        hospitals = self.hospital_rows()
        model = self.model_in_use()
        return {
            "study_id": STUDY_ID,
            "condition": {
                "condition_id": STROKE_CONDITION.condition_id,
                "display_name": STROKE_CONDITION.display_name,
                "intended_use": STROKE_CONDITION.intended_use,
                "limitations": list(STROKE_CONDITION.limitations),
            },
            "dataset": dict(DATASET),
            "feature_contract": {
                "contract_id": STROKE_FEATURE_CONTRACT.contract_id,
                "version": STROKE_FEATURE_CONTRACT.version,
                "required_fields": STROKE_FEATURE_CONTRACT.required_fields(),
            },
            "field_labels": {name: domain.label for name, domain in STROKE_FIELD_DOMAINS.items()},
            "engine": self.engine(),
            "secure_aggregation_threshold": self._secure_threshold,
            "minimum_cohort": STROKE_CLASSIFICATION.minimum_cohort,
            "current_model": {"version": model["version"], "kind": model["kind"]},
            "hospitals": hospitals,
            "release_gates": [gate.model_dump(mode="json") for gate in STROKE_RELEASE_GATES],
        }

    def extension(self) -> dict[str, Any]:
        """What the agents' extension registry holds, as the GMA describes it (``GET /api/v1/extensions``)."""
        return self._agents.gma().request("GET", "/api/v1/extensions").body

    # --- The status report -------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Where the records are stored, whether the stores are ready, and how the agents run.

        Returns ``{cohort_source, store_layout, dataset, ready, hospitals,
        agents, services}``:

        - ``cohort_source``: the kind of store each hospital's LMA reports it
          reads (``postgres`` or ``in-memory``), or a list when they differ.
          For an LMA that does not answer, the kind of the app's store for
          that hospital.
        - ``hospitals``: ``[{site_id, record_count, labelled_count}]`` in display
          order, as each LMA counts them, or ``None`` when one could not be read.
        - ``ready``: every hospital reported, and each store holds a record.
        - ``agents``: ``{mode, engine, lmas}``. ``mode`` is ``in-process`` or
          ``network``; ``lmas`` is ``[{site_id, reachable, source_kind}]`` from
          the GMA, or ``None`` when the GMA did not answer.
        - ``services``: each agent's health check.

        It never raises: the status endpoint must answer while the stores and
        the agents do not. Errors go to the log, and nothing here names a host,
        a user or a token.
        """
        entries: dict[str, SiteDirectoryEntry] | None
        try:
            summaries = self._agents.gma().site_summaries(timeout_s=STATUS_REPORT_TIMEOUT_SECONDS)
            entries = {entry.site_id: entry for entry in summaries}
        except AgentError as error:
            logger.warning("the GMA could not report the hospitals for the status report: %s", loggable(error))
            entries = None

        hospitals: list[dict[str, Any]] | None = None
        if entries is not None:
            hospitals = []
            for site in self.sites:
                entry = entries.get(site.site_id)
                if entry is None or entry.summary is None:
                    logger.warning(
                        "the LMA of %s sent no summary for the status report (it %s)",
                        site.site_id,
                        "answered with an error" if entry is not None and entry.reachable else "did not answer",
                    )
                    hospitals = None
                    break
                hospitals.append(
                    {
                        "site_id": site.site_id,
                        "record_count": entry.summary.record_count,
                        "labelled_count": entry.summary.labelled_count,
                    }
                )

        reported = entries or {}
        kinds = sorted(
            {
                (reported[site.site_id].source_kind if site.site_id in reported else None)
                or self._views[site.site_id].store.kind
                for site in self.sites
            }
        )
        lmas = None
        if entries is not None:
            lmas = [
                {
                    "site_id": site.site_id,
                    "reachable": site.site_id in entries and entries[site.site_id].reachable,
                    "source_kind": entries[site.site_id].source_kind if site.site_id in entries else None,
                }
                for site in self.sites
            ]
        return {
            "cohort_source": kinds[0] if len(kinds) == 1 else kinds,
            "store_layout": _STORE_LAYOUT.get(kinds[0], kinds[0]) if len(kinds) == 1 else "mixed",
            "dataset": dict(DATASET),
            "ready": hospitals is not None and all(row["record_count"] > 0 for row in hospitals),
            "hospitals": hospitals,
            "agents": AgentsStatus(
                mode=self._agents.mode, engine=self.engine(self._status_timeout), lmas=lmas
            ).model_dump(mode="json"),
            "services": self.service_status(),
        }

    def service_status(self) -> list[dict[str, str]]:
        """Which agents answer their health check: ``[{name, status, detail}]``.

        ``status`` is ``ok`` or ``unavailable``. The checks run at the same
        time, and the result is reused for :data:`STATUS_CACHE_SECONDS`. No
        address is returned.
        """
        with self._lock:
            if self._services is not None and time.monotonic() - self._services[0] < STATUS_CACHE_SECONDS:
                return [dict(entry) for entry in self._services[1]]
        statuses = self._agents.health_checks(self._status_timeout)
        with self._lock:
            self._services = (time.monotonic(), statuses)
        return [dict(entry) for entry in statuses]


__all__ = [
    "ApprovalRefused",
    "STATUS_CACHE_SECONDS",
    "STATUS_REPORT_TIMEOUT_SECONDS",
    "SiteReportMissing",
    "SiteUnreachable",
    "StudyOverview",
    "UnknownModelVersion",
]
