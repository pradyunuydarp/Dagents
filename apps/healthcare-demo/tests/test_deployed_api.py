"""Exercise a *deployed* healthcare API, not a local one.

The local suite proves the code works. It cannot prove the thing serving the
published demo works, and those have differed in every way that matters: an
image without the Postgres driver, a port nothing routed to, a CORS value with a
stray parenthesis. Each was invisible to every test in this repository and
obvious within one request to the running service.

So this talks to a real deployment and asserts what the demo claims:

- the encounter store is the source, reported by the service rather than assumed;
- the guard's three levers change the protection strategy, which means the OCaml
  planner is in the image and answering;
- a cohort below the classification's floor is denied;
- a full pilot produces a candidate and is still refused release by a failing
  gate — the demo's central argument;
- every site's audit chain verifies;
- the legacy and versioned health routes agree, as the convention requires.

Set `HEALTHCARE_DEPLOYED_API` to run it. Without that it skips loudly rather
than silently, because a suite that quietly drops its only end-to-end coverage
reports OK having checked nothing.
"""

from __future__ import annotations

import os
import time
import unittest
import warnings

import httpx


API = os.environ.get("HEALTHCARE_DEPLOYED_API", "").rstrip("/")

#: What the frontend will wait for a sleeping container, from `api.ts`. Kept the
#: same here on purpose: if a real wake takes longer than the page allows, the
#: published demo shows an error to whoever arrived first, and this is where
#: that gets caught.
FRONTEND_COLD_START_BUDGET_SECONDS = 90

if not API:
    warnings.warn(
        "HEALTHCARE_DEPLOYED_API is not set, so the deployed-service tests will skip. "
        "They are the only coverage of the thing serving the published demo; set it to "
        "the API's base URL to run them.",
        RuntimeWarning,
        stacklevel=2,
    )


@unittest.skipUnless(API, "HEALTHCARE_DEPLOYED_API is not set")
class DeployedApiTests(unittest.TestCase):
    """One client, one wake, then the assertions."""

    client: httpx.Client
    wake_seconds: float

    @classmethod
    def setUpClass(cls) -> None:
        # Generous per-request timeout: the service may be asleep, and a wake is
        # not a failure. How long it took is reported below.
        cls.client = httpx.Client(
            base_url=API,
            timeout=FRONTEND_COLD_START_BUDGET_SECONDS + 30,
            # No connection reuse, and retry a failed connect. A host that
            # sleeps when idle drops keep-alive sockets, and a reused one that
            # has gone away surfaces as a read error on whichever assertion
            # happened to be next — a confusing way to fail a test about
            # something else. Thirty short-lived connections cost nothing here.
            limits=httpx.Limits(max_keepalive_connections=0),
            transport=httpx.HTTPTransport(retries=2),
        )
        started = time.monotonic()
        response = cls.client.get("/api/v1/health")
        cls.wake_seconds = time.monotonic() - started
        response.raise_for_status()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def get_json(self, path: str, what: str) -> dict:
        """GET and decode, retrying once on a transport error.

        A GET here is idempotent, and a socket that died between requests —
        because the host dropped it, or because a previous request made the
        service close the connection — should not fail an assertion about
        something else. A second failure is real and is reported.
        """
        try:
            response = self.client.get(path)
        except httpx.TransportError:
            response = self.client.get(path)
        return self.json_of(response, what)

    def json_of(self, response: httpx.Response, what: str) -> dict:
        """The body, or a failure that names the status and what came back.

        This deployment has shipped without a binary it needed twice — once the
        Postgres driver, and the Guard fails closed without the planner. From
        outside, both look like a 500 with a short body, and a bare
        `JSONDecodeError` would bury the one fact worth reporting.
        """
        if response.status_code // 100 != 2:
            self.fail(f"{what} returned {response.status_code}: {response.text[:300]!r}")
        try:
            return response.json()
        except ValueError:
            self.fail(f"{what} returned a non-JSON body: {response.text[:300]!r}")

    def test_the_first_request_fits_the_budget_the_page_allows(self) -> None:
        """Reported either way, because the number is the point.

        A warm service answers in well under a second and this proves nothing. A
        cold one takes as long as the image takes to start, and if that exceeds
        what `api.ts` waits for, the published page fails for whoever arrives
        first — which looks like the demo being broken.
        """
        kind = "a cold start" if self.wake_seconds > 5 else "a warm service"
        print(f"\n  first request: {self.wake_seconds:.1f}s ({kind})")
        self.assertLess(
            self.wake_seconds,
            FRONTEND_COLD_START_BUDGET_SECONDS,
            f"the first request took {self.wake_seconds:.0f}s, longer than the "
            f"{FRONTEND_COLD_START_BUDGET_SECONDS}s the published page waits, so a visitor "
            "arriving at a sleeping service sees an error",
        )

    def test_the_two_health_routes_agree(self) -> None:
        """The alias convention, checked where it is actually served."""
        self.assertEqual(
            self.client.get("/health").json(),
            self.client.get("/api/v1/health").json(),
        )

    def test_it_reports_reading_the_encounter_store(self) -> None:
        status = self.get_json("/api/v1/framework/status", "/api/v1/framework/status")
        self.assertEqual(
            "supabase",
            status["cohort_source"],
            "the deployment is generating cohorts in process, so the published demo would "
            "claim live data for invented data",
        )
        self.assertTrue(status["records_are_synthetic"])

    def test_every_site_serves_a_cohort_and_a_worklist(self) -> None:
        overview = self.get_json("/api/v1/overview", "/api/v1/overview")
        self.assertGreaterEqual(len(overview["hospitals"]), 3)
        for hospital in overview["hospitals"]:
            with self.subTest(site=hospital["site_id"]):
                self.assertGreater(hospital["cohort_size"], 0)
                worklist = self.get_json(
                    f"/api/v1/hospitals/{hospital['site_id']}/worklist?limit=5", "a site worklist"
                )["worklist"]
                self.assertTrue(worklist, "a site served no worklist rows")
                # The rows are records, not placeholders.
                self.assertIn("encounter_id", worklist[0])

    def test_the_guard_levers_change_the_strategy(self) -> None:
        """The planner is in the image, and it is deciding.

        If `dagentsc` were missing the Guard would fail closed and deny
        everything — which looks orderly and proves nothing, so this asserts the
        levers produce *different* answers rather than merely valid ones.
        """
        strategies = {}
        for verified in (True, False):
            # Both are values the UI's own selector offers. `table` is the coarse
            # end, where a request can only come back as an aggregate.
            for granularity in ("row", "table"):
                body = {
                    "verified": verified,
                    "granularity": granularity,
                    "cohort_size": 25,
                    "boundary": "before_read",
                }
                plan = self.json_of(
                    self.client.post("/api/v1/governance:probe", json=body),
                    "/api/v1/governance:probe",
                )
                key = (verified, granularity)
                restrictions = plan["plan"]["field_restrictions"]
                # An empty plan is what a planner-less deployment produces: the
                # Guard fails closed, denies, and names no strategy. Said
                # plainly here, because "every combination matched" is a
                # confusing way to report "nothing decided anything".
                self.assertTrue(
                    restrictions,
                    f"{key} came back with no field restrictions at all. The Guard fails "
                    "closed, so this is what a deployment without the OCaml planner looks "
                    "like — check that dagentsc is in the image.",
                )
                strategies[key] = sorted(
                    restriction["strategy"] for restriction in restrictions
                )
        self.assertGreater(
            len({tuple(value) for value in strategies.values()}),
            1,
            f"every lever combination produced the same protection: {strategies}",
        )

    def test_a_cohort_below_the_floor_is_denied(self) -> None:
        """The classification's minimum cohort, enforced by the deployment."""
        plan = self.json_of(
            self.client.post(
            "/api/v1/governance:probe",
            json={
                "verified": True,
                "granularity": "row",
                "cohort_size": 5,
                "boundary": "before_read",
            },
            ),
            "/api/v1/governance:probe",
        )
        self.assertFalse(plan["permitted"], "a five-subject cohort was permitted")

    def test_a_lever_outside_its_set_is_refused_at_the_boundary(self) -> None:
        """A wrong value is the client's mistake, and must be reported as one.

        These three fields are closed sets. They used to be typed as `str` here
        and as `Literal`s one layer down, so a value outside the set was
        accepted by the API and rejected by the domain model — arriving as a 500
        that looked like the service being broken. 422 names the field and lists
        what it accepts.
        """
        response = self.client.post(
            "/api/v1/governance:probe",
            json={
                "verified": True,
                "granularity": "aggregate",  # not one of cell/row/column/table/model_update
                "cohort_size": 25,
                "boundary": "before_read",
            },
        )
        self.assertEqual(
            422,
            response.status_code,
            f"an out-of-range granularity returned {response.status_code}: {response.text[:200]!r}",
        )

    def test_a_pilot_produces_a_candidate_and_is_refused_release(self) -> None:
        """The argument the whole demo exists to make.

        Aggregation yields a candidate, never a release; a gate that fails
        blocks it even when the candidate beats its baseline. Both invariants
        are the framework's, and this checks them where they are deployed.
        """
        pilot = self.json_of(
            self.client.post("/api/v1/pilot:run", json={"round_prefix": "deployed-test"}),
            "/api/v1/pilot:run",
        )
        self.assertIsNotNone(pilot.get("candidate"), "the pilot produced no candidate")

        decision = pilot["release"]["gates"]
        results = decision["gate_results"]
        self.assertTrue(results, "the pilot reached no release gates")
        self.assertNotEqual(
            "release",
            decision["action"],
            "the candidate was recommended for release; a fairness gate is meant to block it",
        )
        self.assertTrue(
            decision["blocking_failures"],
            f"nothing blocked, so the release was not actually gated: {results}",
        )
        print(
            f"\n  release action: {decision['action']} "
            f"(blocked by {', '.join(decision['blocking_failures'])})"
        )

    def test_every_audit_chain_verifies(self) -> None:
        for hospital in self.get_json("/api/v1/overview", "/api/v1/overview")["hospitals"]:
            with self.subTest(site=hospital["site_id"]):
                audit = self.get_json(
                    f"/api/v1/hospitals/{hospital['site_id']}/audit", "a site audit trail"
                )
                self.assertTrue(audit["chain_intact"], "a site's audit chain did not verify")


if __name__ == "__main__":
    unittest.main()
