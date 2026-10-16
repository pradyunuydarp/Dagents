"""Every response the app sends carries X-Dagents-App, whatever its status.

The frontend retries a 502, 503 or 504 that lacks the header, because the
hosting platform sends those while the container starts. It shows a response
that has the header at once, so the app's own 503 ("the hospital records
cannot be reached") is not hidden behind a "connecting" banner for 90 seconds.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.api_support import ApiTestCase, main
from tests.support import HIGH_RISK_ENTRY, dagentsc_available

from app.api.app_header import APP_HEADER, APP_HEADER_VALUE
from app.api.limits import BUSY_MESSAGE
from app.services.agents import AgentError, AgentUnavailableError

#: A connection string, as a store's error can hold one. It must never reach a visitor or the log.
CONNECTION_STRING = "postgresql://admin:secret@db.internal.example:5432/healthcare"

ORIGIN = "https://example.github.io"


class AppHeaderTests(ApiTestCase):
    limits = {"entries": 0, "probes": 0, "pilots": 0, "approvals": 1}

    def setUp(self) -> None:
        main.rate_limits.reset()

    def assert_marked(self, response, status: int) -> None:
        self.assertEqual(status, response.status_code, response.text[:300])
        self.assertEqual(APP_HEADER_VALUE, response.headers.get(APP_HEADER))

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_a_success_is_marked(self) -> None:
        self.assert_marked(self.client.get("/api/v1/health"), 200)
        self.assert_marked(self.client.get("/api/v1/overview"), 200)

    def test_a_route_that_does_not_exist_is_marked(self) -> None:
        self.assert_marked(self.client.get("/api/v1/nothing-here"), 404)

    def test_the_apps_own_404_is_marked(self) -> None:
        self.assert_marked(self.client.get("/api/v1/hospitals/xx-000"), 404)

    def test_a_validation_error_is_marked(self) -> None:
        self.assert_marked(self.client.post("/api/v1/hospitals/uk-134/encounters", json={}), 422)

    def test_a_body_over_the_limit_is_marked(self) -> None:
        """The 413 comes from middleware inside the marker, not from a route."""
        body = b"{" + b" " * (main.settings.max_body_bytes + 10) + b"}"
        response = self.client.post(
            "/api/v1/hospitals/uk-134/encounters", content=body, headers={"Content-Type": "application/json"}
        )
        self.assert_marked(response, 413)

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_a_rate_limited_request_is_marked(self) -> None:
        self.assert_marked(self.client.post("/api/v1/models/none:approve"), 404)
        self.assert_marked(self.client.post("/api/v1/models/none:approve"), 429)

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_an_unchanged_chart_is_marked(self) -> None:
        first = self.client.get("/api/v1/hospitals/uk-134/charts")
        self.assert_marked(first, 200)
        path = first.json()["charts"][0]["svg_path"]
        drawn = self.client.get(path)
        self.assert_marked(drawn, 200)
        again = self.client.get(path, headers={"If-None-Match": drawn.headers["etag"]})
        self.assert_marked(again, 304)

    def test_a_browser_on_another_origin_may_read_it(self) -> None:
        response = self.client.post(
            "/api/v1/hospitals/uk-134/encounters", json=HIGH_RISK_ENTRY | {"age": 999}, headers={"Origin": ORIGIN}
        )
        self.assert_marked(response, 422)
        exposed = {name.strip().lower() for name in response.headers["access-control-expose-headers"].split(",")}
        self.assertIn(APP_HEADER.lower(), exposed)

    def test_a_preflight_is_marked(self) -> None:
        response = self.client.options(
            "/api/v1/hospitals/uk-134/encounters",
            headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
        )
        self.assert_marked(response, 200)


class ErrorResponseTests(ApiTestCase):
    """Every error is JSON, marked, and readable by a page on another origin.

    A response without CORS headers looks to the browser like a network
    error, so the page would retry for 90 seconds and then report an outage
    instead of showing the message.
    """

    def failing(self, error: BaseException) -> object:
        return mock.patch.object(self.demo.overview, "overview", side_effect=error)

    def get_overview(self) -> object:
        # The app sends its 500 and then raises the error again, as Starlette
        # does, so the server logs it. Here that would fail the test instead.
        client = TestClient(main.app, raise_server_exceptions=False)
        return client.get("/api/v1/overview", headers={"Origin": ORIGIN})

    def assert_json_error(self, response, status: int, detail: str) -> None:
        self.assertEqual(status, response.status_code, response.text[:300])
        self.assertEqual({"detail": detail}, response.json())
        self.assertEqual(APP_HEADER_VALUE, response.headers.get(APP_HEADER))
        self.assertEqual("*", response.headers.get("access-control-allow-origin"))
        self.assertNotIn("boom", response.text)

    def test_an_unexpected_error_is_a_json_500(self) -> None:
        with self.failing(RuntimeError("boom at 10.0.0.7")):
            self.assert_json_error(self.get_overview(), 500, main.UNEXPECTED_ERROR_MESSAGE)

    def assert_logged_without_address(self, output: list[str]) -> None:
        text = "\n".join(output)
        self.assertIn("[address]", text)
        self.assertNotIn(CONNECTION_STRING, text)
        self.assertNotIn("secret", text)

    def test_a_database_error_is_a_plain_503(self) -> None:
        import psycopg

        message = f"boom: could not connect to {CONNECTION_STRING}"
        for error in (psycopg.errors.SerializationFailure(message), psycopg.errors.CheckViolation(message)):
            with self.subTest(error=type(error).__name__), self.failing(error):
                with self.assertLogs("healthcare_demo", level="ERROR") as logged:
                    self.assert_json_error(self.get_overview(), 503, main.STORE_UNAVAILABLE_MESSAGE)
                self.assert_logged_without_address(logged.output)

    def test_a_database_without_this_versions_tables_says_so(self) -> None:
        import psycopg

        from app.services.site_store import StoreNotSetUpError

        message = f"boom at {CONNECTION_STRING}"
        for error in (
            StoreNotSetUpError(f"hospital uk-134: {message}"),
            psycopg.errors.UndefinedTable(message),
            psycopg.errors.InvalidSchemaName(message),
            psycopg.errors.InsufficientPrivilege(message),
            self.agent_error("store_not_set_up"),
        ):
            with self.subTest(error=type(error).__name__), self.failing(error):
                with self.assertLogs("healthcare_demo", level="ERROR") as logged:
                    self.assert_json_error(self.get_overview(), 503, main.DATABASE_NOT_SET_UP_MESSAGE)
                self.assert_logged_without_address(logged.output)

    @staticmethod
    def agent_error(code: str, status: int | None = 503) -> AgentError:
        return AgentError(
            service="gma", site_id=None, path="/api/v1/federation/sites/summaries", status=status, code=code,
            detail=f"boom: could not connect to {CONNECTION_STRING}",
        )

    def test_a_missing_planner_is_a_plain_503(self) -> None:
        """An agent that cannot run the planner answers 503 planner_unavailable; the page gets the app's words."""
        with self.failing(self.agent_error("planner_unavailable")):
            with self.assertLogs("healthcare_demo", level="ERROR") as logged:
                self.assert_json_error(self.get_overview(), 503, main.PLANNER_NEEDED_MESSAGE)
        self.assertNotIn(CONNECTION_STRING, "\n".join(logged.output))

    def test_each_agent_error_code_gets_the_apps_own_message(self) -> None:
        """The agent's detail is logged with its address removed, and never sent to the browser."""
        cases = {
            "store_unavailable": (503, main.STORE_UNAVAILABLE_MESSAGE),
            "store_not_set_up": (503, main.DATABASE_NOT_SET_UP_MESSAGE),
            "agent_unavailable": (503, main.AGENT_UNAVAILABLE_MESSAGE),
            "conflict": (500, main.UNEXPECTED_ERROR_MESSAGE),
            # An LMA's 502: the GMA behind it failed. A temporary outage, as a store error was.
            "bad_gateway": (503, main.AGENT_FAILED_MESSAGE),
        }
        for code, (status, message) in cases.items():
            with self.subTest(code=code), self.failing(self.agent_error(code, 409 if code == "conflict" else 503)):
                with self.assertLogs("healthcare_demo", level="WARNING") as logged:
                    response = self.get_overview()
                self.assert_json_error(response, status, message)
                self.assertNotIn("db.internal.example", response.text)
                output = "\n".join(logged.output)
                self.assertNotIn(CONNECTION_STRING, output)
                self.assertNotIn("secret", output)
                self.assertIn("[address]", output)

    def test_an_agent_that_does_not_answer_is_a_plain_503(self) -> None:
        error = AgentUnavailableError(
            service="lma", site_id="uk-134", path="/api/v1/site/summary", status=None, code="agent_unavailable",
            detail="the call timed out",
        )
        with self.failing(error), self.assertLogs("healthcare_demo", level="WARNING"):
            self.assert_json_error(self.get_overview(), 503, main.AGENT_UNAVAILABLE_MESSAGE)

    def test_the_busy_answer_is_marked_and_readable(self) -> None:
        """Beyond the in-flight cap a request waits, then gets the app's own 503 with CORS and the marker."""
        cap = self.in_flight_cap()
        with mock.patch.object(cap, "max_in_flight", 0), mock.patch.object(cap, "wait_seconds", 0.0):
            response = self.client.get("/api/v1/conditions", headers={"Origin": ORIGIN})
            health = self.client.get("/api/v1/health", headers={"Origin": ORIGIN})
        self.assert_json_error(response, 503, BUSY_MESSAGE)
        self.assertEqual("60", response.headers.get("retry-after"))
        self.assertEqual(200, health.status_code, "the health check is never capped")

    def in_flight_cap(self):
        """The app's in-flight cap, as the middleware stack built it."""
        from app.api.limits import InFlightCap

        self.client.get("/api/v1/health")  # builds the middleware stack
        layer = main.app.middleware_stack
        while layer is not None and not isinstance(layer, InFlightCap):
            layer = getattr(layer, "app", None)
        self.assertIsInstance(layer, InFlightCap)
        return layer


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GmaFailureBehindAnLmaTests(ApiTestCase):
    """The GMA's store fails while a hospital's LMA asks the GMA for the released model.

    The LMA answers the app 502 ``bad_gateway``: the GMA behind it failed. The
    app answers 503 with its own message, the CORS headers and its marker. It
    is a temporary outage, as the same store failure was before the agents,
    and the page shows the message at once.
    """

    PATHS = ("/api/v1/hospitals/uk-134", "/api/v1/hospitals/uk-134/worklist", "/api/v1/hospitals/uk-134/thresholds")

    def test_a_hospital_page_is_a_plain_503(self) -> None:
        from agents.common.domain.errors import StoreUnavailableError
        from agents.gma.application.model_registry import ModelRegistry

        self.demo.ready()  # the app's model versions are registered before the store fails
        error = StoreUnavailableError(f"could not connect to {CONNECTION_STRING}")
        client = TestClient(main.app, raise_server_exceptions=False)
        with mock.patch.object(ModelRegistry, "released", side_effect=error):
            with self.assertLogs(level="WARNING") as logged:
                responses = {path: client.get(path, headers={"Origin": ORIGIN}) for path in self.PATHS}
        for path, response in responses.items():
            with self.subTest(path=path):
                self.assertEqual(503, response.status_code, response.text[:300])
                self.assertEqual({"detail": main.AGENT_FAILED_MESSAGE}, response.json())
                self.assertEqual(APP_HEADER_VALUE, response.headers.get(APP_HEADER))
                self.assertEqual("*", response.headers.get("access-control-allow-origin"))
                self.assertNotIn("db.internal.example", response.text)
        output = "\n".join(logged.output)
        self.assertNotIn(CONNECTION_STRING, output)
        self.assertNotIn("secret", output)


class StatusLogTests(ApiTestCase):
    """The status report logs why the GMA could not report, with the address removed."""

    def test_a_gma_error_detail_is_logged_without_its_address(self) -> None:
        from agents.common.domain.errors import StoreUnavailableError
        from agents.gma.application.site_directory import SiteDirectory

        error = StoreUnavailableError(f"could not connect to {CONNECTION_STRING}")
        with mock.patch.object(SiteDirectory, "summaries", side_effect=error), self.assertLogs(level="WARNING") as logged:
            response = self.client.get("/api/v1/framework/status", headers={"Origin": ORIGIN})
        status = self.ok(response)
        self.assertIs(False, status["ready"])
        self.assertNotIn("db.internal.example", response.text)
        output = "\n".join(logged.output)
        self.assertIn("[address]", output)
        self.assertNotIn(CONNECTION_STRING, output)
        self.assertNotIn("secret", output)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PilotAgentDetailTests(ApiTestCase):
    """An LMA's error detail never reaches the pilot report, the stored report or the log's addresses.

    A hospital's store that cannot connect raises an error whose text names the
    database host and hosting vendors (the app's connection hint does). The LMA
    answers the GMA's job call with 503 and that text; the GMA's round keeps the
    status and code only, and the report the page shows holds no part of it.
    """

    SECRET = "postgresql://admin:hunter2@db.abcdefgh.example-cloud.test:5432/postgres"
    MESSAGE = f"could not connect to {SECRET} (ExampleCloud direct host; ExampleHost has IPv4 only)"
    LEAKS = ("hunter2", "example-cloud", "ExampleCloud", "ExampleHost", "postgresql://")

    def test_a_store_error_at_offer_or_execute_stays_out_of_the_report(self) -> None:
        """The GMA's one jobs:run call reaches both steps; a failure at either fails the call."""
        from agents.common.domain.errors import StoreUnavailableError
        from agents.lma.application import federation_jobs

        message = self.MESSAGE
        for step in ("offer", "execute"):
            with self.subTest(step=step):
                original = getattr(federation_jobs.FederationJobService, step)

                def failing(service, job, _original=original):  # noqa: ANN001, ANN202 - same signature as the method
                    if service.site_id == "uk-134":
                        raise StoreUnavailableError(message)
                    return _original(service, job)

                with mock.patch.object(federation_jobs.FederationJobService, step, failing):
                    with self.assertLogs(level="WARNING") as logged:
                        response = self.client.post("/api/v1/pilot:run", json={})
                report = self.ok(response)
                analytics = report["rounds"]["analytics"]
                result = next(item for item in analytics["results"] if item["site_id"] == "uk-134")
                self.assertEqual("failed", result["participation"])
                self.assertEqual(
                    "the job run failed: the LMA of site uk-134 answered 503 (store_unavailable)", result["refusal_reason"]
                )
                [row] = [row for row in analytics["dispatch"] if row["site_id"] == "uk-134"]
                self.assertEqual(("run", 503, "failed"), (row["step"], row["status_code"], row["outcome"]))
                stored = self.demo.coordinator_store.latest_pilot_report()
                for text in (response.text, json.dumps(stored)):
                    for leak in self.LEAKS:
                        self.assertNotIn(leak, text)
                output = "\n".join(logged.output)
                self.assertIn("[address]", output)
                self.assertNotIn("hunter2", output)
                self.assertNotIn("postgresql://", output)


if __name__ == "__main__":
    unittest.main()
