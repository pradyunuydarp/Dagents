"""Exercise a *deployed* healthcare API, not a local one.

The local suite tests the code. It cannot test the service behind the published
demo, and the two have differed: an image without the Postgres driver, a port
nothing routed to, a CORS value with a stray parenthesis. Each passed every
test in this repository and failed on the first request to the running service.

So this talks to a real deployment and checks what the demo shows:

- the records come from Postgres, as the service itself reports;
- the Dagents LMAs and GMA run inside the API, every LMA answers the GMA, and
  each answer lists the agent calls behind it, by route only;
- every hospital serves a worklist of real rows;
- the Guard's levers change the protection strategy, so the OCaml planner is in
  the image and answering;
- an aggregate below the classification's minimum cohort is denied;
- a pilot produces a decision that matches its own gate results (the test does
  not assume which decision);
- every hospital's audit chain verifies;
- the consortium charts are listed and one draws;
- the legacy and versioned health routes agree;
- the app marks its own responses with ``X-Dagents-App``;
- the rate limits read the client from the ``X-Forwarded-For`` entry the
  platform's edge wrote;
- it never offers to add a hospital, because that starts a process or a
  container.

It never enters records, records outcomes, approves models or adds a hospital,
so it changes no hospital's data. A probe and a pilot do add audit records and model versions,
as they do when a visitor uses the page.

Set ``HEALTHCARE_DEPLOYED_API`` to run it. Without it the tests skip with a
warning, because they are the only check of the deployed service.

Set ``HEALTHCARE_DEPLOYED_API_READ_ONLY=1`` to skip the five tests that send a
probe or run a pilot. The daily run of ``healthcare-api-check.yml`` sets it, so
that run adds no audit record and no model version. Its only POST requests are
the two that ask to add a hospital, which the hosted API refuses with 403
before it does anything. A manual run leaves it unset and runs every test.
"""

from __future__ import annotations

import json
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

#: Set by the scheduled run of healthcare-api-check.yml. See the module docstring.
READ_ONLY = os.environ.get("HEALTHCARE_DEPLOYED_API_READ_ONLY", "").strip().lower() in {"1", "true", "yes"}

#: Marks a test that sends a probe or runs a pilot. A probe adds an audit
#: record, and a pilot adds audit records and a model version. The probe with
#: a value outside its set is refused before it is recorded, and it skips too,
#: so a read-only run sends no probe at all.
writes_to_the_demo = unittest.skipIf(
    READ_ONLY,
    "HEALTHCARE_DEPLOYED_API_READ_ONLY is set, and this test sends a probe or runs a pilot",
)

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

    def test_it_reports_reading_postgres(self) -> None:
        status = self.get_json("/api/v1/framework/status", "/api/v1/framework/status")
        self.assertEqual(
            "postgres",
            status["cohort_source"],
            "the deployment keeps its records in memory, so the published demo would show "
            "records that are not in the database",
        )
        self.assertEqual("schema-per-site", status["store_layout"])
        self.assertNotIn("records_are_synthetic", status)
        self.assertIn("notice", status["dataset"])
        self.assertIs(
            True,
            status.get("ready"),
            f"the hospital stores are not ready (hospitals: {status.get('hospitals')}); "
            "run Actions → Healthcare data with seed on",
        )
        for row in status["hospitals"]:
            with self.subTest(site=row["site_id"]):
                self.assertGreater(row["record_count"], 0)

    def test_the_agents_run_inside_the_api_and_every_lma_answers(self) -> None:
        """The deployed image is the one that calls the Dagents LMAs and GMA through their APIs.

        An image from before the agents passes every other test here, so a
        deploy that kept the old image would look green without this one.
        """
        status = self.get_json("/api/v1/framework/status", "/api/v1/framework/status")
        agents = status.get("agents")
        self.assertIsInstance(
            agents, dict, "the status report has no agents block, so the deployed image predates the Dagents agents"
        )
        self.assertEqual("in-process", agents["mode"])
        self.assertEqual("http", agents["engine"], "the GMA does not run the rounds over HTTP")
        self.assertTrue(agents["lmas"], "the GMA reported no LMA")
        for lma in agents["lmas"]:
            with self.subTest(site=lma["site_id"]):
                self.assertTrue(lma["reachable"], f"the LMA of {lma['site_id']} did not answer the GMA")

    def assert_routes_only(self, calls: list[dict]) -> None:
        """Each call names its agent and route, and no address."""
        for call in calls:
            with self.subTest(call=call):
                self.assertEqual({"service", "site_id", "method", "path", "status", "ms", "via"}, set(call))
                self.assertNotIn("://", json.dumps(call), "a call names an address")

    def test_the_overview_lists_the_agent_calls_behind_it(self) -> None:
        """The overview reads the hospitals through the GMA."""
        overview = self.get_json("/api/v1/overview", "/api/v1/overview")
        overview_calls = overview.get("framework_calls")
        self.assertTrue(overview_calls, "the overview lists no agent call")
        self.assertIn(
            ("gma", None, "GET", "/api/v1/federation/sites/summaries"),
            {(call["service"], call["site_id"], call["method"], call["path"]) for call in overview_calls},
        )
        self.assert_routes_only(overview_calls)

    @writes_to_the_demo
    def test_a_probe_lists_the_lma_call_behind_it(self) -> None:
        """A probe is answered by the hospital's LMA."""
        overview = self.get_json("/api/v1/overview", "/api/v1/overview")
        site_id = overview["hospitals"][0]["site_id"]
        probe = self.json_of(
            self.client.post("/api/v1/governance:probe", json={"site_id": site_id}),
            "/api/v1/governance:probe",
        )
        probe_calls = probe.get("framework_calls")
        self.assertTrue(probe_calls, "the probe lists no agent call")
        self.assertIn(
            ("lma", site_id, "POST", "/api/v1/governance/restrictions:probe"),
            {(call["service"], call["site_id"], call["method"], call["path"]) for call in probe_calls},
        )
        self.assert_routes_only(probe_calls)

    def test_the_app_marks_its_own_responses(self) -> None:
        """The page retries a 503 without X-Dagents-App, and shows one with it at once."""
        for path in ("/api/v1/health", "/api/v1/hospitals/no-such-site"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual("healthcare-demo", response.headers.get("x-dagents-app"), response.status_code)

    def test_the_rate_limits_count_each_visitor_separately(self) -> None:
        """The client is the X-Forwarded-For entry the platform's edge wrote.

        This request sends no X-Forwarded-For, so every entry it arrives with
        was appended by a proxy in front of the app. Fewer entries than
        HEALTHCARE_DEMO_FORWARDED_FOR_HOPS means every visitor is counted as
        the proxy; more means the limits count a proxy's address.
        """
        status = self.get_json("/api/v1/framework/status", "/api/v1/framework/status")
        client = status["rate_limit_client"]
        self.assertEqual(
            "x-forwarded-for",
            client["source"],
            "the rate limits use the TCP peer, which is the platform's proxy, so all visitors share one count",
        )
        self.assertEqual(
            client["forwarded_for_hops"],
            client["forwarded_for_entries"],
            "set HEALTHCARE_DEMO_FORWARDED_FOR_HOPS to the number of entries a request arrives with",
        )

    def test_every_hospital_serves_records_and_a_worklist(self) -> None:
        overview = self.get_json("/api/v1/overview", "/api/v1/overview")
        self.assertGreaterEqual(len(overview["hospitals"]), 3)
        for hospital in overview["hospitals"]:
            with self.subTest(site=hospital["site_id"]):
                self.assertGreaterEqual(hospital["record_count"], overview["minimum_cohort"])
                self.assertEqual("postgres", hospital["store"]["kind"])
                worklist = self.get_json(
                    f"/api/v1/hospitals/{hospital['site_id']}/worklist?limit=5", "a site worklist"
                )["worklist"]
                self.assertTrue(worklist, "a site served no worklist rows")
                # The rows are records, not placeholders.
                self.assertIn("encounter_id", worklist[0])

    @writes_to_the_demo
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

    @writes_to_the_demo
    def test_the_cohort_floor_applies_where_the_granularity_makes_it_matter(self) -> None:
        """The minimum cohort, and the condition on it.

        `cohort_sensitive_granularity` in the governance compiler holds for
        column, table and model-update requests and not for cell or row ones:
        five rows are five records, while an aggregate over five subjects is a
        disclosure risk. So the floor refuses the coarse request and leaves the
        row-level one alone, and this asserts the contrast rather than half of
        it — getting that backwards is what made this test fail against a
        deployment that was behaving correctly.
        """
        def probe(granularity: str) -> dict:
            return self.json_of(
                self.client.post(
                    "/api/v1/governance:probe",
                    json={
                        "verified": True,
                        "granularity": granularity,
                        "cohort_size": 5,
                        "boundary": "before_read",
                    },
                ),
                f"/api/v1/governance:probe at {granularity} granularity",
            )

        coarse = probe("table")
        self.assertFalse(
            coarse["permitted"],
            "an aggregate over five subjects was permitted, below a floor of 20",
        )
        self.assertTrue(
            any("below minimum_cohort" in violation for violation in coarse["plan"]["violations"]),
            f"it was refused for some other reason: {coarse['plan']['violations']}",
        )

        self.assertTrue(
            probe("row")["permitted"],
            "a five-row request was refused; the floor is for cohort-sensitive granularities",
        )

    @writes_to_the_demo
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

    @writes_to_the_demo
    def test_a_pilot_reports_a_decision_that_matches_its_gates(self) -> None:
        """Aggregation yields a candidate, and the gates decide what happens to it.

        The test does not assume the decision. It checks that the decision
        follows from the gate results the same response reports: the candidate
        is released only when no blocking gate failed.
        """
        overview = self.get_json("/api/v1/overview", "/api/v1/overview")
        blocking = {gate["gate_id"] for gate in overview["release_gates"] if gate["blocking"]}
        pilot = self.json_of(
            self.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}),
            "/api/v1/pilot:run",
        )
        if pilot.get("release") is None:
            self.assertTrue(pilot.get("stop_reason"), "the pilot produced no release decision and no reason")
            print(f"\n  the pilot stopped: {pilot['stop_reason']}")
            return
        self.assertIsNotNone(pilot.get("candidate"), "the pilot produced no candidate")
        decision = pilot["release"]["gates"]
        results = decision["gate_results"]
        self.assertEqual(len(overview["release_gates"]), len(results))
        failed = {r["gate_id"] for r in results if r["outcome"] != "passed" and r["gate_id"] in blocking}
        self.assertEqual(failed, {failure.split(":")[0] for failure in decision["blocking_failures"]})
        self.assertEqual("reject" if failed else "release", decision["action"])
        print(f"\n  release action: {decision['action']} (blocking failures: {sorted(failed) or 'none'})")

    def test_every_audit_chain_verifies(self) -> None:
        for hospital in self.get_json("/api/v1/overview", "/api/v1/overview")["hospitals"]:
            with self.subTest(site=hospital["site_id"]):
                audit = self.get_json(
                    f"/api/v1/hospitals/{hospital['site_id']}/audit?limit=50", "a site audit trail"
                )
                self.assertTrue(audit["chain_intact"], "a site's audit chain did not verify")

    def test_the_charts_are_listed_and_one_draws(self) -> None:
        charts = self.get_json("/api/v1/charts", "/api/v1/charts")["charts"]
        self.assertTrue(charts, "no consortium charts are listed")
        available = [chart for chart in charts if chart["available"]]
        self.assertTrue(available, f"no chart is available: {[c['reason'] for c in charts]}")
        response = self.client.get(available[0]["svg_path"] + "?theme=light")
        self.assertEqual(200, response.status_code, response.text[:200])
        self.assertTrue(response.headers["content-type"].startswith("image/svg+xml"))
        self.assertIn("<svg", response.text)
        self.assertIn("International Stroke Trial", response.text)

    def test_the_hosted_api_never_offers_to_add_a_hospital(self) -> None:
        """Adding a hospital starts a process or a container, which no visitor may cause.

        A deployment from before the action answers 404 for both routes. A newer
        one answers the view with ``available: false``, because its agents run
        in process, and refuses the POST. The POST is sent once without an
        ``Origin``, as curl sends it, and once as a page on another host would
        send it.
        """
        view = self.client.get("/api/v1/deployments")
        self.assertIn(view.status_code, (200, 404), view.text[:300])
        if view.status_code == 200:
            body = self.json_of(view, "/api/v1/deployments")
            self.assertIs(False, body["available"], "the hosted API offers to add a hospital")
            self.assertTrue(body["reason"])
            self.assertEqual("off", body["applier"])
            self.assertEqual([], body["deployments"])
        for headers in ({}, {"Origin": "https://example.github.io"}):
            with self.subTest(origin=headers.get("Origin")):
                response = self.client.post("/api/v1/deployments", json={"site_id": "pl-290"}, headers=headers)
                self.assertIn(response.status_code, (403, 404, 405), response.text[:300])


if __name__ == "__main__":
    unittest.main()
