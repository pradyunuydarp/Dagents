"""In a split run, the app sends each hospital's routes to that hospital's LMA.

The app here is a small FastAPI app with the demo's middleware and a few
routes of its own; the LMAs are small apps that answer with what reached them,
called in process through ``httpx.ASGITransport``. So these tests check the
routing, the headers, the limits and the errors of
:func:`app.api.forward.install_forwarding`, not the hospitals.
``test_split_processes.py`` runs the real processes.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
import unittest

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.api.app_header import APP_HEADER, APP_HEADER_VALUE, AppHeader
from app.api.errors import UnhandledErrors
from app.api.forward import install_forwarding, is_clean, is_entry
from app.api.limits import BodySizeLimit, RateLimiter

LMAS = {
    "uk-134": "http://lma-uk-134.internal",
    "it-002": "http://lma-it-002.internal",
    "ch-245": "http://lma-ch-245.internal",
}
ORIGIN = "https://example.github.io"
NO_LIMITS = {"entries": 0, "probes": 0, "pilots": 0, "approvals": 0}


def settings(timeout: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(lma_url_map=lambda: dict(LMAS), upstream_timeout_seconds=timeout)


def fake_lma(name: str, seen: list[dict]) -> FastAPI:
    """An LMA that answers every request with what it received."""
    lma = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @lma.get("/api/v1/hospitals/{site_id}/charts/{chart}.svg")
    def svg(request: Request) -> Response:
        headers = {
            "ETag": '"abc"',
            "Cache-Control": "no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
            "X-Dagents-App": "lma",
            "Access-Control-Allow-Origin": "https://elsewhere.example",
            "X-Internal": "never passed",
        }
        if request.headers.get("if-none-match") == '"abc"':
            return Response(status_code=304, headers=headers)
        return Response(content="<svg/>", media_type="image/svg+xml", headers=headers)

    @lma.post("/api/v1/governance:probe")
    async def probe(request: Request) -> Response:
        seen.append({"upstream": name, "path": request.url.path})
        if name == "ch-245":
            return Response(
                content=json.dumps({"detail": "hourly cap"}), status_code=429, media_type="application/json",
                headers={"Retry-After": "120"},
            )
        return Response(content=json.dumps({"upstream": name, "body": (await request.body()).decode()}),
                        media_type="application/json")

    @lma.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def anything(request: Request) -> dict:
        record = {
            "upstream": name,
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query,
            "body": (await request.body()).decode(),
            "headers": dict(request.headers.items()),
        }
        seen.append(record)
        return record

    return lma


def demo_app(mounts: dict[str, httpx.AsyncBaseTransport], limits: dict[str, int]) -> tuple[FastAPI, RateLimiter]:
    """An app with the demo's middleware, its own routes, and forwarding installed."""
    app = FastAPI()
    rate_limits = RateLimiter(limits)

    @app.get("/api/v1/hospitals")
    def hospitals() -> list[dict]:
        return [{"site_id": site_id, "served_by": "app"} for site_id in LMAS]

    @app.get("/api/v1/hospitals/{site_id}")
    def hospital(site_id: str) -> dict:
        """The app's own route, which forwarding must take precedence over."""
        return {"served_by": "app"}

    @app.post("/api/v1/governance:probe")
    def probe() -> dict:
        return {"served_by": "app"}

    @app.get("/api/v1/overview")
    def overview() -> dict:
        return {"served_by": "app"}

    app.add_middleware(BodySizeLimit, max_bytes=64 * 1024)
    app.add_middleware(UnhandledErrors)
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
        expose_headers=["Retry-After", "ETag", APP_HEADER],
    )
    app.add_middleware(AppHeader)
    install_forwarding(app, settings(), rate_limits, mounts=mounts)
    return app, rate_limits


class Unreachable(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)


class TooSlow(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)


async def raw_get(app, path: str) -> tuple[int, bytes]:
    """Send one GET with exactly this path, as a server passes it on.

    An HTTP client removes ``.`` and ``..`` segments before it sends, so the
    tests of those paths call the ASGI app directly.
    """
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"host", b"testserver")], "client": ("127.0.0.1", 5000), "server": ("testserver", 80),
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


class ForwardingCase(unittest.TestCase):
    limits = NO_LIMITS

    def setUp(self) -> None:
        self.seen: list[dict] = []
        mounts: dict[str, httpx.AsyncBaseTransport] = {
            url: httpx.ASGITransport(app=fake_lma(site_id, self.seen)) for site_id, url in LMAS.items()
        }
        self.app, self.rate_limits = demo_app(mounts, self.limits)
        self.client = TestClient(self.app)

    def reached(self, response: httpx.Response) -> str:
        self.assertEqual(200, response.status_code, response.text[:300])
        body = response.json()
        return body.get("upstream") or body.get("served_by")


class RoutingTests(ForwardingCase):
    def test_hospital_paths_go_to_that_hospitals_lma(self) -> None:
        for site_id in LMAS:
            for suffix in ("", "/worklist?limit=3", "/thresholds", "/audit", "/charts", "/entry-form", "/encounters"):
                with self.subTest(site=site_id, suffix=suffix):
                    response = self.client.get(f"/api/v1/hospitals/{site_id}{suffix}")
                    self.assertEqual(site_id, self.reached(response))
                    self.assertEqual(f"/api/v1/hospitals/{site_id}{suffix.split('?')[0]}", response.json()["path"])
        answer = self.client.post(
            "/api/v1/hospitals/it-002/encounters/it-002-e1/outcome", json={"poor_outcome_6m": True}
        )
        self.assertEqual("it-002", self.reached(answer))
        self.assertEqual("POST", answer.json()["method"])
        self.assertEqual({"poor_outcome_6m": True}, json.loads(answer.json()["body"]))

    def test_the_query_string_is_passed_unchanged(self) -> None:
        response = self.client.get("/api/v1/hospitals/uk-134/worklist?limit=3&x=a%20b")
        self.assertEqual("limit=3&x=a%20b", response.json()["query"])

    def test_the_apps_own_routes_stay_in_the_app(self) -> None:
        listed = self.client.get("/api/v1/hospitals")
        self.assertEqual({"app"}, {entry["served_by"] for entry in listed.json()})
        self.assertEqual("app", self.reached(self.client.get("/api/v1/overview")))
        self.assertEqual([], self.seen)

    def test_the_openapi_document_does_not_change(self) -> None:
        paths = self.client.get("/openapi.json").json()["paths"]
        self.assertEqual(
            {"/api/v1/hospitals", "/api/v1/hospitals/{site_id}", "/api/v1/governance:probe", "/api/v1/overview"},
            set(paths),
        )

    def test_an_unknown_hospital_is_a_404_with_the_apps_message(self) -> None:
        for path in ("/api/v1/hospitals/xx-999", "/api/v1/hospitals/xx-999/worklist"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(404, response.status_code)
                self.assertEqual({"detail": "There is no hospital with the id 'xx-999'."}, response.json())
        self.assertEqual([], self.seen)

    def test_a_path_that_leaves_the_hospital_is_never_forwarded(self) -> None:
        for path in (
            "/api/v1/hospitals/uk-134/../../site/report",
            "/api/v1/hospitals/uk-134/../../federation/jobs:execute",
            "/api/v1/hospitals/uk-134/./worklist",
            "/api/v1/hospitals/uk-134//worklist",
        ):
            with self.subTest(path=path):
                status, body = asyncio.run(raw_get(self.app, path))
                self.assertEqual(404, status, body)
        # Encoded slashes are decoded before the route is matched.
        response = self.client.get("/api/v1/hospitals/uk-134%2F..%2F..%2Fsite/report")
        self.assertEqual(404, response.status_code)
        self.assertEqual([], self.seen)

    def test_a_probe_goes_to_the_hospital_its_body_names(self) -> None:
        for site_id in ("uk-134", "it-002"):
            with self.subTest(site=site_id):
                response = self.client.post("/api/v1/governance:probe", json={"site_id": site_id, "verified": True})
                self.assertEqual(site_id, self.reached(response))
                self.assertEqual({"site_id": site_id, "verified": True}, json.loads(response.json()["body"]))

    def test_a_probe_without_a_site_goes_to_the_first_hospital(self) -> None:
        for body in ({}, {"site_id": None}, {"site_id": ""}, {"site_id": 7}):
            with self.subTest(body=body):
                self.assertEqual("uk-134", self.reached(self.client.post("/api/v1/governance:probe", json=body)))
        not_json = self.client.post(
            "/api/v1/governance:probe", content=b"{nope", headers={"content-type": "application/json"}
        )
        self.assertEqual("uk-134", self.reached(not_json))
        self.assertEqual("{nope", not_json.json()["body"], "the LMA answers the invalid body itself")

    def test_a_probe_for_an_unknown_hospital_is_a_404(self) -> None:
        response = self.client.post("/api/v1/governance:probe", json={"site_id": "xx-999"})
        self.assertEqual(404, response.status_code)
        self.assertEqual("There is no hospital with the id 'xx-999'.", response.json()["detail"])

    def test_a_trace_goes_to_the_hospital_its_query_names(self) -> None:
        response = self.client.get("/api/v1/framework/trace?site_id=ch-245&limit=5")
        self.assertEqual("ch-245", self.reached(response))
        self.assertEqual("site_id=ch-245&limit=5", response.json()["query"])
        self.assertEqual("uk-134", self.reached(self.client.get("/api/v1/framework/trace")))
        self.assertEqual("uk-134", self.reached(self.client.get("/api/v1/framework/trace?site_id=")))
        self.assertEqual(404, self.client.get("/api/v1/framework/trace?site_id=xx-999").status_code)

    def test_the_entry_routes_are_recognized(self) -> None:
        self.assertTrue(is_entry("encounters"))
        self.assertTrue(is_entry("encounters/uk-134-e1/outcome"))
        for rest in ("", "worklist", "encounters/uk-134-e1", "encounters/a/b/outcome", "charts/encounters"):
            with self.subTest(rest=rest):
                self.assertFalse(is_entry(rest))

    def test_clean_paths(self) -> None:
        self.assertTrue(is_clean("/api/v1/hospitals/uk-134/charts/age.svg"))
        self.assertTrue(is_clean("/api/v1/hospitals/uk-134/"))
        for path in ("/a/../b", "/a/./b", "//a", "/a//b", "/a\\b", "a/b"):
            with self.subTest(path=path):
                self.assertFalse(is_clean(path))


class HeaderTests(ForwardingCase):
    def test_request_headers_that_matter_are_passed(self) -> None:
        response = self.client.post(
            "/api/v1/hospitals/uk-134/encounters",
            json={"age": 70},
            headers={
                "Idempotency-Key": "key-1",
                "Accept": "application/json",
                "If-None-Match": '"etag"',
                "Cookie": "session=secret",
                "Authorization": "Bearer secret",
                "X-Forwarded-For": "203.0.113.9",
                "Origin": ORIGIN,
            },
        )
        headers = response.json()["headers"]
        self.assertEqual("key-1", headers["idempotency-key"])
        self.assertEqual("application/json", headers["content-type"])
        self.assertEqual("application/json", headers["accept"])
        self.assertEqual('"etag"', headers["if-none-match"])
        for dropped in ("cookie", "authorization", "x-forwarded-for", "origin"):
            self.assertNotIn(dropped, headers)

    def test_a_chart_comes_back_with_its_headers_and_a_304(self) -> None:
        response = self.client.get("/api/v1/hospitals/uk-134/charts/age.svg?theme=dark")
        self.assertEqual(200, response.status_code)
        self.assertEqual("<svg/>", response.text)
        self.assertEqual("image/svg+xml", response.headers["content-type"])
        self.assertEqual('"abc"', response.headers["etag"])
        self.assertEqual("no-cache", response.headers["cache-control"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])
        self.assertEqual("default-src 'none'", response.headers["content-security-policy"])
        self.assertNotIn("x-internal", response.headers)
        self.assertEqual(APP_HEADER_VALUE, response.headers[APP_HEADER])

        again = self.client.get("/api/v1/hospitals/uk-134/charts/age.svg", headers={"If-None-Match": '"abc"'})
        self.assertEqual(304, again.status_code)
        self.assertEqual(b"", again.content)
        self.assertEqual('"abc"', again.headers["etag"])

    def test_an_lma_429_keeps_its_retry_after(self) -> None:
        response = self.client.post("/api/v1/governance:probe", json={"site_id": "ch-245"})
        self.assertEqual(429, response.status_code)
        self.assertEqual("120", response.headers["retry-after"])
        self.assertEqual({"detail": "hourly cap"}, response.json())

    def test_every_forwarded_answer_carries_the_apps_cors_and_app_header(self) -> None:
        responses = {
            "forwarded": self.client.get("/api/v1/hospitals/uk-134", headers={"Origin": ORIGIN}),
            "chart": self.client.get("/api/v1/hospitals/uk-134/charts/age.svg", headers={"Origin": ORIGIN}),
            "unknown hospital": self.client.get("/api/v1/hospitals/xx-999", headers={"Origin": ORIGIN}),
            "preflight": self.client.options(
                "/api/v1/hospitals/uk-134/encounters",
                headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
            ),
        }
        for name, response in responses.items():
            with self.subTest(response=name):
                self.assertEqual(APP_HEADER_VALUE, response.headers.get(APP_HEADER))
                self.assertEqual("*", response.headers.get("access-control-allow-origin"))

    def test_a_body_over_the_limit_is_refused_before_it_is_forwarded(self) -> None:
        response = self.client.post("/api/v1/hospitals/uk-134/encounters", content=b"x" * (1024 * 1024))
        self.assertEqual(413, response.status_code)
        self.assertEqual(APP_HEADER_VALUE, response.headers[APP_HEADER])
        self.assertEqual([], self.seen)


class LmaDownTests(unittest.TestCase):
    def test_an_unreachable_hospital_is_a_503_that_names_it(self) -> None:
        app, _ = demo_app({url: Unreachable() for url in LMAS.values()}, NO_LIMITS)
        client = TestClient(app)
        cases = {
            "/api/v1/hospitals/it-002": "Hospital 2 cannot be reached right now. Try again in a minute.",
            "/api/v1/framework/trace": "Hospital 134 cannot be reached right now. Try again in a minute.",
        }
        for path, message in cases.items():
            with self.subTest(path=path):
                response = client.get(path, headers={"Origin": ORIGIN})
                self.assertEqual(503, response.status_code)
                self.assertEqual({"detail": message}, response.json())
                self.assertEqual(APP_HEADER_VALUE, response.headers[APP_HEADER])
                self.assertIn("access-control-allow-origin", response.headers)
                self.assertNotIn("internal", response.text, "the message names no address")
        self.assertEqual(200, client.get("/api/v1/overview").status_code, "the app's own routes still answer")

    def test_a_hospital_that_times_out_is_a_503(self) -> None:
        app, _ = demo_app({url: TooSlow() for url in LMAS.values()}, NO_LIMITS)
        response = TestClient(app).post("/api/v1/governance:probe", json={"site_id": "ch-245"})
        self.assertEqual(503, response.status_code)
        self.assertEqual("Hospital 245 did not answer in time. Try again in a minute.", response.json()["detail"])

    def test_one_hospital_down_does_not_stop_the_others(self) -> None:
        seen: list[dict] = []
        mounts: dict[str, httpx.AsyncBaseTransport] = {
            LMAS["uk-134"]: httpx.ASGITransport(app=fake_lma("uk-134", seen)),
            LMAS["it-002"]: Unreachable(),
            LMAS["ch-245"]: httpx.ASGITransport(app=fake_lma("ch-245", seen)),
        }
        app, _ = demo_app(mounts, NO_LIMITS)
        client = TestClient(app)
        self.assertEqual(503, client.get("/api/v1/hospitals/it-002").status_code)
        self.assertEqual(200, client.get("/api/v1/hospitals/ch-245").status_code)


class RateLimitTests(ForwardingCase):
    limits = {"entries": 1, "probes": 1, "pilots": 0, "approvals": 0}

    def test_entries_and_probes_spend_their_own_limit_per_client(self) -> None:
        requests = {
            "entries": [
                ("/api/v1/hospitals/uk-134/encounters", {}),
                ("/api/v1/hospitals/it-002/encounters/it-002-e1/outcome", {"poor_outcome_6m": False}),
            ],
            "probes": [("/api/v1/governance:probe", {"site_id": "uk-134"})] * 2,
        }
        for group, ((first, first_body), (second, second_body)) in requests.items():
            with self.subTest(group=group):
                self.assertEqual(200, self.client.post(first, json=first_body).status_code)
                refused = self.client.post(second, json=second_body, headers={"Origin": ORIGIN})
                self.assertEqual(429, refused.status_code)
                self.assertTrue(refused.json()["detail"].startswith("Too many requests."))
                self.assertGreaterEqual(int(refused.headers["retry-after"]), 1)
                self.assertEqual(APP_HEADER_VALUE, refused.headers[APP_HEADER])
                self.assertIn("access-control-allow-origin", refused.headers)
        self.assertEqual(2, len(self.seen), "a refused request is not forwarded")

    def test_a_request_to_an_unknown_hospital_still_spends_the_limit(self) -> None:
        """As in the combined app, whose route dependency runs before the hospital is looked up."""
        self.assertEqual(404, self.client.post("/api/v1/hospitals/xx-999/encounters", json={}).status_code)
        self.assertEqual(429, self.client.post("/api/v1/hospitals/uk-134/encounters", json={}).status_code)

    def test_reads_are_not_limited(self) -> None:
        for _ in range(5):
            self.assertEqual(200, self.client.get("/api/v1/hospitals/uk-134").status_code)
            self.assertEqual(200, self.client.get("/api/v1/hospitals/uk-134/encounters").status_code)


if __name__ == "__main__":
    unittest.main()
