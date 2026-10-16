"""What each hospital's own staff see, and how the app writes a hospital's records.

Each hospital has two parts in this demo:

- **The hospital's Dagents LMA** (local monitoring agent). It holds the
  hospital's Guard and audit chain, its running profile, its outcome-rate
  decision, the model in use and its job log. A hospital view calls it through
  its HTTP API with the LMA's ``site`` token, and calls no other agent.
- **The app at the hospital**: this module. It writes entries and outcomes to
  the hospital's store, and builds the hospital's product views (worklist,
  entered records, threshold sweep and chart data) from the hospital's own
  records, read in the store as the hospital's role. It scores records with the
  model the hospital's LMA reports in use.

Nothing here leaves the hospital except through the LMA. No lock or store
connection is held while an agent is called: a write finishes, then the view
asks the LMA to read the store again (``POST /api/v1/site/source:refresh``).

The response shapes are the ones the demo served before it used the agents.
The generic names the LMA uses map to the app's words: the label rate is the
poor outcome rate (``outcome_rate``), and ``no_labels`` is ``no_outcomes``.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Callable, Mapping

from agents.common.domain.site import JobLogEntry, ModelInUse, ProbeRequest, SiteSummary

from app.domain import stroke_rule
from app.domain.conditions import STROKE_FEATURE_CONTRACT, STROKE_FIELD_DOMAINS, STROKE_LABEL_FIELD
from app.domain.dataset import AGE_BANDS, Site
from app.domain.model_kinds import kind_for
from app.services.site_source import StoreSiteSource
from app.services.site_store import SiteStore

if TYPE_CHECKING:  # pragma: no cover - for annotations only
    from app.services.agents import Agents, LmaClient

LABEL = STROKE_LABEL_FIELD

#: Fields each hospital's running profile tracks: provenance, the exact age
#: (which stays at the hospital) and every contract field. The LMA builds the
#: profile; the app tells it which fields.
PROFILE_FIELDS: tuple[str, ...] = ("origin", "age", *STROKE_FEATURE_CONTRACT.field_names())

#: What a hospital can do in a round. The manifest names the capability each
#: phase needs.
CAPABILITIES: tuple[str, ...] = ("local_profiling", "local_evaluation", "local_training", "telemetry")

#: The version of each hospital's local policy, as its registration reports it.
POLICY_VERSION = "demo-hospital-policy-v1"

#: Why the coordinator asks for a hospital's summary. The hospital's LMA plans
#: the outcome-rate decision with it.
REPORTING_PURPOSE = "stroke_triage_research"

#: Findings the presentation chart shows, in display order.
PRESENTATION_FIELDS: tuple[str, ...] = (
    "face_weakness",
    "arm_weakness",
    "leg_weakness",
    "speech_difficulty",
    "visual_field_loss",
    "atrial_fibrillation",
)

#: The LMA's generic label-rate statuses, in the app's words. A status not
#: listed keeps its name.
_RATE_STATUS = {"no_labels": "no_outcomes"}

#: Metrics the local evaluation reports that the hospital view leaves out: the
#: log loss is for the training curve, not for the hospital's own page.
_LOCAL_METRICS_LEFT_OUT = frozenset({"log_loss"})


def outcome_rate_status(label_rate_status: str) -> str:
    """The LMA's label-rate status as the app names it (``no_labels`` is ``no_outcomes``)."""
    return _RATE_STATUS.get(label_rate_status, label_rate_status)


def store_info(store: SiteStore) -> dict[str, str]:
    """Where a hospital's records are stored: its kind and location. Never a host or a credential."""
    return {"kind": store.kind, **store.location}


def hospital_row(site: Site, summary: SiteSummary, entered_count: int, store: SiteStore) -> dict[str, Any]:
    """One hospital, as the coordinator's pages show it.

    ``summary`` is what the hospital's LMA reports: its counts, and the poor
    outcome rate only when the hospital's planner permits it. The entered count
    and the store location come from the hospital's store, counted by the app
    at the hospital; the LMA does not know which records were entered.
    """
    return {
        "site_id": site.site_id,
        "display_name": site.display_name,
        "country": site.country,
        "record_count": summary.record_count,
        "entered_count": entered_count,
        "labelled_count": summary.labelled_count,
        "outcome_rate": summary.label_rate,
        "outcome_rate_status": outcome_rate_status(summary.label_rate_status),
        "minimum_cohort": summary.minimum_cohort,
        "store": store_info(store),
    }


def model_summary(model: ModelInUse) -> dict[str, Any]:
    """The model in use without its parameters: version, kind, digest, status, parent, age and release decision."""
    return {
        "version": model.version,
        "kind": model.kind,
        "digest": model.digest,
        "status": model.status,
        "parent_version": model.parent_version,
        "created_at": model.created_at,
        "release_decision": model.release_decision.model_dump(mode="json") if model.release_decision else None,
    }


def contribution(entry: JobLogEntry) -> dict[str, Any]:
    """One entry of the hospital's job log, as the hospital page shows it."""
    return {
        "round_id": entry.round_id,
        "phase": entry.phase,
        "participation": entry.participation,
        "contributed_examples": entry.contributed_examples,
        "update_norm": entry.update_norm,
        "metrics": dict(entry.metrics),
        "reason": entry.reason,
        "submitted_at": entry.submitted_at,
    }


def _check_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive whole number, got {limit!r}")
    return limit


class _Scorer:
    """The model a hospital's LMA reports in use, as the app scores records with it."""

    def __init__(self, model: ModelInUse) -> None:
        self.model = model
        self._kind = kind_for(model.kind)

    def score(self, record: Mapping[str, Any]) -> float | None:
        return self._kind.score(self.model.params, record)

    def assess(self, record: Mapping[str, Any]) -> dict[str, Any]:
        return self._kind.assess(self.model.version, self.model.params, record)


class HospitalView:
    """One hospital's own views: its LMA's reports and the app's product views.

    Params:
    - `site`: the hospital.
    - `store`: the hospital's store. Every read and write acts as the
      hospital's role.
    - `agents`: how the app reaches the agents. The view calls only this
      hospital's LMA.
    - `source`: the store as the LMA reads it. Its version tells the view when
      to read the records again. In process, the hospital's LMA reads the same
      object.
    - `series_slot`: the hospital's chart colour slot (1-3), the same on its
      own charts and on the consortium charts. ``None`` when the consortium has
      more hospitals than colours.
    - `models_ready`: called before anything that needs the model in use, so
      the GMA holds the app's model versions first. The Guard probe, the
      summary and the audit trail do not call it, so they answer while the
      planner or the GMA's store is down.

    Building a view reads nothing.
    """

    def __init__(
        self,
        site: Site,
        store: SiteStore,
        agents: "Agents",
        *,
        source: StoreSiteSource | None = None,
        series_slot: int | None = None,
        models_ready: Callable[[], None] | None = None,
    ) -> None:
        self.site = site
        self.site_id = site.site_id
        self.display_name = site.display_name
        self.country = site.country
        self.store = store
        self.series_slot = series_slot
        self._agents = agents
        self._source = source or StoreSiteSource(store)
        self._models_ready = models_ready
        self._lock = threading.Lock()
        self._records: tuple[str, list[dict[str, Any]]] | None = None

    @property
    def max_entered(self) -> int:
        """Most records the data-entry form may add at this hospital."""
        return self.store.max_entered

    def lma(self) -> "LmaClient":
        """This hospital's LMA, with its ``site`` token."""
        return self._agents.lma(self.site_id)

    # --- The hospital's records, read in its store ----------------------------------

    def records(self) -> list[dict[str, Any]]:
        """Every record, read as the hospital's role, and kept until the store's version changes.

        The list is shared. Callers copy a record before changing it.
        """
        version = self._source.version()
        with self._lock:
            if self._records is not None and self._records[0] == version:
                return self._records[1]
        rows = self.store.records()
        with self._lock:
            self._records = (version, rows)
        return rows

    def counts(self) -> dict[str, int]:
        """``{"total", "entered", "labelled"}``, counted in the store."""
        return self.store.count()

    def record_count(self) -> int:
        """How many records the hospital's store holds."""
        return len(self.records())

    def _need_models(self) -> None:
        if self._models_ready is not None:
            self._models_ready()

    def _scorer(self) -> _Scorer:
        self._need_models()
        return _Scorer(self.lma().model_in_use())

    # --- What the hospital's LMA reports ---------------------------------------------

    def summary(self) -> dict[str, Any]:
        """What this hospital reports to the coordinator, as its LMA answers ``GET /api/v1/site/summary``."""
        summary = self.lma().site_summary()
        return hospital_row(self.site, summary, self.counts()["entered"], self.store)

    def dashboard(self, contribution_limit: int = 20) -> dict[str, Any]:
        """Everything the hospital page shows about this hospital.

        From the LMA: the outcome-rate decision, the running profile, the model
        in use measured on the hospital's labelled records, and the job log.
        From the store: the counts and where the records are.
        """
        self._need_models()
        lma = self.lma()
        summary = lma.site_summary()
        profile = lma.site_profile()
        evaluation = lma.evaluate_model()
        model = lma.model_in_use()
        jobs = lma.job_log(_check_limit(contribution_limit))
        counts = self.counts()
        return {
            "site_id": self.site_id,
            "display_name": self.display_name,
            "country": self.country,
            "store": store_info(self.store),
            "counts": {"total": counts["total"], "entered": counts["entered"], "labelled": counts["labelled"]},
            "outcome_rate": summary.label_rate,
            "outcome_rate_status": outcome_rate_status(summary.label_rate_status),
            "minimum_cohort": summary.minimum_cohort,
            "max_entered": self.max_entered,
            "profile": profile,
            "local_metrics": {
                "model_version": evaluation.version,
                "labelled_records": evaluation.labelled_count,
                "metrics": {
                    name: value for name, value in evaluation.metrics.items() if name not in _LOCAL_METRICS_LEFT_OUT
                },
            },
            "current_model": model_summary(model),
            "contributions": [contribution(entry) for entry in jobs],
        }

    def audit_trail(self, limit: int = 50) -> dict[str, Any]:
        """The hospital's Guard decisions, newest first, and whether its audit chain verifies. Local only."""
        body = self.lma().request("GET", "/api/v1/governance/audit", params={"limit": _check_limit(limit)}).body
        return {
            "site_id": self.site_id,
            "chain_intact": bool(body.get("chain_intact")),
            "records": list(body.get("records") or []),
        }

    def probe(self, request: ProbeRequest) -> dict[str, Any]:
        """Run one request through the hospital's Guard, at its LMA. The answer holds no record."""
        answer = self.lma().probe(request)
        return {"site_id": self.site_id, **answer.model_dump(mode="json")}

    # --- The app's product views at the hospital --------------------------------------

    def worklist(self, limit: int = 12) -> list[dict[str, Any]]:
        """Records ranked by the model in use, highest risk first.

        Nothing is removed. Records the model cannot score go to the end.
        """
        _check_limit(limit)
        scorer = self._scorer()
        scored = [{**row, "assessment": scorer.assess(row)} for row in self.records()]
        scored.sort(key=lambda item: (not item["assessment"]["suppressed"], item["assessment"]["score"]), reverse=True)
        return scored[:limit]

    def threshold_sweep(self) -> list[dict[str, float]]:
        """Sensitivity, specificity and alerts at a range of thresholds, for the model in use."""
        scorer = self._scorer()
        return stroke_rule.sweep_alert_thresholds(self.records(), score_fn=scorer.score)

    def entered(self, limit: int = 20) -> list[dict[str, Any]]:
        """Records entered in the demo, newest first, each with its assessment."""
        rows = self.store.list_entered(_check_limit(limit))
        scorer = self._scorer()
        return [{**row, "assessment": scorer.assess(row)} for row in rows]

    def enter(self, fields: Mapping[str, Any], client_request_id: str | None = None) -> dict[str, Any]:
        """Store a new record, as this hospital, and return it with its assessment and the new profile.

        Params:
        - `fields`: ``age`` and contract fields. ``None`` means missing. The
          store refuses any other key and checks every value.
        - `client_request_id`: the idempotency key. Repeating it with the same
          fields returns the first record and changes nothing.

        Returns ``{record, assessment, counts, profile}``. The store raises
        ``RecordRejected``, ``IdempotencyKeyReused`` or ``EntryLimitReached``.
        The write finishes before the LMA is asked to read the store again.
        The model versions are registered with the GMA before the write, so a
        failed registration stores nothing.
        """
        self._need_models()
        record = self.store.append(fields, client_request_id)
        lma = self.lma()
        lma.refresh_source()
        profile = lma.site_profile()
        scorer = _Scorer(lma.model_in_use())
        return {
            "record": record,
            "assessment": scorer.assess(record),
            "counts": self.counts(),
            "profile": profile,
        }

    def set_outcome(self, encounter_id: str, poor_outcome_6m: bool) -> dict[str, Any]:
        """Record the six-month outcome of an entered record.

        Returns ``{record, counts, profile}``. The store raises
        ``UnknownEncounterError``, ``RecordRejected`` or
        ``OutcomeAlreadyRecorded``.
        """
        record = self.store.set_outcome(encounter_id, poor_outcome_6m)
        lma = self.lma()
        lma.refresh_source()
        return {"record": record, "counts": self.counts(), "profile": lma.site_profile()}

    # --- Data for the hospital's charts -------------------------------------------------

    def outcome_by_age_band(self) -> dict[str, Any]:
        """Labelled records and poor outcomes per age band, youngest first."""
        labelled = [0] * len(AGE_BANDS)
        poor = [0] * len(AGE_BANDS)
        unlabelled = [0] * len(AGE_BANDS)
        for row in self.records():
            band = row.get("age_band")
            if band not in AGE_BANDS:
                continue
            index = AGE_BANDS.index(band)
            if isinstance(row.get(LABEL), bool):
                labelled[index] += 1
                poor[index] += 1 if row[LABEL] else 0
            else:
                unlabelled[index] += 1
        return {
            "categories": list(AGE_BANDS),
            "labelled": labelled,
            "poor_outcome": poor,
            "outcome_rate": [p / n if n else None for p, n in zip(poor, labelled, strict=True)],
            "unlabelled": unlabelled,
        }

    def presentation_shares(self) -> dict[str, Any]:
        """For each finding, how many records have it among those that recorded it."""
        rows = self.records()
        found = [sum(1 for row in rows if row.get(name) is True) for name in PRESENTATION_FIELDS]
        recorded = [sum(1 for row in rows if isinstance(row.get(name), bool)) for name in PRESENTATION_FIELDS]
        return {
            "fields": list(PRESENTATION_FIELDS),
            "labels": [STROKE_FIELD_DOMAINS[name].label for name in PRESENTATION_FIELDS],
            "with_finding": found,
            "recorded": recorded,
            "share": [f / r if r else None for f, r in zip(found, recorded, strict=True)],
        }

    def systolic_bp_histogram(self, bin_width: int = 20) -> dict[str, Any]:
        """Systolic blood pressure counts in bins over the contract's range, 60-300 mmHg."""
        low, high = 60, 300
        if bin_width <= 0 or (high - low) % bin_width:
            raise ValueError(f"bin_width must divide {high - low}")
        edges = list(range(low, high + 1, bin_width))
        counts = [0] * (len(edges) - 1)
        missing = 0
        for row in self.records():
            value = row.get("systolic_bp")
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                missing += 1
                continue
            index = min(int((value - low) // bin_width), len(counts) - 1)
            if 0 <= index:
                counts[index] += 1
        return {"bin_edges": edges, "counts": counts, "missing": missing, "unit": "mmHg"}

    def risk_score_distribution(self, bins: int = 10) -> dict[str, Any]:
        """Scores of the model in use, in equal bins from 0 to 1, by recorded outcome."""
        scorer = self._scorer()
        edges = [round(index / bins, 4) for index in range(bins + 1)]
        series = {"poor outcome": [0] * bins, "no poor outcome": [0] * bins, "outcome not recorded": [0] * bins}
        not_scored = 0
        for row in self.records():
            score = scorer.score(row)
            if score is None:
                not_scored += 1
                continue
            index = min(int(score * bins), bins - 1)
            label = row.get(LABEL)
            key = "poor outcome" if label is True else "no poor outcome" if label is False else "outcome not recorded"
            series[key][index] += 1
        return {
            "model_version": scorer.model.version,
            "bin_edges": edges,
            "series": series,
            "alert_threshold": stroke_rule.ALERT_THRESHOLD,
            "not_scored": not_scored,
        }


__all__ = [
    "CAPABILITIES",
    "HospitalView",
    "POLICY_VERSION",
    "PRESENTATION_FIELDS",
    "PROFILE_FIELDS",
    "REPORTING_PURPOSE",
    "contribution",
    "hospital_row",
    "model_summary",
    "outcome_rate_status",
    "store_info",
]
