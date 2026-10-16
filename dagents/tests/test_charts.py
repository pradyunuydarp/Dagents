"""Tests for the SVG chart renderer in dagents/common/infrastructure/charts.py.

The renderer serves pictures to people, so the tests check what a reader
depends on: the SVG parses, it names itself for screen readers, every colour in
it is a design-system token for the requested theme, the same input gives the
same bytes, and the table view holds the same values as the picture.

The token tests and the missing-matplotlib tests need no matplotlib. The
rendering tests skip with a message when matplotlib is not installed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

from dagents.common.infrastructure import charts


REPO_ROOT = Path(__file__).resolve().parents[2]
DESIGN_SYSTEM = REPO_ROOT / "design" / "dagents-design-system.css"
SVG_NS = "{http://www.w3.org/2000/svg}"
HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None
NO_MATPLOTLIB = "matplotlib is not installed; install it to test chart rendering"

HOSPITALS = ["Hospital 134", "Hospital 2", "Hospital 245"]
GATES = [
    {"label": "discrimination", "observed": 0.74, "threshold": 0.70,
     "direction": "at_least", "outcome": "passed"},
    {"label": "subgroup_fairness", "observed": 0.13, "threshold": 0.10,
     "direction": "at_most", "outcome": "failed"},
    {"label": "alert_burden", "observed": 812.0, "threshold": 750.0,
     "direction": "at_most", "outcome": "failed", "blocking": False, "format": "{:.0f}"},
    {"label": "sensitivity_at_alert_threshold", "observed": None, "threshold": 0.80,
     "direction": "at_least", "outcome": "not_evaluated"},
]


def chart_cases() -> dict[str, tuple]:
    """One call per chart function, with suppressed values where they apply."""
    return {
        "bar": (charts.bar_chart, (HOSPITALS, [524, 292, None]),
                {"value_label": "Records", "category_label": "Hospital"}),
        "bar_horizontal": (charts.bar_chart,
                           (["Face weakness", "Arm weakness", "Speech difficulty"], [0.71, 0.66, 0.48]),
                           {"orientation": "horizontal", "value_format": "{:.0%}"}),
        "grouped": (charts.grouped_bar_chart,
                    (HOSPITALS, {"Current release": [0.71, 0.69, 0.73],
                                 "Candidate": [0.74, None, 0.75]}),
                    {"value_label": "AUC", "value_format": "{:.2f}"}),
        "grouped_three": (charts.grouped_bar_chart,
                          (["<50", "50-59", "60-69"], {"A": [1, 2, 3], "B": [2, 3, 4], "C": [3, 4, 5]}),
                          {}),
        "stacked": (charts.stacked_bar_chart,
                    (["<50", "50-59", "60-69", "70-79"],
                     {"Poor outcome": [10, 30, 70, 120], "Good outcome": [40, 50, 60, None]}),
                    {"value_label": "Patients"}),
        "histogram": (charts.histogram, ([60, 100, 140, 180, 220, 260], [3, 40, 120, None, 12]),
                      {"x_label": "Systolic BP (mmHg)"}),
        "histogram_series": (charts.histogram,
                             ([0, 0.25, 0.5, 0.75, 1.0], {"Poor outcome": [5, 20, 40, 60],
                                                          "Good outcome": [50, 30, 12, 4]}),
                             {"x_label": "Risk score"}),
        "histogram_panels": (charts.histogram_panels,
                             ([0, 0.25, 0.5, 0.75, 1.0], {"Poor outcome": [5, 20, None, 60],
                                                          "No poor outcome": [50, 30, 12, 4],
                                                          "Outcome not recorded": [1, 2, 3, 4]}),
                             {"x_label": "Risk score", "series_slot": 2}),
        "line": (charts.line_chart, ([1, 2, 3, 4], {"Training loss": [0.69, 0.61, None, 0.55]}),
                 {"x_label": "Round", "y_label": "Loss"}),
        "line_series": (charts.line_chart,
                        ([1, 2, 3], {"Hospital 134": [0.6, 0.55, 0.52], "Hospital 2": [0.7, 0.66, 0.6],
                                     "Hospital 245": [0.65, 0.58, 0.57]}),
                        {"x_label": "Round"}),
        "dots": (charts.dot_threshold_chart, (GATES,), {}),
    }


def svg_colours(svg: str) -> set[str]:
    """Every hex colour the SVG uses, lower-cased."""
    return {match.lower() for match in re.findall(r"#[0-9a-fA-F]{6}\b", svg)}


def svg_texts(svg: str) -> list[str]:
    """The text of every <text> element (drawn text, not <title> or <desc>)."""
    root = ET.fromstring(svg.split("?>", 1)[1])
    return ["".join(node.itertext()) for node in root.iter(f"{SVG_NS}text")]


class TokenMirrorTests(unittest.TestCase):
    """The Python mirror of the colour tokens must match the CSS source."""

    def test_the_mirror_matches_the_design_system(self) -> None:
        parsed = charts.parse_design_tokens(DESIGN_SYSTEM.read_text(encoding="utf-8"))
        for theme in charts.THEMES:
            for token in charts.CHART_TOKENS:
                with self.subTest(theme=theme, token=token):
                    self.assertIn(token, parsed[theme], f"{token} is missing from the design system")
                    self.assertEqual(
                        parsed[theme][token],
                        charts.TOKENS[theme][token],
                        f"charts.TOKENS[{theme!r}][{token!r}] differs from "
                        "design/dagents-design-system.css; copy the CSS value into charts.py",
                    )

    def test_load_tokens_reads_a_design_system_file(self) -> None:
        self.assertEqual(charts.TOKENS, charts.load_tokens(DESIGN_SYSTEM))

    def test_load_tokens_without_a_path_returns_a_copy_of_the_mirror(self) -> None:
        tokens = charts.load_tokens()
        self.assertEqual(charts.TOKENS, tokens)
        tokens["light"]["--ds-panel"] = "#000000"
        self.assertEqual("#ffffff", charts.TOKENS["light"]["--ds-panel"])

    def test_dark_blocks_that_disagree_are_refused(self) -> None:
        css = (
            ":root { --ds-panel: #ffffff; }\n"
            "@media (prefers-color-scheme: dark) {\n"
            '  :root:not([data-theme="light"]) { --ds-panel: #111111; }\n'
            "}\n"
            ':root[data-theme="dark"] { --ds-panel: #222222; }\n'
        )
        with self.assertRaises(ValueError):
            charts.parse_design_tokens(css)

    def test_series_colours_are_not_decision_or_link_colours(self) -> None:
        for theme in charts.THEMES:
            tokens = charts.TOKENS[theme]
            reserved = {tokens[name] for name in ("--ds-permit", "--ds-narrow", "--ds-deny")}
            parsed = charts.parse_design_tokens(DESIGN_SYSTEM.read_text(encoding="utf-8"))
            reserved.add(parsed[theme]["--ds-focus"])
            series = {tokens[f"--ds-series-{slot}"] for slot in (1, 2, 3)}
            with self.subTest(theme=theme):
                self.assertEqual(3, len(series))
                self.assertFalse(series & reserved)


class MissingMatplotlibTests(unittest.TestCase):
    """Without matplotlib, drawing raises a typed error; tables still work."""

    def _without_matplotlib(self):
        blocked = {name: None for name in list(sys.modules) if name.split(".")[0] == "matplotlib"}
        blocked["matplotlib"] = None
        return mock.patch.dict(sys.modules, blocked)

    def test_every_chart_raises_charts_unavailable(self) -> None:
        with self._without_matplotlib():
            self.assertFalse(charts.charts_available())
            for name, (function, args, kwargs) in chart_cases().items():
                with self.subTest(chart=name):
                    with self.assertRaises(charts.ChartsUnavailableError) as raised:
                        function(*args, **kwargs)
                    self.assertIn("matplotlib", str(raised.exception))

    def test_the_error_is_a_runtime_error(self) -> None:
        self.assertTrue(issubclass(charts.ChartsUnavailableError, RuntimeError))

    def test_tables_do_not_need_matplotlib(self) -> None:
        with self._without_matplotlib():
            table = charts.bar_chart_table(HOSPITALS, [524, 292, None])
        self.assertEqual(["Hospital 134", 524], table["rows"][0])


@unittest.skipUnless(HAS_MATPLOTLIB, NO_MATPLOTLIB)
class RenderTests(unittest.TestCase):
    """Every chart function returns valid, accessible, themed SVG."""

    def render(self, name: str, theme: str, **extra) -> str:
        function, args, kwargs = chart_cases()[name]
        return function(*args, **{**kwargs, **extra, "theme": theme})

    def test_every_chart_is_valid_svg_in_both_themes(self) -> None:
        for name in chart_cases():
            for theme in charts.THEMES:
                with self.subTest(chart=name, theme=theme):
                    svg = self.render(name, theme, title=f"{name} title", description=f"{name} summary")
                    self.assertTrue(svg.startswith("<?xml"))
                    root = ET.fromstring(svg.split("?>", 1)[1])
                    self.assertEqual(f"{SVG_NS}svg", root.tag)
                    self.assertEqual("img", root.get("role"))
                    title = root.find(f"{SVG_NS}title")
                    desc = root.find(f"{SVG_NS}desc")
                    self.assertEqual(f"{name} title", title.text)
                    self.assertEqual(f"{name} summary", desc.text)
                    self.assertEqual(f"{title.get('id')} {desc.get('id')}", root.get("aria-labelledby"))

    def test_a_note_is_drawn_under_every_chart_kind(self) -> None:
        note = ("Contains information from the Example database (https://example.org/data), "
                "made available under the ODC Attribution License.")
        for name in chart_cases():
            with self.subTest(chart=name):
                drawn = " ".join(svg_texts(self.render(name, "light", note=note)))
                self.assertIn("ODC Attribution License.", drawn)
                self.assertIn("https://example.org/data", drawn)

    def test_the_title_is_not_drawn(self) -> None:
        svg = self.render("bar", "light", title="Records per hospital")
        self.assertNotIn("Records per hospital", svg_texts(svg))

    def test_a_missing_description_is_written_from_the_data(self) -> None:
        svg = self.render("bar", "light")
        desc = ET.fromstring(svg.split("?>", 1)[1]).find(f"{SVG_NS}desc").text
        self.assertIn("Hospital 134", desc)
        self.assertIn("suppressed", desc)

    def test_the_same_input_gives_the_same_bytes(self) -> None:
        for name in chart_cases():
            for theme in charts.THEMES:
                with self.subTest(chart=name, theme=theme):
                    self.assertEqual(self.render(name, theme), self.render(name, theme))

    def test_the_bytes_do_not_depend_on_the_process(self) -> None:
        """A formatter function must not put its memory address into the SVG ids."""
        script = (
            "import sys\n"
            "from dagents.common.infrastructure import charts\n"
            "def pct(value):\n"
            "    return f'{value:.0%}'\n"
            "sys.stdout.write(charts.bar_chart(['a', 'b'], [0.2, 0.5], value_format=pct,\n"
            "                                  tokens=charts.load_tokens()))\n"
        )
        outputs = [
            subprocess.run(
                [sys.executable, "-c", script], cwd=REPO_ROOT, capture_output=True, text=True, check=True
            ).stdout
            for _ in range(2)
        ]
        self.assertTrue(outputs[0].startswith("<?xml"))
        self.assertEqual(outputs[0], outputs[1])

    def test_the_svg_has_no_date_or_metadata(self) -> None:
        svg = self.render("line", "light")
        self.assertNotIn("<metadata", svg)
        self.assertNotIn("dc:date", svg)

    def test_every_colour_is_a_token_of_the_requested_theme(self) -> None:
        for name in chart_cases():
            for theme in charts.THEMES:
                with self.subTest(chart=name, theme=theme):
                    allowed = {value.lower() for value in charts.TOKENS[theme].values()}
                    used = svg_colours(self.render(name, theme))
                    self.assertTrue(used, "the SVG uses no colour at all")
                    self.assertEqual(set(), used - allowed, "colours outside the token set")

    def test_the_background_is_the_theme_panel(self) -> None:
        for theme in charts.THEMES:
            with self.subTest(theme=theme):
                self.assertIn(charts.TOKENS[theme]["--ds-panel"], self.render("bar", theme))

    def test_series_use_the_series_tokens_in_order(self) -> None:
        for theme in charts.THEMES:
            tokens = charts.TOKENS[theme]
            with self.subTest(theme=theme):
                single = svg_colours(self.render("bar", theme))
                self.assertIn(tokens["--ds-series-1"], single)
                self.assertNotIn(tokens["--ds-series-2"], single)
                three = svg_colours(self.render("grouped_three", theme))
                for slot in (1, 2, 3):
                    self.assertIn(tokens[f"--ds-series-{slot}"], three)

    def test_series_slots_keep_an_entity_on_its_colour(self) -> None:
        tokens = charts.TOKENS["light"]
        svg = charts.grouped_bar_chart(
            ["Round 1"], {"Hospital 245": [3.0]}, series_slots={"Hospital 245": 3}
        )
        used = svg_colours(svg)
        self.assertIn(tokens["--ds-series-3"], used)
        self.assertNotIn(tokens["--ds-series-1"], used)

    def test_a_single_series_can_take_any_slot(self) -> None:
        """A chart about one hospital draws in that hospital's colour."""
        for theme in charts.THEMES:
            tokens = charts.TOKENS[theme]
            with self.subTest(theme=theme):
                bars = svg_colours(charts.bar_chart(HOSPITALS, [1, 2, 3], series_slot=3, theme=theme))
                self.assertIn(tokens["--ds-series-3"], bars)
                self.assertNotIn(tokens["--ds-series-1"], bars)
                bins = svg_colours(charts.histogram([0, 1, 2], [4, 5], series_slot=2, theme=theme))
                self.assertIn(tokens["--ds-series-2"], bins)
                self.assertNotIn(tokens["--ds-series-1"], bins)

    def test_the_slot_changes_the_bytes(self) -> None:
        self.assertNotEqual(
            charts.bar_chart(HOSPITALS, [1, 2, 3]), charts.bar_chart(HOSPITALS, [1, 2, 3], series_slot=2)
        )

    def test_panels_draw_every_category_in_one_series_colour(self) -> None:
        """Categories that are not entities get a panel each, not a colour each."""
        for theme in charts.THEMES:
            tokens = charts.TOKENS[theme]
            with self.subTest(theme=theme):
                svg = self.render("histogram_panels", theme)
                used = svg_colours(svg)
                self.assertIn(tokens["--ds-series-2"], used)
                self.assertNotIn(tokens["--ds-series-1"], used)
                self.assertNotIn(tokens["--ds-series-3"], used)
                drawn = svg_texts(svg)
                for label in ("Poor outcome", "No poor outcome", "Outcome not recorded"):
                    self.assertIn(label, drawn)
                self.assertIn("suppressed", drawn)

    def test_series_charts_never_use_decision_colours(self) -> None:
        for name in ("bar", "grouped", "stacked", "histogram_series", "line_series"):
            for theme in charts.THEMES:
                tokens = charts.TOKENS[theme]
                reserved = {tokens["--ds-permit"], tokens["--ds-narrow"], tokens["--ds-deny"]}
                with self.subTest(chart=name, theme=theme):
                    self.assertFalse(svg_colours(self.render(name, theme)) & reserved)

    def test_the_dot_chart_marks_outcomes_with_decision_colours_and_words(self) -> None:
        for theme in charts.THEMES:
            tokens = charts.TOKENS[theme]
            with self.subTest(theme=theme):
                svg = self.render("dots", theme)
                used = svg_colours(svg)
                for token in ("--ds-permit", "--ds-deny", "--ds-narrow"):
                    self.assertIn(tokens[token], used)
                texts = svg_texts(svg)
                for word in ("Passed", "Failed", "Failed (advisory)", "Not evaluated"):
                    self.assertIn(word, texts)
                for label in ("discrimination", "subgroup_fairness", "alert_burden"):
                    self.assertIn(label, texts)

    def test_two_or_more_series_get_a_legend(self) -> None:
        texts = svg_texts(self.render("grouped", "light"))
        self.assertIn("Current release", texts)
        self.assertIn("Candidate", texts)

    def test_a_single_series_gets_no_legend(self) -> None:
        texts = svg_texts(self.render("line", "light"))
        self.assertNotIn("Training loss", texts)

    def test_suppressed_values_are_labelled(self) -> None:
        for name in ("bar", "grouped", "stacked", "histogram", "line"):
            with self.subTest(chart=name):
                self.assertIn("suppressed", svg_texts(self.render(name, "light")))

    def test_a_gap_in_one_of_several_lines_names_the_line(self) -> None:
        svg = charts.line_chart([1, 2, 3], {"A": [1.0, None, 2.0], "B": [1.5, 1.7, 1.9]})
        self.assertIn("A suppressed", svg_texts(svg))

    def test_small_bar_charts_label_their_values(self) -> None:
        texts = svg_texts(self.render("bar", "light"))
        self.assertIn("524", texts)
        self.assertIn("292", texts)

    def test_axis_ticks_share_one_number_of_decimals(self) -> None:
        svg = charts.line_chart([1, 2, 3], {"Loss": [0.69, 0.61, 0.56]}, direct_labels="none")
        ticks = [text for text in svg_texts(svg) if text.startswith("0.")]
        self.assertTrue(ticks)
        self.assertEqual({4}, {len(text) for text in ticks}, ticks)

    def test_percentage_formats_give_percentage_ticks(self) -> None:
        texts = svg_texts(self.render("bar_horizontal", "light"))
        self.assertIn("0%", texts)
        self.assertIn("71%", texts)

    def test_direct_labels_can_be_turned_off(self) -> None:
        texts = svg_texts(self.render("bar", "light", direct_labels="none"))
        self.assertNotIn("524", texts)

    def test_two_charts_share_no_element_id(self) -> None:
        first = set(re.findall(r'\bid="([^"]+)"', self.render("bar", "light")))
        second = set(re.findall(r'\bid="([^"]+)"', self.render("line", "light")))
        self.assertTrue(first and second)
        self.assertEqual(set(), first & second)

    def test_every_internal_reference_resolves(self) -> None:
        for name in chart_cases():
            with self.subTest(chart=name):
                svg = self.render(name, "dark")
                ids = set(re.findall(r'\bid="([^"]+)"', svg))
                refs = set(re.findall(r"url\(#([^)]+)\)", svg))
                refs |= set(re.findall(r'href="#([^"]+)"', svg))
                self.assertEqual(set(), refs - ids)

    def test_the_svg_is_640_pixels_wide_by_default(self) -> None:
        root = ET.fromstring(self.render("bar", "light").split("?>", 1)[1])
        # 480pt is 640 CSS pixels.
        self.assertEqual("480pt", root.get("width"))

    def test_the_text_uses_the_design_system_fonts(self) -> None:
        svg = self.render("bar", "light")
        self.assertNotIn("DejaVu", svg)
        self.assertIn("Archivo", svg)


class InputTests(unittest.TestCase):
    """Bad input fails with a typed error before anything is drawn, so before matplotlib loads."""

    def test_an_unknown_theme_is_refused(self) -> None:
        with self.assertRaises(charts.ChartInputError):
            charts.bar_chart(["a"], [1], theme="sepia")

    def test_more_than_three_series_is_refused(self) -> None:
        series = {name: [1.0] for name in ("a", "b", "c", "d")}
        with self.assertRaises(charts.ChartInputError):
            charts.grouped_bar_chart(["x"], series)

    def test_lengths_must_match(self) -> None:
        with self.assertRaises(charts.ChartInputError):
            charts.bar_chart(["a", "b"], [1])
        with self.assertRaises(charts.ChartInputError):
            charts.histogram([0, 1, 2], [1, 2, 3])
        with self.assertRaises(charts.ChartInputError):
            charts.line_chart([1, 2], {"s": [1.0]})

    def test_nan_is_refused_rather_than_shown_as_suppressed(self) -> None:
        with self.assertRaises(charts.ChartInputError):
            charts.bar_chart(["a"], [float("nan")])

    def test_stacked_bars_need_non_negative_values(self) -> None:
        with self.assertRaises(charts.ChartInputError):
            charts.stacked_bar_chart(["a"], {"s": [-1.0]})

    def test_histogram_edges_must_increase(self) -> None:
        with self.assertRaises(charts.ChartInputError):
            charts.histogram([0, 2, 1], [1, 2])

    def test_a_dot_item_needs_a_known_outcome_and_direction(self) -> None:
        bad_outcome = [{**GATES[0], "outcome": "maybe"}]
        bad_direction = [{**GATES[0], "direction": "around"}]
        bad_range = [{**GATES[0], "range": (1.0, 0.0)}]
        for items in (bad_outcome, bad_direction, bad_range):
            with self.assertRaises(charts.ChartInputError):
                charts.dot_threshold_chart(items)

    def test_a_series_slot_must_be_one_to_three(self) -> None:
        for slot in (0, 4, True, "1"):
            with self.subTest(slot=slot):
                with self.assertRaises(charts.ChartInputError):
                    charts.bar_chart(["a"], [1], series_slot=slot)
                with self.assertRaises(charts.ChartInputError):
                    charts.histogram([0, 1], [1], series_slot=slot)
                with self.assertRaises(charts.ChartInputError):
                    charts.histogram_panels([0, 1], {"a": [1]}, series_slot=slot)

    def test_series_slot_is_for_one_series(self) -> None:
        with self.assertRaises(charts.ChartInputError):
            charts.histogram([0, 1], {"a": [1], "b": [2]}, series_slot=2)

    def test_panels_need_at_least_one_and_at_most_six(self) -> None:
        with self.assertRaises(charts.ChartInputError):
            charts.histogram_panels([0, 1], {})
        with self.assertRaises(charts.ChartInputError):
            charts.histogram_panels([0, 1], {str(index): [1] for index in range(7)})

    def test_the_input_error_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(charts.ChartInputError, ValueError))


def bar_fills(svg: str) -> list[str]:
    """The fill colour of every bar: the filled, clipped paths inside the plot."""
    root = ET.fromstring(svg.split("?>", 1)[1])
    fills = []
    for node in root.iter(f"{SVG_NS}path"):
        match = re.search(r"fill:\s*(#[0-9a-fA-F]{6})", node.attrib.get("style", ""))
        if "clip-path" in node.attrib and match:
            fills.append(match.group(1).lower())
    return fills


class NeutralSlotTests(unittest.TestCase):
    """``series_slot=None`` draws an entity that has no series colour in neutral ink.

    The design system has three series colours. A fourth entity gets none, so
    a chart about it must not borrow another entity's colour.
    """

    def neutral_charts(self, theme: str) -> list[tuple[str, str, int]]:
        """(name, SVG, number of bars) for each function with a ``series_slot``."""
        return [
            ("bar", charts.bar_chart(HOSPITALS, [1, 2, 3], series_slot=None, theme=theme), 3),
            ("bar_horizontal", charts.bar_chart(["a", "b"], [0.2, 0.5], series_slot=None,
                                                orientation="horizontal", theme=theme), 2),
            ("histogram", charts.histogram([0, 1, 2], [4, 5], series_slot=None, theme=theme), 2),
            ("histogram_panels", charts.histogram_panels([0, 1, 2], {"a": [1, 2], "b": [3, 4]},
                                                         series_slot=None, theme=theme), 4),
        ]

    @unittest.skipUnless(HAS_MATPLOTLIB, NO_MATPLOTLIB)
    def test_none_draws_every_bar_in_ink_2_and_uses_no_series_colour(self) -> None:
        for theme in charts.THEMES:
            tokens = charts.TOKENS[theme]
            series = {tokens[f"--ds-series-{slot}"] for slot in range(1, charts.MAX_SERIES + 1)}
            for name, svg, bars in self.neutral_charts(theme):
                with self.subTest(theme=theme, chart=name):
                    self.assertEqual(bar_fills(svg), [tokens["--ds-ink-2"]] * bars)
                    self.assertFalse(svg_colours(svg) & series)

    @unittest.skipUnless(HAS_MATPLOTLIB, NO_MATPLOTLIB)
    def test_a_slot_still_draws_in_its_series_colour(self) -> None:
        """The control for the test above: the same helper sees the series colour."""
        tokens = charts.TOKENS["light"]
        self.assertEqual(bar_fills(charts.bar_chart(HOSPITALS, [1, 2, 3], series_slot=2)), [tokens["--ds-series-2"]] * 3)
        self.assertEqual(charts.bar_chart(HOSPITALS, [1, 2, 3]), charts.bar_chart(HOSPITALS, [1, 2, 3], series_slot=1))
        self.assertNotEqual(
            charts.bar_chart(HOSPITALS, [1, 2, 3], series_slot=None), charts.bar_chart(HOSPITALS, [1, 2, 3])
        )

    @unittest.skipUnless(HAS_MATPLOTLIB, NO_MATPLOTLIB)
    def test_a_neutral_chart_is_deterministic(self) -> None:
        first = charts.histogram_panels([0, 1, 2], {"a": [1, 2]}, series_slot=None)
        self.assertEqual(first, charts.histogram_panels([0, 1, 2], {"a": [1, 2]}, series_slot=None))

    def test_zero_four_and_true_are_still_refused(self) -> None:
        for slot in (0, 4, True, -1, 1.0):
            with self.subTest(slot=slot):
                for draw in (
                    lambda: charts.bar_chart(["a"], [1], series_slot=slot),
                    lambda: charts.histogram([0, 1], [1], series_slot=slot),
                    lambda: charts.histogram_panels([0, 1], {"a": [1]}, series_slot=slot),
                ):
                    with self.assertRaises(charts.ChartInputError) as raised:
                        draw()
                    self.assertIn("1 to 3 or None", str(raised.exception))

    def test_named_series_still_need_slots_one_to_three(self) -> None:
        with self.assertRaises(charts.ChartInputError):
            charts.grouped_bar_chart(["a"], {"x": [1]}, series_slots={"x": None})
        with self.assertRaises(charts.ChartInputError):
            charts.histogram([0, 1], {"x": [1], "y": [2]}, series_slot=None)


class TableTests(unittest.TestCase):
    """The table view holds the same values as the chart."""

    def test_bar_table(self) -> None:
        table = charts.bar_chart_table(
            HOSPITALS, [524, 292, None], category_label="Hospital", value_label="Records"
        )
        self.assertEqual(["Hospital", "Records"], table["columns"])
        self.assertEqual(
            [["Hospital 134", 524], ["Hospital 2", 292], ["Hospital 245", "suppressed"]],
            table["rows"],
        )

    def test_grouped_table(self) -> None:
        table = charts.grouped_bar_chart_table(
            HOSPITALS, {"Current release": [0.71, 0.69, 0.73], "Candidate": [0.74, None, 0.75]},
            category_label="Hospital",
        )
        self.assertEqual(["Hospital", "Current release", "Candidate"], table["columns"])
        self.assertEqual(["Hospital 2", 0.69, "suppressed"], table["rows"][1])

    def test_stacked_table_has_a_total(self) -> None:
        table = charts.stacked_bar_chart_table(
            ["<50", "50-59"], {"Poor": [10, 30], "Good": [40, None]}, category_label="Age band"
        )
        self.assertEqual(["Age band", "Poor", "Good", "Total"], table["columns"])
        self.assertEqual(["<50", 10, 40, 50], table["rows"][0])
        self.assertEqual(["50-59", 30, "suppressed", "suppressed"], table["rows"][1])

    def test_histogram_table(self) -> None:
        table = charts.histogram_table([60, 100, 140], [3, None], value_label="Patients")
        self.assertEqual(["Bin", "Patients"], table["columns"])
        self.assertEqual([["60–100", 3], ["100–140", "suppressed"]], table["rows"])

    def test_histogram_table_with_series(self) -> None:
        table = charts.histogram_table([0, 0.5, 1], {"Poor": [1, 2], "Good": [3, 4]})
        self.assertEqual(["Bin", "Poor", "Good"], table["columns"])
        self.assertEqual(["0.0–0.5", 1, 3], table["rows"][0])

    def test_line_table(self) -> None:
        table = charts.line_chart_table([1, 2], {"Loss": [0.69, None]}, x_label="Round")
        self.assertEqual(["Round", "Loss"], table["columns"])
        self.assertEqual([[1, 0.69], [2, "suppressed"]], table["rows"])

    def test_dot_threshold_table(self) -> None:
        table = charts.dot_threshold_chart_table(GATES, label_column="Gate")
        self.assertEqual(["Gate", "Observed", "Required", "Outcome"], table["columns"])
        self.assertEqual(["discrimination", 0.74, "at least 0.70", "Passed"], table["rows"][0])
        self.assertEqual(["alert_burden", 812.0, "at most 750", "Failed (advisory)"], table["rows"][2])
        self.assertEqual("missing", table["rows"][3][1])
        self.assertEqual("Not evaluated", table["rows"][3][3])

    def test_every_chart_kind_has_a_table_builder(self) -> None:
        self.assertEqual(
            {"bar", "grouped_bar", "stacked_bar", "histogram", "histogram_panels", "line", "dot_threshold"},
            set(charts.TABLE_BUILDERS),
        )


if __name__ == "__main__":
    unittest.main()
