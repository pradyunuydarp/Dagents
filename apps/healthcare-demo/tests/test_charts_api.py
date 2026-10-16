"""The chart endpoints: lists, SVGs in both themes, caching, and the empty states.

Hospital charts come from one hospital's records. Consortium charts come from
the hospitals' summaries and the last pilot; before a pilot, the charts that
need one are listed as unavailable, and the list itself never fails. The
after-pilot tests use the real planner and skip without it.
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.api_support import ApiTestCase, main, parse_svg
from tests.support import HIGH_RISK_ENTRY, dagentsc_available, tiny_site

from app.domain.dataset import DATASET_NOTICE, SITES
from app.services.charts_service import CONSORTIUM_CHART_IDS, NEEDS_PILOT, SITE_CHART_IDS
from app.services.consortium import Consortium

ENTRY_KEYS = {
    "chart_id", "title", "description", "alt", "available", "reason", "table", "notice", "svg_path", "version",
}
NEEDS_A_PILOT = {"site-auc", "training-curve", "release-gates"}


class HospitalChartTests(ApiTestCase):
    def charts(self, site_id: str = "uk-134") -> list[dict]:
        payload = self.ok(self.client.get(f"/api/v1/hospitals/{site_id}/charts"))
        self.assertEqual(site_id, payload["site_id"])
        return payload["charts"]

    def test_the_list_describes_every_chart(self) -> None:
        charts = self.charts()
        self.assertEqual(list(SITE_CHART_IDS), [chart["chart_id"] for chart in charts])
        for chart in charts:
            with self.subTest(chart=chart["chart_id"]):
                self.assertEqual(ENTRY_KEYS, set(chart))
                self.assertTrue(chart["available"])
                self.assertIsNone(chart["reason"])
                self.assertTrue(chart["title"] and chart["description"] and chart["alt"])
                self.assertTrue(chart["table"]["columns"])
                self.assertTrue(chart["table"]["rows"])
                self.assertTrue(all(len(row) == len(chart["table"]["columns"]) for row in chart["table"]["rows"]))
                self.assertEqual(DATASET_NOTICE, chart["notice"])
                self.assertEqual(f"/api/v1/hospitals/uk-134/charts/{chart['chart_id']}.svg", chart["svg_path"])

    def test_every_chart_draws_in_both_themes(self) -> None:
        for chart in self.charts("ch-245"):
            svgs = {}
            for theme in ("light", "dark"):
                with self.subTest(chart=chart["chart_id"], theme=theme):
                    response = self.client.get(f"{chart['svg_path']}?theme={theme}")
                    root = parse_svg(self, response)
                    self.assertEqual(chart["title"], root.find("{http://www.w3.org/2000/svg}title").text)
                    self.assertIn("International Stroke Trial", response.text)
                    svgs[theme] = response.text
            self.assertNotEqual(svgs["light"], svgs["dark"])

    def test_an_unchanged_chart_is_served_from_the_cache_with_an_etag(self) -> None:
        path = "/api/v1/hospitals/it-002/charts/systolic-bp.svg?theme=dark"
        first = self.client.get(path)
        parse_svg(self, first)
        renders = main.chart_service.renders
        second = self.client.get(path)
        self.assertEqual(first.content, second.content)
        self.assertEqual(renders, main.chart_service.renders, "an unchanged chart was drawn again")
        self.assertEqual("no-cache", second.headers["cache-control"])
        not_modified = self.client.get(path, headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(304, not_modified.status_code)

    def test_an_entry_changes_the_hospitals_charts(self) -> None:
        before = {chart["chart_id"]: chart for chart in self.charts("it-002")}
        etag = self.client.get("/api/v1/hospitals/it-002/charts/systolic-bp.svg").headers["etag"]
        self.ok(self.client.post("/api/v1/hospitals/it-002/encounters", json=HIGH_RISK_ENTRY), 201)
        after = {chart["chart_id"]: chart for chart in self.charts("it-002")}
        # The entry has no outcome yet, so the outcome rates do not move; the
        # table counts it as not recorded. The other charts draw it.
        for chart_id in ("presentation", "systolic-bp", "risk-scores"):
            self.assertNotEqual(before[chart_id]["version"], after[chart_id]["version"], chart_id)
        unrecorded = {row[0]: row[4] for row in after["outcome-by-age-band"]["table"]["rows"]}
        earlier = {row[0]: row[4] for row in before["outcome-by-age-band"]["table"]["rows"]}
        self.assertEqual(earlier["90+"] + 1, unrecorded["90+"])
        response = self.client.get("/api/v1/hospitals/it-002/charts/systolic-bp.svg")
        self.assertNotEqual(etag, response.headers["etag"])

    def test_each_hospital_keeps_its_series_colour_on_its_own_charts(self) -> None:
        """A hospital's charts use the slot it has on the consortium charts, and no other series colour."""
        import re

        from agents.common.infrastructure import charts as renderer

        for slot, site in enumerate(SITES, start=1):
            for theme in ("light", "dark"):
                tokens = renderer.TOKENS[theme]
                others = {tokens[f"--ds-series-{n}"] for n in (1, 2, 3) if n != slot}
                for chart_id in SITE_CHART_IDS:
                    with self.subTest(site=site.site_id, chart=chart_id, theme=theme):
                        response = self.client.get(f"/api/v1/hospitals/{site.site_id}/charts/{chart_id}.svg?theme={theme}")
                        parse_svg(self, response)
                        used = {colour.lower() for colour in re.findall(r"#[0-9a-fA-F]{6}\b", response.text)}
                        self.assertIn(tokens[f"--ds-series-{slot}"], used)
                        self.assertEqual(set(), used & others)

    def test_the_risk_chart_shows_each_outcome_in_its_own_panel(self) -> None:
        charts = {chart["chart_id"]: chart for chart in self.charts("uk-134")}
        risk = charts["risk-scores"]
        self.assertEqual(
            ["Estimated risk", "Poor outcome", "No poor outcome", "Outcome not recorded"], risk["table"]["columns"]
        )
        svg = self.client.get(f"{risk['svg_path']}?theme=light").text
        for label in ("Poor outcome", "No poor outcome", "Outcome not recorded"):
            self.assertIn(f">{label}<", svg.replace("\n", ""))

    def test_errors(self) -> None:
        response = self.client.get("/api/v1/hospitals/uk-134/charts/presentation.svg?theme=sepia")
        self.assertEqual(422, response.status_code)
        self.assertEqual({"theme": "must be one of light, dark"}, response.json()["errors"])
        response = self.client.get("/api/v1/hospitals/uk-134/charts/no-such-chart.svg")
        self.assertEqual(404, response.status_code)
        self.assertIn("no-such-chart", response.json()["detail"])


class ConsortiumChartsBeforeAPilotTests(ApiTestCase):
    def test_the_list_never_fails_and_says_what_is_missing(self) -> None:
        charts = self.ok(self.client.get("/api/v1/charts"))["charts"]
        self.assertEqual(list(CONSORTIUM_CHART_IDS), [chart["chart_id"] for chart in charts])
        for chart in charts:
            with self.subTest(chart=chart["chart_id"]):
                self.assertEqual(ENTRY_KEYS, set(chart))
                if chart["chart_id"] in NEEDS_A_PILOT:
                    self.assertFalse(chart["available"])
                    self.assertEqual(NEEDS_PILOT, chart["reason"])
                    self.assertEqual({"columns": [], "rows": []}, chart["table"])
                else:
                    self.assertTrue(chart["available"])
                    self.assertTrue(chart["table"]["rows"])

    def test_a_chart_that_needs_a_pilot_is_a_404_only_when_asked_for(self) -> None:
        response = self.client.get("/api/v1/charts/release-gates.svg?theme=light")
        self.assertEqual(404, response.status_code)
        self.assertEqual(NEEDS_PILOT, response.json()["detail"])

    def test_the_site_summary_charts_draw(self) -> None:
        for chart_id in ("hospital-records", "hospital-outcome-rates"):
            for theme in ("light", "dark"):
                with self.subTest(chart=chart_id, theme=theme):
                    parse_svg(self, self.client.get(f"/api/v1/charts/{chart_id}.svg?theme={theme}"))

    def test_the_records_chart_uses_reported_counts(self) -> None:
        charts = {c["chart_id"]: c for c in self.ok(self.client.get("/api/v1/charts"))["charts"]}
        rows = {row[0]: row for row in charts["hospital-records"]["table"]["rows"]}
        for site in SITES:
            summary = self.consortium.hospital(site.site_id).summary()
            self.assertEqual(summary["record_count"], rows[site.display_name][3])
            self.assertEqual(summary["entered_count"], rows[site.display_name][2])


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SuppressedRateChartTests(ApiTestCase):
    """A hospital below the minimum cohort shows as suppressed, and four hospitals still draw.

    The planner decides which rates may leave a hospital, so this needs it.
    """

    @classmethod
    def make_consortium(cls) -> Consortium:
        tiny, store = tiny_site(5)
        return Consortium(sites=[*SITES, tiny], site_stores={tiny.site_id: store})

    def test_the_small_hospitals_rate_is_suppressed(self) -> None:
        charts = {c["chart_id"]: c for c in self.ok(self.client.get("/api/v1/charts"))["charts"]}
        rows = {row[0]: row for row in charts["hospital-outcome-rates"]["table"]["rows"]}
        self.assertEqual("suppressed", rows["Tiny clinic"][2])
        self.assertIsInstance(rows["Hospital 134"][2], float)
        parse_svg(self, self.client.get("/api/v1/charts/hospital-outcome-rates.svg?theme=dark"))
        parse_svg(self, self.client.get("/api/v1/charts/hospital-records.svg?theme=light"))

    def test_a_rate_withheld_for_another_reason_is_not_called_suppressed(self) -> None:
        """The table repeats the hospital's reason; a missing planner is not a small cohort."""
        from app.services.hospital import Hospital

        with mock.patch.object(Hospital, "outcome_rate_status", return_value="withheld"):
            main.chart_service.clear()
            charts = {c["chart_id"]: c for c in self.ok(self.client.get("/api/v1/charts"))["charts"]}
        main.chart_service.clear()
        rows = {row[0]: row for row in charts["hospital-outcome-rates"]["table"]["rows"]}
        self.assertEqual("withheld: the planner could not be asked", rows["Hospital 134"][2])
        self.assertEqual("withheld: the planner could not be asked", rows["Tiny clinic"][2])


class NeutralInkChartTests(ApiTestCase):
    """With four hospitals no hospital has a series colour, so every chart of a hospital is in neutral ink.

    The design system has three series colours, and a colour marks which
    hospital a chart is about. A fourth hospital drawn in the first colour
    would look like the first hospital.
    """

    @classmethod
    def make_consortium(cls) -> Consortium:
        tiny, store = tiny_site(60)
        return Consortium(sites=[*SITES, tiny], site_stores={tiny.site_id: store})

    def colours(self, path: str, theme: str) -> set[str]:
        import re

        response = self.client.get(f"{path}?theme={theme}")
        parse_svg(self, response)
        return {colour.lower() for colour in re.findall(r"#[0-9a-fA-F]{6}\b", response.text)}

    def test_a_hospital_without_a_slot_has_none(self) -> None:
        from app.services.charts_service import _slot

        self.assertIsNone(_slot(self.consortium.hospital("tiny-001")))
        self.assertIsNone(_slot(object()))
        self.assertEqual(2, _slot(mock.Mock(series_slot=2)))

    def test_hospital_and_consortium_charts_use_no_series_colour(self) -> None:
        from agents.common.infrastructure import charts as renderer

        paths = [f"/api/v1/hospitals/tiny-001/charts/{chart_id}.svg" for chart_id in SITE_CHART_IDS]
        paths += [f"/api/v1/hospitals/uk-134/charts/{chart_id}.svg" for chart_id in SITE_CHART_IDS]
        paths += ["/api/v1/charts/hospital-records.svg", "/api/v1/charts/hospital-outcome-rates.svg"]
        for theme in ("light", "dark"):
            tokens = renderer.TOKENS[theme]
            series = {tokens[f"--ds-series-{n}"].lower() for n in (1, 2, 3)}
            for path in paths:
                with self.subTest(path=path, theme=theme):
                    used = self.colours(path, theme)
                    self.assertIn(tokens["--ds-ink-2"].lower(), used)
                    self.assertEqual(set(), used & series)


class FourHospitalConsortiumChartTests(unittest.TestCase):
    """With four hospitals the consortium charts give no hospital a series colour.

    The design system has three series colours. A chart that compares four
    hospitals draws each in neutral ink and names it on the axis. The AUC chart
    does not draw its two models in the first two series colours, because
    those mark two of the hospitals. Each alt text describes the chart drawn.
    """

    @staticmethod
    def data(count: int) -> "ConsortiumData":
        from app.services.charts_service import ConsortiumData

        summaries = [
            {"site_id": f"site-{n}", "display_name": f"Hospital {n}", "record_count": 100 + n, "entered_count": n,
             "labelled_count": 90, "outcome_rate": 0.5, "outcome_rate_status": "reported"}
            for n in range(1, count + 1)
        ]
        results = [{"site_id": f"site-{n}", "metrics": {"auc": 0.7}} for n in range(1, count + 1)]
        pilot = {
            "rounds": {"evaluation": {"results": results}, "candidate_validation": {"results": results}},
            "model_in_use": {"version": "v1"},
            "candidate": {"candidate_version": "v2"},
        }
        return ConsortiumData(summaries=summaries, last_pilot=pilot, minimum_cohort=20)

    @staticmethod
    def draw(spec, theme: str) -> str:
        from app.services.charts_service import _DRAW

        return _DRAW[spec.kind](*spec.args, theme=theme, **spec.kwargs)

    def test_charts_comparing_four_hospitals_use_no_series_colour(self) -> None:
        import re

        from agents.common.infrastructure import charts as renderer
        from app.services.charts_service import _hospital_outcome_rates, _hospital_records, _site_auc

        data = self.data(4)
        for build in (_hospital_records, _hospital_outcome_rates, _site_auc):
            spec = build(data)
            for theme in ("light", "dark"):
                with self.subTest(chart=spec.chart_id, theme=theme):
                    tokens = renderer.TOKENS[theme]
                    series = {tokens[f"--ds-series-{n}"].lower() for n in (1, 2, 3)}
                    used = {c.lower() for c in re.findall(r"#[0-9a-fA-F]{6}\b", self.draw(spec, theme))}
                    self.assertIn(tokens["--ds-ink-2"].lower(), used)
                    self.assertEqual(set(), used & series)

    def test_the_auc_chart_names_each_hospital_and_model(self) -> None:
        from app.services.charts_service import _site_auc

        spec = _site_auc(self.data(4))
        self.assertEqual("bar", spec.kind)
        self.assertEqual(8, len(spec.args[0]))
        self.assertEqual(["Hospital 1: model in use", "Hospital 1: candidate"], spec.args[0][:2])
        self.assertTrue(spec.alt.startswith("Horizontal bar chart"), spec.alt)

    def test_each_alt_text_names_the_chart_drawn(self) -> None:
        from app.services.charts_service import _hospital_outcome_rates, _hospital_records, _site_auc

        for count in (3, 4):
            for build in (_hospital_records, _hospital_outcome_rates, _site_auc):
                spec = build(self.data(count))
                with self.subTest(chart=spec.chart_id, hospitals=count):
                    self.assertIn("bar chart", spec.alt.lower())
                    if "grouped" in spec.alt.lower():
                        self.assertEqual("grouped_bar", spec.kind)

    def test_three_hospitals_keep_their_series_colours(self) -> None:
        from app.services.charts_service import _hospital_outcome_rates, _hospital_records, _site_auc

        expected = {"Hospital 1": 1, "Hospital 2": 2, "Hospital 3": 3}
        for build in (_hospital_records, _hospital_outcome_rates, _site_auc):
            spec = build(self.data(3))
            with self.subTest(chart=spec.chart_id):
                self.assertEqual("grouped_bar", spec.kind)
                self.assertEqual(expected, spec.kwargs["series_slots"])


class GateThresholdTests(unittest.TestCase):
    """The gate chart shows the threshold the planner reported, and computes none itself."""

    @staticmethod
    def pilot(results: list[dict], baseline: dict | None = None) -> dict:
        return {"release": {"gates": {"gate_results": results, "action": "reject"}}, "baseline_metrics": baseline or {}}

    def test_the_chart_uses_the_planners_threshold(self) -> None:
        from app.services.charts_service import _gate_items

        # The baseline here would give 0.70 + 0.02; the planner's 0.8123 must win.
        results = [
            {"gate_id": "discrimination", "outcome": "passed", "observed": 0.78, "threshold": 0.7},
            {"gate_id": "improves_on_current_release", "outcome": "failed", "observed": 0.78, "threshold": 0.8123},
        ]
        items = _gate_items(self.pilot(results, {"auc": 0.70}))
        self.assertEqual([0.7, 0.8123], [item["threshold"] for item in items])
        self.assertEqual(["at_least", "at_least"], [item["direction"] for item in items])

    def test_a_gate_without_a_threshold_makes_the_chart_unavailable(self) -> None:
        from app.services.charts_service import _gate_items

        results = [
            {"gate_id": "discrimination", "outcome": "passed", "observed": 0.78, "threshold": 0.7},
            {"gate_id": "improves_on_current_release", "outcome": "not_evaluated", "observed": 0.78, "threshold": None},
        ]
        # A baseline that the chart could use to recompute the threshold is ignored.
        self.assertIsNone(_gate_items(self.pilot(results, {"auc": 0.70})))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ConsortiumChartsAfterAPilotTests(ApiTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.pilot = cls.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}).json()

    def test_every_consortium_chart_is_available_and_draws(self) -> None:
        charts = self.ok(self.client.get("/api/v1/charts"))["charts"]
        for chart in charts:
            self.assertTrue(chart["available"], chart)
            for theme in ("light", "dark"):
                with self.subTest(chart=chart["chart_id"], theme=theme):
                    parse_svg(self, self.client.get(f"{chart['svg_path']}?theme={theme}"))

    def test_the_gate_chart_shows_the_decisions_outcomes(self) -> None:
        charts = {c["chart_id"]: c for c in self.ok(self.client.get("/api/v1/charts"))["charts"]}
        table = charts["release-gates"]["table"]
        self.assertEqual(["Gate", "Observed", "Required", "Outcome"], table["columns"])
        outcomes = [r["outcome"] for r in self.pilot["release"]["gates"]["gate_results"]]
        words = {"passed": "Passed", "failed": "Failed", "not_evaluated": "Not evaluated"}
        self.assertEqual([words[o] for o in outcomes], [row[3].replace(" (advisory)", "") for row in table["rows"]])
        self.assertIn(self.pilot["release"]["gates"]["action"], charts["release-gates"]["description"])
        # The "Required" column shows the planner's threshold for each gate.
        thresholds = [r["threshold"] for r in self.pilot["release"]["gates"]["gate_results"]]
        self.assertTrue(all(isinstance(value, float) for value in thresholds), thresholds)

    def test_the_auc_chart_uses_the_reported_site_metrics(self) -> None:
        charts = {c["chart_id"]: c for c in self.ok(self.client.get("/api/v1/charts"))["charts"]}
        rows = {row[0]: row for row in charts["site-auc"]["table"]["rows"]}
        for result in self.pilot["rounds"]["candidate_validation"]["results"]:
            name = self.consortium.hospital(result["site_id"]).display_name
            self.assertAlmostEqual(result["metrics"]["auc"], rows[name][2], places=4)

    def test_the_training_curve_has_one_point_per_round_and_the_start(self) -> None:
        charts = {c["chart_id"]: c for c in self.ok(self.client.get("/api/v1/charts"))["charts"]}
        rows = charts["training-curve"]["table"]["rows"]
        self.assertEqual(len(self.pilot["training_curve"]) + 1, len(rows))
        self.assertEqual(0, rows[0][0])


if __name__ == "__main__":
    unittest.main()
