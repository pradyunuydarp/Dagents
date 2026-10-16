"""The public demo's limits: the token bucket, the 429, the client address and the in-flight cap."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import unittest
from unittest import mock

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from tests.api_support import ApiTestCase, main
from tests.support import HIGH_RISK_ENTRY, dagentsc_available

from app.api.limits import BUSY_MESSAGE, BodySizeLimit, HourlyCap, InFlightCap, RateLimiter, client_key
from app.core.config import Settings


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TokenBucketTests(unittest.TestCase):
    def test_a_client_may_burst_to_the_limit_then_waits(self) -> None:
        clock = FakeClock()
        limiter = RateLimiter({"entries": 3}, clock=clock)
        self.assertEqual([0.0, 0.0, 0.0], [limiter.take("entries", "a") for _ in range(3)])
        wait = limiter.take("entries", "a")
        self.assertAlmostEqual(20.0, wait)  # one request per 20 seconds at 3 per minute
        clock.now += 20.0
        self.assertEqual(0.0, limiter.take("entries", "a"))

    def test_clients_and_groups_are_counted_apart(self) -> None:
        limiter = RateLimiter({"entries": 1, "probes": 1}, clock=FakeClock())
        self.assertEqual(0.0, limiter.take("entries", "a"))
        self.assertEqual(0.0, limiter.take("entries", "b"))
        self.assertEqual(0.0, limiter.take("probes", "a"))
        self.assertGreater(limiter.take("entries", "a"), 0.0)

    def test_zero_or_a_missing_group_is_no_limit(self) -> None:
        limiter = RateLimiter({"entries": 0}, clock=FakeClock())
        self.assertTrue(all(limiter.take("entries", "a") == 0.0 for _ in range(1000)))
        self.assertEqual(0.0, limiter.take("pilots", "a"))

    def test_configure_rejects_a_bad_limit_and_resets_counts(self) -> None:
        limiter = RateLimiter({"entries": 1}, clock=FakeClock())
        limiter.take("entries", "a")
        with self.assertRaises(ValueError):
            limiter.configure({"entries": -1})
        limiter.configure({"entries": 1})
        self.assertEqual(0.0, limiter.take("entries", "a"))

    def test_buckets_do_not_grow_without_bound(self) -> None:
        from app.api import limits

        clock = FakeClock()
        limiter = RateLimiter({"entries": 2}, clock=clock)
        original = limits.MAX_BUCKETS
        limits.MAX_BUCKETS = 50
        try:
            for number in range(500):
                limiter.take("entries", f"client-{number}")
            self.assertLessEqual(len(limiter._buckets), 50)
        finally:
            limits.MAX_BUCKETS = original


class ClientAddressTests(unittest.TestCase):
    def key_for(self, hops: int, headers: dict[str, str] | list[tuple[str, str]]) -> str:
        app = FastAPI()

        @app.get("/")
        def read(request: Request) -> dict[str, str]:
            return {"key": client_key(request, hops)}

        return TestClient(app).get("/", headers=headers).json()["key"]

    def test_the_entry_the_last_proxy_wrote_is_used(self) -> None:
        headers = {"X-Forwarded-For": "203.0.113.7, 198.51.100.4"}
        self.assertEqual("198.51.100.4", self.key_for(1, headers))

    def test_an_address_the_client_wrote_is_never_used(self) -> None:
        """A client that sends its own header can only add entries on the left."""
        spoofed = {"X-Forwarded-For": "192.0.2.44, 203.0.113.7"}  # the proxy appended 203.0.113.7
        self.assertEqual("203.0.113.7", self.key_for(1, spoofed))
        for fake in ("192.0.2.1", "192.0.2.2", "192.0.2.3"):
            with self.subTest(fake=fake):
                self.assertEqual("203.0.113.7", self.key_for(1, {"X-Forwarded-For": f"{fake}, 203.0.113.7"}))

    def test_behind_three_proxies_the_client_is_third_from_the_right(self) -> None:
        """Render's edge appends the client, a Cloudflare address and an internal one."""
        unchanged = {"X-Forwarded-For": "81.107.0.110, 172.71.0.9, 10.0.0.5"}
        spoofed = {"X-Forwarded-For": "192.0.2.44, 81.107.0.110, 172.71.0.9, 10.0.0.5"}
        self.assertEqual("81.107.0.110", self.key_for(3, unchanged))
        self.assertEqual("81.107.0.110", self.key_for(3, spoofed))

    def test_a_header_shorter_than_the_proxy_chain_is_ignored(self) -> None:
        """It did not pass through every proxy, so any entry in it may be the client's."""
        self.assertEqual("testclient", self.key_for(3, {"X-Forwarded-For": "192.0.2.44, 10.0.0.5"}))
        self.assertEqual("testclient", self.key_for(1, {}))

    def test_every_header_line_counts(self) -> None:
        """A proxy that adds a second header line instead of appending is still read from the right."""
        lines = [("X-Forwarded-For", "192.0.2.44"), ("X-Forwarded-For", "203.0.113.7")]
        self.assertEqual("203.0.113.7", self.key_for(1, lines))

    def test_without_trusted_proxies_the_peer_is_used(self) -> None:
        self.assertEqual("testclient", self.key_for(0, {"X-Forwarded-For": "203.0.113.7, 198.51.100.4"}))

    def test_a_negative_hop_count_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            RateLimiter({"entries": 1}, forwarded_for_hops=-1)
        with self.assertRaises(ValueError):
            RateLimiter({"entries": 1}, forwarded_for_hops=True)


class ClientAddressSettingsTests(unittest.TestCase):
    """The defaults: off locally; on Render, three proxies."""

    def settings_with(self, environ: dict[str, str]) -> Settings:
        names = ("RENDER", "HEALTHCARE_DEMO_TRUST_FORWARDED_FOR", "HEALTHCARE_DEMO_FORWARDED_FOR_HOPS")
        with mock.patch.dict(os.environ):
            for name in names:
                os.environ.pop(name, None)
            os.environ.update(environ)
            return Settings()

    def test_a_local_run_uses_the_peer(self) -> None:
        self.assertEqual(0, self.settings_with({}).client_proxy_hops())

    def test_on_render_the_client_is_three_entries_from_the_right(self) -> None:
        self.assertEqual(3, self.settings_with({"RENDER": "true"}).client_proxy_hops())

    def test_the_hop_count_and_the_switch_can_be_set(self) -> None:
        self.assertEqual(
            1, self.settings_with({"HEALTHCARE_DEMO_TRUST_FORWARDED_FOR": "true"}).client_proxy_hops()
        )
        self.assertEqual(
            2,
            self.settings_with(
                {"RENDER": "true", "HEALTHCARE_DEMO_FORWARDED_FOR_HOPS": "2"}
            ).client_proxy_hops(),
        )
        self.assertEqual(
            0,
            self.settings_with({"RENDER": "true", "HEALTHCARE_DEMO_TRUST_FORWARDED_FOR": "false"}).client_proxy_hops(),
        )


class ClientAddressStatusTests(ApiTestCase):
    def test_the_status_reports_how_the_client_is_identified(self) -> None:
        original = main.rate_limits.forwarded_for_hops
        self.addCleanup(main.rate_limits.configure, self.limits, forwarded_for_hops=original)
        main.rate_limits.configure(self.limits, forwarded_for_hops=3)
        status = self.ok(
            self.client.get("/api/v1/framework/status", headers={"X-Forwarded-For": "81.107.0.110, 172.71.0.9"})
        )
        self.assertEqual(
            {"source": "x-forwarded-for", "forwarded_for_hops": 3, "forwarded_for_entries": 2},
            status["rate_limit_client"],
        )
        main.rate_limits.configure(self.limits, forwarded_for_hops=0)
        status = self.ok(self.client.get("/api/v1/framework/status"))
        self.assertEqual(
            {"source": "peer", "forwarded_for_hops": 0, "forwarded_for_entries": 0}, status["rate_limit_client"]
        )


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RateLimitedApiTests(ApiTestCase):
    """Entries are answered with an assessment by the model in use, which the GMA checks with the planner."""

    limits = {"entries": 2, "probes": 0, "pilots": 1, "approvals": 1}

    def test_a_client_cannot_escape_the_limit_by_writing_its_own_forwarded_for(self) -> None:
        original = main.rate_limits.forwarded_for_hops
        self.addCleanup(main.rate_limits.configure, self.limits, forwarded_for_hops=original)
        main.rate_limits.configure(self.limits, forwarded_for_hops=1)
        codes = [
            self.client.post(
                "/api/v1/hospitals/uk-134/encounters",
                json=HIGH_RISK_ENTRY,
                headers={"X-Forwarded-For": f"192.0.2.{number}, 203.0.113.7"},
            ).status_code
            for number in range(3)
        ]
        self.assertEqual([201, 201, 429], codes)
        # Another client, as the proxy reports it, has its own count.
        other = self.client.post(
            "/api/v1/hospitals/uk-134/encounters", json=HIGH_RISK_ENTRY, headers={"X-Forwarded-For": "203.0.113.8"}
        )
        self.assertEqual(201, other.status_code)

    def setUp(self) -> None:
        main.rate_limits.reset()

    def test_writes_over_the_limit_get_429_with_retry_after(self) -> None:
        for _ in range(2):
            self.ok(self.client.post("/api/v1/hospitals/uk-134/encounters", json=HIGH_RISK_ENTRY), 201)
        response = self.client.post("/api/v1/hospitals/uk-134/encounters", json=HIGH_RISK_ENTRY)
        self.assertEqual(429, response.status_code)
        self.assertEqual("30", response.headers["retry-after"])
        self.assertEqual("Too many requests. Try again in 30 seconds.", response.json()["detail"])
        # Outcomes count against the same group.
        path = "/api/v1/hospitals/uk-134/encounters/uk-134-e1/outcome"
        response = self.client.post(path, json={"poor_outcome_6m": True})
        self.assertEqual(429, response.status_code)

    def test_a_refused_request_stores_nothing(self) -> None:
        for _ in range(2):
            self.client.post("/api/v1/hospitals/ch-245/encounters", json=HIGH_RISK_ENTRY)
        before = self.ok(self.client.get("/api/v1/hospitals/ch-245"))["counts"]
        self.assertEqual(429, self.client.post("/api/v1/hospitals/ch-245/encounters", json=HIGH_RISK_ENTRY).status_code)
        self.assertEqual(before, self.ok(self.client.get("/api/v1/hospitals/ch-245"))["counts"])

    def test_reads_are_not_limited(self) -> None:
        for _ in range(20):
            self.ok(self.client.get("/api/v1/hospitals/uk-134/entry-form"))

    def test_approvals_are_limited(self) -> None:
        self.assertEqual(404, self.client.post("/api/v1/models/none:approve").status_code)
        response = self.client.post("/api/v1/models/none:approve")
        self.assertEqual(429, response.status_code)
        self.assertEqual("60", response.headers["retry-after"])

    def test_the_429_carries_cors_headers_for_the_browser(self) -> None:
        for _ in range(2):
            self.client.post("/api/v1/hospitals/it-002/encounters", json=HIGH_RISK_ENTRY)
        response = self.client.post(
            "/api/v1/hospitals/it-002/encounters",
            json=HIGH_RISK_ENTRY,
            headers={"Origin": "https://example.github.io"},
        )
        self.assertEqual(429, response.status_code)
        self.assertIn("access-control-allow-origin", response.headers)
        self.assertIn("retry-after", response.headers.get("access-control-expose-headers", "").lower())


class HourlyCapTests(unittest.TestCase):
    """A cap on an action in any 60 minutes, whoever asks."""

    def test_the_cap_refuses_until_the_oldest_action_is_an_hour_old(self) -> None:
        clock = FakeClock()
        cap = HourlyCap({"probes": 3}, clock=clock)
        self.assertEqual(0.0, cap.take("probes", "uk-134"))
        clock.now += 600
        self.assertEqual([0.0, 0.0], [cap.take("probes", "uk-134") for _ in range(2)])
        self.assertAlmostEqual(3000.0, cap.take("probes", "uk-134"))
        clock.now += 3000
        self.assertEqual(0.0, cap.take("probes", "uk-134"))
        self.assertGreater(cap.take("probes", "uk-134"), 0.0)

    def test_a_refused_action_is_not_counted(self) -> None:
        clock = FakeClock()
        cap = HourlyCap({"pilots": 1}, clock=clock)
        self.assertEqual(0.0, cap.take("pilots"))
        for _ in range(5):
            self.assertGreater(cap.take("pilots"), 0.0)
        clock.now += 3600
        self.assertEqual(0.0, cap.take("pilots"))

    def test_keys_and_groups_are_counted_apart(self) -> None:
        cap = HourlyCap({"probes": 1, "pilots": 1}, clock=FakeClock())
        self.assertEqual(0.0, cap.take("probes", "uk-134"))
        self.assertEqual(0.0, cap.take("probes", "it-002"))
        self.assertEqual(0.0, cap.take("pilots"))
        self.assertGreater(cap.take("probes", "uk-134"), 0.0)

    def test_zero_or_a_missing_group_is_no_cap(self) -> None:
        cap = HourlyCap({"probes": 0}, clock=FakeClock())
        self.assertTrue(all(cap.take("probes", "uk-134") == 0.0 for _ in range(1000)))
        self.assertEqual(0.0, cap.take("pilots"))

    def test_configure_rejects_a_bad_cap_and_resets_counts(self) -> None:
        cap = HourlyCap({"pilots": 1}, clock=FakeClock())
        cap.take("pilots")
        with self.assertRaises(ValueError):
            cap.configure({"pilots": -1})
        with self.assertRaises(ValueError):
            cap.configure({"pilots": True})
        cap.configure({"pilots": 1})
        self.assertEqual(0.0, cap.take("pilots"))

    def test_the_defaults_come_from_the_settings(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("HEALTHCARE_DEMO_PROBES_PER_SITE_PER_HOUR", "HEALTHCARE_DEMO_PILOTS_PER_HOUR"):
                os.environ.pop(name, None)
            settings = Settings()
        self.assertEqual(600, settings.probes_per_site_per_hour)
        self.assertEqual(20, settings.pilots_per_hour)
        with mock.patch.dict(os.environ, {"HEALTHCARE_DEMO_PROBES_PER_SITE_PER_HOUR": "7",
                                          "HEALTHCARE_DEMO_PILOTS_PER_HOUR": "0"}):
            settings = Settings()
        self.assertEqual((7, 0), (settings.probes_per_site_per_hour, settings.pilots_per_hour))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class HourlyCappedApiTests(ApiTestCase):
    """Beyond an hourly cap, the API answers 429 with Retry-After and says which limit was reached."""

    caps = {"probes": 2, "pilots": 1}

    def setUp(self) -> None:
        main.hourly_caps.configure(self.caps)
        self.addCleanup(main.hourly_caps.configure, {"probes": 0, "pilots": 0})

    def probe(self, site_id: str):
        return self.client.post("/api/v1/governance:probe", json={"site_id": site_id, "granularity": "row"})

    def audit_count(self, site_id: str) -> int:
        return len(self.ok(self.client.get(f"/api/v1/hospitals/{site_id}/audit?limit=500"))["records"])

    def test_probes_beyond_the_hourly_cap_at_a_hospital_are_refused(self) -> None:
        self.assertEqual([200, 200], [self.probe("uk-134").status_code for _ in range(2)])
        before = self.audit_count("uk-134")
        response = self.probe("uk-134")
        self.assertEqual(429, response.status_code)
        self.assertGreaterEqual(int(response.headers["retry-after"]), 3500)
        self.assertEqual("healthcare-demo", response.headers.get("x-dagents-app"))
        self.assertRegex(
            response.json()["detail"],
            r"^The demo's hourly limit for Guard probes at Hospital 134 is reached\. Try again in about \d+ minutes\.$",
        )
        self.assertEqual(before, self.audit_count("uk-134"), "a refused probe writes no audit record")
        # Each hospital has its own count, and an unknown hospital is not counted.
        self.assertEqual(200, self.probe("it-002").status_code)
        self.assertEqual(404, self.client.post("/api/v1/governance:probe", json={"site_id": "nowhere"}).status_code)

    def test_pilots_beyond_the_hourly_cap_are_refused_for_everyone(self) -> None:
        self.ok(self.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}))
        response = self.client.post(
            "/api/v1/pilot:run", json={"round_prefix": "api"}, headers={"X-Forwarded-For": "203.0.113.9"}
        )
        self.assertEqual(429, response.status_code)
        self.assertGreaterEqual(int(response.headers["retry-after"]), 3500)
        self.assertRegex(
            response.json()["detail"],
            r"^The demo's hourly limit for pilots is reached\. Try again in about \d+ minutes\.$",
        )


class InFlightCapTests(unittest.TestCase):
    """At most a set number of requests run at once; the others wait, then get the app's own 503.

    Each in-process agent call holds a thread from one shared pool while it
    waits, so the public edge must cap how many requests it lets in.
    """

    def capped_app(self, max_in_flight: int, wait_seconds: float) -> tuple[TestClient, InFlightCap, threading.Event]:
        release = threading.Event()
        inner = FastAPI()

        @inner.get("/slow")
        def slow() -> dict[str, str]:
            release.wait(10)
            return {"ok": "yes"}

        @inner.get("/fails")
        def fails() -> dict[str, str]:
            raise RuntimeError("boom")

        @inner.get("/api/v1/health")
        def health() -> dict[str, str]:
            return {"status": "ok"}

        cap = InFlightCap(inner, max_in_flight=max_in_flight, wait_seconds=wait_seconds)
        self.addCleanup(release.set)
        return TestClient(cap, raise_server_exceptions=False), cap, release

    @staticmethod
    def in_thread(client: TestClient, path: str) -> tuple[threading.Thread, list]:
        answers: list = []
        thread = threading.Thread(target=lambda: answers.append(client.get(path)), daemon=True)
        thread.start()
        return thread, answers

    def wait_for(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            self.assertLess(time.monotonic(), deadline, "the condition did not hold in time")
            time.sleep(0.01)

    def test_a_request_beyond_the_cap_waits_then_gets_the_busy_answer(self) -> None:
        client, cap, release = self.capped_app(max_in_flight=1, wait_seconds=0.2)
        first, first_answer = self.in_thread(client, "/slow")
        self.wait_for(lambda: cap.active == 1)
        started = time.monotonic()
        busy = client.get("/slow")
        self.assertGreaterEqual(time.monotonic() - started, 0.2, "the request did not wait for a slot")
        self.assertEqual(503, busy.status_code)
        self.assertEqual({"detail": BUSY_MESSAGE}, busy.json())
        self.assertEqual("60", busy.headers["retry-after"])
        self.assertEqual(1, cap.refused)
        # The health check never waits for a slot.
        self.assertEqual(200, client.get("/api/v1/health").status_code)
        release.set()
        first.join(5)
        self.assertEqual(200, first_answer[0].status_code)
        self.assertEqual(0, cap.active)
        self.assertEqual(200, client.get("/slow").status_code)

    def test_a_waiting_request_takes_the_slot_when_it_is_freed(self) -> None:
        client, cap, release = self.capped_app(max_in_flight=1, wait_seconds=5.0)
        first, first_answer = self.in_thread(client, "/slow")
        self.wait_for(lambda: cap.active == 1)
        second, second_answer = self.in_thread(client, "/slow")
        time.sleep(0.1)
        self.assertEqual([], second_answer, "the second request ran beyond the cap")
        release.set()
        first.join(5)
        second.join(5)
        self.assertEqual([200, 200], [first_answer[0].status_code, second_answer[0].status_code])
        self.assertEqual(0, cap.refused)

    def test_a_failing_request_gives_its_slot_back(self) -> None:
        client, cap, _ = self.capped_app(max_in_flight=1, wait_seconds=0.0)
        self.assertEqual(500, client.get("/fails").status_code)
        self.assertEqual(0, cap.active)
        self.assertEqual(500, client.get("/fails").status_code)
        self.assertEqual(0, cap.refused)

    def test_the_cap_must_be_a_whole_number_of_one_or_more(self) -> None:
        for value in (0, -1, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                InFlightCap(FastAPI(), max_in_flight=value)  # type: ignore[arg-type]

    def test_the_app_sizes_the_agent_threads_for_the_cap(self) -> None:
        """Nested agent calls hold threads from one pool: at least 4 per request in flight, plus 8."""
        Settings(max_in_flight=12, agent_thread_tokens=56).check_agent_capacity()
        with self.assertRaises(ValueError) as raised:
            Settings(max_in_flight=12, agent_thread_tokens=55).check_agent_capacity()
        self.assertIn("HEALTHCARE_DEMO_AGENT_THREAD_TOKENS", str(raised.exception))


class SlowBodyTests(unittest.IsolatedAsyncioTestCase):
    """A request whose body has not arrived yet holds no in-flight slot.

    A client controls how slowly it sends a body, and the server has no
    body-read timeout. If a request took its slot before its body arrived, a
    few slow uploads would hold every slot and every page would answer 503.
    The app's own middleware stack is called directly, so the order of its
    layers is what is tested.
    """

    @staticmethod
    async def call(scope: dict, receive) -> tuple[int, dict]:  # noqa: ANN001 - an ASGI receive callable
        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(message)

        await main.app(scope, receive, send)
        status = next(message["status"] for message in sent if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        return status, json.loads(body)

    @staticmethod
    def scope(method: str, path: str, headers: list[tuple[bytes, bytes]] | None = None) -> dict:
        return {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": headers or [], "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 80),
        }

    @staticmethod
    async def no_body() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    def in_flight_cap(self) -> InFlightCap:
        layer = main.app.middleware_stack
        while layer is not None and not isinstance(layer, InFlightCap):
            layer = getattr(layer, "app", None)
        self.assertIsInstance(layer, InFlightCap)
        return layer

    async def test_slow_uploads_do_not_hold_the_slots(self) -> None:
        status, _ = await self.call(self.scope("GET", "/api/v1/health"), self.no_body)  # builds the stack
        self.assertEqual(200, status)
        cap = self.in_flight_cap()
        never = asyncio.Event()

        def trickle() -> object:
            first = True

            async def receive() -> dict:
                nonlocal first
                if first:
                    first = False
                    return {"type": "http.request", "body": b"{", "more_body": True}
                await never.wait()
                return {"type": "http.disconnect"}

            return receive

        uploads = [
            asyncio.create_task(
                self.call(
                    self.scope("POST", "/api/v1/triage:assess", [(b"content-length", b"1000"), (b"content-type", b"application/json")]),
                    trickle(),
                )
            )
            for _ in range(cap.max_in_flight)
        ]
        try:
            await asyncio.sleep(0.2)
            self.assertEqual(0, cap.active, "a request whose body has not arrived holds a slot")
            with mock.patch.object(cap, "wait_seconds", 0.5):
                status, body = await asyncio.wait_for(self.call(self.scope("GET", "/api/v1/conditions"), self.no_body), 5)
            self.assertEqual(200, status, body)
        finally:
            for task in uploads:
                task.cancel()
            await asyncio.gather(*uploads, return_exceptions=True)
        self.assertEqual(0, cap.active)


class BodySizeLimitTests(unittest.IsolatedAsyncioTestCase):
    """The middleware counts the bytes it receives, whatever the headers say.

    The server's HTTP parser frames a request that has both Content-Length and
    Transfer-Encoding by the chunked encoding, so a small Content-Length can
    come with a large body.
    """

    LIMIT = 1024

    async def call(self, headers: list[tuple[bytes, bytes]], chunks: list[bytes], method: str = "POST"):
        received: list[bytes] = []
        sent: list[dict] = []
        pending = [
            {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
            for index, chunk in enumerate(chunks)
        ]

        async def inner(scope, receive, send) -> None:
            while True:
                message = await receive()
                received.append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def receive() -> dict:
            return pending.pop(0) if pending else {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            sent.append(message)

        scope = {"type": "http", "method": method, "headers": headers}
        await BodySizeLimit(inner, max_bytes=self.LIMIT)(scope, receive, send)
        status = next(message["status"] for message in sent if message["type"] == "http.response.start")
        body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
        return status, json.loads(body) if status != 200 else body, b"".join(received)

    async def test_both_framing_headers_are_refused_before_the_app_runs(self) -> None:
        status, body, received = await self.call(
            [(b"content-length", b"10"), (b"transfer-encoding", b"chunked")], [b"x" * 600, b"x" * 600]
        )
        self.assertEqual(400, status)
        self.assertEqual({"detail": "The request has both Content-Length and Transfer-Encoding."}, body)
        self.assertEqual(b"", received, "the app must not run")

    async def test_a_body_longer_than_its_content_length_is_still_counted(self) -> None:
        status, body, received = await self.call([(b"content-length", b"10")], [b"x" * 600, b"x" * 600])
        self.assertEqual(413, status)
        self.assertIn("larger than", body["detail"])
        self.assertEqual(b"", received)

    async def test_a_chunked_body_over_the_limit_is_refused(self) -> None:
        status, _, received = await self.call([(b"transfer-encoding", b"chunked")], [b"x" * 600] * 3)
        self.assertEqual(413, status)
        self.assertEqual(b"", received)

    async def test_a_declared_length_over_the_limit_is_refused_without_reading(self) -> None:
        status, _, received = await self.call([(b"content-length", b"5000")], [b"x" * 5000])
        self.assertEqual(413, status)
        self.assertEqual(b"", received)

    async def test_a_body_within_the_limit_reaches_the_app_whole(self) -> None:
        status, body, received = await self.call([(b"content-length", b"1000")], [b"a" * 500, b"b" * 500])
        self.assertEqual(200, status)
        self.assertEqual(b"a" * 500 + b"b" * 500, received)

    async def test_a_get_is_passed_through(self) -> None:
        status, _, _ = await self.call([], [b""], method="GET")
        self.assertEqual(200, status)


if __name__ == "__main__":
    unittest.main()
