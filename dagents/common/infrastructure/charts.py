"""Draw charts as SVG from plain data, in the Dagents design system.

Every function takes lists, numbers and strings, never records or a
connection, so any consumer can chart numbers it has already computed. Each
chart function has a twin ``*_table`` function that returns the same values as
table rows (``{"columns": [...], "rows": [[...], ...]}``), so a page can show
the numbers without reading the picture.

How the charts look, and why:

- Colours come from the design-system tokens in
  ``design/dagents-design-system.css``. ``TOKENS`` mirrors them, because the
  containers that serve charts do not ship the CSS file. A test fails when the
  mirror and the CSS differ.
- Series colours (``--ds-series-1..3``) mark identity only. The decision
  colours (permit, narrow, deny) appear only in ``dot_threshold_chart``, next to
  a word that states the outcome.
- One value axis, thin bars with a 2px gap, a hairline grid, a legend when a
  chart has two or more series, and value labels only where they fit.
- No title is drawn, because the page shows it. The SVG carries ``<title>`` and
  ``<desc>`` for screen readers.
- The same input gives the same bytes: the SVG id salt comes from a digest of
  the input, and the file has no date or metadata block.
- A missing value (``None``) leaves a gap labelled "suppressed". NaN is refused,
  because a value that failed to compute is not a suppressed one.

matplotlib is imported only when a chart is drawn, and the figure is attached to
the Agg canvas directly, so the global pyplot backend is never changed. Without
matplotlib, drawing raises ``ChartsUnavailableError``; the table functions still
work.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from pathlib import Path
import re
import textwrap
import threading
from typing import Any, Callable, Mapping, Sequence
from xml.sax.saxutils import escape


__all__ = [
    "CHART_TOKENS",
    "MAX_SERIES",
    "TABLE_BUILDERS",
    "THEMES",
    "TOKENS",
    "ChartInputError",
    "ChartsUnavailableError",
    "bar_chart",
    "bar_chart_table",
    "charts_available",
    "dot_threshold_chart",
    "dot_threshold_chart_table",
    "grouped_bar_chart",
    "grouped_bar_chart_table",
    "histogram",
    "histogram_panels",
    "histogram_table",
    "line_chart",
    "line_chart_table",
    "load_tokens",
    "parse_design_tokens",
    "stacked_bar_chart",
    "stacked_bar_chart_table",
]


class ChartsUnavailableError(RuntimeError):
    """matplotlib is not installed, so no chart can be drawn."""


class ChartInputError(ValueError):
    """The data cannot be drawn as asked: wrong lengths, NaN, too many series."""


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

THEMES = ("light", "dark")

#: Three series slots, one per ``--ds-series-N`` token. A fourth series would
#: need a fourth colour that has not been checked for colour-vision deficiency,
#: so it is refused.
MAX_SERIES = 3

#: The design-system tokens the charts use.
CHART_TOKENS = (
    "--ds-panel",
    "--ds-line",
    "--ds-line-strong",
    "--ds-ink",
    "--ds-ink-2",
    "--ds-ink-3",
    "--ds-series-1",
    "--ds-series-2",
    "--ds-series-3",
    "--ds-permit",
    "--ds-narrow",
    "--ds-deny",
)

#: Mirror of the token values in ``design/dagents-design-system.css``.
#: ``dagents/tests/test_charts.py`` fails when they differ, so change the CSS
#: first and copy the values here.
TOKENS: dict[str, dict[str, str]] = {
    "light": {
        "--ds-panel": "#ffffff",
        "--ds-line": "#dde1e8",
        "--ds-line-strong": "#bcc4d0",
        "--ds-ink": "#14181f",
        "--ds-ink-2": "#464f60",
        "--ds-ink-3": "#636c7e",
        "--ds-series-1": "#289ebf",
        "--ds-series-2": "#8665f0",
        "--ds-series-3": "#7b1b94",
        "--ds-permit": "#0f6d4f",
        "--ds-narrow": "#8a5200",
        "--ds-deny": "#a52321",
    },
    "dark": {
        "--ds-panel": "#141922",
        "--ds-line": "#262d3a",
        "--ds-line-strong": "#3a4353",
        "--ds-ink": "#e8ebf1",
        "--ds-ink-2": "#a6aebe",
        "--ds-ink-3": "#838c9b",
        "--ds-series-1": "#1b8abd",
        "--ds-series-2": "#8567f3",
        "--ds-series-3": "#8f4e8c",
        "--ds-permit": "#6cd2a8",
        "--ds-narrow": "#e7b25d",
        "--ds-deny": "#f0918c",
    },
}

_TOKEN_RE = re.compile(r"(--ds-[a-z0-9-]+)\s*:\s*([^;]+);")
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_BARE_ROOT = ":root {"
_DARK_MEDIA = ':root:not([data-theme="light"]) {'
_DARK_TOGGLE = ':root[data-theme="dark"] {'


def _css_block(css_text: str, selector: str) -> dict[str, str] | None:
    """The ``--ds-*`` declarations inside the first block that opens with ``selector``."""
    start = css_text.find(selector)
    if start < 0:
        return None
    body_start = start + len(selector)
    end = css_text.find("}", body_start)
    if end < 0:
        raise ValueError(f"the block {selector!r} is not closed")
    body = re.sub(r"/\*.*?\*/", "", css_text[body_start:end], flags=re.S)
    return {name: value.strip().lower() for name, value in _TOKEN_RE.findall(body)}


def parse_design_tokens(css_text: str) -> dict[str, dict[str, str]]:
    """Read the light and dark values of every ``--ds-*`` token from the design-system CSS.

    Params:
    - `css_text`: the content of ``design/dagents-design-system.css`` or a copy.

    Returns:
    - ``{"light": {token: value}, "dark": {token: value}}``. Dark values start
      from the light ones, because a token the dark blocks do not redefine keeps
      its ``:root`` value.

    Raises:
    - ``ValueError`` when the file has no bare ``:root`` block, or when the
      media-query dark block and the ``data-theme="dark"`` block disagree.
    """
    light = _css_block(css_text, _BARE_ROOT)
    if light is None:
        raise ValueError("the design system has no bare :root block")
    media = _css_block(css_text, _DARK_MEDIA) or {}
    toggle = _css_block(css_text, _DARK_TOGGLE) or {}
    if media and toggle and media != toggle:
        differing = sorted(name for name in set(media) | set(toggle) if media.get(name) != toggle.get(name))
        raise ValueError(f"the two dark blocks disagree on: {differing}")
    dark = {**light, **media, **toggle}
    return {"light": light, "dark": dark}


def load_tokens(css_path: str | Path | None = None) -> dict[str, dict[str, str]]:
    """The chart tokens for both themes.

    Params:
    - `css_path`: a design-system CSS file to read. Without it, a copy of the
      built-in mirror ``TOKENS`` is returned.

    Returns:
    - ``{"light": {...}, "dark": {...}}`` holding exactly ``CHART_TOKENS``.

    Raises:
    - ``ChartInputError`` when the file lacks a chart token or a value is not a
      ``#rrggbb`` colour.
    """
    if css_path is None:
        return copy.deepcopy(TOKENS)
    parsed = parse_design_tokens(Path(css_path).read_text(encoding="utf-8"))
    result: dict[str, dict[str, str]] = {}
    for theme in THEMES:
        values = {}
        for token in CHART_TOKENS:
            value = parsed[theme].get(token)
            if value is None or not _HEX_RE.match(value):
                raise ChartInputError(f"{css_path} has no #rrggbb value for {token} ({theme})")
            values[token] = value
        result[theme] = values
    return result


class _Colours:
    """The resolved colours for one theme."""

    def __init__(self, theme: str, tokens: Mapping[str, Mapping[str, str]] | None) -> None:
        if theme not in THEMES:
            raise ChartInputError(f"theme must be one of {THEMES}, not {theme!r}")
        source = (tokens or TOKENS).get(theme)
        if source is None:
            raise ChartInputError(f"the tokens have no {theme!r} theme")
        values: dict[str, str] = {}
        for token in CHART_TOKENS:
            value = source.get(token)
            if value is None or not _HEX_RE.match(value):
                raise ChartInputError(f"token {token} ({theme}) must be a #rrggbb colour")
            values[token] = value.lower()
        self.panel = values["--ds-panel"]
        self.grid = values["--ds-line"]
        self.axis = values["--ds-line-strong"]
        self.ink = values["--ds-ink"]
        self.ink2 = values["--ds-ink-2"]
        self.ink3 = values["--ds-ink-3"]
        self.series = tuple(values[f"--ds-series-{slot}"] for slot in range(1, MAX_SERIES + 1))
        self.permit = values["--ds-permit"]
        self.narrow = values["--ds-narrow"]
        self.deny = values["--ds-deny"]

    def slot_colour(self, slot: int | None) -> str:
        """The colour of a 0-based series slot, or neutral ink for an entity with no slot."""
        return self.ink2 if slot is None else self.series[slot]


# ---------------------------------------------------------------------------
# Input checks
# ---------------------------------------------------------------------------

Number = float | int
Formatter = str | Callable[[float], str] | None

_SUPPRESSED = "suppressed"
_ORIENTATIONS = ("vertical", "horizontal")
_LABEL_MODES = ("auto", "all", "max", "none")


def _number(value: Any, where: str) -> float | None:
    """One value as a float, ``None`` for suppressed; refuses NaN, infinity and non-numbers."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ChartInputError(f"{where} must be a number or None, not {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ChartInputError(f"{where} is {value!r}; pass None for a suppressed value")
    return result


def _values(values: Sequence[Any], expected: int, where: str) -> list[float | None]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ChartInputError(f"{where} must be a list of numbers")
    if len(values) != expected:
        raise ChartInputError(f"{where} has {len(values)} values but needs {expected}")
    return [_number(value, f"{where}[{index}]") for index, value in enumerate(values)]


def _labels(categories: Sequence[Any], where: str = "categories") -> list[str]:
    if isinstance(categories, (str, bytes)) or not isinstance(categories, Sequence):
        raise ChartInputError(f"{where} must be a list of labels")
    if not categories:
        raise ChartInputError(f"{where} is empty; there is nothing to draw")
    return [str(category) for category in categories]


def _series(
    series: Mapping[str, Sequence[Any]],
    expected: int,
    slots: Mapping[str, int] | None,
) -> list[tuple[str, int, list[float | None]]]:
    """Validate named series and give each its colour slot (0-based)."""
    if not isinstance(series, Mapping) or not series:
        raise ChartInputError("series must be a non-empty mapping of name to values")
    if len(series) > MAX_SERIES:
        raise ChartInputError(
            f"{len(series)} series given; a chart has {MAX_SERIES} series colours. "
            "Fold the rest into one series or draw several charts."
        )
    result = []
    used: set[int] = set()
    for position, (name, values) in enumerate(series.items()):
        slot = position + 1 if slots is None or name not in slots else slots[name]
        if not isinstance(slot, int) or not 1 <= slot <= MAX_SERIES:
            raise ChartInputError(f"series slot for {name!r} must be 1 to {MAX_SERIES}, not {slot!r}")
        if slot in used:
            raise ChartInputError(f"two series share colour slot {slot}")
        used.add(slot)
        result.append((str(name), slot - 1, _values(values, expected, f"series {name!r}")))
    return result


def _slot(series_slot: Any) -> int | None:
    """A one-based colour slot, checked; returned 0-based. ``None`` stays ``None``.

    ``None`` is for an entity that has no series colour, such as a fourth
    site when the design system has three series colours. It is drawn in
    neutral ink (``--ds-ink-2``), which marks no identity.
    """
    if series_slot is None:
        return None
    if isinstance(series_slot, bool) or not isinstance(series_slot, int) or not 1 <= series_slot <= MAX_SERIES:
        raise ChartInputError(f"series_slot must be 1 to {MAX_SERIES} or None, not {series_slot!r}")
    return series_slot - 1


def _option(value: str, allowed: Sequence[str], name: str) -> str:
    if value not in allowed:
        raise ChartInputError(f"{name} must be one of {tuple(allowed)}, not {value!r}")
    return value


def _edges(bin_edges: Sequence[Any]) -> list[float]:
    if isinstance(bin_edges, (str, bytes)) or not isinstance(bin_edges, Sequence) or len(bin_edges) < 2:
        raise ChartInputError("bin_edges needs at least two numbers")
    edges = []
    for index, edge in enumerate(bin_edges):
        value = _number(edge, f"bin_edges[{index}]")
        if value is None:
            raise ChartInputError("bin_edges cannot contain None")
        edges.append(value)
    if any(right <= left for left, right in zip(edges, edges[1:])):
        raise ChartInputError("bin_edges must increase")
    return edges


def _counts(
    counts: Sequence[Any] | Mapping[str, Sequence[Any]],
    bins: int,
    slots: Mapping[str, int] | None,
) -> list[tuple[str, int, list[float | None]]]:
    if isinstance(counts, Mapping):
        return _series(counts, bins, slots)
    return [("", 0, _values(counts, bins, "counts"))]


# ---------------------------------------------------------------------------
# Number formatting
# ---------------------------------------------------------------------------


def _minus(text: str) -> str:
    """Use the minus sign, not a hyphen, for negative numbers."""
    return "\u2212" + text[1:] if text.startswith("-") else text


def _auto_format(values: Sequence[float | None]) -> Callable[[float], str]:
    """A formatter that suits the values: whole numbers stay whole, small ones get two decimals."""
    present = [value for value in values if value is not None]
    if all(float(value).is_integer() for value in present):
        spec = ",.0f"
    else:
        largest = max((abs(value) for value in present), default=0.0)
        spec = ",.0f" if largest >= 100 else ",.1f" if largest >= 10 else ".2f"
    return lambda value: _minus(format(value, spec))


def _formatter(value_format: Formatter, values: Sequence[float | None]) -> Callable[[float], str]:
    if value_format is None:
        return _auto_format(values)
    if callable(value_format):
        return value_format
    if not isinstance(value_format, str):
        raise ChartInputError("value_format must be a format string such as '{:.2f}' or a function")
    return lambda value: value_format.format(value)


def _decimals(values: Sequence[float]) -> int:
    """The fewest decimals that show every value exactly, up to six."""
    for decimals in range(7):
        if all(abs(round(value, decimals) - value) < 1e-9 * max(1.0, abs(value)) for value in values):
            return decimals
    return 6


def _tick_labels(ticks: Sequence[float], value_format: Formatter) -> list[str]:
    """Axis tick labels with one number of decimals for the whole axis.

    Percentages when the values are formatted as percentages; thousands
    separators below 10,000 and compact numbers (25K, 1.5M) from there up.
    """
    values = [0.0 if abs(tick) < 1e-12 else float(tick) for tick in ticks]
    if isinstance(value_format, str) and "%}" in value_format:
        scaled = [value * 100 for value in values]
        decimals = _decimals(scaled)
        return [_minus(f"{value:.{decimals}f}%") for value in scaled]
    largest = max((abs(value) for value in values), default=0.0)
    for limit, suffix in ((1e9, "B"), (1e6, "M"), (1e4, "K")):
        if largest >= limit:
            divisor = 1e3 if suffix == "K" else limit
            return ["0" if value == 0 else _minus(f"{value / divisor:.6g}{suffix}") for value in values]
    decimals = _decimals(values)
    return [_minus(f"{value:,.{decimals}f}") for value in values]


def _edge_formatter(edges: Sequence[float], edge_format: Formatter) -> Callable[[float], str]:
    """Bin edges with one number of decimals for all of them, unless a format is given."""
    if edge_format is not None:
        return _formatter(edge_format, edges)
    decimals = _decimals(edges)
    return lambda value: f"{value:,.{decimals}f}"


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _cell(value: float | None, original: Any) -> Any:
    return _SUPPRESSED if value is None else original


def bar_chart_table(
    categories: Sequence[str],
    values: Sequence[Number | None],
    *,
    category_label: str = "Category",
    value_label: str = "Value",
) -> dict[str, list]:
    """The table view of ``bar_chart``: one row per category, ``"suppressed"`` for ``None``."""
    labels = _labels(categories)
    checked = _values(values, len(labels), "values")
    return {
        "columns": [category_label, value_label],
        "rows": [[label, _cell(value, raw)] for label, value, raw in zip(labels, checked, values)],
    }


def grouped_bar_chart_table(
    categories: Sequence[str],
    series: Mapping[str, Sequence[Number | None]],
    *,
    category_label: str = "Category",
) -> dict[str, list]:
    """The table view of ``grouped_bar_chart``: one row per category, one column per series."""
    labels = _labels(categories)
    checked = _series(series, len(labels), None)
    raw = [list(values) for values in series.values()]
    rows = []
    for index, label in enumerate(labels):
        rows.append([label] + [_cell(values[index], raw[s][index]) for s, (_, _, values) in enumerate(checked)])
    return {"columns": [category_label] + [name for name, _, _ in checked], "rows": rows}


def stacked_bar_chart_table(
    categories: Sequence[str],
    series: Mapping[str, Sequence[Number | None]],
    *,
    category_label: str = "Category",
    total_label: str = "Total",
) -> dict[str, list]:
    """The table view of ``stacked_bar_chart``, with a total that is suppressed when any part is."""
    table = grouped_bar_chart_table(categories, series, category_label=category_label)
    for row in table["rows"]:
        parts = row[1:]
        row.append(_SUPPRESSED if _SUPPRESSED in parts else sum(parts))
    table["columns"].append(total_label)
    return table


def histogram_table(
    bin_edges: Sequence[Number],
    counts: Sequence[Number | None] | Mapping[str, Sequence[Number | None]],
    *,
    bin_label: str = "Bin",
    value_label: str = "Count",
    edge_format: Formatter = None,
) -> dict[str, list]:
    """The table view of ``histogram``: one row per bin, named ``"low–high"``."""
    edges = _edges(bin_edges)
    edge_text = _edge_formatter(edges, edge_format)
    bins = [f"{edge_text(low)}–{edge_text(high)}" for low, high in zip(edges, edges[1:])]
    if isinstance(counts, Mapping):
        return grouped_bar_chart_table(bins, counts, category_label=bin_label)
    return bar_chart_table(bins, counts, category_label=bin_label, value_label=value_label)


def line_chart_table(
    x: Sequence[Number | str],
    series: Mapping[str, Sequence[Number | None]],
    *,
    x_label: str = "x",
) -> dict[str, list]:
    """The table view of ``line_chart``: one row per x value, one column per series."""
    xs = list(_x_values(x)[0])
    table = grouped_bar_chart_table([str(value) for value in xs], series, category_label=x_label)
    for row, value in zip(table["rows"], xs):
        row[0] = value
    return table


def dot_threshold_chart_table(
    items: Sequence[Mapping[str, Any]],
    *,
    label_column: str = "Check",
) -> dict[str, list]:
    """The table view of ``dot_threshold_chart``: observed value, requirement and outcome."""
    rows = []
    for item in _dot_items(items):
        rows.append([
            item["label"],
            "missing" if item["observed"] is None else item["raw_observed"],
            item["required"],
            item["outcome_text"],
        ])
    return {"columns": [label_column, "Observed", "Required", "Outcome"], "rows": rows}


#: Table builders by chart kind, for a consumer that lists charts generically.
TABLE_BUILDERS: dict[str, Callable[..., dict[str, list]]] = {
    "bar": bar_chart_table,
    "grouped_bar": grouped_bar_chart_table,
    "stacked_bar": stacked_bar_chart_table,
    "histogram": histogram_table,
    "histogram_panels": histogram_table,
    "line": line_chart_table,
    "dot_threshold": dot_threshold_chart_table,
}


def _describe(kind: str, table: Mapping[str, list], fmt: Callable[[float], str]) -> str:
    """A plain summary of the data for ``<desc>`` when the caller gives none."""
    columns = table["columns"]
    parts = []
    for row in table["rows"][:24]:
        cells = []
        for column, value in zip(columns[1:], row[1:]):
            shown = fmt(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else str(value)
            cells.append(shown if len(columns) == 2 else f"{column} {shown}")
        parts.append(f"{row[0]}: " + ", ".join(cells))
    more = len(table["rows"]) - 24
    suffix = f"; and {more} more rows" if more > 0 else ""
    return f"{kind}. " + "; ".join(parts) + suffix + "."


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

#: The design system's text faces. matplotlib measures text with DejaVu Sans,
#: which is wider than these, so labels laid out with it still fit.
_FONT_STACK = "Archivo, 'Helvetica Neue', Arial, sans-serif"
_FONT_SIZE = 10.0  # points; 13.3 CSS pixels when the chart is shown 640px wide
_SMALL_SIZE = 9.0
_NOTE_SIZE = 8.5
_PT_PER_PX = 0.75
_BAR_MAX_PT = 24 * _PT_PER_PX  # bars are at most 24px thick
_GAP_PT = 2 * _PT_PER_PX  # the surface gap between touching marks
_RADIUS_PT = 4 * _PT_PER_PX  # rounded data end
_LINE_PT = 2 * _PT_PER_PX
_HAIRLINE_PT = 1 * _PT_PER_PX
_MARKER_PT = 6.5  # marker diameter, 8.7px
_KAPPA = 0.5522847498  # cubic Bezier control distance for a quarter circle

# rcParams are process-wide, and matplotlib is not thread-safe, so one chart is
# drawn at a time. A chart takes tens of milliseconds.
_LOCK = threading.Lock()


def _load_matplotlib():
    """Import matplotlib now, or raise ``ChartsUnavailableError``."""
    try:
        import matplotlib
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:
        raise ChartsUnavailableError(
            "Charts need matplotlib, which is not installed. "
            "Install it with: pip install 'matplotlib>=3.9,<4'"
        ) from exc
    return matplotlib, Figure, FigureCanvasAgg


def charts_available() -> bool:
    """Whether matplotlib can be imported, so charts can be drawn."""
    try:
        _load_matplotlib()
    except ChartsUnavailableError:
        return False
    return True


def _format_key(value_format: Any) -> Any:
    """A stable stand-in for a formatter in the digest.

    ``repr`` of a function contains its memory address, which changes between
    processes and would change the SVG ids.
    """
    if value_format is None or isinstance(value_format, str):
        return value_format
    module = getattr(value_format, "__module__", "")
    name = getattr(value_format, "__qualname__", type(value_format).__qualname__)
    return f"{module}.{name}"


def _tokens_key(tokens: Mapping[str, Mapping[str, str]] | None) -> Any:
    if tokens is None:
        return None
    return {str(theme): {str(k): str(v) for k, v in values.items()} for theme, values in tokens.items()}


def _digest(payload: Mapping[str, Any]) -> str:
    """A digest of everything that shapes the chart, used to salt and prefix the SVG ids."""
    text = json.dumps(payload, sort_keys=True, default=str, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _render(
    payload: Mapping[str, Any],
    colours: _Colours,
    width_px: int,
    height_px: int,
    title: str,
    description: str,
    draw: Callable[[Any], None],
    note: str = "",
) -> str:
    """Create a figure, let ``draw`` fill it, and return the finished SVG text."""
    matplotlib, Figure, FigureCanvasAgg = _load_matplotlib()
    if not 160 <= width_px <= 4000 or not 120 <= height_px <= 4000:
        raise ChartInputError("width_px and height_px must be between 160/120 and 4000")
    digest = _digest(payload)
    settings = {
        "svg.hashsalt": f"dagents-{digest}",
        "svg.fonttype": "none",
        "font.family": "sans-serif",
        "font.sans-serif": ["DejaVu Sans"],
        "font.size": _FONT_SIZE,
        "axes.unicode_minus": True,
        "text.color": colours.ink,
    }
    with _LOCK, matplotlib.rc_context(settings):
        # dpi 72 makes one display unit one point, so the sizes below are points.
        figure = Figure(
            figsize=(width_px / 96, height_px / 96),
            dpi=72,
            facecolor=colours.panel,
            layout="constrained",
        )
        FigureCanvasAgg(figure)
        figure.get_layout_engine().set(w_pad=6 / 72, h_pad=6 / 72)
        if note:
            _add_note(figure, note, colours, width_px)
        draw(figure)
        buffer = io.StringIO()
        figure.savefig(
            buffer,
            format="svg",
            facecolor=colours.panel,
            edgecolor="none",
            metadata={"Date": None, "Creator": None, "Format": None, "Type": None},
        )
    return _finish_svg(buffer.getvalue(), digest, title, description)


def _finish_svg(raw: str, digest: str, title: str, description: str) -> str:
    """Make the SVG self-describing, safe to inline next to other charts, and font-correct."""
    body = raw[raw.index("<svg"):]
    body = re.sub(r"\s*<metadata>.*?</metadata>", "", body, flags=re.S)
    prefix = f"dchart-{digest[:10]}-"
    body = re.sub(r'\bid="([^"]+)"', lambda m: f'id="{prefix}{m.group(1)}"', body)
    body = re.sub(r"url\(#([^)]+)\)", lambda m: f"url(#{prefix}{m.group(1)})", body)
    body = re.sub(r'href="#([^"]+)"', lambda m: f'href="#{prefix}{m.group(1)}"', body)
    body = re.sub(r"font-family:[^;\"]*", f"font-family: {_FONT_STACK}", body)
    title_id, desc_id = f"{prefix}title", f"{prefix}desc"
    body = body.replace("<svg ", f'<svg role="img" aria-labelledby="{title_id} {desc_id}" ', 1)
    head_end = body.index(">") + 1
    accessible = (
        f'\n <title id="{title_id}">{escape(_CONTROL_CHARS.sub("", title))}</title>'
        f'\n <desc id="{desc_id}">{escape(_CONTROL_CHARS.sub("", description))}</desc>'
    )
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body[:head_end] + accessible + body[head_end:]


# --- shared drawing helpers -------------------------------------------------


def _style_axes(ax, colours: _Colours, value_axis: str) -> None:
    """Hairline grid on the value axis only, no frame, quiet ticks."""
    ax.set_facecolor(colours.panel)
    for spine in ax.spines.values():
        spine.set_visible(False)
    category_axis = "x" if value_axis == "y" else "y"
    ax.grid(axis=value_axis, color=colours.grid, linewidth=_HAIRLINE_PT, linestyle="-")
    ax.grid(axis=category_axis, visible=False)
    ax.set_axisbelow(True)
    ax.tick_params(axis=value_axis, length=0, pad=6, labelsize=_FONT_SIZE, labelcolor=colours.ink3)
    ax.tick_params(axis=category_axis, length=0, pad=6, labelsize=_FONT_SIZE, labelcolor=colours.ink2)


def _axis_label(ax, axis: str, text: str, colours: _Colours) -> None:
    if not text:
        return
    setter = ax.set_ylabel if axis == "y" else ax.set_xlabel
    setter(text, color=colours.ink2, fontsize=_FONT_SIZE, labelpad=8)


def _nice_ticks(low: float, high: float, nbins: int = 5) -> list[float]:
    from matplotlib.ticker import MaxNLocator

    if high <= low:
        high = low + 1.0
    ticks = MaxNLocator(nbins=nbins, steps=[1, 2, 2.5, 5, 10]).tick_values(low, high)
    cleaned = [0.0 if abs(tick) < 1e-12 else float(tick) for tick in ticks]
    while len(cleaned) > 2 and cleaned[1] <= low:
        cleaned.pop(0)
    while len(cleaned) > 2 and cleaned[-2] >= high:
        cleaned.pop()
    return cleaned


def _set_value_ticks(ax, axis: str, ticks: Sequence[float], value_format: Formatter) -> None:
    (ax.set_yticks if axis == "y" else ax.set_xticks)(list(ticks), _tick_labels(ticks, value_format))


def _legend(ax, entries: Sequence[tuple[str, str]], colours: _Colours, kind: str) -> None:
    """A one-row legend above the plot, aligned with its left edge."""
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    if kind == "line":
        handles = [
            Line2D([], [], color=colour, linewidth=_LINE_PT, marker="o", markersize=_MARKER_PT,
                   markeredgecolor=colours.panel, markeredgewidth=_GAP_PT, solid_capstyle="round")
            for _, colour in entries
        ]
    else:
        handles = [Patch(facecolor=colour, edgecolor="none", linewidth=0) for _, colour in entries]
    ax.legend(
        handles,
        [name for name, _ in entries],
        loc="lower left",
        bbox_to_anchor=(0, 1.0),
        ncols=len(entries),
        frameon=False,
        fontsize=_FONT_SIZE,
        labelcolor=colours.ink2,
        handlelength=1.6 if kind == "line" else 0.9,
        handleheight=0.9,
        handletextpad=0.5,
        columnspacing=1.6,
        borderaxespad=0.4,
        borderpad=0,
    )


def _freeze_layout(figure) -> None:
    """Run the layout once, then fix every axes where it is.

    Marks added afterwards (bars sized in points, labels placed after a fit
    check) need the axes size, and must not move the axes when they are added.
    """
    figure.canvas.draw()
    figure.set_layout_engine("none")


def _points_per_unit(ax) -> tuple[float, float]:
    """How many points one data unit spans along x and along y."""
    box = ax.get_window_extent()
    (x0, x1), (y0, y1) = ax.get_xlim(), ax.get_ylim()
    return box.width / abs(x1 - x0), box.height / abs(y1 - y0)


def _text_size(figure, text: str, size: float = _FONT_SIZE) -> tuple[float, float]:
    from matplotlib.font_manager import FontProperties

    renderer = figure.canvas.get_renderer()
    width, height, _descent = renderer.get_text_width_height_descent(
        text, FontProperties(size=size), ismath=False
    )
    return width, height


def _add_note(figure, note: str, colours: _Colours, width_px: int) -> None:
    """Draw a source or licence line under the plot, wrapped to the figure width.

    The layout leaves room for it, and it is real text in the SVG, so the
    attribution stays with the chart when the SVG is saved on its own.
    """
    available = width_px * _PT_PER_PX - 16
    lines: list[str] = []
    for paragraph in _CONTROL_CHARS.sub("", note).splitlines():
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            if line and _text_size(figure, candidate, _NOTE_SIZE)[0] > available:
                lines.append(line)
                line = word
            else:
                line = candidate
        if line:
            lines.append(line)
    if lines:
        figure.supxlabel("\n".join(lines), x=0.012, ha="left", fontsize=_NOTE_SIZE,
                         color=colours.ink3, linespacing=1.35)


def _bar_path(u0: float, u1: float, base: float, end: float, ru: float, rv: float,
              horizontal: bool, round_end: bool = True):
    """A bar from ``base`` to ``end``, square at the base, with rounded corners at the data end.

    ``u`` runs along the category axis and ``v`` along the value axis. The
    corner radii are given in data units per axis, so the corner draws as a
    circle once the axes are fixed.
    """
    from matplotlib.path import Path as MplPath

    sign = 1.0 if end >= base else -1.0
    ru = min(ru, (u1 - u0) / 2)
    rv = min(rv, abs(end - base))
    if not round_end or ru <= 0 or rv <= 0:
        points = [(u0, base), (u0, end), (u1, end), (u1, base), (u0, base)]
        codes = [MplPath.MOVETO, MplPath.LINETO, MplPath.LINETO, MplPath.LINETO, MplPath.CLOSEPOLY]
    else:
        start = end - sign * rv
        points = [
            (u0, base), (u0, start),
            (u0, start + sign * _KAPPA * rv), (u0 + ru - _KAPPA * ru, end), (u0 + ru, end),
            (u1 - ru, end),
            (u1 - ru + _KAPPA * ru, end), (u1, start + sign * _KAPPA * rv), (u1, start),
            (u1, base), (u0, base),
        ]
        codes = [
            MplPath.MOVETO, MplPath.LINETO,
            MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
            MplPath.LINETO,
            MplPath.CURVE4, MplPath.CURVE4, MplPath.CURVE4,
            MplPath.LINETO, MplPath.CLOSEPOLY,
        ]
    if horizontal:
        points = [(v, u) for u, v in points]
    return MplPath(points, codes)


def _add_bar(ax, colour: str, u0: float, u1: float, base: float, end: float,
             scale_u: float, scale_v: float, horizontal: bool, round_end: bool = True) -> None:
    from matplotlib.patches import PathPatch

    if end == base:
        return
    path = _bar_path(u0, u1, base, end, _RADIUS_PT / scale_u, _RADIUS_PT / scale_v, horizontal, round_end)
    ax.add_patch(PathPatch(path, facecolor=colour, edgecolor="none", linewidth=0, zorder=2))


def _overlaps(boxes: Sequence[Any], pad: float = 1.0) -> bool:
    for index, first in enumerate(boxes):
        for second in boxes[index + 1:]:
            if (first.x0 - pad < second.x1 and second.x0 - pad < first.x1
                    and first.y0 - pad < second.y1 and second.y0 - pad < first.y1):
                return True
    return False


def _headroom_limit(low: float, high: float, extreme: float, need_pt: float, axis_pt: float) -> float:
    """The upper limit that leaves ``need_pt`` points beyond ``extreme`` for a label."""
    if axis_pt <= need_pt * 1.5:
        return high
    span = (extreme - low) / (1 - need_pt / axis_pt)
    return max(high, low + span)


def _wrap(label: str, width: int = 14) -> str:
    return "\n".join(textwrap.wrap(label, width=width, break_long_words=False)) or label


def _default_size(width_px: int, height_px: int | None, fallback: int, note: str = "") -> tuple[int, int]:
    """The drawing size. A default height grows by the lines a note needs, so the plot keeps its size."""
    if height_px is not None:
        return int(width_px), int(height_px)
    extra = 0
    if note:
        # About 0.55em per character at the note's 11.3px size; an estimate is
        # enough, because the layout itself makes the final room.
        lines = math.ceil(len(note) * 0.55 * _NOTE_SIZE / _PT_PER_PX / max(1, width_px - 24))
        extra = 14 + 16 * lines
    return int(width_px), int(fallback + extra)


# --- bars --------------------------------------------------------------------


def _draw_bars(
    figure,
    colours: _Colours,
    categories: list[str],
    series: list[tuple[str, int | None, list[float | None]]],
    *,
    stacked: bool,
    orientation: str,
    value_label: str,
    category_label: str,
    value_format: Formatter,
    direct_labels: str,
    value_limits: tuple[float, float] | None,
) -> None:
    horizontal = orientation == "horizontal"
    value_axis = "x" if horizontal else "y"
    category_axis = "y" if horizontal else "x"
    ax = figure.add_subplot()
    _style_axes(ax, colours, value_axis)
    count = len(categories)
    every_value = [value for _, _, values in series for value in values]
    fmt = _formatter(value_format, every_value)

    if stacked:
        if any(value is not None and value < 0 for value in every_value):
            raise ChartInputError("stacked bars need values of zero or more")
        totals = [
            None if any(values[i] is None for _, _, values in series)
            else sum(values[i] for _, _, values in series)
            for i in range(count)
        ]
        extent = [total for total in totals if total is not None]
    else:
        totals = []
        extent = [value for value in every_value if value is not None]

    low = min([0.0] + extent)
    high = max([0.0] + extent)
    if value_limits is not None:
        low, high = float(value_limits[0]), float(value_limits[1])
        ticks = _nice_ticks(low, high)
        ticks = [tick for tick in ticks if low - 1e-9 <= tick <= high + 1e-9]
    else:
        if high == low:
            high = low + 1.0
        ticks = _nice_ticks(low, high)
        low, high = ticks[0], ticks[-1]

    positions = list(range(count))
    if horizontal:
        ax.set_yticks(positions, categories)
        ax.set_ylim(count - 0.5, -0.5)
        ax.set_xlim(low, high)
    else:
        ax.set_xticks(positions, [_wrap(category) for category in categories])
        ax.set_xlim(-0.5, count - 0.5)
        ax.set_ylim(low, high)
    _set_value_ticks(ax, value_axis, ticks, value_format)
    _axis_label(ax, value_axis, value_label, colours)
    _axis_label(ax, category_axis, category_label, colours)
    if len(series) >= 2:
        _legend(ax, [(name, colours.slot_colour(slot)) for name, slot, _ in series], colours, "bar")
    (ax.axvline if horizontal else ax.axhline)(0, color=colours.axis, linewidth=_HAIRLINE_PT, zorder=1.5)

    _freeze_layout(figure)

    # Which values get a label at the bar end.
    groups = 1 if stacked else len(series)
    bars = count * groups
    mode = direct_labels
    if mode == "auto":
        if stacked or groups == 1:
            mode = "all" if count <= 8 else "max"
        else:
            mode = "all" if bars <= 8 else "none"
    labelled: list[tuple[int, int, float]] = []  # (category, group, value)
    if stacked:
        candidates = [(i, 0, total) for i, total in enumerate(totals) if total is not None]
    else:
        candidates = [
            (i, g, values[i]) for g, (_, _, values) in enumerate(series)
            for i in range(count) if values[i] is not None
        ]
    if mode == "all":
        labelled = candidates
    elif mode == "max" and candidates:
        labelled = [max(candidates, key=lambda item: item[2])]

    # Leave room past the longest labelled bar so its label stays inside the plot.
    scale_x, scale_y = _points_per_unit(ax)
    scale_u, scale_v = (scale_y, scale_x) if horizontal else (scale_x, scale_y)
    axis_pt = (high - low) * scale_v
    if labelled:
        if horizontal:
            need = max(_text_size(figure, fmt(value))[0] for _, _, value in labelled) + 6
        else:
            need = _FONT_SIZE * 1.25 + 4
        positive = [value for _, _, value in labelled if value >= 0]
        if positive and value_limits is None:
            high = _headroom_limit(low, high, max(positive), need, axis_pt)
        negative = [value for _, _, value in labelled if value < 0]
        if negative and value_limits is None:
            low = -_headroom_limit(-high, -low, -min(negative), need, axis_pt)
        (ax.set_xlim if horizontal else ax.set_ylim)(low, high)
        scale_x, scale_y = _points_per_unit(ax)
        scale_u, scale_v = (scale_y, scale_x) if horizontal else (scale_x, scale_y)

    # Bar geometry in points, converted to data units.
    slot_pt = scale_u
    thickness = min(_BAR_MAX_PT, max(2.0, (0.72 * slot_pt - (groups - 1) * _GAP_PT) / groups))
    group_pt = groups * thickness + (groups - 1) * _GAP_PT

    def span(category: int, group: int) -> tuple[float, float]:
        start = -group_pt / 2 + group * (thickness + _GAP_PT)
        return category + start / slot_pt, category + (start + thickness) / slot_pt

    suppressed: list[tuple[int, int]] = []
    if stacked:
        gap_v = _GAP_PT / scale_v
        for i in range(count):
            if totals[i] is None:
                suppressed.append((i, 0))
                continue
            u0, u1 = span(i, 0)
            cumulative = 0.0
            visible = [s for s in range(len(series)) if series[s][2][i] > 0]
            for s, (_, slot, values) in enumerate(series):
                value = values[i]
                if value <= 0:
                    continue
                start = cumulative + (gap_v if s != visible[0] else 0.0)
                end = cumulative + value
                if end > start:
                    _add_bar(ax, colours.slot_colour(slot), u0, u1, start, end, scale_u, scale_v,
                             horizontal, round_end=s == visible[-1])
                cumulative = end
    else:
        for g, (_, slot, values) in enumerate(series):
            for i, value in enumerate(values):
                if value is None:
                    suppressed.append((i, g))
                    continue
                u0, u1 = span(i, g)
                _add_bar(ax, colours.slot_colour(slot), u0, u1, 0.0, value, scale_u, scale_v, horizontal)

    texts = []
    for i, g, value in labelled:
        u0, u1 = span(i, g)
        centre = (u0 + u1) / 2
        if horizontal:
            offset, align = ((4, 0), dict(ha="left", va="center")) if value >= 0 else ((-4, 0), dict(ha="right", va="center"))
            xy = (value, centre)
        else:
            offset, align = ((0, 3), dict(ha="center", va="bottom")) if value >= 0 else ((0, -3), dict(ha="center", va="top"))
            xy = (centre, value)
        texts.append(ax.annotate(fmt(value), xy, xytext=offset, textcoords="offset points",
                                 fontsize=_FONT_SIZE, color=colours.ink, annotation_clip=False, **align))
    renderer = figure.canvas.get_renderer()
    if texts and _overlaps([text.get_window_extent(renderer) for text in texts]):
        for text in texts:
            text.remove()

    for i, g in suppressed:
        u0, u1 = span(i, g)
        centre = (u0 + u1) / 2
        if horizontal:
            ax.annotate(_SUPPRESSED, (0, centre), xytext=(4, 0), textcoords="offset points",
                        ha="left", va="center", fontsize=_SMALL_SIZE, color=colours.ink3, annotation_clip=False)
        else:
            width = _text_size(figure, _SUPPRESSED, _SMALL_SIZE)[0]
            rotate = groups > 1 or width > 0.9 * slot_pt
            ax.annotate(_SUPPRESSED, (centre, 0), xytext=(0, 4), textcoords="offset points",
                        ha="center", va="bottom", rotation=90 if rotate else 0,
                        fontsize=_SMALL_SIZE, color=colours.ink3, annotation_clip=False)


def _bar_height(count: int, groups: int, orientation: str) -> int:
    if orientation == "horizontal":
        return max(200, 90 + count * (groups * 24 + 20))
    return 360


def bar_chart(
    categories: Sequence[str],
    values: Sequence[Number | None],
    *,
    theme: str = "light",
    title: str = "",
    description: str = "",
    value_label: str = "",
    category_label: str = "",
    value_format: Formatter = None,
    orientation: str = "vertical",
    direct_labels: str = "auto",
    value_limits: tuple[float, float] | None = None,
    series_slot: int | None = 1,
    note: str = "",
    width_px: int = 640,
    height_px: int | None = None,
    tokens: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """One series of bars, one per category, in one series colour.

    Params:
    - `categories`, `values`: the bars; ``None`` leaves a gap labelled "suppressed".
    - `theme`: ``"light"`` or ``"dark"``.
    - `title`, `description`: written into the SVG's ``<title>`` and ``<desc>``;
      nothing is drawn. Without a description, one is written from the data.
    - `value_label`, `category_label`: axis labels.
    - `value_format`: a format string (``"{:.0%}"``) or a function for value
      labels; percentage formats also turn the axis ticks into percentages.
    - `orientation`: ``"vertical"`` or ``"horizontal"`` (for long category names).
    - `direct_labels`: ``"auto"`` (every bar up to eight bars, else the largest),
      ``"all"``, ``"max"`` or ``"none"``. Labels that would overlap are dropped.
    - `value_limits`: fixed axis limits; by default the axis starts at zero.
    - `series_slot`: the colour slot (1 to 3) of the entity the bars belong
      to, so a chart about one entity keeps that entity's colour. Default 1.
      ``None`` draws in neutral ink (``--ds-ink-2``), for an entity with no
      series colour, such as a fourth one.
    - `note`: a source or licence line drawn under the chart in small type,
      for data whose licence asks for attribution on every chart.
    - `width_px`, `height_px`: the size the SVG is drawn for.
    - `tokens`: colour tokens to use instead of ``TOKENS`` (see ``load_tokens``).

    Returns:
    - The SVG document as text.
    """
    labels = _labels(categories)
    checked = _values(values, len(labels), "values")
    slot = _slot(series_slot)
    _option(orientation, _ORIENTATIONS, "orientation")
    _option(direct_labels, _LABEL_MODES, "direct_labels")
    colours = _Colours(theme, tokens)
    width, height = _default_size(width_px, height_px, _bar_height(len(labels), 1, orientation), note)
    fmt = _formatter(value_format, checked)
    table = bar_chart_table(labels, values, category_label=category_label or "Category",
                            value_label=value_label or "Value")
    payload = {"kind": "bar", "categories": labels, "values": checked, "theme": theme,
               "title": title, "description": description, "labels": [value_label, category_label],
               "format": _format_key(value_format), "orientation": orientation, "direct": direct_labels,
               "limits": value_limits, "size": [width, height], "tokens": _tokens_key(tokens), "note": note,
               "slot": slot}
    return _render(
        payload, colours, width, height, title or "Bar chart",
        description or _describe("Bar chart", table, fmt),
        lambda figure: _draw_bars(
            figure, colours, labels, [("", slot, checked)], stacked=False, orientation=orientation,
            value_label=value_label, category_label=category_label, value_format=value_format,
            direct_labels=direct_labels, value_limits=value_limits,
        ),
        note=note,
    )


def grouped_bar_chart(
    categories: Sequence[str],
    series: Mapping[str, Sequence[Number | None]],
    *,
    theme: str = "light",
    title: str = "",
    description: str = "",
    value_label: str = "",
    category_label: str = "",
    value_format: Formatter = None,
    orientation: str = "vertical",
    direct_labels: str = "auto",
    value_limits: tuple[float, float] | None = None,
    series_slots: Mapping[str, int] | None = None,
    note: str = "",
    width_px: int = 640,
    height_px: int | None = None,
    tokens: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Up to three series side by side within each category, with a legend.

    Params are those of ``bar_chart``, plus:
    - `series`: series name to values, in legend order. At most three.
    - `series_slots`: series name to colour slot (1 to 3). Give it when a chart
      shows only some of a fixed set of entities, so each keeps its colour.
    - `direct_labels`: ``"auto"`` labels every bar when there are at most eight
      bars in all, else none; the table view holds every value.

    Returns:
    - The SVG document as text.
    """
    labels = _labels(categories)
    checked = _series(series, len(labels), series_slots)
    _option(orientation, _ORIENTATIONS, "orientation")
    _option(direct_labels, _LABEL_MODES, "direct_labels")
    colours = _Colours(theme, tokens)
    width, height = _default_size(width_px, height_px, _bar_height(len(labels), len(checked), orientation), note)
    fmt = _formatter(value_format, [v for _, _, values in checked for v in values])
    table = grouped_bar_chart_table(labels, series, category_label=category_label or "Category")
    payload = {"kind": "grouped_bar", "categories": labels, "series": checked, "theme": theme,
               "title": title, "description": description, "labels": [value_label, category_label],
               "format": _format_key(value_format), "orientation": orientation, "direct": direct_labels,
               "limits": value_limits, "size": [width, height], "tokens": _tokens_key(tokens), "note": note}
    return _render(
        payload, colours, width, height, title or "Grouped bar chart",
        description or _describe("Grouped bar chart", table, fmt),
        lambda figure: _draw_bars(
            figure, colours, labels, checked, stacked=False, orientation=orientation,
            value_label=value_label, category_label=category_label, value_format=value_format,
            direct_labels=direct_labels, value_limits=value_limits,
        ),
        note=note,
    )


def stacked_bar_chart(
    categories: Sequence[str],
    series: Mapping[str, Sequence[Number | None]],
    *,
    theme: str = "light",
    title: str = "",
    description: str = "",
    value_label: str = "",
    category_label: str = "",
    value_format: Formatter = None,
    orientation: str = "vertical",
    direct_labels: str = "auto",
    value_limits: tuple[float, float] | None = None,
    series_slots: Mapping[str, int] | None = None,
    note: str = "",
    width_px: int = 640,
    height_px: int | None = None,
    tokens: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Up to three series stacked into one bar per category, with a legend.

    Values must be zero or more. When any part of a category is ``None`` the
    whole bar is left out and labelled "suppressed", because the other parts
    alone would show a wrong total. Direct labels show each bar's total.

    Params are those of ``grouped_bar_chart``.

    Returns:
    - The SVG document as text.
    """
    labels = _labels(categories)
    checked = _series(series, len(labels), series_slots)
    _option(orientation, _ORIENTATIONS, "orientation")
    _option(direct_labels, _LABEL_MODES, "direct_labels")
    if any(v is not None and v < 0 for _, _, values in checked for v in values):
        raise ChartInputError("stacked bars need values of zero or more")
    colours = _Colours(theme, tokens)
    width, height = _default_size(width_px, height_px, _bar_height(len(labels), 1, orientation), note)
    fmt = _formatter(value_format, [v for _, _, values in checked for v in values])
    table = stacked_bar_chart_table(labels, series, category_label=category_label or "Category")
    payload = {"kind": "stacked_bar", "categories": labels, "series": checked, "theme": theme,
               "title": title, "description": description, "labels": [value_label, category_label],
               "format": _format_key(value_format), "orientation": orientation, "direct": direct_labels,
               "limits": value_limits, "size": [width, height], "tokens": _tokens_key(tokens), "note": note}
    return _render(
        payload, colours, width, height, title or "Stacked bar chart",
        description or _describe("Stacked bar chart", table, fmt),
        lambda figure: _draw_bars(
            figure, colours, labels, checked, stacked=True, orientation=orientation,
            value_label=value_label, category_label=category_label, value_format=value_format,
            direct_labels=direct_labels, value_limits=value_limits,
        ),
        note=note,
    )


# --- histogram -----------------------------------------------------------------


def _draw_histogram(
    figure,
    colours: _Colours,
    edges: list[float],
    series: list[tuple[str, int | None, list[float | None]]],
    *,
    x_label: str,
    y_label: str,
    value_format: Formatter,
    edge_format: Formatter,
) -> None:
    ax = figure.add_subplot()
    _style_axes(ax, colours, "y")
    ax.tick_params(axis="x", labelcolor=colours.ink3)
    present = [value for _, _, values in series for value in values if value is not None]
    if any(value < 0 for value in present):
        raise ChartInputError("histogram counts must be zero or more")
    ticks = _nice_ticks(0.0, max(present, default=1.0) or 1.0)
    ax.set_ylim(ticks[0], ticks[-1])
    _set_value_ticks(ax, "y", ticks, value_format)
    ax.set_xlim(edges[0], edges[-1])
    edge_text = _edge_formatter(edges, edge_format)
    if len(edges) <= 13:
        ax.set_xticks(edges, [edge_text(edge) for edge in edges])
    else:
        from matplotlib.ticker import FuncFormatter, MaxNLocator

        ax.xaxis.set_major_locator(MaxNLocator(nbins=8))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _position: edge_text(value)))
    _axis_label(ax, "x", x_label, colours)
    _axis_label(ax, "y", y_label, colours)
    if len(series) >= 2:
        _legend(ax, [(name, colours.slot_colour(slot)) for name, slot, _ in series], colours, "bar")
    ax.axhline(0, color=colours.axis, linewidth=_HAIRLINE_PT, zorder=1.5)

    _freeze_layout(figure)
    scale_x, scale_y = _points_per_unit(ax)
    groups = len(series)
    for b, (left, right) in enumerate(zip(edges, edges[1:])):
        bin_pt = (right - left) * scale_x
        if groups == 1:
            # One series: bins touch, separated only by the surface gap. A bin
            # wider than 72px gets a 72px bar, so a few bins do not draw as
            # solid blocks.
            thickness = max(1.0, min(3 * _BAR_MAX_PT, bin_pt - _GAP_PT))
            first_pt = (bin_pt - thickness) / 2
        else:
            # Several series: each bin's bars sit together, with air between
            # bins, so a reader sees one group per bin.
            room = 0.78 * bin_pt - (groups - 1) * _GAP_PT
            thickness = max(1.0, min(_BAR_MAX_PT, room / groups))
            first_pt = (bin_pt - (groups * thickness + (groups - 1) * _GAP_PT)) / 2
        for g, (_, slot, values) in enumerate(series):
            start_pt = first_pt + g * (thickness + _GAP_PT)
            u0 = left + start_pt / scale_x
            u1 = left + (start_pt + thickness) / scale_x
            value = values[b]
            if value is None:
                rotate = groups > 1 or _text_size(figure, _SUPPRESSED, _SMALL_SIZE)[0] > 0.9 * bin_pt
                ax.annotate(_SUPPRESSED, ((u0 + u1) / 2, 0), xytext=(0, 4), textcoords="offset points",
                            ha="center", va="bottom", rotation=90 if rotate else 0,
                            fontsize=_SMALL_SIZE, color=colours.ink3, annotation_clip=False)
                continue
            _add_bar(ax, colours.slot_colour(slot), u0, u1, 0.0, value, scale_x, scale_y, horizontal=False)


def histogram(
    bin_edges: Sequence[Number],
    counts: Sequence[Number | None] | Mapping[str, Sequence[Number | None]],
    *,
    theme: str = "light",
    title: str = "",
    description: str = "",
    x_label: str = "",
    y_label: str = "Count",
    value_format: Formatter = None,
    edge_format: Formatter = None,
    series_slots: Mapping[str, int] | None = None,
    series_slot: int | None = 1,
    note: str = "",
    width_px: int = 640,
    height_px: int | None = None,
    tokens: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Counts per bin, drawn from bins the caller already computed.

    Params:
    - `bin_edges`: increasing edges; there is one bin between each pair.
    - `counts`: one count per bin, or a mapping of up to three named series,
      drawn side by side within each bin with a legend. ``None`` leaves a gap
      labelled "suppressed".
    - `x_label`, `y_label`: axis labels.
    - `edge_format`: format for the bin edges on the axis and in the table.
    - `series_slots`: for a mapping, series name to colour slot (1 to 3).
    - `series_slot`: for one series, its colour slot (1 to 3), or ``None``
      for neutral ink. Default 1.
    - Other params as in ``bar_chart``. Histograms carry no value labels; the
      table view holds every count.

    Returns:
    - The SVG document as text.
    """
    edges = _edges(bin_edges)
    slot = _slot(series_slot)
    if isinstance(counts, Mapping) and slot != 0:
        raise ChartInputError("series_slot applies to one series; give series_slots for named series")
    checked = _counts(counts, len(edges) - 1, series_slots)
    if not isinstance(counts, Mapping):
        checked = [(name, slot, values) for name, _, values in checked]
    colours = _Colours(theme, tokens)
    width, height = _default_size(width_px, height_px, 360, note)
    fmt = _formatter(value_format, [v for _, _, values in checked for v in values])
    table = histogram_table(edges, counts, value_label=y_label or "Count", edge_format=edge_format)
    payload = {"kind": "histogram", "edges": edges, "series": checked, "theme": theme, "title": title,
               "description": description, "labels": [x_label, y_label], "format": _format_key(value_format),
               "edge_format": _format_key(edge_format), "size": [width, height], "tokens": _tokens_key(tokens), "note": note}
    return _render(
        payload, colours, width, height, title or "Histogram",
        description or _describe("Histogram", table, fmt),
        lambda figure: _draw_histogram(
            figure, colours, edges, checked, x_label=x_label, y_label=y_label,
            value_format=value_format, edge_format=edge_format,
        ),
        note=note,
    )


#: Most panels ``histogram_panels`` draws. Each panel is about 120px high.
MAX_PANELS = 6


def _draw_histogram_panels(
    figure,
    colours: _Colours,
    edges: list[float],
    panels: list[tuple[str, list[float | None]]],
    slot: int | None,
    *,
    x_label: str,
    y_label: str,
    value_format: Formatter,
    edge_format: Formatter,
) -> None:
    present = [value for _, values in panels for value in values if value is not None]
    if any(value < 0 for value in present):
        raise ChartInputError("histogram counts must be zero or more")
    # One y scale for every panel, so a taller bar means more records in any panel.
    ticks = _nice_ticks(0.0, max(present, default=1.0) or 1.0, nbins=3)
    edge_text = _edge_formatter(edges, edge_format)
    axes = figure.subplots(len(panels), 1, sharex=True, squeeze=False)[:, 0]
    for index, (ax, (label, _values)) in enumerate(zip(axes, panels)):
        _style_axes(ax, colours, "y")
        ax.tick_params(axis="x", labelcolor=colours.ink3)
        ax.set_ylim(ticks[0], ticks[-1])
        _set_value_ticks(ax, "y", ticks, value_format)
        ax.set_xlim(edges[0], edges[-1])
        ax.set_title(label, loc="left", fontsize=_FONT_SIZE, color=colours.ink2, pad=4)
        ax.axhline(0, color=colours.axis, linewidth=_HAIRLINE_PT, zorder=1.5)
        if index == len(panels) - 1:
            if len(edges) <= 13:
                ax.set_xticks(edges, [edge_text(edge) for edge in edges])
            else:
                from matplotlib.ticker import FuncFormatter, MaxNLocator

                ax.xaxis.set_major_locator(MaxNLocator(nbins=8))
                ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _position: edge_text(value)))
            _axis_label(ax, "x", x_label, colours)
        else:
            ax.tick_params(axis="x", labelbottom=False)
    if y_label:
        figure.supylabel(y_label, color=colours.ink2, fontsize=_FONT_SIZE)

    _freeze_layout(figure)
    for ax, (_label, values) in zip(axes, panels):
        scale_x, scale_y = _points_per_unit(ax)
        for b, (left, right) in enumerate(zip(edges, edges[1:])):
            bin_pt = (right - left) * scale_x
            thickness = max(1.0, min(3 * _BAR_MAX_PT, bin_pt - _GAP_PT))
            start_pt = (bin_pt - thickness) / 2
            u0 = left + start_pt / scale_x
            u1 = left + (start_pt + thickness) / scale_x
            value = values[b]
            if value is None:
                rotate = _text_size(figure, _SUPPRESSED, _SMALL_SIZE)[0] > 0.9 * bin_pt
                ax.annotate(_SUPPRESSED, ((u0 + u1) / 2, 0), xytext=(0, 4), textcoords="offset points",
                            ha="center", va="bottom", rotation=90 if rotate else 0,
                            fontsize=_SMALL_SIZE, color=colours.ink3, annotation_clip=False)
                continue
            _add_bar(ax, colours.slot_colour(slot), u0, u1, 0.0, value, scale_x, scale_y, horizontal=False)


def histogram_panels(
    bin_edges: Sequence[Number],
    panels: Mapping[str, Sequence[Number | None]],
    *,
    theme: str = "light",
    title: str = "",
    description: str = "",
    x_label: str = "",
    y_label: str = "Count",
    value_format: Formatter = None,
    edge_format: Formatter = None,
    series_slot: int | None = 1,
    note: str = "",
    width_px: int = 640,
    height_px: int | None = None,
    tokens: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Small multiples: one histogram panel per category, stacked, all in one series colour.

    Use it when the categories are not entities, such as the outcomes recorded
    at one site. A series colour marks an entity, so the categories get a
    panel each, named above it, and every panel uses the entity's slot. The
    panels share the bins and one count scale.

    Params:
    - `bin_edges`: increasing edges; there is one bin between each pair.
    - `panels`: panel name to one count per bin, top to bottom. One to six
      panels. ``None`` leaves a gap labelled "suppressed".
    - `series_slot`: the colour slot (1 to 3) of the entity, or ``None`` for
      neutral ink. Default 1.
    - Other params as in ``histogram``. The table view is ``histogram_table``
      with the panels as columns.

    Returns:
    - The SVG document as text.
    """
    edges = _edges(bin_edges)
    slot = _slot(series_slot)
    if not isinstance(panels, Mapping) or not panels:
        raise ChartInputError("panels must be a non-empty mapping of name to counts")
    if len(panels) > MAX_PANELS:
        raise ChartInputError(f"{len(panels)} panels given; at most {MAX_PANELS} fit in one chart")
    checked = [(str(name), _values(values, len(edges) - 1, f"panel {name!r}")) for name, values in panels.items()]
    colours = _Colours(theme, tokens)
    width, height = _default_size(width_px, height_px, 70 + 120 * len(checked), note)
    fmt = _formatter(value_format, [v for _, values in checked for v in values])
    table = histogram_table(edges, panels, value_label=y_label or "Count", edge_format=edge_format)
    payload = {"kind": "histogram_panels", "edges": edges, "panels": checked, "slot": slot, "theme": theme,
               "title": title, "description": description, "labels": [x_label, y_label],
               "format": _format_key(value_format), "edge_format": _format_key(edge_format),
               "size": [width, height], "tokens": _tokens_key(tokens), "note": note}
    return _render(
        payload, colours, width, height, title or "Histogram panels",
        description or _describe("Histogram panels", table, fmt),
        lambda figure: _draw_histogram_panels(
            figure, colours, edges, checked, slot, x_label=x_label, y_label=y_label,
            value_format=value_format, edge_format=edge_format,
        ),
        note=note,
    )


# --- lines -------------------------------------------------------------------


def _x_values(x: Sequence[Any]) -> tuple[list[Any], bool]:
    """The x values, and whether they are categories (strings) rather than numbers."""
    if isinstance(x, (str, bytes)) or not isinstance(x, Sequence) or not x:
        raise ChartInputError("x must be a non-empty list")
    if all(isinstance(value, str) for value in x):
        return list(x), True
    numbers = []
    for index, value in enumerate(x):
        number = _number(value, f"x[{index}]")
        if number is None:
            raise ChartInputError("x cannot contain None")
        numbers.append(value)
    return numbers, False


def _draw_lines(
    figure,
    colours: _Colours,
    xs: list[Any],
    categorical: bool,
    series: list[tuple[str, int, list[float | None]]],
    *,
    x_label: str,
    y_label: str,
    value_format: Formatter,
    y_limits: tuple[float, float] | None,
    y_from_zero: bool,
    direct_labels: str,
) -> None:
    ax = figure.add_subplot()
    _style_axes(ax, colours, "y")
    ax.tick_params(axis="x", labelcolor=colours.ink3)
    positions = list(range(len(xs))) if categorical else [float(value) for value in xs]
    present = [value for _, _, values in series for value in values if value is not None]
    fmt = _formatter(value_format, present)

    if y_limits is not None:
        low, high = float(y_limits[0]), float(y_limits[1])
        ticks = [tick for tick in _nice_ticks(low, high) if low - 1e-9 <= tick <= high + 1e-9]
    else:
        data_low = min(present, default=0.0)
        data_high = max(present, default=1.0)
        pad = (data_high - data_low) * 0.08 or abs(data_high) * 0.1 or 1.0
        low, high = data_low - pad, data_high + pad
        if y_from_zero:
            low = min(0.0, data_low)
        elif data_low >= 0 and low < 0:
            low = 0.0
        ticks = _nice_ticks(low, high)
        low, high = ticks[0], ticks[-1]
    ax.set_ylim(low, high)
    _set_value_ticks(ax, "y", ticks, value_format)

    if categorical:
        ax.set_xticks(positions, [_wrap(value) for value in xs])
        ax.set_xlim(-0.3, len(xs) - 0.7)
    else:
        span = (max(positions) - min(positions)) or 1.0
        ax.set_xlim(min(positions) - span * 0.03, max(positions) + span * 0.03)
        whole = all(float(value).is_integer() for value in positions)
        if whole and len(positions) <= 20:
            ax.set_xticks(positions, [f"{value:.0f}" for value in positions])
        else:
            from matplotlib.ticker import FuncFormatter, MaxNLocator

            ax.xaxis.set_major_locator(MaxNLocator(nbins=8, integer=whole))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _position: f"{value:.6g}"))
    _axis_label(ax, "x", x_label, colours)
    _axis_label(ax, "y", y_label, colours)
    if len(series) >= 2:
        _legend(ax, [(name, colours.series[slot]) for name, slot, _ in series], colours, "line")

    many_points = len(positions) > 16
    for _, slot, values in series:
        colour = colours.series[slot]
        ys = [math.nan if value is None else value for value in values]
        ax.plot(positions, ys, color=colour, linewidth=_LINE_PT, solid_capstyle="round",
                solid_joinstyle="round", zorder=3)
        marked = [i for i, value in enumerate(values) if value is not None]
        if many_points:
            marked = marked[-1:]
        # A lone point between two gaps has no line, so it always gets a marker.
        isolated = [
            i for i, value in enumerate(values) if value is not None
            and (i == 0 or values[i - 1] is None) and (i == len(values) - 1 or values[i + 1] is None)
        ]
        marked = sorted(set(marked) | set(isolated))
        ax.plot([positions[i] for i in marked], [values[i] for i in marked], linestyle="none",
                marker="o", markersize=_MARKER_PT, markerfacecolor=colour,
                markeredgecolor=colours.panel, markeredgewidth=_GAP_PT, zorder=4, clip_on=False)

    # The value at the end of each line, right of its last point.
    end_labels = []
    if direct_labels != "none":
        for _, _, values in series:
            last = max((i for i, value in enumerate(values) if value is not None), default=None)
            if last is None:
                continue
            end_labels.append(ax.annotate(
                fmt(values[last]), (positions[last], values[last]), xytext=(7, 0),
                textcoords="offset points", ha="left", va="center", fontsize=_FONT_SIZE,
                color=colours.ink, annotation_clip=False,
            ))
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    if end_labels and _overlaps([label.get_window_extent(renderer) for label in end_labels], pad=2.0):
        # Converging lines: stacking labels would detach them from their lines.
        # The legend and the table carry the values instead.
        for label in end_labels:
            label.remove()
    _freeze_layout(figure)

    gaps = sorted({i for _, _, values in series for i, value in enumerate(values) if value is None})
    for i in gaps:
        missing = [name for name, _, values in series if values[i] is None]
        # A gap in one of several lines names the line, because a gap in a
        # line has no position of its own to show which line it is.
        text = _SUPPRESSED if len(missing) == len(series) else f"{', '.join(missing)} {_SUPPRESSED}"
        ax.annotate(text, (positions[i], low), xytext=(0, 4), textcoords="offset points",
                    ha="center", va="bottom", fontsize=_SMALL_SIZE, color=colours.ink3,
                    annotation_clip=False)


def line_chart(
    x: Sequence[Number | str],
    series: Mapping[str, Sequence[Number | None]],
    *,
    theme: str = "light",
    title: str = "",
    description: str = "",
    x_label: str = "",
    y_label: str = "",
    value_format: Formatter = None,
    y_limits: tuple[float, float] | None = None,
    y_from_zero: bool = False,
    direct_labels: str = "auto",
    series_slots: Mapping[str, int] | None = None,
    note: str = "",
    width_px: int = 640,
    height_px: int | None = None,
    tokens: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Up to three lines over a shared x axis, with a legend for two or more.

    Params:
    - `x`: numbers (for example round numbers) or category labels.
    - `series`: series name to values; ``None`` breaks the line and is labelled
      "suppressed" at the bottom of the plot.
    - `y_limits`: fixed limits; by default the axis fits the data, and it never
      goes below zero for data that does not.
    - `y_from_zero`: start the axis at zero.
    - `direct_labels`: ``"auto"`` or ``"all"`` label each line's last value,
      unless two labels would overlap; ``"none"`` turns them off.
    - Other params as in ``grouped_bar_chart``.

    Returns:
    - The SVG document as text.
    """
    xs, categorical = _x_values(x)
    checked = _series(series, len(xs), series_slots)
    _option(direct_labels, _LABEL_MODES, "direct_labels")
    colours = _Colours(theme, tokens)
    width, height = _default_size(width_px, height_px, 360, note)
    fmt = _formatter(value_format, [v for _, _, values in checked for v in values])
    table = line_chart_table(xs, series, x_label=x_label or "x")
    payload = {"kind": "line", "x": xs, "series": checked, "theme": theme, "title": title,
               "description": description, "labels": [x_label, y_label], "format": _format_key(value_format),
               "limits": y_limits, "zero": y_from_zero, "direct": direct_labels,
               "size": [width, height], "tokens": _tokens_key(tokens), "note": note}
    return _render(
        payload, colours, width, height, title or "Line chart",
        description or _describe("Line chart", table, fmt),
        lambda figure: _draw_lines(
            figure, colours, xs, categorical, checked, x_label=x_label, y_label=y_label,
            value_format=value_format, y_limits=y_limits, y_from_zero=y_from_zero,
            direct_labels=direct_labels,
        ),
        note=note,
    )


# --- dots against thresholds -------------------------------------------------

_OUTCOMES = ("passed", "failed", "not_evaluated")
_DIRECTIONS = {"at_least": "at least", "at_most": "at most"}


def _dot_items(items: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate the items of a dot chart and add the words shown for each."""
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence) or not items:
        raise ChartInputError("items must be a non-empty list")
    result = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ChartInputError(f"items[{index}] must be a mapping")
        label = str(item.get("label", "")).strip()
        if not label:
            raise ChartInputError(f"items[{index}] needs a label")
        outcome = item.get("outcome")
        if outcome not in _OUTCOMES:
            raise ChartInputError(f"items[{index}].outcome must be one of {_OUTCOMES}, not {outcome!r}")
        direction = item.get("direction")
        if direction not in _DIRECTIONS:
            raise ChartInputError(
                f"items[{index}].direction must be one of {tuple(_DIRECTIONS)}, not {direction!r}"
            )
        threshold = _number(item.get("threshold"), f"items[{index}].threshold")
        if threshold is None:
            raise ChartInputError(f"items[{index}] needs a threshold")
        observed = _number(item.get("observed"), f"items[{index}].observed")
        blocking = bool(item.get("blocking", True))
        span = item.get("range")
        if span is not None:
            if isinstance(span, (str, bytes)) or not isinstance(span, Sequence) or len(span) != 2:
                raise ChartInputError(f"items[{index}].range must be (low, high)")
            low, high = (_number(value, f"items[{index}].range") for value in span)
            if low is None or high is None or high <= low:
                raise ChartInputError(f"items[{index}].range needs two numbers with low below high")
            span = (low, high)
        fmt = _formatter(item.get("format"), [threshold, observed])
        if outcome == "passed":
            outcome_text = "Passed"
        elif outcome == "failed":
            outcome_text = "Failed" if blocking else "Failed (advisory)"
        else:
            outcome_text = "Not evaluated"
        result.append({
            "label": label,
            "observed": observed,
            "raw_observed": item.get("observed"),
            "threshold": threshold,
            "direction": direction,
            "outcome": outcome,
            "blocking": blocking,
            "format": fmt,
            "required": f"{_DIRECTIONS[direction]} {fmt(threshold)}",
            "outcome_text": outcome_text,
            "range": span,
        })
    return result


def _dot_range(item: Mapping[str, Any]) -> tuple[float, float]:
    if item["range"] is not None:
        return item["range"]
    values = [item["threshold"]] + ([item["observed"]] if item["observed"] is not None else [])
    low, high = min(values + [0.0]), max(values)
    if low >= 0 and high <= 1:
        return 0.0, 1.0
    ticks = _nice_ticks(low, high * 1.15 if high > 0 else high + 1)
    return ticks[0], ticks[-1]


def _draw_dots(figure, colours: _Colours, items: list[dict[str, Any]]) -> None:
    grid = figure.add_gridspec(len(items), 1)
    for row, item in enumerate(items):
        ax = figure.add_subplot(grid[row, 0])
        ax.set_facecolor(colours.panel)
        for spine in ax.spines.values():
            spine.set_visible(False)
        low, high = _dot_range(item)
        pad = (high - low) * 0.03
        ax.set_xlim(low - pad, high + pad)
        ax.set_ylim(-1, 1)
        ax.set_xticks([])
        ax.set_yticks([0], [item["label"]])
        ax.tick_params(axis="y", length=0, pad=10, labelsize=_FONT_SIZE, labelcolor=colours.ink)

        threshold = item["threshold"]
        # The track, and the side of the threshold that passes as a wider band.
        ax.plot([low, high], [0, 0], color=colours.axis, linewidth=_HAIRLINE_PT, solid_capstyle="butt", zorder=1)
        passing = (threshold, high) if item["direction"] == "at_least" else (low, threshold)
        ax.plot(passing, [0, 0], color=colours.grid, linewidth=6, solid_capstyle="butt", zorder=1.5)
        ax.plot([threshold, threshold], [-0.42, 0.42], color=colours.ink2, linewidth=_LINE_PT,
                solid_capstyle="butt", zorder=2)
        ax.annotate(item["required"], (threshold, -0.42), xytext=(0, -2), textcoords="offset points",
                    ha="center", va="top", fontsize=_SMALL_SIZE, color=colours.ink2, annotation_clip=False)

        observed = item["observed"]
        if observed is not None and item["outcome"] != "not_evaluated":
            if item["outcome"] == "passed":
                colour, marker, size = colours.permit, "o", _MARKER_PT + 2
            else:
                colour = colours.deny if item["blocking"] else colours.narrow
                marker, size = "X", _MARKER_PT + 5
            ax.plot([observed], [0], linestyle="none", marker=marker, markersize=size,
                    markerfacecolor=colour, markeredgecolor=colours.panel, markeredgewidth=_GAP_PT,
                    zorder=4, clip_on=False)
            ax.annotate(item["format"](observed), (observed, 0.3), xytext=(0, 2), textcoords="offset points",
                        ha="center", va="bottom", fontsize=_SMALL_SIZE, color=colours.ink, annotation_clip=False)
        ax.text(1.03, 0.5, item["outcome_text"], transform=ax.transAxes, ha="left", va="center",
                fontsize=_FONT_SIZE, color=colours.ink)


def dot_threshold_chart(
    items: Sequence[Mapping[str, Any]],
    *,
    theme: str = "light",
    title: str = "",
    description: str = "",
    note: str = "",
    width_px: int = 640,
    height_px: int | None = None,
    tokens: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """Observed values against their thresholds, one row per check, each row on its own scale.

    The checks usually measure different things (an AUC, a rate per 1,000), so
    each row has its own scale instead of sharing one axis. The outcome is the
    caller's: this function never decides whether a value passes, it shows the
    decision it is given.

    Params:
    - `items`: mappings with ``label``, ``observed`` (``None`` when missing),
      ``threshold``, ``direction`` (``"at_least"`` or ``"at_most"``) and
      ``outcome`` (``"passed"``, ``"failed"`` or ``"not_evaluated"``, the
      framework's gate outcomes). Optional: ``blocking`` (default ``True``; a
      failed advisory check is drawn in the narrow colour), ``format`` and
      ``range`` (``(low, high)``).
    - Other params as in ``bar_chart``.

    What it draws:
    - A passed check is a circle in the permit colour, a failed blocking check
      an X in the deny colour, a failed advisory check an X in the narrow colour.
      Each row also states its outcome in words, so colour is never the only cue.

    Returns:
    - The SVG document as text.
    """
    checked = _dot_items(items)
    colours = _Colours(theme, tokens)
    width, height = _default_size(width_px, height_px, max(130, 40 + 58 * len(checked)), note)
    table = dot_threshold_chart_table(items)
    payload = {"kind": "dot_threshold",
               "items": [{k: v for k, v in item.items() if k != "format"} for item in checked],
               "formats": [_format_key(item.get("format")) for item in items], "theme": theme,
               "title": title, "description": description, "size": [width, height], "tokens": _tokens_key(tokens), "note": note}
    return _render(
        payload, colours, width, height, title or "Checks against thresholds",
        description or _describe("Checks against thresholds", table, str),
        lambda figure: _draw_dots(figure, colours, checked),
        note=note,
    )
