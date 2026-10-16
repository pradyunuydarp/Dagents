"""The demo's charts: which ones exist, the data behind each, and the drawn SVG.

Drawing is the framework's (``agents.common.infrastructure.charts``). This
module decides what each chart shows and where its numbers come from:

- **Hospital charts** use one hospital's own records, through the hospital
  view's chart-data methods (``app.services.hospital_views``): the records are
  read in the hospital's store as its role, and the risk chart scores them with
  the model the hospital's LMA reports in use. They are shown only in that
  hospital's view.
- **Consortium charts** use only what the hospitals report through the GMA
  (each LMA's summary) and the stored report of the last pilot. A hospital
  reports its outcome rate only when its planner permits it; otherwise the
  chart shows a gap and the table gives the hospital's reason.

Charts never decide anything. The gate chart shows the outcome the release
decision gave each gate, and the threshold that gate was checked against.

Each chart has a list entry (title, description, alt text, table, notice) and
an SVG in a light and a dark theme. A chart whose data does not exist yet (the
consortium charts that need a pilot, or any chart over an empty store) is listed
with ``available: false`` and a reason. Drawn SVGs are cached by scope, chart, theme and a digest of what is
drawn, so a repeated request for unchanged data is not drawn again. Each list
entry's ``version`` (and the SVG's ETag) is a digest of the whole entry,
including the table, so it changes whenever anything the page shows changes.

Each list entry also says who drew the chart (``drawn_by``) and where its
numbers come from (``data_from``).
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from agents.common.infrastructure import charts

from app.domain.conditions import STROKE_CLASSIFICATION, STROKE_RELEASE_GATES
from app.domain.dataset import DATASET_NOTICE

#: Hospital charts, in display order.
SITE_CHART_IDS: tuple[str, ...] = ("outcome-by-age-band", "presentation", "systolic-bp", "risk-scores")

#: Consortium charts, in display order.
CONSORTIUM_CHART_IDS: tuple[str, ...] = (
    "hospital-records",
    "hospital-outcome-rates",
    "site-auc",
    "training-curve",
    "release-gates",
)

#: Why a consortium chart that needs a pilot is not drawn yet.
NEEDS_PILOT = "Run the pilot to see this chart. It shows results from the last pilot."

#: Why a chart over a hospital's records is not drawn: the store is empty, for
#: example in a database that is migrated but not seeded yet.
EMPTY_STORE = "This hospital's store holds no records yet."

#: Why a consortium chart of the hospitals' records is not drawn: every
#: hospital's store is empty.
NO_STORE_HAS_RECORDS = "No hospital's store holds records yet."

#: Plain names for the release gates, for the gate chart.
GATE_LABELS: dict[str, str] = {
    "discrimination": "Discrimination (AUC)",
    "improves_on_current_release": "Improves on the model in use (AUC)",
    "subgroup_fairness": "AUC gap between women and men",
    "sensitivity_at_alert_threshold": "Sensitivity at the alert threshold",
    "alert_burden": "Alerts per 1,000 patients",
}

#: How each gate metric is written.
METRIC_FORMATS: dict[str, str] = {
    "auc": "{:.3f}",
    "subgroup_auc_gap": "{:.3f}",
    "sensitivity": "{:.0%}",
    "specificity": "{:.0%}",
    "alerts_per_1000": "{:.0f}",
    "log_loss": "{:.3f}",
}

#: Most drawn SVGs kept in memory.
CACHE_SIZE = 256

#: Who draws every chart. The renderer is a library call: it holds no data and
#: makes no decision.
DRAWN_BY = "The app, with the Dagents chart renderer"

#: Where each chart's numbers come from.
DATA_FROM_SITE_RECORDS = "The hospital's own records, read in its store as the hospital's role"
DATA_FROM_SITE_SCORES = "The hospital's own records, scored with the model its LMA reports in use"
DATA_FROM_REPORTS = "What each hospital's LMA reports through the GMA"
DATA_FROM_REPORTS_AND_ENTRIES = (
    "What each hospital's LMA reports through the GMA, and each hospital's count of entered records"
)
DATA_FROM_PILOT = "The report of the last pilot, built from the GMA's answers"


class UnknownChartError(KeyError):
    """No chart has this id."""


class ChartNotAvailable(LookupError):
    """The chart exists, but its data does not yet. ``reason`` says why."""

    def __init__(self, chart_id: str, reason: str) -> None:
        super().__init__(reason)
        self.chart_id = chart_id
        self.reason = reason


@dataclass
class ChartSpec:
    """Everything that shapes one chart: its text, its data and how it is drawn."""

    chart_id: str
    title: str
    description: str
    alt: str
    kind: str = ""
    args: tuple = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    table: dict[str, list] | None = None
    reason: str | None = None
    data_from: str = DATA_FROM_SITE_RECORDS

    @property
    def available(self) -> bool:
        return self.reason is None

    def _drawing(self) -> dict[str, Any]:
        """Everything the drawn SVG depends on."""
        return {
            "chart_id": self.chart_id,
            "title": self.title,
            "kind": self.kind,
            "args": self.args,
            "kwargs": self.kwargs,
            "reason": self.reason,
            "note": DATASET_NOTICE,
        }

    @property
    def drawing_version(self) -> str:
        """A digest of what the SVG draws. Drawn SVGs are cached by it."""
        return _digest(self._drawing())

    @property
    def version(self) -> str:
        """A digest of the whole list entry: the drawing, the text and the table.

        The list's ``version`` and the SVG's ETag use it. A table can change
        while the drawing does not, for example the "Outcome not recorded"
        column after an entry without an outcome, and the version changes then
        too.
        """
        return _digest({
            **self._drawing(),
            "description": self.description,
            "alt": self.alt,
            "table": self.table,
        })


def _digest(content: Mapping[str, Any]) -> str:
    text = json.dumps(content, sort_keys=True, default=str, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class RenderedChart:
    svg: str
    version: str

    def etag(self, theme: str) -> str:
        return f'"{self.version}-{theme}"'


_DRAW: dict[str, Callable[..., str]] = {
    "bar": charts.bar_chart,
    "grouped_bar": charts.grouped_bar_chart,
    "histogram": charts.histogram,
    "histogram_panels": charts.histogram_panels,
    "line": charts.line_chart,
    "dot_threshold": charts.dot_threshold_chart,
}


def _rounded(values: Sequence[float | None], digits: int = 4) -> list[float | None]:
    return [None if value is None else round(float(value), digits) for value in values]


# --- Hospital charts ------------------------------------------------------------------


def _slot(hospital: Any) -> int:
    """The hospital's colour slot, so its charts match its colour on the consortium charts."""
    return getattr(hospital, "series_slot", None) or 1


def _outcome_by_age_band(hospital: Any) -> ChartSpec:
    data = hospital.outcome_by_age_band()
    rates = _rounded(data["outcome_rate"])
    name = hospital.display_name
    table = {
        "columns": [
            "Age band", "Labelled records", "Poor outcome", "Share with a poor outcome", "Outcome not recorded",
        ],
        "rows": [
            [band, labelled, poor, "no labelled records" if rate is None else rate, unlabelled]
            for band, labelled, poor, rate, unlabelled in zip(
                data["categories"], data["labelled"], data["poor_outcome"], rates, data["unlabelled"], strict=True
            )
        ],
    }
    return ChartSpec(
        chart_id="outcome-by-age-band",
        title="Poor outcome at six months, by age band",
        description=(
            f"The share of {name}'s labelled records in each age band where the patient was dead or "
            "dependent six months after the stroke. Records without a recorded outcome are left out."
        ),
        alt=f"Bar chart of the share of patients with a poor outcome in each age band at {name}.",
        kind="bar",
        args=(list(data["categories"]), rates),
        kwargs={
            "value_label": "Share with a poor outcome",
            "category_label": "Age band",
            "value_format": "{:.0%}",
            "value_limits": (0.0, 1.0),
            "series_slot": _slot(hospital),
        },
        table=table,
        data_from=DATA_FROM_SITE_RECORDS,
    )


def _presentation(hospital: Any) -> ChartSpec:
    data = hospital.presentation_shares()
    shares = _rounded(data["share"])
    name = hospital.display_name
    table = {
        "columns": ["Finding", "Records with the finding", "Records where it was assessed", "Share"],
        "rows": [
            [label, found, recorded, "not assessed" if share is None else share]
            for label, found, recorded, share in zip(
                data["labels"], data["with_finding"], data["recorded"], shares, strict=True
            )
        ],
    }
    return ChartSpec(
        chart_id="presentation",
        title="Findings at presentation",
        description=(
            f"For each examination finding, the share of {name}'s records that have it, among the "
            "records where it was assessed."
        ),
        alt=f"Horizontal bar chart of how often each finding was present at {name}.",
        kind="bar",
        args=(list(data["labels"]), shares),
        kwargs={
            "value_label": "Share of assessed records",
            "value_format": "{:.0%}",
            "orientation": "horizontal",
            "value_limits": (0.0, 1.0),
            "series_slot": _slot(hospital),
        },
        table=table,
    )


def _systolic_bp(hospital: Any) -> ChartSpec:
    data = hospital.systolic_bp_histogram()
    name = hospital.display_name
    edges = list(data["bin_edges"])
    counts = list(data["counts"])
    table = charts.histogram_table(edges, counts, bin_label="Systolic blood pressure (mmHg)",
                                   value_label="Records", edge_format="{:.0f}")
    missing = data["missing"]
    note = f" {missing} records have no value." if missing else ""
    return ChartSpec(
        chart_id="systolic-bp",
        title="Systolic blood pressure at presentation",
        description=f"How many of {name}'s records fall in each 20 mmHg band of systolic blood pressure.{note}",
        alt=f"Histogram of systolic blood pressure at {name}, in 20 mmHg bands from 60 to 300.",
        kind="histogram",
        args=(edges, counts),
        kwargs={"x_label": "Systolic blood pressure (mmHg)", "y_label": "Records", "edge_format": "{:.0f}",
                "series_slot": _slot(hospital)},
        table=table,
    )


def _risk_scores(hospital: Any) -> ChartSpec:
    data = hospital.risk_score_distribution()
    name = hospital.display_name
    edges = list(data["bin_edges"])
    series = {label[:1].upper() + label[1:]: list(counts) for label, counts in data["series"].items()}
    table = charts.histogram_table(edges, series, bin_label="Estimated risk", edge_format="{:.1f}")
    threshold = data["alert_threshold"]
    not_scored = data["not_scored"]
    extra = f" {not_scored} records could not be scored." if not_scored else ""
    return ChartSpec(
        chart_id="risk-scores",
        title="Estimated risk, by recorded outcome",
        description=(
            f"The risk of a poor outcome that the model in use ({data['model_version']}) estimates for each "
            f"of {name}'s records, grouped by the outcome recorded six months later. A patient is flagged "
            f"when the estimate is {threshold:g} or more.{extra}"
        ),
        alt=(
            f"Three histograms of estimated risk at {name}, one above the other: records with a poor outcome, "
            "records without one, and records whose outcome is not recorded."
        ),
        # The outcomes are categories, not hospitals, so they get a panel each
        # in the hospital's colour instead of a series colour each.
        kind="histogram_panels",
        args=(edges, series),
        kwargs={"x_label": "Estimated risk of a poor outcome", "y_label": "Records",
                "edge_format": "{:.1f}", "series_slot": _slot(hospital)},
        table=table,
        data_from=DATA_FROM_SITE_SCORES,
    )


_SITE_BUILDERS: dict[str, Callable[[Any], ChartSpec]] = {
    "outcome-by-age-band": _outcome_by_age_band,
    "presentation": _presentation,
    "systolic-bp": _systolic_bp,
    "risk-scores": _risk_scores,
}


def _site_spec(chart_id: str, hospital: Any, empty: bool) -> ChartSpec:
    """One hospital chart, or its unavailable entry when the store holds no records."""
    spec = _SITE_BUILDERS[chart_id](hospital)
    if empty:
        spec.reason = EMPTY_STORE
        spec.table = None
    return spec


# --- Consortium charts ------------------------------------------------------------------


@dataclass(frozen=True)
class ConsortiumData:
    """What the consortium charts may use: site summaries and the last pilot."""

    summaries: list[dict[str, Any]]
    last_pilot: dict[str, Any] | None
    minimum_cohort: int

    @property
    def names(self) -> list[str]:
        return [summary["display_name"] for summary in self.summaries]

    @property
    def empty_reason(self) -> str | None:
        """Why the record charts are not drawn: every hospital's store is empty."""
        if self.summaries and all(summary["record_count"] == 0 for summary in self.summaries):
            return NO_STORE_HAS_RECORDS
        return None

    @property
    def slots(self) -> dict[str, int] | None:
        """Each hospital's colour slot, by its place in the consortium."""
        if len(self.summaries) > charts.MAX_SERIES:
            return None
        return {name: index for index, name in enumerate(self.names, start=1)}


def _hospital_series(data: ConsortiumData, values: Sequence[Sequence[Any]]) -> dict[str, list]:
    return {name: list(value) for name, value in zip(data.names, values, strict=True)}


def _hospital_records(data: ConsortiumData) -> ChartSpec:
    categories = ["From the dataset", "Entered in the demo"]
    values = [
        [summary["record_count"] - summary["entered_count"], summary["entered_count"]]
        for summary in data.summaries
    ]
    table = {
        "columns": ["Hospital", "Records from the dataset", "Entered in the demo", "Total", "With an outcome"],
        "rows": [
            [s["display_name"], s["record_count"] - s["entered_count"], s["entered_count"], s["record_count"],
             s["labelled_count"]]
            for s in data.summaries
        ],
    }
    spec = ChartSpec(
        reason=data.empty_reason,
        chart_id="hospital-records",
        title="Records each hospital holds",
        description=(
            "How many records each hospital reports holding: records from the International Stroke Trial, "
            "and records entered in this demo. Each hospital reports its own counts; the coordinator reads "
            "no records."
        ),
        alt="Grouped bar chart of record counts per hospital, from the dataset and entered in the demo.",
        table=table,
        data_from=DATA_FROM_REPORTS_AND_ENTRIES,
    )
    if data.slots is None:
        spec.kind = "bar"
        spec.args = (data.names, [s["record_count"] for s in data.summaries])
        spec.kwargs = {"value_label": "Records", "category_label": "Hospital"}
        return spec
    spec.kind = "grouped_bar"
    spec.args = (categories, _hospital_series(data, values))
    spec.kwargs = {"value_label": "Records", "series_slots": data.slots}
    return spec


#: What the table says for a rate a hospital did not report, by the hospital's
#: ``outcome_rate_status``. The reason comes from the hospital; the chart only
#: words it.
RATE_NOT_REPORTED = {
    "below_minimum_cohort": "suppressed",
    "no_outcomes": "no outcomes yet",
    "denied": "withheld by the Guard",
    "withheld": "withheld: the planner could not be asked",
}


def _hospital_outcome_rates(data: ConsortiumData) -> ChartSpec:
    rates = [None if s["outcome_rate"] is None else round(s["outcome_rate"], 4) for s in data.summaries]
    table = {
        "columns": ["Hospital", "Records with an outcome", "Share with a poor outcome"],
        "rows": [
            [
                s["display_name"],
                s["labelled_count"],
                RATE_NOT_REPORTED.get(s.get("outcome_rate_status", ""), "withheld") if rate is None else rate,
            ]
            for s, rate in zip(data.summaries, rates, strict=True)
        ],
    }
    spec = ChartSpec(
        reason=data.empty_reason,
        chart_id="hospital-outcome-rates",
        title="Poor outcome at six months, by hospital",
        description=(
            "The share of each hospital's labelled records where the patient was dead or dependent at six "
            "months, as the hospital reports it. A hospital that reports no rate has a gap marked "
            f"suppressed. The table gives its reason: fewer than {data.minimum_cohort} labelled records, "
            "the Guard withheld the rate, or the planner could not be asked."
        ),
        alt="Bar chart of the share of patients with a poor outcome at each hospital.",
        table=table,
        data_from=DATA_FROM_REPORTS,
    )
    if data.slots is None:
        spec.kind = "bar"
        spec.args = (data.names, rates)
        spec.kwargs = {"value_label": "Share with a poor outcome", "value_format": "{:.0%}",
                       "value_limits": (0.0, 1.0)}
        return spec
    spec.kind = "grouped_bar"
    spec.args = (["Poor outcome at six months"], {name: [rate] for name, rate in zip(data.names, rates, strict=True)})
    spec.kwargs = {"value_label": "Share with a poor outcome", "value_format": "{:.0%}",
                   "value_limits": (0.0, 1.0), "series_slots": data.slots}
    return spec


def _needs_pilot(chart_id: str, title: str, description: str, alt: str) -> ChartSpec:
    return ChartSpec(
        chart_id=chart_id, title=title, description=description, alt=alt, reason=NEEDS_PILOT, data_from=DATA_FROM_PILOT
    )


def _site_aucs(round_report: Mapping[str, Any] | None, site_ids: Sequence[str]) -> list[float | None]:
    results = {result["site_id"]: result for result in (round_report or {}).get("results") or []}
    values: list[float | None] = []
    for site_id in site_ids:
        auc = (results.get(site_id) or {}).get("metrics", {}).get("auc")
        values.append(None if auc is None else round(float(auc), 4))
    return values


def _site_auc(data: ConsortiumData) -> ChartSpec:
    title = "AUC at each hospital: model in use and candidate"
    alt = "Horizontal grouped bar chart of each hospital's AUC for the model in use and the federated candidate."
    pilot = data.last_pilot
    if pilot is None or not (pilot.get("rounds") or {}).get("candidate_validation"):
        description = (
            "Each hospital measures the model in use and the federated candidate on its own labelled records, "
            "and reports the AUC (0.5 is chance, 1.0 is perfect ranking)."
        )
        reason = (
            NEEDS_PILOT if pilot is None else "The last pilot produced no candidate, so there is nothing to compare."
        )
        spec = _needs_pilot("site-auc", title, description, alt)
        spec.reason = reason
        return spec
    site_ids = [summary["site_id"] for summary in data.summaries]
    in_use = _site_aucs(pilot["rounds"].get("evaluation"), site_ids)
    candidate = _site_aucs(pilot["rounds"].get("candidate_validation"), site_ids)
    in_use_version = (pilot.get("model_in_use") or {}).get("version", "the model in use")
    candidate_version = (pilot.get("candidate") or {}).get("candidate_version", "the candidate")
    categories = ["Model in use", "Federated candidate"]
    table = {
        "columns": ["Hospital", f"Model in use ({in_use_version})", f"Federated candidate ({candidate_version})"],
        "rows": [
            [name, "not reported" if a is None else a, "not reported" if b is None else b]
            for name, a, b in zip(data.names, in_use, candidate, strict=True)
        ],
    }
    spec = ChartSpec(
        chart_id="site-auc",
        title=title,
        description=(
            f"In the last pilot, each hospital measured the model in use ({in_use_version}) and the federated "
            f"candidate ({candidate_version}) on its own labelled records and reported the AUC. 0.5 is chance; "
            "1.0 ranks every poor outcome above every good one. The candidate was measured on the records it "
            "was trained on."
        ),
        alt=alt,
        table=table,
        data_from=DATA_FROM_PILOT,
    )
    if data.slots is None:
        spec.kind = "grouped_bar"
        spec.args = (data.names, {"Model in use": in_use, "Federated candidate": candidate})
        spec.kwargs = {"value_label": "AUC", "value_format": "{:.3f}", "orientation": "horizontal",
                       "value_limits": (0.0, 1.0)}
        return spec
    spec.kind = "grouped_bar"
    spec.args = (
        categories,
        {name: [a, b] for name, a, b in zip(data.names, in_use, candidate, strict=True)},
    )
    spec.kwargs = {"value_label": "AUC", "value_format": "{:.3f}", "orientation": "horizontal",
                   "value_limits": (0.0, 1.0), "series_slots": data.slots}
    return spec


def _training_curve(data: ConsortiumData) -> ChartSpec:
    title = "Training loss by round"
    alt = "Line chart of the federated model's log loss after each training round."
    pilot = data.last_pilot
    curve = (pilot or {}).get("training_curve") or []
    if pilot is None or not curve:
        description = (
            "The federated model's log loss before training and after each training round, averaged over "
            "the hospitals with the planner's weights. Lower is better."
        )
        spec = _needs_pilot("training-curve", title, description, alt)
        if pilot is not None:
            spec.reason = "The last pilot completed no training round."
        return spec
    rounds = [0] + [int(point["round"]) for point in curve]
    losses = _rounded([curve[0].get("start_loss")] + [point.get("loss") for point in curve])
    series = {"Log loss": losses}
    table = charts.line_chart_table(rounds, series, x_label="Round")
    return ChartSpec(
        chart_id="training-curve",
        title=title,
        description=(
            "The federated model's log loss on the hospitals' labelled records, before training (round 0) and "
            "after each training round, averaged over the hospitals with the planner's weights. Lower is "
            "better. Each hospital trained on its own records and sent only a clipped update."
        ),
        alt=alt,
        kind="line",
        args=(rounds, series),
        kwargs={"x_label": "Training round", "y_label": "Log loss", "value_format": "{:.3f}"},
        table=table,
        data_from=DATA_FROM_PILOT,
    )


def _gate_items(pilot: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Gate rows for the dot chart, or ``None`` when a gate has no threshold.

    The threshold is the one the planner reported with each gate result. The
    chart never recomputes it; an improvement gate whose baseline was not
    measured has no threshold, and then the chart is not drawn.
    """
    results = {result["gate_id"]: result for result in pilot["release"]["gates"]["gate_results"]}
    items = []
    for gate in STROKE_RELEASE_GATES:
        result = results.get(gate.gate_id)
        if result is None:
            continue
        threshold = result.get("threshold")
        if threshold is None:
            return None
        # The direction is how the gate's comparison reads, not a decision: an
        # improvement gate asks for at least the threshold.
        direction = "at_most" if gate.comparison.kind == "at_most" else "at_least"
        observed = result.get("observed")
        digits = 1 if gate.metric == "alerts_per_1000" else 4
        label = GATE_LABELS.get(gate.gate_id, gate.gate_id)
        items.append({
            "label": label if gate.blocking else f"{label} (advisory)",
            "observed": None if observed is None else round(float(observed), digits),
            "threshold": round(float(threshold), digits),
            "direction": direction,
            "outcome": result["outcome"],
            "blocking": gate.blocking,
            "format": METRIC_FORMATS.get(gate.metric),
        })
    return items or None


def _release_gates(data: ConsortiumData) -> ChartSpec:
    title = "Release gates: observed value and threshold"
    alt = "Chart of each release gate's observed value against its threshold, with the outcome in words."
    pilot = data.last_pilot
    description = (
        "Each gate the candidate must pass before anyone may release it, with the value measured in the last "
        "pilot and the threshold it was checked against. The outcomes come from the release decision; the "
        "chart only shows them. A failed advisory gate does not block a release."
    )
    if pilot is None or not pilot.get("release"):
        spec = _needs_pilot("release-gates", title, description, alt)
        if pilot is not None:
            spec.reason = "The last pilot produced no candidate, so no gate was checked."
        return spec
    items = _gate_items(pilot)
    if items is None:
        spec = _needs_pilot("release-gates", title, description, alt)
        spec.reason = "The last pilot did not measure the model in use, so a gate's threshold is unknown."
        return spec
    gates = pilot["release"]["gates"]
    table = charts.dot_threshold_chart_table(items, label_column="Gate")
    return ChartSpec(
        chart_id="release-gates",
        title=title,
        description=f"{description} The decision was: {gates['action']}.",
        alt=alt,
        kind="dot_threshold",
        args=(items,),
        kwargs={},
        table=table,
        data_from=DATA_FROM_PILOT,
    )


_CONSORTIUM_BUILDERS: dict[str, Callable[[ConsortiumData], ChartSpec]] = {
    "hospital-records": _hospital_records,
    "hospital-outcome-rates": _hospital_outcome_rates,
    "site-auc": _site_auc,
    "training-curve": _training_curve,
    "release-gates": _release_gates,
}


# --- The service --------------------------------------------------------------------------


def consortium_data(overview: Any) -> ConsortiumData:
    """The hospitals' rows and the last pilot, read the way the coordinator may read them.

    ``overview`` is the study's overview (``app.services.overview.StudyOverview``):
    the rows are what each hospital's LMA reports through the GMA, and the
    pilot is the stored report.
    """
    return ConsortiumData(
        summaries=overview.hospital_rows(),
        last_pilot=overview.last_pilot(),
        minimum_cohort=STROKE_CLASSIFICATION.minimum_cohort,
    )


class ChartService:
    """Lists the charts and draws them, with a cache of drawn SVGs."""

    def __init__(self, cache_size: int = CACHE_SIZE) -> None:
        self._cache: OrderedDict[tuple[str, str, str, str], str] = OrderedDict()
        self._cache_size = cache_size
        self._lock = threading.Lock()
        self.renders = 0

    # Lists -----------------------------------------------------------------------------

    @staticmethod
    def _entry(spec: ChartSpec, svg_path: str) -> dict[str, Any]:
        return {
            "chart_id": spec.chart_id,
            "title": spec.title,
            "description": spec.description,
            "alt": spec.alt,
            "available": spec.available,
            "reason": spec.reason,
            "table": spec.table if spec.table is not None else {"columns": [], "rows": []},
            "notice": DATASET_NOTICE,
            "svg_path": svg_path,
            "version": spec.version,
            "drawn_by": DRAWN_BY,
            "data_from": spec.data_from,
        }

    def site_list(self, hospital: Any) -> list[dict[str, Any]]:
        """The hospital's charts, from its own records. Unavailable while its store is empty.

        ``hospital`` is a hospital view (``app.services.hospital_views.HospitalView``).
        """
        empty = hospital.record_count() == 0
        return [
            self._entry(_site_spec(chart_id, hospital, empty), site_svg_path(hospital.site_id, chart_id))
            for chart_id in SITE_CHART_IDS
        ]

    def consortium_list(self, overview: Any) -> list[dict[str, Any]]:
        """The consortium charts. Never raises for a chart that needs a pilot."""
        data = consortium_data(overview)
        return [
            self._entry(_CONSORTIUM_BUILDERS[chart_id](data), consortium_svg_path(chart_id))
            for chart_id in CONSORTIUM_CHART_IDS
        ]

    # SVGs ------------------------------------------------------------------------------

    def site_svg(self, hospital: Any, chart_id: str, theme: str) -> RenderedChart:
        if chart_id not in _SITE_BUILDERS:
            raise UnknownChartError(chart_id)
        spec = _site_spec(chart_id, hospital, hospital.record_count() == 0)
        return self._render(f"site:{hospital.site_id}", spec, theme)

    def consortium_svg(self, overview: Any, chart_id: str, theme: str) -> RenderedChart:
        builder = _CONSORTIUM_BUILDERS.get(chart_id)
        if builder is None:
            raise UnknownChartError(chart_id)
        return self._render("consortium", builder(consortium_data(overview)), theme)

    def _render(self, scope: str, spec: ChartSpec, theme: str) -> RenderedChart:
        if not spec.available:
            raise ChartNotAvailable(spec.chart_id, spec.reason or NEEDS_PILOT)
        if theme not in charts.THEMES:
            raise charts.ChartInputError(f"theme must be one of {', '.join(charts.THEMES)}")
        version = spec.version
        # Keyed by the drawing, so a change to the table alone reuses the SVG.
        key = (scope, spec.chart_id, theme, spec.drawing_version)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return RenderedChart(cached, version)
        svg = _DRAW[spec.kind](
            *spec.args,
            **spec.kwargs,
            theme=theme,
            title=spec.title,
            note=DATASET_NOTICE,
        )
        with self._lock:
            self.renders += 1
            self._cache[key] = svg
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        return RenderedChart(svg, version)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


def site_svg_path(site_id: str, chart_id: str) -> str:
    return f"/api/v1/hospitals/{site_id}/charts/{chart_id}.svg"


def consortium_svg_path(chart_id: str) -> str:
    return f"/api/v1/charts/{chart_id}.svg"
