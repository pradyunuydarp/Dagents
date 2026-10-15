"""The healthcare demo split into hospital LMAs, the Dagents GMA and the app.

In a split run each hospital runs in its own process (``HEALTHCARE_DEMO_ROLE=lma``),
the Dagents GMA service runs the rounds, and the app process
(``HEALTHCARE_DEMO_ROLE=app``) keeps the model registry and the update store.
These tests run each process as its own FastAPI app in this process and
connect them with a small in-process network (``tests.split_support``).

They check the boundary between the processes: a hospital reports counts and
summaries, never records; a hospital loads models from the app and checks
their digests; the app stores an update only for a round being dispatched now;
an unreachable process is a 503 that names it without its address; and a
pilot over the split processes has the same rounds as one in a single process.

The tests that need the planner skip without ``dagentsc``. The others run
everywhere.
"""

from __future__ import annotations

import json
import re
import unittest
from typing import Any

import httpx

from tests.split_support import APP_URL, GMA_URL, LMA_URLS, SplitRun  # noqa: E402  (sets sys.path)

from agents.common.domain.federation import (
    AggregationMethod,
    FederatedJob,
    RoundManifest,
    SiteRegistration,
)
from agents.common.infrastructure.federation_http import EXECUTE_PATH, OFFER_PATH, UPDATE_PATH

from app.api.role_paths import is_hospital_path, lma_serves
from app.domain import stroke_model
from app.domain.conditions import STROKE_CLASSIFICATION, STROKE_FEATURE_CONTRACT
from app.domain.dataset import SITES
from app.services.consortium import Consortium
from app.services.remote import (
    GmaReleaseGuard,
    GmaRoundClient,
    RemoteModelRegistry,
    RemoteServiceUnavailable,
)
from app.services.runtime import SEED_VERSION, ModelVerificationError, code_digest, update_digest

from tests.support import HIGH_RISK_ENTRY, dagentsc_available

#: Every record field and row key. None may appear in what a hospital reports.
RECORD_FIELDS = set(STROKE_FEATURE_CONTRACT.field_names()) | {
    "encounter_id",
    "source_row",
    "client_request_id",
    "records",
}

#: An encounter id of any of the three hospitals, from the extract or entered.
ENCOUNTER_ID = re.compile(r"\b(uk-134|it-002|ch-245)-(\d{5}|e\d+)\b")

FIRST = SITES[0].site_id


def _keys(value: Any) -> set[str]:
    """Every key in a JSON value, at any depth."""
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in _keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


def _update(scale: float) -> dict[str, float]:
    return {name: scale * (index + 1) / 100.0 for index, name in enumerate(stroke_model.FEATURES)}


class SplitTestCase(unittest.TestCase):
    split: SplitRun

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.split = SplitRun()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.split.close()
        super().tearDownClass()

    def ok(self, response: httpx.Response, status: int = 200) -> Any:
        self.assertEqual(status, response.status_code, response.text[:500])
        return response.json()

    def training_job(self, round_id: str, site_id: str = FIRST, digest: str = "sha256:round") -> FederatedJob:
        model = self.split.app.consortium.registry.ensure_initial()
        manifest = RoundManifest(
            round_id=round_id,
            study_id="stroke-study",
            condition_id="suspected_stroke",
            phase="training",
            model_version=model.version,
            model_artifact_digest=model.digest,
            training_code_digest=code_digest(model.kind),
            feature_contract_version="v3",
            aggregation=AggregationMethod(kind="secure_aggregation", threshold=2),
        )
        return FederatedJob(round_id=round_id, site_id=site_id, manifest=manifest, manifest_digest=digest, task="train")


class LmaTests(SplitTestCase):
    """A hospital's LMA serves its own hospital and reports no records."""

    def test_the_site_report_holds_counts_and_summaries_and_no_records(self) -> None:
        client = self.split.lma_client(FIRST)
        report = self.ok(client.get("/api/v1/site/report"))
        self.assertEqual(
            {"site_id", "summary", "registration", "counts", "store_kind", "audit_summary"}, set(report)
        )
        self.assertEqual(FIRST, report["site_id"])
        self.assertEqual("in-memory", report["store_kind"])
        self.assertEqual(report["counts"]["total"], report["summary"]["record_count"])
        self.assertEqual(report["counts"]["total"], report["registration"]["cohort_size"])
        self.assertEqual({"site_id", "chain_intact", "record_count"}, set(report["audit_summary"]))
        SiteRegistration.model_validate(report["registration"])
        self.assertFalse(_keys(report) & RECORD_FIELDS, _keys(report) & RECORD_FIELDS)
        self.assertIsNone(ENCOUNTER_ID.search(json.dumps(report)))

    def test_an_lma_serves_its_own_hospital_and_no_other(self) -> None:
        client = self.split.lma_client(FIRST)
        self.ok(client.get("/health"))
        self.ok(client.get("/api/v1/health"))
        other = SITES[1].site_id
        response = client.get(f"/api/v1/hospitals/{other}")
        self.assertEqual(404, response.status_code)
        self.assertEqual(f"There is no hospital with the id {other!r}.", response.json()["detail"])
        for path in ("/api/v1/overview", "/api/v1/hospitals", "/api/v1/models", "/api/v1/charts"):
            self.assertEqual(404, client.get(path).status_code, path)
        self.assertEqual(404, client.post("/api/v1/pilot:run", json={}).status_code)
        # The app's internal routes are not an LMA's.
        self.assertEqual(404, client.get("/api/v1/registry/model-in-use").status_code)

    def test_a_hospital_page_uses_the_model_the_app_serves(self) -> None:
        client = self.split.lma_client(FIRST)
        dashboard = self.ok(client.get(f"/api/v1/hospitals/{FIRST}"))
        in_use = self.split.app.consortium.registry.model_in_use()
        self.assertEqual(in_use.version, dashboard["current_model"]["version"])
        worklist = self.ok(client.get(f"/api/v1/hospitals/{FIRST}/worklist", params={"limit": 3}))
        self.assertEqual(3, len(worklist["worklist"]))
        self.assertEqual(SEED_VERSION, worklist["worklist"][0]["assessment"]["model_version"])

    def test_an_lma_refuses_a_job_for_another_hospital(self) -> None:
        job = self.training_job("other-site", site_id=SITES[1].site_id)
        response = self.split.lma_client(FIRST).post(OFFER_PATH, json=job.model_dump(mode="json"))
        self.assertEqual(404, response.status_code)

    def test_the_public_api_is_the_same_in_every_role(self) -> None:
        from tests.api_support import main as combined

        public = combined.app.openapi()
        self.assertEqual(public, self.split.lmas[FIRST].app.openapi())
        self.assertEqual(public, self.split.app.app.openapi())
        paths = set(public["paths"])
        for internal in ("/api/v1/site/report", OFFER_PATH, EXECUTE_PATH, UPDATE_PATH, "/api/v1/registry/model-in-use"):
            self.assertNotIn(internal, paths)

    def test_the_combined_process_has_no_internal_routes(self) -> None:
        from tests.api_support import main as combined

        paths = {getattr(route, "path", "") for route in combined.app.routes}
        self.assertFalse({p for p in paths if p.startswith(("/api/v1/site", "/api/v1/registry", "/api/v1/federation"))})

    def test_which_paths_are_about_one_hospital(self) -> None:
        self.assertTrue(is_hospital_path("/api/v1/hospitals/uk-134/worklist"))
        self.assertTrue(is_hospital_path("/api/v1/governance:probe"))
        self.assertTrue(is_hospital_path("/api/v1/framework/trace"))
        self.assertFalse(is_hospital_path("/api/v1/hospitals"))
        self.assertFalse(is_hospital_path("/api/v1/overview"))
        self.assertTrue(lma_serves(OFFER_PATH))
        self.assertFalse(lma_serves("/api/v1/framework/status"))


class RegistryTests(SplitTestCase):
    """The app serves models; a hospital checks every one it loads."""

    def test_the_app_serves_a_model_with_its_parameters(self) -> None:
        client = self.split.app_client
        in_use = self.ok(client.get("/api/v1/registry/model-in-use"))
        self.assertEqual(SEED_VERSION, in_use["version"])
        self.assertIn("params", in_use)
        one = self.ok(client.get(f"/api/v1/registry/models/{SEED_VERSION}"))
        self.assertEqual(in_use["digest"], one["digest"])
        self.assertEqual(in_use["params"], one["params"])

    def test_an_unknown_version_is_a_404(self) -> None:
        response = self.split.app_client.get("/api/v1/registry/models/no-such-model")
        self.assertEqual(404, response.status_code)
        self.assertEqual("There is no model version 'no-such-model'.", response.json()["detail"])

    def test_a_hospital_loads_a_model_only_with_the_digest_the_round_names(self) -> None:
        registry = RemoteModelRegistry(APP_URL, client=self.split.router)  # type: ignore[arg-type]
        initial = self.split.app.consortium.registry.ensure_initial()
        self.assertEqual(initial.params, registry.load(initial.version, initial.digest).params)
        with self.assertRaisesRegex(ModelVerificationError, "do not match the digest the round names"):
            registry.load(initial.version, "sha256:" + "0" * 64)
        with self.assertRaisesRegex(ModelVerificationError, "is not in the registry"):
            registry.load("no-such-model", initial.digest)

    def test_a_hospital_refuses_a_model_whose_parameters_changed_on_the_way(self) -> None:
        row = self.split.app.consortium.registry.ensure_initial().row()
        row["params"] = {**row["params"], "intercept": 5.0}

        class Tampering:
            def request(self, method: str, url: str, **_: Any) -> httpx.Response:
                return httpx.Response(200, json=row)

        registry = RemoteModelRegistry(APP_URL, client=Tampering())  # type: ignore[arg-type]
        with self.assertRaisesRegex(ModelVerificationError, "do not match its stored digest"):
            registry.load(row["version"], row["digest"])

    def test_a_registry_that_cannot_be_reached_refuses_the_model(self) -> None:
        self.split.router.down.add(httpx.URL(APP_URL).host)
        try:
            registry = RemoteModelRegistry(APP_URL, client=self.split.router)  # type: ignore[arg-type]
            with self.assertRaisesRegex(ModelVerificationError, "could not be reached"):
                registry.load("stroke-outcome-logreg-init", "sha256:any")
            with self.assertRaises(RemoteServiceUnavailable) as raised:
                registry.model_in_use()
            self.assertNotIn("http", raised.exception.public_message)
            response = self.split.lma_client(FIRST).get(f"/api/v1/hospitals/{FIRST}/worklist")
            self.assertEqual(503, response.status_code)
            self.assertNotIn("healthcare-app", response.text)
            self.assertIn("coordinator", response.json()["detail"])
        finally:
            self.split.router.down.clear()


class UpdateTests(SplitTestCase):
    """The app stores an update only for a round whose jobs are being dispatched now."""

    def post_update(self, path_round: str, job: FederatedJob, update: dict[str, float]) -> httpx.Response:
        return self.split.app_client.post(
            UPDATE_PATH.format(round_id=path_round),
            json={"job": job.model_dump(mode="json"), "update": update},
        )

    def test_an_update_for_a_running_round_is_stored_once(self) -> None:
        consortium: Consortium = self.split.app.consortium
        job = self.training_job("r-running")
        with consortium._round_running("r-running", job.manifest_digest):
            receipt = self.ok(self.post_update("r-running", job, _update(1.0)))
            self.assertEqual(update_digest(_update(1.0)), receipt["update_digest"])
            self.assertEqual(409, self.post_update("r-running", job, _update(1.0)).status_code)
        self.assertEqual(["r-running"], consortium.runtime.pending_rounds())
        consortium.runtime.discard("r-running")

    def test_an_update_for_a_round_that_is_not_running_is_refused(self) -> None:
        job = self.training_job("r-not-running")
        response = self.post_update("r-not-running", job, _update(1.0))
        self.assertEqual(409, response.status_code)
        self.assertIn("not running", response.json()["detail"])

    def test_an_update_whose_round_or_site_does_not_match_is_refused(self) -> None:
        consortium: Consortium = self.split.app.consortium
        job = self.training_job("r-a")
        stranger = self.training_job("r-a", site_id="xx-999")
        wrong_digest = self.training_job("r-a", digest="sha256:another")
        evaluation = self.training_job("r-a").model_copy(update={"task": "evaluate"})
        with consortium._round_running("r-a", job.manifest_digest):
            self.assertEqual(409, self.post_update("r-b", job, _update(1.0)).status_code)
            self.assertEqual(409, self.post_update("r-a", stranger, _update(1.0)).status_code)
            self.assertEqual(409, self.post_update("r-a", wrong_digest, _update(1.0)).status_code)
            self.assertEqual(409, self.post_update("r-a", evaluation, _update(1.0)).status_code)
        self.assertEqual([], consortium.runtime.pending_rounds())

    def test_a_hospital_sees_a_refused_update_as_an_error(self) -> None:
        node = self.split.lmas[FIRST].consortium
        with self.assertRaisesRegex(RuntimeError, "409"):
            node.update_channel().submit(self.training_job("r-closed"), _update(1.0))


class AppTests(SplitTestCase):
    """The app works from what each LMA reports, and names a hospital it cannot reach."""

    def test_the_overview_shows_what_each_lma_reports(self) -> None:
        overview = self.ok(self.split.app_client.get("/api/v1/overview"))
        self.assertEqual("dagents-gma", overview["engine"])
        self.assertEqual([site.site_id for site in SITES], [row["site_id"] for row in overview["hospitals"]])
        for row in overview["hospitals"]:
            report = self.ok(self.split.lma_client(row["site_id"]).get("/api/v1/site/report"))
            self.assertEqual(report["summary"], row)

    def test_the_overview_includes_a_record_just_entered_at_an_lma(self) -> None:
        def first_count() -> int:
            return self.ok(self.split.app_client.get("/api/v1/hospitals"))[0]["record_count"]

        before = first_count()
        self.ok(self.split.lma_client(FIRST).post(f"/api/v1/hospitals/{FIRST}/encounters", json=HIGH_RISK_ENTRY), 201)
        self.assertEqual(before + 1, first_count())

    def test_the_status_reports_where_the_lmas_keep_their_records(self) -> None:
        status = self.ok(self.split.app_client.get("/api/v1/framework/status"))
        self.assertEqual("in-memory", status["cohort_source"])
        self.assertTrue(status["ready"])
        self.assertEqual([site.site_id for site in SITES], [row["site_id"] for row in status["hospitals"]])

    def test_a_hospital_that_cannot_be_reached_is_a_503_that_names_it(self) -> None:
        host = httpx.URL(LMA_URLS[SITES[1].site_id]).host
        self.split.router.down.add(host)
        try:
            response = self.split.app_client.get("/api/v1/overview")
            self.assertEqual(503, response.status_code)
            self.assertIn(SITES[1].display_name, response.json()["detail"])
            self.assertNotIn(host, response.text)
            status = self.ok(self.split.app_client.get("/api/v1/framework/status"))
            self.assertFalse(status["ready"])
        finally:
            self.split.router.down.clear()


class GmaClientTests(SplitTestCase):
    """The app's round controller and release Guard call the Dagents GMA's API."""

    def test_sites_are_enrolled_at_the_gma(self) -> None:
        client = GmaRoundClient(GMA_URL, self.split.app.consortium.runtime, client=self.split.router)  # type: ignore[arg-type]
        registration = SiteRegistration(
            site_id="uk-134", capabilities=["local_profiling"], feature_contract_version="v3", cohort_size=10
        )
        client.register_site(registration)
        self.assertIn("uk-134", [site.site_id for site in client.list_sites()])
        self.assertEqual(["uk-134"], [site.site_id for site in self.split.gma_rounds.list_sites() if site.site_id == "uk-134"])
        self.assertIsNone(client.get_round("no-such-round"))
        with self.assertRaises(KeyError):
            client.aggregate("no-such-round")

    def test_a_gma_that_cannot_be_reached_is_a_503_without_its_address(self) -> None:
        client = GmaRoundClient("http://nowhere", self.split.app.consortium.runtime, client=self.split.router)  # type: ignore[arg-type]
        with self.assertRaises(RemoteServiceUnavailable) as raised:
            client.list_sites()
        self.assertIn("Dagents GMA", raised.exception.public_message)
        self.assertNotIn("nowhere", raised.exception.public_message)

    def test_the_release_guard_runs_at_the_gma_and_is_audited_there(self) -> None:
        guard = GmaReleaseGuard(GMA_URL, STROKE_CLASSIFICATION, client=self.split.router)  # type: ignore[arg-type]
        before = guard.audit_record_count()
        decision = self.split.app.consortium.release_guard_decision("r-release", self._candidate())
        if not dagentsc_available():
            # Without the planner the GMA's Guard fails closed.
            self.assertFalse(decision["permitted"])
        self.assertEqual(before + 1, guard.audit_record_count())
        self.assertEqual(before + 1, self.split.gma_governance.audit_record_count())
        self.assertTrue(guard.audit_chain_intact())
        ids = [row["classification_id"] for row in self.ok(self.split.gma_client.get("/api/v1/governance/classifications"))]
        self.assertIn(STROKE_CLASSIFICATION.classification_id, ids)

    @staticmethod
    def _candidate() -> Any:
        from agents.common.domain.federation import CandidateModel

        return CandidateModel(
            round_id="r-release",
            candidate_version="no-such-candidate",
            parent_version="stroke-outcome-logreg-init",
            aggregation_method="secure_aggregation",
            contributed_examples=500,
        )


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SplitPlannerTests(SplitTestCase):
    """With the planner: digests, refusals and a whole pilot over the split processes."""

    def test_an_lma_refuses_a_job_whose_digest_is_not_the_one_it_computes(self) -> None:
        consortium: Consortium = self.split.app.consortium
        manifest = consortium.manifest("r-wrong-digest", "analytics")
        job = FederatedJob(
            round_id="r-wrong-digest",
            site_id=FIRST,
            manifest=manifest,
            manifest_digest="sha256:" + "0" * 64,
            task="profile",
        )
        client = self.split.lma_client(FIRST)
        acceptance = self.ok(client.post(OFFER_PATH, json=job.model_dump(mode="json")))
        self.assertFalse(acceptance["accepted"])
        self.assertIn("digest", acceptance["reason"])
        result = self.ok(client.post(EXECUTE_PATH, json=job.model_dump(mode="json")))
        self.assertEqual("rejected", result["participation"])
        self.assertEqual({}, result["metrics"])
        dashboard = self.ok(client.get(f"/api/v1/hospitals/{FIRST}"))
        refused = [row for row in dashboard["contributions"] if row["round_id"] == "r-wrong-digest"]
        self.assertTrue(refused)
        self.assertTrue(all("digest" in (row["reason"] or "") for row in refused))

    def test_a_pilot_over_the_split_processes_has_the_rounds_of_one_process(self) -> None:
        split_report = self.split.app.consortium.run_pilot("split", training_rounds=2)
        combined_report = Consortium().run_pilot("combined", training_rounds=2)
        self.assertEqual(_shape(combined_report), _shape(split_report))
        self.assertEqual("dagents-gma", split_report["engine"])
        self.assertIsNotNone(split_report["candidate"])
        self.assertTrue(split_report["coordinator_audit"]["chain_intact"])
        self.assertGreaterEqual(split_report["coordinator_audit"]["record_count"], 1)
        # The app holds no update after the pilot, and the GMA's rounds hold no record.
        self.assertEqual([], self.split.app.consortium.runtime.pending_rounds())
        stored = json.dumps([record.model_dump(mode="json") for record in self.split.gma_rounds.list_rounds(limit=50)])
        self.assertIsNone(ENCOUNTER_ID.search(stored))
        self.assertFalse(_keys(json.loads(stored)) & RECORD_FIELDS)
        # The pilot's candidate is in the app's registry, and the LMAs can load it.
        candidate = split_report["candidate"]
        model = self.split.app.consortium.registry.get(candidate["candidate_version"])
        self.assertIsNotNone(model)
        node_registry = self.split.lmas[FIRST].consortium.registry
        self.assertEqual(model.params, node_registry.load(model.version, model.digest).params)


def _round_shape(report: dict[str, Any] | None) -> Any:
    """What a round did, without ids, digests or measurements."""
    if report is None:
        return None
    return {
        "phase": report["phase"],
        "status": report["status"],
        "selected": report["plan"]["selected_sites"],
        "quorum_met": report["plan"]["quorum_met"],
        "results": [(row["site_id"], row["participation"], row["contributed_examples"]) for row in report["results"]],
        "permitted": (report["readiness"] or {}).get("aggregation_permitted"),
        "has_candidate": report.get("candidate_version") is not None,
    }


def _shape(report: dict[str, Any]) -> dict[str, Any]:
    rounds = report["rounds"]
    release = report["release"] or {}
    return {
        "analytics": _round_shape(rounds["analytics"]),
        "evaluation": _round_shape(rounds["evaluation"]),
        "training": [_round_shape(item) for item in rounds["training"]],
        "candidate_validation": _round_shape(rounds["candidate_validation"]),
        "curve_rounds": [point["round"] for point in report["training_curve"]],
        "stop_reason": report["stop_reason"],
        "candidate_status": (report["candidate"] or {}).get("status"),
        "release_action": (release.get("gates") or {}).get("action"),
        "guard_permitted": (release.get("guard") or {}).get("permitted"),
        "baseline_metrics": sorted(report["baseline_metrics"]),
        "site_audits": sorted(report["site_audits"]),
    }


if __name__ == "__main__":
    unittest.main()
