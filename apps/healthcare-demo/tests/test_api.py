"""The healthcare demo's HTTP API, endpoint by endpoint.

Each class runs against its own app services: in-memory stores and the four
Dagents agents in process (see ``api_support``). Every route that shows a
model version needs the planner, because the GMA checks each version with it,
so most classes skip without it; CI fails on that skip. Data entry is in
``test_entry_api.py``, charts in ``test_charts_api.py``, rate limits in
``test_rate_limits.py`` and the Postgres run in ``test_api_postgres.py``.
"""

from __future__ import annotations

import unittest
from unittest import mock

from dagents.common.domain.federation import GateComparison, ModelVersionRegistration, ReleaseGate

from tests.api_support import ApiTestCase, main, without_calls
from tests.support import HIGH_RISK_ENTRY, built_demo, dagentsc_available, gated_extension, tiny_site

from app.domain.conditions import (
    GUARD_PROBE_DEFAULT_FIELDS,
    STROKE_FEATURE_CONTRACT,
    STROKE_FEATURE_CONTRACT_VERSION,
    STROKE_RELEASE_GATES,
)
from app.domain.dataset import DATASET_NOTICE, SITES, age_band_for
from app.domain import stroke_model
from app.domain.model_kinds import LOGISTIC_REGRESSION
from app.services.agents import HealthcareDemo

SITE_IDS = [site.site_id for site in SITES]
HOSPITAL_SUMMARY_KEYS = {
    "site_id",
    "display_name",
    "country",
    "record_count",
    "entered_count",
    "labelled_count",
    "outcome_rate",
    "outcome_rate_status",
    "minimum_cohort",
    "store",
}


class DescriptionTests(ApiTestCase):
    """Endpoints that describe the app and need no planner."""

    def test_health_names_the_dataset_and_both_routes_agree(self) -> None:
        payload = self.ok(self.client.get("/api/v1/health"))
        self.assertEqual("ok", payload["status"])
        self.assertEqual("ist-1-deidentified", payload["data"])
        self.assertEqual(payload, self.ok(self.client.get("/health")))

    def test_the_extension_lists_the_stroke_condition_and_the_boundary(self) -> None:
        payload = self.ok(self.client.get("/api/v1/extension"))
        self.assertEqual(["suspected_stroke"], [p["condition_id"] for p in payload["registry"]["condition_packs"]])
        self.assertTrue(payload["boundary"]["framework_owns"])
        self.assertTrue(payload["boundary"]["app_owns"])

    def test_conditions_carry_the_v3_contract(self) -> None:
        payload = self.ok(self.client.get("/api/v1/conditions"))
        self.assertEqual(1, len(payload["implemented"]))
        self.assertTrue(all(entry["status"] == "not implemented" for entry in payload["planned"]))
        self.assertEqual(STROKE_FEATURE_CONTRACT_VERSION, payload["feature_contract"]["version"])
        self.assertEqual(20, payload["classification"]["minimum_cohort"])

    def test_assessing_a_record_returns_its_basis(self) -> None:
        record = {**HIGH_RISK_ENTRY, "age_band": age_band_for(HIGH_RISK_ENTRY["age"])}
        payload = self.ok(self.client.post("/api/v1/triage:assess", json={"record": record}))
        self.assertEqual("urgent", payload["priority"])
        self.assertTrue(payload["basis"])

    def test_a_worklist_ranks_records_and_removes_none(self) -> None:
        low = {"age_band": "<50", "sex": "male", "conscious_level": "alert", "face_weakness": False}
        high = {**HIGH_RISK_ENTRY, "age_band": "90+"}
        payload = self.ok(self.client.post("/api/v1/triage:worklist", json={"records": [low, high]}))
        self.assertEqual(2, len(payload["worklist"]))
        self.assertEqual("90+", payload["worklist"][0]["age_band"])

    def test_ingesting_a_fhir_bundle_reports_unmapped_codes(self) -> None:
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Encounter", "id": "e1", "age": 71}},
                {
                    "resource": {
                        "resourceType": "Observation",
                        "encounter": {"reference": "Encounter/e1"},
                        "code": {"coding": [{"code": "00000-0"}]},
                        "valueQuantity": {"value": 3, "unit": "x"},
                    }
                },
            ],
        }
        payload = self.ok(self.client.post("/api/v1/ingest:fhir", json={"bundle": bundle}))
        self.assertEqual(1, payload["record_count"])
        self.assertIn("00000-0", payload["unmapped_codes"])

    def test_framework_status_reports_the_storage_and_not_the_old_flag(self) -> None:
        payload = self.ok(self.client.get("/api/v1/framework/status"))
        self.assertEqual(
            {
                "cohort_source", "store_layout", "dataset", "ready", "hospitals", "agents", "services",
                "allowed_origins", "rate_limit_client", "framework_calls",
            },
            set(payload),
        )
        self.assertTrue(payload["ready"])
        self.assertEqual(SITE_IDS, [hospital["site_id"] for hospital in payload["hospitals"]])
        self.assertNotIn("records_are_synthetic", payload)
        self.assertEqual("in-memory", payload["cohort_source"])
        self.assertEqual("in-memory-per-site", payload["store_layout"])
        self.assertEqual(DATASET_NOTICE, payload["dataset"]["notice"])
        for service in payload["services"]:
            self.assertEqual({"name", "status", "detail"}, set(service))
            self.assertIn(service["status"], ("ok", "unavailable"))

    def test_framework_status_says_how_the_agents_run(self) -> None:
        """The page shows what the backend reports: the agents run inside the demo server, one LMA per hospital."""
        payload = self.ok(self.client.get("/api/v1/framework/status"))
        agents = payload["agents"]
        self.assertEqual({"mode", "engine", "lmas"}, set(agents))
        self.assertEqual("in-process", agents["mode"])
        self.assertEqual("http", agents["engine"])
        self.assertEqual(
            [{"site_id": site_id, "reachable": True, "source_kind": "in-memory"} for site_id in SITE_IDS],
            agents["lmas"],
        )
        self.assertEqual(
            ["gma", *(f"lma-{site_id}" for site_id in SITE_IDS)], [service["name"] for service in payload["services"]]
        )
        self.assertEqual({"ok"}, {service["status"] for service in payload["services"]})
        # Every agent call is listed by route: never a host, a port or a token.
        calls = payload["framework_calls"]
        self.assertIn(("gma", "GET", "/api/v1/federation/sites/summaries"),
                      {(call["service"], call["method"], call["path"]) for call in calls})
        for call in calls:
            self.assertEqual({"service", "site_id", "method", "path", "status", "ms", "via"}, set(call))
            self.assertTrue(call["path"].startswith("/api/v1/"), call)
            self.assertNotIn("://", str(call))

    def test_the_trace_route_is_removed(self) -> None:
        """It ran planner commands inside the app; the app now reaches the planner only through the agents."""
        self.assertEqual(404, self.client.get("/api/v1/framework/trace?site_id=uk-134").status_code)

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_models_lists_the_model_in_use(self) -> None:
        payload = self.ok(self.client.get("/api/v1/models"))
        self.assertEqual("stroke-outcome-rule-v1", payload["model_in_use"]["version"])
        self.assertEqual("released", payload["model_in_use"]["status"])
        self.assertIn("stroke-outcome-rule-v1", [model["version"] for model in payload["models"]])


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ModelListTests(ApiTestCase):
    """Every pilot adds model versions, so the list returns only the newest. The GMA keeps the registry."""

    def test_models_lists_the_model_in_use_and_the_20_newest_versions(self) -> None:
        agents = built_demo(self.demo)  # registers the seed rule and the initial model
        gma = self.demo.agents.gma()
        for number in range(1, 26):
            gma.register_model_version(
                f"lr-{number:02d}",
                ModelVersionRegistration(kind=LOGISTIC_REGRESSION.kind, params=stroke_model.initial_params()),
            )
        store = agents["gma"].state.models.store
        with mock.patch.object(store, "list", wraps=store.list) as listed:
            payload = self.ok(self.client.get("/api/v1/models"))
        self.assertEqual("stroke-outcome-rule-v1", payload["model_in_use"]["version"])
        self.assertEqual([f"lr-{number:02d}" for number in range(6, 26)], [m["version"] for m in payload["models"]])
        # The store selects the 20 newest itself (ORDER BY ... LIMIT in SQL); the API never reads every row.
        self.assertEqual([mock.call(20)], listed.call_args_list)
        self.assertIn(("gma", "GET", "/api/v1/model-versions"),
                      {(c["service"], c["method"], c["path"]) for c in payload["framework_calls"]})


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class HospitalTests(ApiTestCase):
    """What the coordinator and each hospital see. The pages that show the model in use need the planner."""

    def test_the_overview_shows_what_each_hospital_reports(self) -> None:
        payload = self.ok(self.client.get("/api/v1/overview"))
        self.assertEqual(SITE_IDS, [hospital["site_id"] for hospital in payload["hospitals"]])
        for hospital in payload["hospitals"]:
            self.assertEqual(HOSPITAL_SUMMARY_KEYS, set(hospital))
            # The numbers are the hospital's own report, as its LMA answers
            # it, not a count the coordinator made from rows.
            self.assertEqual(self.demo.hospital(hospital["site_id"]).summary(), hospital)
        self.assertEqual({"version": "stroke-outcome-rule-v1", "kind": "points_rule"}, payload["current_model"])
        self.assertEqual(20, payload["minimum_cohort"])
        self.assertEqual(len(STROKE_RELEASE_GATES), len(payload["release_gates"]))
        self.assertEqual(DATASET_NOTICE, payload["dataset"]["notice"])
        for key in ("study_id", "condition", "feature_contract", "engine", "secure_aggregation_threshold"):
            self.assertIn(key, payload)
        # The coordinator reads what the hospitals report through the GMA, and calls no LMA itself.
        calls = payload["framework_calls"]
        self.assertIn(("gma", "GET", "/api/v1/federation/sites/summaries"),
                      {(call["service"], call["method"], call["path"]) for call in calls})
        self.assertEqual({"gma"}, {call["service"] for call in calls})

    def test_the_hospitals_list_matches_the_overview(self) -> None:
        self.assertEqual(
            self.ok(self.client.get("/api/v1/overview"))["hospitals"],
            self.ok(self.client.get("/api/v1/hospitals")),
        )

    def test_a_dashboard_is_the_hospitals_own_view(self) -> None:
        payload = self.ok(self.client.get("/api/v1/hospitals/it-002"))
        self.assertEqual(
            {
                "site_id", "display_name", "country", "store", "counts", "outcome_rate", "outcome_rate_status",
                "minimum_cohort", "max_entered", "profile", "local_metrics", "current_model", "contributions",
            },
            set(without_calls(payload)),
        )
        # The hospital's page calls its own LMA and no other agent.
        lma_calls = [call for call in payload["framework_calls"] if call["service"] == "lma"]
        self.assertEqual({"it-002"}, {call["site_id"] for call in lma_calls})
        self.assertLessEqual(
            {"/api/v1/site/summary", "/api/v1/site/profile", "/api/v1/site/model:evaluate", "/api/v1/site/model"},
            {call["path"] for call in lma_calls},
        )
        self.assertNotIn("log_loss", payload["local_metrics"]["metrics"])
        self.assertEqual({"kind": "in-memory", "memory": "process"}, payload["store"])
        self.assertEqual(292, payload["counts"]["total"])
        self.assertEqual("stroke-outcome-rule-v1", payload["local_metrics"]["model_version"])
        self.assertIn("auc", payload["local_metrics"]["metrics"])

    def test_the_worklist_is_ranked_and_limited(self) -> None:
        payload = self.ok(self.client.get("/api/v1/hospitals/uk-134/worklist?limit=12"))
        rows = payload["worklist"]
        self.assertEqual(12, len(rows))
        self.assertTrue(all(row["encounter_id"].startswith("uk-134-") for row in rows))
        scores = [row["assessment"]["score"] for row in rows if not row["assessment"]["suppressed"]]
        self.assertEqual(sorted(scores, reverse=True), scores)
        self.assertEqual(422, self.client.get("/api/v1/hospitals/uk-134/worklist?limit=0").status_code)

    def test_the_audit_trail_has_a_chain_status(self) -> None:
        payload = self.ok(self.client.get("/api/v1/hospitals/ch-245/audit?limit=50"))
        self.assertEqual({"site_id", "chain_intact", "records"}, set(without_calls(payload)))
        self.assertTrue(payload["chain_intact"])

    def test_entered_records_are_listed_and_only_entered_ones(self) -> None:
        payload = self.ok(self.client.get("/api/v1/hospitals/ch-245/encounters?origin=entered&limit=20"))
        self.assertEqual({"site_id", "origin", "encounters", "counts", "max_entered"}, set(without_calls(payload)))
        self.assertEqual([], payload["encounters"])
        response = self.client.get("/api/v1/hospitals/ch-245/encounters?origin=dataset")
        self.assertEqual(422, response.status_code)
        self.assertIn("origin", response.json()["errors"])

    def test_the_threshold_sweep_is_served(self) -> None:
        payload = self.ok(self.client.get("/api/v1/hospitals/uk-134/thresholds"))
        self.assertGreater(len(payload["sweep"]), 5)

    def test_an_unknown_hospital_is_a_404_everywhere(self) -> None:
        for path in (
            "/api/v1/hospitals/nowhere",
            "/api/v1/hospitals/nowhere/worklist",
            "/api/v1/hospitals/nowhere/audit",
            "/api/v1/hospitals/nowhere/encounters",
            "/api/v1/hospitals/nowhere/entry-form",
            "/api/v1/hospitals/nowhere/charts",
            "/api/v1/hospitals/nowhere/charts/presentation.svg",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(404, response.status_code)
                self.assertIn("nowhere", response.json()["detail"])
        response = self.client.post("/api/v1/hospitals/nowhere/encounters", json=HIGH_RISK_ENTRY)
        self.assertEqual(404, response.status_code)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SuppressionTests(ApiTestCase):
    """A hospital below the minimum cohort reports no outcome rate. The planner decides, so this needs it."""

    @classmethod
    def make_demo(cls) -> HealthcareDemo:
        tiny, store = tiny_site(5)
        return HealthcareDemo(sites=[*SITES, tiny], site_stores={tiny.site_id: store})

    def test_a_small_hospital_reports_counts_but_no_rate(self) -> None:
        hospitals = {h["site_id"]: h for h in self.ok(self.client.get("/api/v1/overview"))["hospitals"]}
        self.assertEqual(5, hospitals["tiny-001"]["record_count"])
        self.assertIsNone(hospitals["tiny-001"]["outcome_rate"])
        self.assertEqual("below_minimum_cohort", hospitals["tiny-001"]["outcome_rate_status"])
        self.assertIsNotNone(hospitals["uk-134"]["outcome_rate"])
        self.assertEqual("released", hospitals["uk-134"]["outcome_rate_status"])


#: The keys a released record or summary row can have: the contract's fields,
#: the site-only fields, and the summary names a table request produces.
RECORD_KEYS = frozenset(STROKE_FEATURE_CONTRACT.field_names()) | {
    "age", "encounter_id", "source_row", "origin", "recorded_at", "outcome_recorded_at", "record_count",
}


def lists_of_records(value: object, keys: frozenset[str], path: str = "") -> list[str]:
    """Where ``value`` holds a list of records: a list of objects keyed by record fields."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += lists_of_records(item, keys, f"{path}.{key}")
    elif isinstance(value, list):
        if any(isinstance(item, dict) and keys & set(item) for item in value):
            found.append(path or "the response")
        for index, item in enumerate(value):
            found += lists_of_records(item, keys, f"{path}[{index}]")
    return found


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ProbeReleasesNoRecordsTests(ApiTestCase):
    """The probe is called from the coordinator's page, so its answer holds no patient record.

    It reports how many records the Guard would release and their field names.
    Every combination of the page's controls is checked, at every hospital,
    with the largest sample a probe may ask for.
    """

    def test_no_probe_response_contains_a_list_of_records(self) -> None:
        checked = 0
        for site_id in SITE_IDS:
            for verified in (True, False):
                for granularity in ("cell", "row", "column", "table", "model_update"):
                    for cohort_size in (5, 25):
                        body = {"site_id": site_id, "verified": verified, "granularity": granularity,
                                "cohort_size": cohort_size, "record_limit": 20}
                        payload = self.ok(self.client.post("/api/v1/governance:probe", json=body))
                        keys = RECORD_KEYS | set(payload.get("released_fields", []))
                        with self.subTest(**body):
                            self.assertNotIn("payload", payload)
                            self.assertEqual([], lists_of_records(payload, keys))
                            self.assertIsInstance(payload["released_count"], int)
                            self.assertTrue(all(isinstance(name, str) for name in payload["released_fields"]))
                            if not payload["permitted"]:
                                self.assertEqual(0, payload["released_count"])
                        checked += 1
        self.assertEqual(60, checked)

    def test_the_hospital_reports_the_release_without_the_records(self) -> None:
        payload = self.ok(self.client.post(
            "/api/v1/governance:probe",
            json={"site_id": "ch-245", "verified": True, "granularity": "row", "record_limit": 20},
        ))
        self.assertEqual(20, payload["released_count"])
        self.assertEqual(sorted(GUARD_PROBE_DEFAULT_FIELDS), sorted(payload["released_fields"]))
        self.assertEqual(
            {"site_id", "request_id", "boundary", "permitted", "plan", "released_count", "released_fields",
             "withheld_fields", "transformed_fields", "audit_id", "message", "framework_calls"},
            set(payload),
        )
        # The hospital's LMA ran the Guard, and only that LMA was called.
        self.assertEqual(
            [("lma", "ch-245", "POST", "/api/v1/governance/restrictions:probe", 200)],
            [(c["service"], c["site_id"], c["method"], c["path"], c["status"]) for c in payload["framework_calls"]],
        )

    def test_the_record_finder_finds_records(self) -> None:
        """The check above would pass on any response if this helper found nothing."""
        self.assertEqual([".payload"], lists_of_records({"payload": [{"sex": "female"}]}, RECORD_KEYS))
        self.assertEqual([".rows"], lists_of_records({"rows": [{"systolic_bp_mean": 150.0}]},
                                                     RECORD_KEYS | {"systolic_bp_mean"}))
        self.assertEqual([], lists_of_records({"plan": {"field_restrictions": [{"field": "sex"}]}}, RECORD_KEYS))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GovernedApiTests(ApiTestCase):
    """The Guard probe, the pilot and approval, with the real planner."""

    pilot: dict

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.pilot = cls.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}).json()

    def probe(self, **body) -> dict:
        return self.ok(self.client.post("/api/v1/governance:probe", json=body))

    def test_the_probe_asks_for_the_default_fields(self) -> None:
        payload = self.probe(verified=True, granularity="row")
        self.assertEqual("uk-134", payload["site_id"])
        fields = [restriction["field"] for restriction in payload["plan"]["field_restrictions"]]
        self.assertEqual(sorted(GUARD_PROBE_DEFAULT_FIELDS), sorted(fields))

    def test_the_probe_changes_with_the_requester(self) -> None:
        verified = self.probe(verified=True, granularity="row")
        unverified = self.probe(verified=False, granularity="table")
        self.assertTrue(verified["permitted"])
        self.assertFalse(unverified["permitted"])
        self.assertGreater(unverified["plan"]["filtering_score"], verified["plan"]["filtering_score"])

    def test_the_cohort_floor_refuses_a_small_aggregate(self) -> None:
        coarse = self.probe(verified=True, granularity="table", cohort_size=5)
        self.assertFalse(coarse["permitted"])
        self.assertTrue(any("below minimum_cohort" in v for v in coarse["plan"]["violations"]))
        self.assertTrue(self.probe(verified=True, granularity="row", cohort_size=5)["permitted"])

    def test_a_probe_at_a_chosen_site_is_audited_there_and_nowhere_else(self) -> None:
        def audit_count(site_id: str) -> int:
            records = self.ok(self.client.get(f"/api/v1/hospitals/{site_id}/audit?limit=500"))["records"]
            return len(records)

        before = {site_id: audit_count(site_id) for site_id in SITE_IDS}
        payload = self.probe(site_id="ch-245", verified=True, granularity="row", cohort_size=25)
        self.assertEqual("ch-245", payload["site_id"])
        after = {site_id: audit_count(site_id) for site_id in SITE_IDS}
        self.assertEqual(before["ch-245"] + 1, after["ch-245"])
        self.assertEqual(before["uk-134"], after["uk-134"])
        self.assertEqual(before["it-002"], after["it-002"])
        newest = self.ok(self.client.get("/api/v1/hospitals/ch-245/audit?limit=1"))["records"][0]
        self.assertEqual(payload["audit_id"], newest["audit_id"])
        self.assertTrue(self.ok(self.client.get("/api/v1/hospitals/ch-245/audit"))["chain_intact"])

    def test_the_probe_reads_the_chosen_sites_records(self) -> None:
        payload = self.probe(site_id="it-002", verified=True, granularity="row", requested_fields=["sex"])
        self.assertNotIn("payload", payload)
        self.assertEqual(5, payload["released_count"])
        self.assertEqual(["sex"], payload["released_fields"])
        self.assertEqual([], payload["withheld_fields"])

    def test_a_table_probe_reports_the_summary_it_would_release(self) -> None:
        payload = self.probe(site_id="uk-134", verified=True, granularity="table", cohort_size=25, record_limit=20)
        self.assertNotIn("payload", payload)
        self.assertEqual(1, payload["released_count"])
        self.assertIn("record_count", payload["released_fields"])

    def test_probe_errors(self) -> None:
        self.assertEqual(404, self.client.post("/api/v1/governance:probe", json={"site_id": "nowhere"}).status_code)
        response = self.client.post("/api/v1/governance:probe", json={"granularity": "aggregate"})
        self.assertEqual(422, response.status_code)
        self.assertIn("granularity", response.json()["errors"])
        response = self.client.post("/api/v1/governance:probe", json={"record_limit": 1000})
        self.assertEqual(422, response.status_code)

    def audit_lengths(self) -> dict[str, int]:
        return {
            site_id: len(self.ok(self.client.get(f"/api/v1/hospitals/{site_id}/audit?limit=500"))["records"])
            for site_id in SITE_IDS
        }

    def test_a_probe_takes_no_free_text(self) -> None:
        """The audit chain is public and cannot be edited, so a visitor's text must never reach it."""
        before = self.audit_lengths()
        for body in (
            {"site_id": "ch-245", "requester_id": "Jane Doe, NHS 943 476 5919"},
            {"site_id": "ch-245", "declared_purpose": "visit http://spam.example"},
            {"site_id": "ch-245", "requested_fields": ["call 555-0100 for free stuff"]},
        ):
            with self.subTest(body=body):
                response = self.client.post("/api/v1/governance:probe", json=body)
                self.assertEqual(422, response.status_code, response.text[:300])
                field = next(key for key in body if key != "site_id")
                self.assertIn(field, response.json()["errors"])
                if field != "requested_fields":
                    # A probe is a request, not a record, and the message says so.
                    self.assertEqual("not a field of this request", response.json()["errors"][field])
        self.assertEqual(before, self.audit_lengths())

    def test_a_probe_cannot_ask_for_the_label_or_site_only_fields(self) -> None:
        """The exact age, the row keys and the training label stay at the hospital."""
        before = self.audit_lengths()
        for field in ("age", "poor_outcome_6m", "source_row", "encounter_id", "origin", "recorded_at"):
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/v1/governance:probe",
                    json={"site_id": "uk-134", "granularity": "model_update", "verified": False,
                          "cohort_size": 25, "requested_fields": ["sex", field], "record_limit": 3},
                )
                self.assertEqual(422, response.status_code, response.text[:300])
                self.assertIn("requested_fields", response.json()["errors"])
                self.assertIn(field, response.json()["errors"]["requested_fields"])
        self.assertEqual(before, self.audit_lengths())

    def test_the_probe_records_a_fixed_requester_and_purpose(self) -> None:
        payload = self.probe(site_id="it-002", verified=True, granularity="row")
        newest = self.ok(self.client.get("/api/v1/hospitals/it-002/audit?limit=1"))["records"][0]
        self.assertEqual(payload["audit_id"], newest["audit_id"])
        self.assertEqual("demo-operator", newest["requester_id"])
        self.assertFalse(any("purpose" in violation for violation in newest["violations"]))

    def test_the_pilot_returns_its_evidence(self) -> None:
        pilot = self.pilot
        for key in (
            "study_id", "pilot_id", "rounds", "training_curve", "candidate", "release", "baseline_metrics",
            "site_audits", "coordinator_audit", "notice", "dataset_notice",
        ):
            self.assertIn(key, pilot)
        self.assertTrue(pilot["pilot_id"].startswith("api-"))
        self.assertEqual({"analytics", "evaluation", "training", "candidate_validation"}, set(pilot["rounds"]))
        self.assertEqual(5, len(pilot["rounds"]["training"]))
        self.assertEqual(5, len(pilot["training_curve"]))
        self.assertIn("auc", pilot["baseline_metrics"])
        for site_id, audit in pilot["site_audits"].items():
            # The coordinator sees whether each chain verifies and how long it
            # is, never the records themselves.
            self.assertEqual({"site_id": site_id, "chain_intact": True, "record_count": audit["record_count"]}, audit)
            self.assertGreater(audit["record_count"], 0)
        self.assertEqual({"chain_intact", "record_count"}, set(pilot["coordinator_audit"]))
        self.assertEqual(DATASET_NOTICE, pilot["dataset_notice"])

    def test_the_pilot_lists_the_agent_calls_behind_it(self) -> None:
        """The study operator calls only the GMA; the GMA's calls to each LMA follow the round that made them."""
        calls = self.pilot["framework_calls"]
        app_calls = [call for call in calls if call["via"] == "app"]
        self.assertTrue(app_calls)
        self.assertEqual({"gma"}, {call["service"] for call in app_calls})
        paths = {(call["method"], call["path"]) for call in app_calls}
        for expected in (
            ("POST", "/api/v1/federation/sites:refresh"),
            ("POST", "/api/v1/federation/rounds"),
            ("POST", "/api/v1/federation/rounds/{round_id}:aggregate"),
            ("POST", "/api/v1/federation/rounds/{round_id}:evaluate-release"),
        ):
            self.assertIn(expected, paths)
        dispatch = [call for call in calls if call["via"] == "gma"]
        self.assertEqual({"lma"}, {call["service"] for call in dispatch})
        for site_id in SITE_IDS:
            steps = {call["path"] for call in dispatch if call["site_id"] == site_id}
            self.assertEqual({"/api/v1/federation/jobs:run"}, steps, site_id)
        rounds = 3 + len(self.pilot["rounds"]["training"])
        self.assertEqual(rounds * len(SITE_IDS), len(dispatch), "one jobs:run call per hospital per round")
        for round_report in (self.pilot["rounds"]["analytics"], *self.pilot["rounds"]["training"]):
            self.assertEqual(sorted(SITE_IDS), sorted(a["site_id"] for a in round_report["acceptances"]))
            self.assertTrue(all(a["accepted"] for a in round_report["acceptances"]))
            self.assertEqual(len(SITE_IDS), len(round_report["dispatch"]))
            self.assertEqual({"run"}, {row["step"] for row in round_report["dispatch"]})
        self.assertNotIn("://", str(calls))

    def test_the_decision_matches_its_gate_results(self) -> None:
        gates = self.pilot["release"]["gates"]
        blocking = {gate.gate_id for gate in STROKE_RELEASE_GATES if gate.blocking}
        self.assertEqual(len(STROKE_RELEASE_GATES), len(gates["gate_results"]))
        failed = {r["gate_id"] for r in gates["gate_results"] if r["outcome"] != "passed" and r["gate_id"] in blocking}
        self.assertEqual(failed, {failure.split(":")[0] for failure in gates["blocking_failures"]})
        self.assertEqual("release" if not failed else "reject", gates["action"])

    def test_the_models_list_holds_the_candidate(self) -> None:
        models = {m["version"]: m for m in self.ok(self.client.get("/api/v1/models"))["models"]}
        candidate = self.pilot["candidate"]
        self.assertEqual(candidate["status"], models[candidate["candidate_version"]]["status"])

    def test_approval_follows_the_release_decision(self) -> None:
        version = self.pilot["candidate"]["candidate_version"]
        response = self.client.post(f"/api/v1/models/{version}:approve")
        if self.pilot["release"]["gates"]["action"] != "release" or not self.pilot["release"]["guard"]["permitted"]:
            self.assertEqual(409, response.status_code)
            self.assertIn("cannot be approved", response.json()["detail"])
            in_use = self.ok(self.client.get("/api/v1/models"))["model_in_use"]
            self.assertEqual("stroke-outcome-rule-v1", in_use["version"])
        else:
            payload = self.ok(response)
            self.assertEqual(version, payload["released"]["version"])
            self.assertEqual(409, self.client.post(f"/api/v1/models/{version}:approve").status_code)

    def test_approval_errors(self) -> None:
        self.assertEqual(404, self.client.post("/api/v1/models/no-such-model:approve").status_code)
        self.assertEqual(409, self.client.post("/api/v1/models/stroke-outcome-rule-v1:approve").status_code)

    def test_a_bad_round_prefix_is_a_field_error(self) -> None:
        """Pilot ids go into stored model versions, so a visitor cannot choose them."""
        for prefix in ("no spaces", "second", "x" * 40):
            with self.subTest(prefix=prefix):
                response = self.client.post("/api/v1/pilot:run", json={"round_prefix": prefix})
                self.assertEqual(422, response.status_code)
                self.assertEqual("must be one of ui, api", response.json()["errors"]["round_prefix"])

    def test_a_second_pilot_while_one_runs_is_refused(self) -> None:
        self.assertTrue(main.pilot_flight.try_enter())
        try:
            response = self.client.post("/api/v1/pilot:run", json={"round_prefix": "ui"})
        finally:
            main.pilot_flight.leave()
        self.assertEqual(409, response.status_code)
        self.assertEqual("A pilot is already running. Try again when it finishes.", response.json()["detail"])


def _gates(*gates):
    return [ReleaseGate(gate_id=g, metric="auc", comparison=GateComparison(kind="at_least", value=v)) for g, v in gates]


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ApprovalTests(ApiTestCase):
    """Approval with gates set so the decision is known in advance.

    The real gates decide whatever the data says. These tests replace them
    with one gate that cannot pass, or one that cannot fail, so both paths of
    approval are tested whatever the data says. The planner still makes the
    decision.
    """

    def run_pilot_with(self, gates) -> dict:
        """Put app services whose condition pack has these gates behind the app, for this test, and run a pilot."""
        demo = HealthcareDemo(extension=gated_extension(gates))
        self.addCleanup(demo.close)
        patcher = mock.patch.object(main, "demo", demo)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.ok(self.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}))

    def test_approval_is_refused_after_a_reject(self) -> None:
        in_use = self.ok(self.client.get("/api/v1/models"))["model_in_use"]["version"]
        pilot = self.run_pilot_with(_gates(("impossible", 1.01)))
        self.assertEqual("reject", pilot["release"]["gates"]["action"])
        version = pilot["candidate"]["candidate_version"]
        response = self.client.post(f"/api/v1/models/{version}:approve")
        self.assertEqual(409, response.status_code)
        self.assertIn("decided reject", response.json()["detail"])
        self.assertEqual(in_use, self.ok(self.client.get("/api/v1/models"))["model_in_use"]["version"])

    def test_an_approved_candidate_becomes_the_model_every_hospital_uses(self) -> None:
        pilot = self.run_pilot_with(_gates(("any_auc", 0.0)))
        self.assertEqual("release", pilot["release"]["gates"]["action"])
        self.assertTrue(pilot["release"]["guard"]["permitted"])
        version = pilot["candidate"]["candidate_version"]
        payload = self.ok(self.client.post(f"/api/v1/models/{version}:approve"))
        self.assertEqual(version, payload["released"]["version"])
        self.assertEqual("stroke-outcome-rule-v1", payload["previous"])
        self.assertEqual(version, self.ok(self.client.get("/api/v1/models"))["model_in_use"]["version"])
        overview = self.ok(self.client.get("/api/v1/overview"))
        self.assertEqual(version, overview["current_model"]["version"])
        row = self.ok(self.client.get("/api/v1/hospitals/it-002/worklist?limit=1"))["worklist"][0]
        self.assertEqual(version, row["assessment"]["model_version"])
        # A version is approved once.
        self.assertEqual(409, self.client.post(f"/api/v1/models/{version}:approve").status_code)


if __name__ == "__main__":
    unittest.main()
