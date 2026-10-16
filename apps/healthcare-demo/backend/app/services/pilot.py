"""The study operator: the pilot's rounds, run as calls to the Dagents GMA.

The GMA (global monitoring agent) coordinates the study. It enrols the
hospitals' LMAs, sends each round's job to them over HTTP, keeps their clipped
updates until it combines them, keeps the model registry, and runs the release
Guard and the release gates. The app calls only the GMA, with its ``admin``
token. It never reads a hospital's records.

What the app keeps is the study itself: the order of the rounds, the values in
each round manifest, the training settings, the round-prefix rule, who asks for
a release, how the report is put together, and where it is stored.

A pilot runs these rounds, in order:

1. analytics: every hospital profiles its records;
2. evaluation: every hospital measures the model in use, which gives the
   baseline a candidate must beat;
3. training, several rounds: every hospital trains the logistic regression from
   the previous round's version and sends its clipped update to the GMA, which
   combines the updates with the planner's weights into the next version;
4. candidate validation: every hospital measures the final version;
5. release: the GMA runs the release Guard and the release gates on the
   validation and evaluation rounds. A candidate that passes stays a candidate
   until a person approves it.

As GMA calls: ``POST /api/v1/federation/sites:refresh``, the released model,
the GMA's health (for its engine name), then for each round
``POST /api/v1/federation/rounds``, which answers with the planner's readiness
once every result is in, and, when permitted,
``POST .../rounds/{round_id}:aggregate``. Aggregation deletes the round's
updates; a training round that was not aggregated is discarded with
``POST .../rounds/{round_id}:discard``, so no hospital's update stays stored.
Then ``POST .../rounds/{round_id}:evaluate-release`` and the audit summaries.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Sequence

from dagents.common.domain.federation import (
    AggregationMethod,
    CandidateModel,
    DispatchCall,
    ModelVersion,
    ReleaseEvaluationRequest,
    RoundManifest,
    RoundPhase,
    RoundPlan,
)
from dagents.common.domain.governance import KyuAttribute, Requester

from app.domain import stroke_model
from app.domain.conditions import (
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT_VERSION,
    STROKE_STUDY_ID,
)
from app.domain.dataset import DATASET_NOTICE, Site
from app.domain.model_kinds import INITIAL_VERSION, SEED_VERSION, kind_for, startup_registrations
from app.services.agents import AgentError, loggable
from app.services.site_store import CoordinatorStore

if TYPE_CHECKING:  # pragma: no cover - for annotations only
    from app.services.agents import Agents

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

#: When the planner stops a round early.
STOP_CONDITIONS = ["schema_failure", "quorum_not_met", "unsafe_metric"]

#: A round prefix: letters, digits, ``-`` and ``_``, starting with a letter or
#: digit, at most 40 characters.
ROUND_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")

#: Why the release is asked for. It must be one of the condition pack's
#: approved purposes.
RELEASE_PURPOSE = "stroke_triage_research"

#: The reason a training round is discarded with when it ends. The GMA deletes
#: any update it still holds for the round.
TRAINING_ROUND_ENDED = "the training round ended"

NOTICE = (
    "The records are real, de-identified patient records from the International Stroke Trial, "
    "recruited in 1991-1996. They are used here to show how the framework works. The seed rule and "
    "the federated model are demonstrations, not validated clinical models, and nothing here is "
    "medical advice."
)

#: The LMA's generic metric names, in the app's words. A hospital's analytics
#: result reports the share of labelled records with a poor outcome as
#: ``label_rate``; the report has always called it ``outcome_rate``.
_METRIC_NAMES = {"label_rate": "outcome_rate"}


def _result(result: Any) -> dict[str, Any]:
    body = result.model_dump(mode="json")
    metrics = body.get("metrics")
    if isinstance(metrics, dict):
        body["metrics"] = {_METRIC_NAMES.get(name, name): value for name, value in metrics.items()}
    return body


#: Plain words for the planner's stop reasons. The planner decides; this only
#: words its decision for people.
_STOP_REASON_TEXT = {
    "quorum_not_met": "fewer hospitals took part than the round needs",
}


def stop_reason_text(reason: str | None) -> str:
    """Word a planner stop reason, keeping an unknown one readable."""
    if not reason:
        return _STOP_REASON_TEXT["quorum_not_met"]
    return _STOP_REASON_TEXT.get(reason, reason.replace("_", " "))


def release_committee() -> Requester:
    """Who asks for a release: the study's release committee, as the release Guard assesses it."""
    return Requester(
        requester_id="stroke-consortium-release-committee",
        requester_kind="human",
        affiliation="stroke-triage-consortium",
        stated_purpose=RELEASE_PURPOSE,
        compliance_history=0.95,
        attributes=[
            KyuAttribute(attribute_id="verified_committee_identity", verified=True),
            KyuAttribute(attribute_id="signed_study_protocol", verified=True),
            KyuAttribute(attribute_id="governance_training", verified=True),
        ],
    )


def version_summary(model: ModelVersion) -> dict[str, Any]:
    """A model version as the app's responses show it: everything except the parameters."""
    return model.model_dump(mode="json", exclude={"params"})


@dataclass(frozen=True)
class ModelRef:
    """The model a round runs: its version, kind and parameter digest."""

    version: str
    kind: str
    digest: str


class PilotService:
    """Runs the federated pilot through the GMA.

    Params:
    - `sites`: the hospitals, in display order. Every one is invited to every
      round.
    - `agents`: how the app reaches the agents. The service calls only the GMA.
    - `coordinator_store`: where the pilot report is stored.
    - `secure_aggregation_threshold`: sites below which secure aggregation must
      not reveal a contribution. It raises the quorum.
    - `on_report`: called with each finished report, so the charts show it.
    - `note_dispatch`: called with the calls the GMA made to the LMAs for a
      round, so a response can list them.
    - `models_ready`: called before a pilot starts, so the GMA holds the app's
      model versions. Without it, the pilot calls :meth:`register_models`.
    """

    def __init__(
        self,
        sites: Sequence[Site],
        agents: "Agents",
        coordinator_store: CoordinatorStore,
        *,
        secure_aggregation_threshold: int = 3,
        on_report: Callable[[dict[str, Any]], None] | None = None,
        note_dispatch: Callable[[Sequence[DispatchCall]], None] | None = None,
        models_ready: Callable[[], None] | None = None,
    ) -> None:
        self.sites = tuple(sites)
        self._agents = agents
        self._store = coordinator_store
        self._secure_threshold = secure_aggregation_threshold
        self._on_report = on_report
        self._note_dispatch = note_dispatch
        self._models_ready = models_ready

    def set_sites(self, sites: Sequence[Site]) -> None:
        """Invite these hospitals to every round from now on, as after a hospital joined.

        The pilot and a join never run at the same time (the API's flight), so
        a round's invited hospitals do not change while it runs.
        """
        self.sites = tuple(sites)

    def _need_models(self) -> None:
        if self._models_ready is not None:
            self._models_ready()
        else:
            self.register_models()

    # --- Model versions ------------------------------------------------------------

    def register_models(self) -> None:
        """Register the seed rule (asked for as released) and the all-zero logistic regression with the GMA.

        ``PUT /api/v1/model-versions/{version}`` changes nothing for a version
        already stored with the same content, so this runs at every start. When
        another version is already released, the GMA refuses to release the
        seed rule again (409), and the app goes on with the version released.
        """
        gma = self._agents.gma()
        for version, registration in startup_registrations():
            try:
                gma.register_model_version(version, registration)
            except AgentError as error:
                if version == SEED_VERSION and error.status == 409 and gma.list_model_versions(1).model_in_use:
                    logger.info("a model version is already released, so %s is not registered again", version)
                    continue
                raise

    # --- Rounds ----------------------------------------------------------------------

    def manifest(self, round_id: str, phase: RoundPhase, model: ModelRef) -> RoundManifest:
        """The round contract every hospital verifies before it accepts.

        The manifest carries the model's parameter digest and the digest of the
        code its model kind runs, so a hospital can check both. Secure
        aggregation is the default profile; its threshold raises the number of
        sites needed.
        """
        return RoundManifest(
            round_id=round_id,
            study_id=STUDY_ID,
            condition_id=STROKE_CONDITION.condition_id,
            phase=phase,
            model_version=model.version,
            model_artifact_digest=model.digest,
            training_code_digest=kind_for(model.kind).code_digest(),
            feature_contract_version=STROKE_FEATURE_CONTRACT_VERSION,
            privacy_profile=PRIVACY_PROFILE,
            aggregation=AggregationMethod(kind="secure_aggregation", threshold=self._secure_threshold),
            minimum_participants=MINIMUM_PARTICIPANTS,
            minimum_cohort_per_site=STROKE_CLASSIFICATION.minimum_cohort,
            required_capabilities=PHASE_CAPABILITIES[phase],
            stop_conditions=list(STOP_CONDITIONS),
            invited_sites=sorted(site.site_id for site in self.sites),
        )

    def preview_round(self, round_id: str) -> RoundPlan:
        """Which hospitals the GMA's planner would select for an analytics round now, with the model in use.

        ``POST /api/v1/federation/rounds:plan`` stores and dispatches nothing,
        so ``round_id`` can be any id. The planner decides; the app only reads
        its answer.
        """
        self._need_models()
        gma = self._agents.gma()
        released = gma.released_model()
        if released is None:  # pragma: no cover - only with a known digest
            raise RuntimeError("the GMA sent no released model")
        return gma.plan_round(self.manifest(round_id, "analytics", ModelRef(released.version, released.kind, released.digest)))

    def run_round(self, round_id: str, phase: RoundPhase, model: ModelRef) -> dict[str, Any]:
        """Dispatch one round through the GMA and report what happened.

        Returns ``{round_id, phase, model_version, model_artifact_digest,
        training_code_digest, digest, plan, results, readiness, status,
        acceptances, dispatch}``. ``results`` holds each hospital's result:
        counts, metrics, a norm and an update digest, never records or an
        update. ``dispatch`` lists the calls the GMA made to each LMA.
        """
        gma = self._agents.gma()
        manifest = self.manifest(round_id, phase, model)
        record = gma.dispatch_round(manifest)
        if self._note_dispatch is not None:
            self._note_dispatch(record.dispatch)
        # The GMA's HTTP engine collects every result while it dispatches, so the
        # round comes back with the planner's readiness; only another engine
        # needs the separate call.
        readiness = record.readiness
        if readiness is None and record.plan.quorum_met:
            readiness = gma.readiness(round_id)
        return {
            "round_id": round_id,
            "phase": phase,
            "model_version": manifest.model_version,
            "model_artifact_digest": manifest.model_artifact_digest,
            "training_code_digest": manifest.training_code_digest,
            "digest": record.plan.round_digest,
            "plan": record.plan.model_dump(mode="json"),
            "results": [_result(result) for result in record.results],
            "readiness": readiness.model_dump(mode="json") if readiness else None,
            "status": record.status,
            "acceptances": [acceptance.model_dump(mode="json") for acceptance in record.acceptances],
            "dispatch": [call.model_dump(mode="json") for call in record.dispatch],
        }

    @staticmethod
    def _permitted(report: dict[str, Any] | None) -> bool:
        return bool(report and report["readiness"] and report["readiness"]["aggregation_permitted"])

    @staticmethod
    def pilot_id(round_prefix: str | None) -> str:
        """A pilot id that no other pilot shares, even one started in the same second."""
        prefix = round_prefix or "pilot"
        if not ROUND_PREFIX.fullmatch(prefix):
            raise ValueError("round_prefix must be 1-40 letters, digits, '-' or '_', starting with a letter or digit")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return f"{prefix}-{stamp}-{secrets.token_hex(3)}"

    def _discard(self, round_id: str) -> None:
        """Ask the GMA to delete what it holds for a training round. A failure is logged, not raised."""
        try:
            self._agents.gma().discard_round(round_id, TRAINING_ROUND_ENDED)
        except AgentError as error:
            logger.warning("the GMA could not discard round %s: %s", round_id, loggable(error))

    # --- The pilot ---------------------------------------------------------------------

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
        training_curve, training_settings, candidate, release {guard, gates,
        metrics_source, baseline_version}, baseline_metrics, stop_reason,
        site_audits, coordinator_audit, notice, dataset_notice}``.
        ``site_audits`` maps each hospital to ``{site_id, chain_intact,
        record_count}`` as its LMA reports it through the GMA, and
        ``coordinator_audit`` is the GMA's ``{chain_intact, record_count}``; no
        audit record is in the report.

        The report is saved in the coordinator store, so the charts that show
        the last pilot survive a restart.
        """
        if isinstance(training_rounds, bool) or not isinstance(training_rounds, int) or not 1 <= training_rounds <= 20:
            raise ValueError("training_rounds must be a whole number from 1 to 20")
        pilot_id = self.pilot_id(round_prefix)
        self._need_models()
        gma = self._agents.gma()

        # Enrolment, once per pilot: each LMA reports its current cohort size.
        gma.refresh_sites()
        released = gma.released_model()
        if released is None:  # pragma: no cover - only with a known digest
            raise RuntimeError("the GMA sent no released model")
        model_in_use = ModelRef(released.version, released.kind, released.digest)
        engine = str(gma.health().get("engine") or "")

        analytics = self.run_round(f"{pilot_id}-analytics", "analytics", model_in_use)

        # The model in use, measured at every hospital. Its size-weighted
        # metrics are the baseline a candidate has to beat; the GMA reads them
        # from this round when it evaluates the release.
        evaluation_id = f"{pilot_id}-evaluation"
        evaluation = self.run_round(evaluation_id, "evaluation", model_in_use)
        baseline: dict[str, float] = {}
        if self._permitted(evaluation):
            baseline = dict(gma.aggregate(evaluation_id).metrics)

        initial = gma.get_model_version(INITIAL_VERSION)
        current = ModelRef(initial.version, initial.kind, initial.digest)
        training: list[dict[str, Any]] = []
        curve: list[dict[str, Any]] = []
        stop_reason: str | None = None
        candidate_model: CandidateModel | None = None
        last_training_id: str | None = None
        for number in range(1, training_rounds + 1):
            round_id = f"{pilot_id}-training-{number}"
            produced: CandidateModel | None = None
            # A training round's updates are deleted whatever happens, so no
            # hospital's single clipped update stays in the GMA's memory: the
            # GMA deletes them when it aggregates the round, and the app
            # discards a round that was not aggregated.
            try:
                report = self.run_round(round_id, "training", current)
                report["candidate_version"] = None
                training.append(report)
                if not self._permitted(report):
                    reason = (report["readiness"] or {}).get("stop_reason") or report["plan"].get("stop_reason")
                    stop_reason = f"training round {number} could not be aggregated: {stop_reason_text(reason)}"
                    break
                try:
                    produced = gma.aggregate(round_id)
                except AgentError as error:
                    if error.status != 409:
                        raise
                    report["aggregation_error"] = error.detail
                    stop_reason = f"the GMA refused to aggregate training round {number}: {error.detail}"
                    break
            finally:
                if produced is None:
                    self._discard(round_id)
            report["candidate_version"] = produced.candidate_version
            curve.append(self._curve_point(number, report, produced))
            current = ModelRef(produced.candidate_version, current.kind, produced.artifact_digest)
            candidate_model = produced
            last_training_id = round_id

        validation = None
        release = None
        candidate = None
        if candidate_model is not None and last_training_id is not None:
            validation_id = f"{pilot_id}-candidate-validation"
            validation = self.run_round(validation_id, "evaluation", current)
            if self._permitted(validation):
                validated = gma.aggregate(validation_id)
                candidate_model = candidate_model.model_copy(update={"metrics": dict(validated.metrics)})
            else:
                # Without validation there are no gate metrics, so every gate
                # is not evaluated and the candidate is rejected.
                candidate_model = candidate_model.model_copy(update={"metrics": {}})

            evaluated = gma.evaluate_release(
                last_training_id,
                ReleaseEvaluationRequest(
                    candidate_version=candidate_model.candidate_version,
                    condition_id=STROKE_CONDITION.condition_id,
                    metrics_from_round=validation_id,
                    baseline_from_round=evaluation_id,
                    change_from_version=INITIAL_VERSION,
                    rollback_version=model_in_use.version,
                    release_requester=release_committee(),
                    declared_purpose=RELEASE_PURPOSE,
                ),
            )
            release = {
                "guard": evaluated.guard.model_dump(mode="json") if evaluated.guard is not None else None,
                "gates": evaluated.decision.model_dump(mode="json"),
                "metrics_source": evaluated.metrics_source,
                "baseline_version": evaluated.baseline_version,
            }
            candidate = {**candidate_model.model_dump(mode="json"), "status": evaluated.candidate_status}
        self._fill_curve_losses(curve, candidate_model.metrics.get("log_loss") if candidate_model else None)

        site_audits = self._site_audits(gma.site_summaries(include_audit=True))
        coordinator_audit = gma.audit_summary().model_dump(mode="json")

        report = {
            "study_id": STUDY_ID,
            "pilot_id": pilot_id,
            "condition": STROKE_CONDITION.model_dump(mode="json"),
            "engine": engine,
            "model_in_use": version_summary(released),
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
            # Each hospital's audit records stay at the hospital. Its LMA
            # reports whether its chain verifies and how many decisions it holds.
            "site_audits": site_audits,
            "coordinator_audit": coordinator_audit,
            "notice": NOTICE,
            "dataset_notice": DATASET_NOTICE,
        }
        # Stored so the charts that show the last pilot survive a restart.
        self._store.save_pilot_report(pilot_id, report)
        if self._on_report is not None:
            self._on_report(report)
        return report

    def _site_audits(self, entries: Sequence[Any]) -> dict[str, dict[str, Any] | None]:
        """Each hospital's audit chain state, in display order. ``None`` for a hospital whose LMA did not report it."""
        by_site = {entry.site_id: entry for entry in entries}
        audits: dict[str, dict[str, Any] | None] = {}
        for site in self.sites:
            entry = by_site.get(site.site_id)
            audit = entry.audit if entry is not None else None
            audits[site.site_id] = {"site_id": site.site_id, **audit.model_dump(mode="json")} if audit else None
        return audits

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


__all__ = [
    "MINIMUM_PARTICIPANTS",
    "NOTICE",
    "PHASE_CAPABILITIES",
    "PRIVACY_PROFILE",
    "PilotService",
    "RELEASE_PURPOSE",
    "ROUND_PREFIX",
    "STUDY_ID",
    "TRAINING_ROUND_ENDED",
    "release_committee",
    "stop_reason_text",
    "version_summary",
]
