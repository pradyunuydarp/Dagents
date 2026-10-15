"""Tests for site addresses the GMA learns while it runs.

``HttpFederationEngine.attach_site`` accepts only the exact (site id, address)
pairs listed as joinable, and builds one worker per joined site. The routes
that expose it are checked through the GMA's FastAPI app, with the engine
swapped for one built in the test.

Only the test that dispatches a round to a joined site needs the planner,
because the controller asks it which sites to select. It skips without
``dagentsc``.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
from unittest import mock

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.common.application.federation import (
    FederatedRoundController,
    InProcessFederationEngine,
    SiteAddressBook,
)
from agents.common.domain.federation import SiteAddress, SiteAddressRefused
from agents.common.infrastructure.federation_http import HttpFederationEngine
from agents.gma.config import Settings
from agents.gma.di import build_federation_engine, build_round_controller
from agents.tests.test_federation_http import FakeSiteWorker, failing_client, site_app
from agents.tests.test_federation_updates import build_manifest, build_registration
from agents.tests.test_governance import dagentsc_available

CONFIGURED = {"site-a": "http://lma-a.internal:8082", "site-b": "http://lma-b.internal:8083"}
JOINABLE = {"site-d": "http://lma-d.internal:8085", "site-c": "http://lma-c.internal:8084"}


def build_engine(**overrides) -> HttpFederationEngine:
    payload = {
        "site_urls": CONFIGURED,
        "joinable": JOINABLE,
        "client": failing_client(httpx.ConnectError("no network in this test")),
    }
    payload.update(overrides)
    site_urls = payload.pop("site_urls")
    return HttpFederationEngine(site_urls, **payload)


class SiteAttachTests(unittest.TestCase):
    """``attach_site`` applies its rules in the order the engine documents."""

    def setUp(self) -> None:
        self.engine = build_engine()

    def refusal(self, site_id: str, url: str) -> SiteAddressRefused:
        with self.assertRaises(SiteAddressRefused) as raised:
            self.engine.attach_site(site_id, url)
        self.assertEqual(raised.exception.site_id, site_id)
        return raised.exception

    def test_the_engine_is_a_site_address_book_and_the_in_process_engine_is_not(self) -> None:
        self.assertIsInstance(self.engine, SiteAddressBook)
        self.assertNotIsInstance(InProcessFederationEngine(), SiteAddressBook)

    def test_1_a_configured_site_with_its_own_address_is_accepted_as_configured(self) -> None:
        address = self.engine.attach_site("site-a", "http://lma-a.internal:8082")
        self.assertEqual(address, SiteAddress(site_id="site-a", url="http://lma-a.internal:8082", source="configured"))
        self.assertEqual([item.source for item in self.engine.addresses().addresses], ["configured", "configured"])

    def test_2_a_configured_site_with_another_address_is_refused(self) -> None:
        self.assertEqual(self.refusal("site-a", "http://elsewhere.internal:8082").reason, "configured_elsewhere")

    def test_2_a_configured_site_that_is_also_named_joinable_cannot_exist(self) -> None:
        # Rule 2 comes before rule 3: a configured id never reaches the joinable checks.
        self.assertEqual(self.refusal("site-b", JOINABLE["site-c"]).reason, "configured_elsewhere")

    def test_3_a_site_that_is_not_joinable_is_refused(self) -> None:
        self.assertEqual(self.refusal("site-z", "http://lma-z.internal:8090").reason, "not_joinable")

    def test_4_a_joinable_site_at_another_address_is_refused(self) -> None:
        self.assertEqual(self.refusal("site-c", "http://lma-c.internal:9999").reason, "address_differs")
        self.assertEqual(self.refusal("site-c", "https://lma-c.internal:8084").reason, "address_differs")
        self.assertEqual(self.engine.addresses().addresses[-1].site_id, "site-b")

    def test_5_and_6_a_joinable_site_joins_once_and_is_accepted_again(self) -> None:
        with mock.patch.object(self.engine, "register_worker", wraps=self.engine.register_worker) as register:
            first = self.engine.attach_site("site-c", "http://lma-c.internal:8084")
            worker = self.engine._workers["site-c"]
            second = self.engine.attach_site("site-c", "http://lma-c.internal:8084")
        expected = SiteAddress(site_id="site-c", url="http://lma-c.internal:8084", source="joined")
        self.assertEqual((first, second), (expected, expected))
        self.assertEqual(register.call_count, 1)
        self.assertIs(self.engine._workers["site-c"], worker)
        self.assertEqual(worker.site_id, "site-c")
        self.assertEqual(worker._base_url, "http://lma-c.internal:8084")
        self.assertEqual(worker._timeout, self.engine.timeout)

    def test_a_refused_site_gets_no_worker(self) -> None:
        for site_id, url in (("site-z", "http://lma-z.internal:8090"), ("site-c", "http://wrong.internal:1")):
            with self.assertRaises(SiteAddressRefused):
                self.engine.attach_site(site_id, url)
        self.assertEqual(sorted(self.engine._workers), ["site-a", "site-b"])

    def test_two_threads_build_one_worker(self) -> None:
        barrier = threading.Barrier(8)
        answers: list[SiteAddress] = []
        built = []
        make = self.engine._worker

        def slow_worker(site_id: str, url: str):
            built.append(site_id)
            time.sleep(0.02)  # widen the window a race would need
            return make(site_id, url)

        def attach() -> None:
            barrier.wait()
            answers.append(self.engine.attach_site("site-d", "http://lma-d.internal:8085"))

        with mock.patch.object(self.engine, "_worker", side_effect=slow_worker):
            threads = [threading.Thread(target=attach) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
        self.assertEqual(built, ["site-d"])
        self.assertEqual(len(answers), 8)
        self.assertEqual({answer.source for answer in answers}, {"joined"})

    def test_a_trailing_slash_is_ignored_on_both_sides(self) -> None:
        engine = build_engine(site_urls={"site-a": "http://lma-a.internal:8082/"}, joinable={"site-c": "http://lma-c.internal:8084/"})
        self.assertEqual(engine.attach_site("site-a", "http://lma-a.internal:8082").source, "configured")
        self.assertEqual(engine.attach_site("site-c", "http://lma-c.internal:8084").url, "http://lma-c.internal:8084")
        self.assertEqual(engine.attach_site("site-c", "http://lma-c.internal:8084/").source, "joined")
        self.assertEqual(engine.joinable, {"site-c": "http://lma-c.internal:8084"})

    def test_a_site_in_both_maps_is_refused_at_construction(self) -> None:
        with self.assertRaises(ValueError) as raised:
            HttpFederationEngine({"site-a": "http://a:1"}, joinable={"site-a": "http://a:1"})
        self.assertEqual(str(raised.exception), "site site-a is both configured and joinable")

    def test_an_engine_with_no_configured_site_accepts_joinable_ones(self) -> None:
        engine = build_engine(site_urls={})
        self.assertEqual(engine.site_urls, {})
        self.assertEqual(engine.attach_site("site-d", "http://lma-d.internal:8085").source, "joined")

    def test_addresses_list_configured_then_joined_each_sorted(self) -> None:
        self.assertEqual(self.engine.addresses().joinable, ["site-c", "site-d"])
        self.engine.attach_site("site-d", "http://lma-d.internal:8085")
        self.engine.attach_site("site-c", "http://lma-c.internal:8084")
        listed = self.engine.addresses()
        self.assertEqual(listed.joinable, ["site-c", "site-d"])
        self.assertEqual(
            [(item.site_id, item.source) for item in listed.addresses],
            [("site-a", "configured"), ("site-b", "configured"), ("site-c", "joined"), ("site-d", "joined")],
        )

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_a_joined_site_receives_the_offer_and_the_job_of_a_dispatched_round(self) -> None:
        site = FakeSiteWorker("site-c")
        network = FastAPI()
        network.mount("/site-c", site_app(site))
        engine = HttpFederationEngine({}, joinable={"site-c": "http://lmas.internal/site-c"}, client=TestClient(network))
        controller = FederatedRoundController(engine)
        controller.register_site(build_registration("site-c"))
        manifest = build_manifest(minimum_participants=1, invited_sites=["site-c"])

        before = controller.dispatch_round(manifest.model_copy(update={"round_id": "round-before"}))
        self.assertEqual([(r.site_id, r.participation) for r in before.results], [("site-c", "failed")])
        self.assertEqual(site.offered, [])

        engine.attach_site("site-c", "http://lmas.internal/site-c")
        after = controller.dispatch_round(manifest)

        self.assertEqual(after.plan.selected_sites, ["site-c"])
        self.assertEqual([(r.site_id, r.participation) for r in after.results], [("site-c", "completed")])
        self.assertEqual([job.round_id for job in site.offered], [manifest.round_id])
        self.assertEqual([job.round_id for job in site.executed], [manifest.round_id])
        self.assertIs(controller.engine, engine)


class GmaConfigTests(unittest.TestCase):
    """The GMA reads ``GMA_FEDERATION_JOINABLE_SITES`` to choose and build its engine."""

    NAMES = ("GMA_FEDERATION_SITES", "GMA_FEDERATION_JOINABLE_SITES", "GMA_FEDERATION_TIMEOUT_SECONDS")

    def settings_with(self, **env: str) -> Settings:
        with mock.patch.dict(os.environ, env):
            for name in self.NAMES:
                if name not in env:
                    os.environ.pop(name, None)
            return Settings()

    def test_the_setting_is_empty_by_default(self) -> None:
        config = self.settings_with()
        self.assertEqual(config.federation_joinable_sites, "")
        self.assertIs(type(build_federation_engine(config)), InProcessFederationEngine)

    def test_joinable_sites_alone_build_the_http_engine(self) -> None:
        config = self.settings_with(
            GMA_FEDERATION_JOINABLE_SITES="site-d=http://lma-d:8085/", GMA_FEDERATION_TIMEOUT_SECONDS="30"
        )
        engine = build_federation_engine(config)
        self.assertIsInstance(engine, HttpFederationEngine)
        self.assertEqual(engine.site_urls, {})
        self.assertEqual(engine.joinable, {"site-d": "http://lma-d:8085"})
        self.assertEqual(engine.timeout, 30.0)
        self.assertEqual(build_round_controller(config).engine_name, "http-sites")

    def test_configured_and_joinable_sites_share_one_engine(self) -> None:
        config = self.settings_with(
            GMA_FEDERATION_SITES="site-a=http://lma-a:8082", GMA_FEDERATION_JOINABLE_SITES="site-d=http://lma-d:8085"
        )
        engine = build_round_controller(config).engine
        self.assertEqual((engine.site_urls, engine.joinable), ({"site-a": "http://lma-a:8082"}, {"site-d": "http://lma-d:8085"}))

    def test_a_site_in_both_settings_stops_the_gma_naming_both(self) -> None:
        config = self.settings_with(
            GMA_FEDERATION_SITES="site-a=http://lma-a:8082", GMA_FEDERATION_JOINABLE_SITES="site-a=http://lma-a:8082"
        )
        with self.assertRaises(ValueError) as raised:
            build_round_controller(config)
        self.assertIn("GMA_FEDERATION_SITES", str(raised.exception))
        self.assertIn("GMA_FEDERATION_JOINABLE_SITES", str(raised.exception))
        self.assertIn("site-a", str(raised.exception))

    def test_a_mistake_in_the_joinable_list_stops_the_gma(self) -> None:
        with self.assertRaises(ValueError):
            build_round_controller(self.settings_with(GMA_FEDERATION_JOINABLE_SITES="site-d"))


if __name__ == "__main__":
    unittest.main()
