"""Every response the app sends carries X-Dagents-App, whatever its status.

The frontend retries a 502, 503 or 504 that lacks the header, because the
hosting platform sends those while the container starts. It shows a response
that has the header at once, so the app's own 503 ("the hospital records
cannot be reached") is not hidden behind a "connecting" banner for 90 seconds.
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.api_support import ApiTestCase, main
from tests.support import HIGH_RISK_ENTRY

from app.api.app_header import APP_HEADER, APP_HEADER_VALUE

ORIGIN = "https://example.github.io"


class AppHeaderTests(ApiTestCase):
    limits = {"entries": 0, "probes": 0, "pilots": 0, "approvals": 1}

    def setUp(self) -> None:
        main.rate_limits.reset()

    def assert_marked(self, response, status: int) -> None:
        self.assertEqual(status, response.status_code, response.text[:300])
        self.assertEqual(APP_HEADER_VALUE, response.headers.get(APP_HEADER))

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

    def test_a_rate_limited_request_is_marked(self) -> None:
        self.assert_marked(self.client.post("/api/v1/models/none:approve"), 404)
        self.assert_marked(self.client.post("/api/v1/models/none:approve"), 429)

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
        return mock.patch.object(self.consortium, "overview", side_effect=error)

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

    def test_a_database_error_is_a_plain_503(self) -> None:
        import psycopg

        for error in (psycopg.errors.SerializationFailure("boom"), psycopg.errors.CheckViolation("boom")):
            with self.subTest(error=type(error).__name__), self.failing(error):
                with self.assertLogs("healthcare_demo", level="ERROR"):
                    self.assert_json_error(self.get_overview(), 503, main.STORE_UNAVAILABLE_MESSAGE)

    def test_a_database_without_this_versions_tables_says_so(self) -> None:
        import psycopg

        from app.services.site_store import StoreNotSetUpError

        for error in (
            StoreNotSetUpError("hospital uk-134: boom"),
            psycopg.errors.UndefinedTable("boom"),
            psycopg.errors.InvalidSchemaName("boom"),
            psycopg.errors.InsufficientPrivilege("boom"),
        ):
            with self.subTest(error=type(error).__name__), self.failing(error):
                with self.assertLogs("healthcare_demo", level="ERROR"):
                    self.assert_json_error(self.get_overview(), 503, main.DATABASE_NOT_SET_UP_MESSAGE)

    def test_a_missing_planner_is_a_plain_503(self) -> None:
        from agents.common.infrastructure.dagents_runner import PlannerUnavailableError

        with self.failing(PlannerUnavailableError("dagentsc could not be started: boom")):
            self.assert_json_error(self.get_overview(), 503, main.PLANNER_UNAVAILABLE_MESSAGE)


if __name__ == "__main__":
    unittest.main()
