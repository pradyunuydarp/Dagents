"""The consortium: the coordinator of a governed federated pilot across three hospitals.

The framework makes every planning decision: which sites are eligible, whether
quorum holds, whether contributions may be aggregated, and whether a candidate
passes its release gates. This module supplies the study: which hospitals, which
condition, which gates, and the order of the rounds.

The coordinator never reads a hospital's records. Every number it shows comes
from what a hospital reports: its summary (counts, and an outcome rate over at
least the minimum cohort), its registration, and the results of the jobs it
accepted. Model updates reach the coordinator only through the runtime, after
each hospital's Guard has clipped them, and only their weighted sum is kept.

A pilot runs these rounds, in order:

1. analytics: every hospital profiles its records;
2. evaluation: every hospital measures the model in use, which gives the
   baseline a candidate must beat;
3. training, several rounds: every hospital trains the logistic regression from
   the previous round's version and sends its clipped update; the runtime
   applies the planner's weights and stores the next version;
4. candidate validation: every hospital measures the final version;
5. release: the Guard checks the release request, and the planner applies the
   release gates. A candidate that passes stays a candidate until a person
   approves it with :meth:`Consortium.approve`.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from agents.common.application.federation import FederatedRoundController
from agents.common.application.governance_service import GovernanceService
from agents.common.domain.federation import AggregationMethod, CandidateModel, RoundManifest, RoundPhase
from agents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest
from agents.common.infrastructure.charts import MAX_SERIES as MAX_CHART_SERIES

from app.domain import stroke_model
from app.domain.conditions import (
    STROKE_APPROVED_PURPOSES,
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT,
    STROKE_FEATURE_CONTRACT_VERSION,
    STROKE_FIELD_DOMAINS,
    STROKE_RELEASE_GATES,
    STROKE_STUDY_ID,
)
from app.domain.dataset import DATASET, DATASET_NOTICE, SITES, Site, UnknownSiteError, load_site_records
from app.services.hospital import DEFAULT_MAX_ENTERED, Hospital
from app.services.runtime import (
    INITIAL_VERSION,
    AggregationRefused,
    ModelRegistry,
    ParameterAveragingRuntime,
    RegisteredModel,
    code_digest,
)
from app.services.site_store import (
    CoordinatorStore,
    MemoryCoordinatorStore,
    MemorySiteStore,
    PostgresCoordinatorStore,
    PostgresSiteStore,
    SiteStore,
    UnknownModelError,
    site_database_url,
)

logger = logging.getLogger(__name__)

#: The study this demo runs.
STUDY_ID = STROKE_STUDY_ID

#: The privacy profile every manifest names.
PRIVACY_PROFILE = "stroke-consortium-profile-v1"

#: Fewest sites a round needs. Secure aggregation's threshold can raise it.
MINIMUM_PARTICIPANTS = 2

#: The capability each phase needs from a site.
PHASE_CAPABILITIES: dict[str, list[str]] = {
    "analytics": ["local_profiling"],
    "evaluation": ["local_evaluation"],
    "training": ["local_training"],
}

#: A round prefix: letters, digits, ``-`` and ``_``, starting with a letter or
#: digit, at most 40 characters.
ROUND_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

NOTICE = (
    "The records are real, de-identified patient records from the International Stroke Trial, "
    "recruited in 1991-1996. They are used here to show how the framework works. The seed rule and "
    "the federated model are demonstrations, not validated clinical models, and nothing here is "
    "medical advice."
)


class ApprovalRefused(RuntimeError):
    """A model cannot be approved for release."""


def build_site_store(site: Site, database_url: str, environ: Mapping[str, str] | None = None) -> SiteStore:
    """The store for one hospital.

    With a database URL (or the hospital's own
    ``HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL``), the hospital's schema in
    Postgres. Without one, an in-memory store loaded from the committed extract.
    """
    if database_url.strip() or site_database_url(site, "", environ):
        return PostgresSiteStore.from_environment(site, database_url.strip(), environ)
    return MemorySiteStore(site, load_site_records(site.site_id))


class Consortium:
    """The coordinator: hospitals, model registry, runtime and round controller.

    Params:
    - `sites`: the hospitals, in display order. Defaults to the three IST
      hospitals.
    - `database_url`: ``HEALTHCARE_DEMO_DATABASE_URL``. Empty means in-memory
      stores loaded from the committed extract.
    - `secure_aggregation_threshold`: sites below which secure aggregation must
      not reveal a contribution. It raises the quorum.
    - `max_entered_per_site`: most records each hospital's data-entry form adds.
    - `environ`: where per-site connection strings are read from (tests pass
      their own). Defaults to ``os.environ``.
    - `site_stores`, `coordinator_store`: stores to use instead of building
      them, for tests.

    Building a consortium reads no records and opens no connection.
    """

    def __init__(
        self,
        sites: Sequence[Site] | None = None,
        *,
        database_url: str = "",
        secure_aggregation_threshold: int = 3,
        max_entered_per_site: int = DEFAULT_MAX_ENTERED,
        environ: Mapping[str, str] | None = None,
        site_stores: Mapping[str, SiteStore] | None = None,
        coordinator_store: CoordinatorStore | None = None,
    ) -> None:
        self.sites: tuple[Site, ...] = tuple(sites or SITES)
        self._database_url = database_url.strip()
        self._environ = os.environ if environ is None else environ
        self._secure_threshold = secure_aggregation_threshold
        injected = dict(site_stores or {})

        if coordinator_store is not None:
            self.coordinator_store = coordinator_store
        elif self._database_url:
            self.coordinator_store = PostgresCoordinatorStore(self._database_url, self.sites)
        else:
            self.coordinator_store = MemoryCoordinatorStore()

        self.registry = ModelRegistry(self.coordinator_store)
        self.runtime = ParameterAveragingRuntime(self.registry)
        self.controller = FederatedRoundController(engine=self.runtime)
        # The release Guard's audit chain is kept in the coordinator store, so
        # it survives a restart like the release decisions it backs.
        self.governance = GovernanceService(audit=self.coordinator_store.audit_sink())
        self.governance.register_classification(STROKE_CLASSIFICATION)

        # A hospital's chart colour is its place in the consortium, as on the
        # consortium charts. More hospitals than colours means no hospital has one.
        slots = len(self.sites) <= MAX_CHART_SERIES
        self.hospitals: dict[str, Hospital] = {
            site.site_id: Hospital(
                site,
                store_factory=self._store_factory(site, injected.get(site.site_id)),
                registry=self.registry,
                contributions=self._contributions_factory(site),
                max_entered=max_entered_per_site,
                series_slot=position if slots else None,
            )
            for position, site in enumerate(self.sites, start=1)
        }
        self._lock = threading.RLock()
        self._pilot_lock = threading.Lock()
        self._last_pilot: dict[str, Any] | None = None

    def _store_factory(self, site: Site, injected: SiteStore | None) -> Callable[[], SiteStore]:
        if injected is not None:
            return lambda: injected
        return lambda: build_site_store(site, self._database_url, self._environ)

    def _contributions_factory(self, site: Site) -> Callable[[], CoordinatorStore]:
        """Where a hospital writes its round contributions.

        In Postgres, a hospital writes with its own connection string, acting as
        its own role. In memory, it writes to the coordinator store.
        """
        def factory() -> CoordinatorStore:
            if not isinstance(self.coordinator_store, PostgresCoordinatorStore):
                return self.coordinator_store
            dsn = site_database_url(site, self._database_url, self._environ)
            if dsn == self._database_url:
                return self.coordinator_store
            return PostgresCoordinatorStore(dsn, self.sites)

        return factory

    def close(self) -> None:
        """Release every store's database connection, for shutdown.

        Nothing else changes; a later operation opens a new connection.
        """
        for hospital in self.hospitals.values():
            hospital.close()
        self.coordinator_store.close()

    # --- Lookups -------------------------------------------------------------------

    def hospital(self, site_id: str) -> Hospital:
        """The hospital with this id. Raises ``UnknownSiteError`` (a ``KeyError``)."""
        hospital = self.hospitals.get(site_id)
        if hospital is None:
            raise UnknownSiteError(site_id)
        return hospital

    @property
    def secure_aggregation_threshold(self) -> int:
        return self._secure_threshold

    def model_in_use(self) -> dict[str, Any]:
        """The released model's version, kind, digest and status."""
        return self.registry.model_in_use().summary()

    def models(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Every registered model version, or the newest ``limit``, oldest first, without parameters."""
        return self.registry.list(limit)

    def last_pilot(self) -> dict[str, Any] | None:
        """The report of the most recent pilot, or ``None``.

        A pilot this process ran is kept in memory. Otherwise the newest report
        in the coordinator store is read, so the charts that show the last
        pilot survive a restart. The store is asked again on each call until it
        holds a report.
        """
        # Not under self._lock, which a running pilot holds for its whole run.
        pilot = self._last_pilot
        if pilot is not None:
            return pilot
        loaded = self.coordinator_store.latest_pilot_report()
        with self._pilot_lock:
            # A pilot that finished while the store was read keeps its report.
            if self._last_pilot is None:
                self._last_pilot = loaded
            return self._last_pilot

    def data_source_status(self) -> dict[str, Any]:
        """Where the hospitals' records are stored, and whether the stores are ready.

        Returns ``{cohort_source, store_layout, dataset, ready, hospitals}``:

        - ``hospitals``: ``[{site_id, record_count, labelled_count}]`` in
          display order, as each hospital's store counts them, or ``None`` when
          a store cannot be read (for example, a database that is not migrated).
        - ``ready``: every store could be read and holds at least one record.

        It never raises for a store that cannot be read: the status endpoint
        must answer while the stores do not. The error goes to the log, and the
        response names no host or user. Builds the stores if needed; a Postgres
        store opens no connection to report its kind.
        """
        kinds = sorted({hospital.store.kind for hospital in self.hospitals.values()})
        cohort_source: str | list[str] = kinds[0] if len(kinds) == 1 else kinds
        layout = {"postgres": "schema-per-site", "in-memory": "in-memory-per-site"}
        hospitals: list[dict[str, Any]] | None
        try:
            hospitals = []
            for site in self.sites:
                counts = self.hospitals[site.site_id].counts()
                hospitals.append(
                    {"site_id": site.site_id, "record_count": counts["total"], "labelled_count": counts["labelled"]}
                )
        except Exception as error:  # noqa: BLE001 - the status report must answer while a store does not
            logger.warning("The hospital stores could not be read for the status report: %s", error)
            hospitals = None
        return {
            "cohort_source": cohort_source,
            "store_layout": layout.get(kinds[0], kinds[0]) if len(kinds) == 1 else "mixed",
            "dataset": dict(DATASET),
            "ready": hospitals is not None and all(row["record_count"] > 0 for row in hospitals),
            "hospitals": hospitals,
        }

    # --- Overview ---------------------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        """The consortium view. Hospital numbers are what each hospital reports.

        ``field_labels`` maps every record field to the label the entry form
        uses, so every page names a field the same way.
        """
        model = self.registry.model_in_use()
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
            "engine": self.controller.engine_name,
            "secure_aggregation_threshold": self._secure_threshold,
            "minimum_cohort": STROKE_CLASSIFICATION.minimum_cohort,
            "current_model": {"version": model.version, "kind": model.kind},
            "hospitals": [self.hospitals[site.site_id].summary() for site in self.sites],
            "release_gates": [gate.model_dump(mode="json") for gate in STROKE_RELEASE_GATES],
        }

    # --- Rounds -----------------------------------------------------------------------

    def _refresh_registrations(self) -> None:
        """Enrol each hospital, or refresh the record count it reported.

        Runs before every manifest, so a round's cohort floor uses the count
        each hospital reports now, including entered records. A registration
        made explicitly, such as a site on an older contract, keeps its other
        fields.
        """
        known = {registration.site_id: registration for registration in self.controller.list_sites()}
        for site in self.sites:
            reported = self.hospitals[site.site_id].registration()
            existing = known.get(site.site_id)
            if existing is None:
                self.controller.register_site(reported)
            else:
                self.controller.register_site(existing.model_copy(update={"cohort_size": reported.cohort_size}))

    def manifest(
        self, round_id: str, phase: RoundPhase = "analytics", model: RegisteredModel | None = None
    ) -> RoundManifest:
        """The round contract every hospital verifies before it accepts.

        Params:
        - `model`: the model the round uses. Defaults to the model in use.

        The manifest carries the model's parameter digest and the digest of the
        code that runs it, so a hospital can check both. Secure aggregation is
        the default profile; its threshold raises the number of sites needed.
        """
        self._refresh_registrations()
        model = model or self.registry.model_in_use()
        return RoundManifest(
            round_id=round_id,
            study_id=STUDY_ID,
            condition_id=STROKE_CONDITION.condition_id,
            phase=phase,
            model_version=model.version,
            model_artifact_digest=model.digest,
            training_code_digest=code_digest(model.kind),
            feature_contract_version=STROKE_FEATURE_CONTRACT_VERSION,
            privacy_profile=PRIVACY_PROFILE,
            aggregation=AggregationMethod(kind="secure_aggregation", threshold=self._secure_threshold),
            minimum_participants=MINIMUM_PARTICIPANTS,
            minimum_cohort_per_site=STROKE_CLASSIFICATION.minimum_cohort,
            required_capabilities=PHASE_CAPABILITIES[phase],
            stop_conditions=["schema_failure", "quorum_not_met", "unsafe_metric"],
            invited_sites=sorted(self.hospitals),
        )

    def _attach_workers(self, manifest: RoundManifest) -> str:
        """Give every hospital the digest it verifies the job against."""
        digest = self.controller.digest(manifest)
        for hospital in self.hospitals.values():
            self.runtime.register_worker(hospital.worker(expected_digest=digest, update_channel=self.runtime))
        return digest

    def run_round(
        self, round_id: str, phase: RoundPhase = "analytics", model: RegisteredModel | None = None
    ) -> dict[str, Any]:
        """Plan, dispatch and collect one round, and report what happened.

        Returns ``{round_id, phase, model_version, digest, plan, results,
        readiness, status}``. ``results`` holds each hospital's result: counts,
        metrics, a norm and an update digest, never records or an update.
        """
        with self._lock:
            manifest = self.manifest(round_id, phase, model)
            digest = self._attach_workers(manifest)
            record = self.controller.dispatch_round(manifest)
            readiness = self.controller.evaluate_readiness(round_id) if record.plan.quorum_met else None
            return {
                "round_id": round_id,
                "phase": phase,
                "model_version": manifest.model_version,
                "model_artifact_digest": manifest.model_artifact_digest,
                "training_code_digest": manifest.training_code_digest,
                "digest": digest,
                "plan": record.plan.model_dump(mode="json"),
                "results": [result.model_dump(mode="json") for result in record.results],
                "readiness": readiness.model_dump(mode="json") if readiness else None,
                "status": record.status,
            }

    @staticmethod
    def _permitted(report: dict[str, Any] | None) -> bool:
        return bool(report and report["readiness"] and report["readiness"]["aggregation_permitted"])

    @staticmethod
    def _pilot_id(round_prefix: str | None) -> str:
        """A pilot id that no other pilot shares, even one started in the same second."""
        prefix = round_prefix or "pilot"
        if not ROUND_PREFIX.fullmatch(prefix):
            raise ValueError(
                "round_prefix must be 1-40 letters, digits, '-' or '_', starting with a letter or digit"
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{prefix}-{stamp}-{secrets.token_hex(3)}"

    def run_pilot(
        self, round_prefix: str | None = None, training_rounds: int = stroke_model.DEFAULT_ROUNDS
    ) -> dict[str, Any]:
        """Run the whole pilot and return its evidence.

        Params:
        - `round_prefix`: the start of every round id. A timestamp and a random
          suffix follow it, so two pilots never share a round id.
        - `training_rounds`: how many training rounds to run, 1-20.

        Returns ``{study_id, pilot_id, condition, engine, model_in_use, rounds
        {analytics, evaluation, training: [...], candidate_validation},
        training_curve, training_settings, candidate, release {guard, gates},
        baseline_metrics, stop_reason, site_audits, coordinator_audit, notice,
        dataset_notice}``. ``site_audits`` maps each hospital to
        :meth:`Hospital.audit_summary` and ``coordinator_audit`` is
        ``{chain_intact, record_count}``; no audit record is in the report.

        The report is saved in the coordinator store, so :meth:`last_pilot`
        returns it after a restart.
        """
        if isinstance(training_rounds, bool) or not isinstance(training_rounds, int) or not 1 <= training_rounds <= 20:
            raise ValueError("training_rounds must be a whole number from 1 to 20")
        # Pinned: the rounds load the same model versions about 60 times, and
        # each version is read from the store once. Every load still checks
        # the digests the round names.
        with self._lock, self.registry.pinned():
            return self._run_pilot(round_prefix, training_rounds)

    def _run_pilot(self, round_prefix: str | None, training_rounds: int) -> dict[str, Any]:
        pilot_id = self._pilot_id(round_prefix)
        model_in_use = self.registry.model_in_use()

        analytics = self.run_round(f"{pilot_id}-analytics", "analytics", model_in_use)

        # The model in use, measured at every hospital. Its size-weighted
        # metrics are the baseline a candidate has to beat.
        evaluation_id = f"{pilot_id}-evaluation"
        evaluation = self.run_round(evaluation_id, "evaluation", model_in_use)
        baseline: dict[str, float] = {}
        if self._permitted(evaluation):
            baseline = dict(self.controller.aggregate(evaluation_id).metrics)

        training: list[dict[str, Any]] = []
        curve: list[dict[str, Any]] = []
        stop_reason: str | None = None
        current = self.registry.ensure_initial()
        candidate_model: CandidateModel | None = None
        last_training_id: str | None = None
        for number in range(1, training_rounds + 1):
            round_id = f"{pilot_id}-training-{number}"
            # A round that is not aggregated still holds the updates the sites
            # sent. They are deleted whatever happens, so no site's single
            # clipped update stays in the coordinator's memory.
            try:
                report = self.run_round(round_id, "training", current)
                report["candidate_version"] = None
                training.append(report)
                if not self._permitted(report):
                    reason = (report["readiness"] or {}).get("stop_reason") or report["plan"].get("stop_reason")
                    stop_reason = f"training round {number} could not be aggregated: {_stop_reason_text(reason)}"
                    break
                try:
                    produced = self.controller.aggregate(round_id)
                except AggregationRefused as error:
                    report["aggregation_error"] = str(error)
                    stop_reason = f"the runtime refused to aggregate training round {number}: {error}"
                    break
            finally:
                self.runtime.discard(round_id)
            report["candidate_version"] = produced.candidate_version
            curve.append(self._curve_point(number, report, produced))
            # The version this round started from is replaced by its successor.
            self.registry.set_status(current.version, "superseded")
            successor = self.registry.get(produced.candidate_version)
            if successor is None:  # pragma: no cover - the runtime registered it
                raise AggregationRefused(f"{produced.candidate_version} is not in the registry")
            current = successor
            candidate_model = produced
            last_training_id = round_id

        validation = None
        release = None
        candidate = None
        if candidate_model is not None and last_training_id is not None:
            validation_id = f"{pilot_id}-candidate-validation"
            validation = self.run_round(validation_id, "evaluation", current)
            if self._permitted(validation):
                validated = self.controller.aggregate(validation_id)
                candidate_model = candidate_model.model_copy(update={"metrics": dict(validated.metrics)})
            else:
                # Without validation there are no gate metrics, so every
                # gate is not evaluated and the candidate is rejected.
                candidate_model = candidate_model.model_copy(update={"metrics": {}})

            guard = self.release_guard_decision(last_training_id, candidate_model)
            gates = self.controller.evaluate_release(
                last_training_id,
                STROKE_RELEASE_GATES,
                candidate_model,
                baseline_metrics=baseline,
                rollback_version=model_in_use.version,
            ).model_dump(mode="json")
            # Stored with the model, so approve() can check it after a restart.
            stored = self.registry.record_release_decision(
                candidate_model.candidate_version,
                action=gates["action"],
                guard_permitted=bool(guard["permitted"]),
                gate_results=gates["gate_results"],
                round_id=last_training_id,
                baseline_version=model_in_use.version,
            )
            if gates["action"] != "release" or not guard["permitted"]:
                stored = self.registry.set_status(candidate_model.candidate_version, "rejected")
            release = {"guard": guard, "gates": gates}
            candidate = {**candidate_model.model_dump(mode="json"), "status": stored.status}
        self._fill_curve_losses(curve, candidate_model.metrics.get("log_loss") if candidate_model else None)

        report = {
            "study_id": STUDY_ID,
            "pilot_id": pilot_id,
            "condition": STROKE_CONDITION.model_dump(mode="json"),
            "engine": self.controller.engine_name,
            "model_in_use": model_in_use.summary(),
            "rounds": {
                "analytics": analytics,
                "evaluation": evaluation,
                "training": training,
                "candidate_validation": validation,
            },
            "training_curve": curve,
            "training_settings": {
                "rounds": training_rounds,
                "epochs": stroke_model.DEFAULT_EPOCHS,
                "learning_rate": stroke_model.DEFAULT_LEARNING_RATE,
                "l2": stroke_model.DEFAULT_L2,
                "initial_version": INITIAL_VERSION,
            },
            "candidate": candidate,
            "release": release,
            "baseline_metrics": baseline,
            "stop_reason": stop_reason,
            # Each hospital's audit records stay at the hospital. The coordinator
            # sees whether each chain verifies and how many decisions it holds.
            "site_audits": {site.site_id: self.hospitals[site.site_id].audit_summary() for site in self.sites},
            "coordinator_audit": {
                "chain_intact": self.governance.audit_chain_intact(),
                "record_count": self.governance.audit_record_count(),
            },
            "notice": NOTICE,
            "dataset_notice": DATASET_NOTICE,
        }
        # Stored so the charts that show the last pilot survive a restart.
        self.coordinator_store.save_pilot_report(pilot_id, report)
        with self._pilot_lock:
            self._last_pilot = report
        return report

    @staticmethod
    def _curve_point(number: int, report: dict[str, Any], produced: CandidateModel) -> dict[str, Any]:
        """One training round for the curve. ``loss`` is filled in later.

        - ``start_loss``: the loss of the version the round started from.
        - ``local_loss``: the loss after each site's local training.
        - ``loss``: the loss of the version the round produced. The next round
          measures it as its ``start_loss``, and candidate validation measures
          it for the last round.

        Each is the mean over sites, weighted by the planner's weights, of the
        log loss on the site's labelled records.
        """
        accepted = set(produced.contributing_sites)
        norms = {
            result["site_id"]: result["update_norm"]
            for result in report["results"]
            if result["site_id"] in accepted and result["update_norm"] is not None
        }
        return {
            "round": number,
            "round_id": report["round_id"],
            "loss": None,
            "start_loss": produced.metrics.get("start_loss"),
            "local_loss": produced.metrics.get("train_loss"),
            "update_norms": norms,
            "max_update_norm": max(norms.values()) if norms else None,
            "contributing_sites": sorted(accepted),
            "contributed_examples": produced.contributed_examples,
            "candidate_version": produced.candidate_version,
        }

    @staticmethod
    def _fill_curve_losses(curve: list[dict[str, Any]], final_loss: float | None) -> None:
        for point, following in zip(curve, curve[1:], strict=False):
            point["loss"] = following["start_loss"]
        if curve:
            curve[-1]["loss"] = final_loss

    def release_guard_decision(self, round_id: str, candidate: CandidateModel) -> dict[str, Any]:
        """Run the Guard at the release boundary before the gates are applied.

        The cohort is the number of labelled records that trained the candidate,
        as the planner accepted them. The payload is the size of the candidate's
        change from the model training started from.
        """
        requester = Requester(
            requester_id="stroke-consortium-release-committee",
            requester_kind="human",
            affiliation="stroke-triage-consortium",
            stated_purpose="stroke_triage_research",
            compliance_history=0.95,
            attributes=[
                KyuAttribute(attribute_id="verified_committee_identity", verified=True),
                KyuAttribute(attribute_id="signed_study_protocol", verified=True),
                KyuAttribute(attribute_id="governance_training", verified=True),
            ],
        )
        model = self.registry.get(candidate.candidate_version)
        start = self.registry.get(INITIAL_VERSION)
        change = (
            stroke_model.l2_norm(stroke_model.delta(model.params, start.params))
            if model is not None and start is not None
            else 0.0
        )
        decision = self.governance.enforce(
            RestrictionRequest(
                request_id=f"{round_id}:release:{candidate.candidate_version}",
                boundary="before_release",
                requester=requester,
                classification=STROKE_CLASSIFICATION,
                requested_fields=["model_update"],
                granularity="model_update",
                cohort_size=candidate.contributed_examples,
                declared_purpose="stroke_triage_research",
                approved_purposes=STROKE_APPROVED_PURPOSES,
            ),
            [{"model_update": change}],
            correlation_id=round_id,
        )
        return decision.model_dump(mode="json")

    def approve(self, version: str) -> dict[str, Any]:
        """Release a candidate, as the person who approves releases.

        Allowed only when the latest release decision for this version was
        ``release``, its release Guard check was permitted, the version is
        still a candidate, and the model its gates compared it with is still
        the model in use. A candidate checked against an older model must be
        tested again by a new pilot. The released model replaces the previous
        one, which becomes ``superseded``; hospitals then score with it.

        Raises ``UnknownModelError`` for an unknown version and
        :class:`ApprovalRefused` otherwise. The release decision is read from
        the coordinator store, where the pilot stored it with the model, so an
        approval works after a restart.
        """
        with self._lock:
            model = self.registry.get(version)
            if model is None:
                raise UnknownModelError(version)
            decision = model.release_decision
            if decision is None:
                raise ApprovalRefused(f"{version} has no release decision; only a pilot's candidate can be approved")
            if decision["action"] != "release":
                raise ApprovalRefused(f"the release gates decided {decision['action']} for {version}")
            if not decision["guard_permitted"]:
                raise ApprovalRefused(f"the release Guard did not permit {version}")
            if model.status != "candidate":
                raise ApprovalRefused(f"{version} is {model.status}, not a candidate")
            previous = self.registry.model_in_use()
            baseline = decision.get("baseline_version")
            if baseline != previous.version:
                checked = (
                    f"was checked against {baseline}"
                    if baseline
                    else "has no record of the model it was checked against"
                )
                raise ApprovalRefused(f"{version} {checked}, but {previous.version} is now in use. Run the pilot again.")
            released = self.registry.set_status(version, "released")
            return {"released": released.summary(), "previous": previous.version}


#: Plain words for the planner's stop reasons. The planner decides; this only
#: words its decision for people.
_STOP_REASON_TEXT = {
    "quorum_not_met": "fewer hospitals took part than the round needs",
}


def _stop_reason_text(reason: str | None) -> str:
    """Word a planner stop reason, keeping an unknown one readable."""
    if not reason:
        return _STOP_REASON_TEXT["quorum_not_met"]
    return _STOP_REASON_TEXT.get(reason, reason.replace("_", " "))
