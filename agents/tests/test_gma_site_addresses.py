"""The GMA learns the address of a joinable site's LMA while it runs.

A GMA calls only LMAs in its agent network. At start the network holds the
addresses in ``GMA_SITE_ENDPOINTS``. ``GMA_JOINABLE_SITE_ENDPOINTS`` lists
sites that may join later, each with the one address the GMA accepts for it.
The study operator sends that address with ``PUT
/api/v1/federation/sites/{site_id}/address``; from then on the site directory
(refresh, summaries) and the HTTP engine (dispatch) call it, because both read
the network.

What is checked:

- ``SiteAddressBookTests``: the book's rules, in order, with no planner and no
  socket: a configured site at its own address, a configured site at another
  address, a site that may not join, an address that differs from the listed
  one, a site that joined already, and a trailing ``/``; removal; the listing;
  concurrent joins; no message repeats an address.
- ``SiteAddressRouteTests``: the three route pairs through ``create_app`` and
  ``TestClient``: tokens, every refusal with its status and code, no answer or
  log line repeats an address, the short and versioned paths answer alike, and
  a GMA with an in-process network lists nothing and refuses every address.
- ``JoinableSettingsTests``: ``GMA_JOINABLE_SITE_ENDPOINTS`` and the start-up
  refusals that come with it.
- ``OverHttpJoinTests``: fixture LMAs served by uvicorn. After the address is
  accepted, a refresh enrols the joined site and a round's job reaches its LMA
  with its coordinator token; after the address is removed, no call reaches it.
  The plans come from the real ``dagentsc``; the class skips without it.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import unittest
from typing import Any

import uvicorn
from fastapi.testclient import TestClient

from agents.common.domain.federation import SiteAddressList, SiteAddressState
from agents.common.infrastructure.agent_client import GMA_AGENT, AgentNetwork, lma_agent
from agents.gma.app_factory import GmaDeps, create_app
from agents.gma.application.site_addresses import SiteAddressBook
from agents.gma.config import Settings
from agents.gma.domain.errors import (
    InvalidSiteAddress,
    SiteAddressDiffers,
    SiteAddressRefused,
    SiteConfigured,
    SiteConfiguredElsewhere,
    SiteNotJoinable,
)
from agents.tests.fixtures.gma_fixtures import (
    ADMIN_TOKEN,
    COORDINATOR_TOKENS,
    SITE_TOKENS,
    THRESHOLD_VERSION,
    build_fixture_lma,
    register_models,
    toy_manifest,
    toy_registry,
)
from agents.tests.test_governance import dagentsc_available

CONFIGURED_URL = "http://127.0.0.1:9/site-a"
JOINABLE_URL = "http://127.0.0.1:9/site-d"
OTHER_URL = "http://127.0.0.1:9/elsewhere"

#: The joinable site's GMA token (scope ``site``, bound to site-d).
SITE_D_TOKEN = "gma-site-d-token-eeeeeeeeeeeeeeeeeeeeeeee"


class Forgotten:
    """Records the sites whose cached directory entries the book asked to drop."""

    def __init__(self) -> None:
        self.sites: list[str] = []

    def __call__(self, site_id: str) -> None:
        self.sites.append(site_id)


class SiteAddressBookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.network = AgentNetwork.over_http({lma_agent("site-a"): CONFIGURED_URL})
        self.addCleanup(self.network.close)
        self.forgotten = Forgotten()
        self.book = SiteAddressBook(
            self.network,
            configured={"site-a": CONFIGURED_URL},
            joinable={"site-d": JOINABLE_URL},
            on_change=self.forgotten,
        )

    def assert_refused(self, error_type: type[SiteAddressRefused], site_id: str, url: str) -> SiteAddressRefused:
        with self.assertRaises(error_type) as raised:
            self.book.attach(site_id, url)
        self.assertNotIn("127.0.0.1", str(raised.exception))
        self.assertEqual(raised.exception.site_id, site_id)
        return raised.exception

    def test_1_a_configured_site_at_its_own_address_is_accepted_as_configured(self) -> None:
        self.assertEqual(
            self.book.attach("site-a", CONFIGURED_URL + "/"), SiteAddressState(site_id="site-a", source="configured")
        )
        self.assertEqual(self.network.agents, [lma_agent("site-a")])

    def test_2_a_configured_site_at_another_address_is_refused_with_409(self) -> None:
        error = self.assert_refused(SiteConfiguredElsewhere, "site-a", OTHER_URL)
        self.assertEqual((error.code, error.http_status), ("configured_elsewhere", 409))

    def test_3_a_site_that_may_not_join_is_refused_with_403(self) -> None:
        error = self.assert_refused(SiteNotJoinable, "site-z", JOINABLE_URL)
        self.assertEqual((error.code, error.http_status), ("not_joinable", 403))

    def test_4_an_address_other_than_the_listed_one_is_refused_with_403(self) -> None:
        error = self.assert_refused(SiteAddressDiffers, "site-d", OTHER_URL)
        self.assertEqual((error.code, error.http_status), ("address_differs", 403))
        self.assertFalse(self.network.has_lma("site-d"))

    def test_5_a_joinable_site_joins_once_and_is_accepted_again(self) -> None:
        state = self.book.attach("site-d", JOINABLE_URL)
        self.assertEqual(state, SiteAddressState(site_id="site-d", source="joined"))
        client = self.network._transport._clients[lma_agent("site-d")]
        self.assertEqual(self.book.attach("site-d", JOINABLE_URL + "/"), state)
        self.assertIs(self.network._transport._clients[lma_agent("site-d")], client)
        self.assertEqual(self.forgotten.sites, ["site-d", "site-d"])

    def test_6_a_trailing_slash_is_ignored_on_both_sides(self) -> None:
        book = SiteAddressBook(self.network, configured={}, joinable={"site-d": JOINABLE_URL + "/"})
        self.assertEqual(book.attach("site-d", JOINABLE_URL).source, "joined")

    def test_an_address_that_is_not_a_base_url_is_refused_with_422(self) -> None:
        for url in ("ftp://127.0.0.1:9/d", "http://user:pw@127.0.0.1:9/d", JOINABLE_URL + "?a=1", JOINABLE_URL + "#f", "d"):
            with self.subTest(url=url):
                error = self.assert_refused(InvalidSiteAddress, "site-d", url)
                self.assertEqual((error.code, error.http_status), ("invalid_request", 422))

    def test_the_rules_are_checked_in_order(self) -> None:
        # A configured site is never "not joinable", and a site that may not
        # join is told so before its address is compared.
        self.assert_refused(SiteConfiguredElsewhere, "site-a", JOINABLE_URL)
        self.assert_refused(SiteNotJoinable, "site-z", OTHER_URL)

    def test_detach_removes_a_joined_address_only(self) -> None:
        self.book.attach("site-d", JOINABLE_URL)
        self.book.detach("site-d")
        self.assertFalse(self.network.has_lma("site-d"))
        self.book.detach("site-d")  # not joined any more: nothing happens
        with self.assertRaises(SiteConfigured) as raised:
            self.book.detach("site-a")
        self.assertEqual((raised.exception.code, raised.exception.http_status), ("configured", 409))
        self.assertTrue(self.network.has_lma("site-a"))
        with self.assertRaises(SiteNotJoinable):
            self.book.detach("site-z")
        self.assertEqual(self.forgotten.sites, ["site-d", "site-d"])

    def test_the_listing_names_the_joinable_sites_and_every_address_source_sorted(self) -> None:
        self.assertEqual(
            self.book.listing(),
            SiteAddressList(joinable=["site-d"], sites=[SiteAddressState(site_id="site-a", source="configured")]),
        )
        self.book.attach("site-d", JOINABLE_URL)
        self.assertEqual(
            self.book.listing(),
            SiteAddressList(
                joinable=["site-d"],
                sites=[
                    SiteAddressState(site_id="site-a", source="configured"),
                    SiteAddressState(site_id="site-d", source="joined"),
                ],
            ),
        )
        self.assertNotIn("127.0.0.1", self.book.listing().model_dump_json())

    def test_concurrent_joins_of_one_site_all_succeed_with_one_client(self) -> None:
        states: list[SiteAddressState] = []
        errors: list[BaseException] = []

        def join() -> None:
            try:
                states.append(self.book.attach("site-d", JOINABLE_URL))
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=join) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual({state.source for state in states}, {"joined"})
        self.assertEqual(self.network.agents, [lma_agent("site-a"), lma_agent("site-d")])

    def test_a_book_without_a_network_lists_nothing_and_refuses_every_site(self) -> None:
        book = SiteAddressBook(None, configured={}, joinable={})
        self.assertEqual(book.listing(), SiteAddressList(joinable=[], sites=[]))
        with self.assertRaises(SiteNotJoinable):
            book.attach("site-d", JOINABLE_URL)


def gma_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "engine": "http",
        "site_endpoints": {"site-a": CONFIGURED_URL},
        "joinable_site_endpoints": {"site-d": JOINABLE_URL},
        "lma_tokens": {"site-a": COORDINATOR_TOKENS["site-a"], "site-d": COORDINATOR_TOKENS["site-d"]},
        "admin_token": ADMIN_TOKEN,
        "site_tokens": {"site-a": SITE_TOKENS["site-a"], "site-d": SITE_D_TOKEN},
        "require_tokens": True,
    }
    values.update(overrides)
    return Settings(**values)


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class SiteAddressRouteTests(unittest.TestCase):
    PAIRS = {
        "put": ("/federation/sites/{site}/address", "/api/v1/federation/sites/{site}/address"),
        "list": ("/federation/site-addresses", "/api/v1/federation/site-addresses"),
    }

    def setUp(self) -> None:
        self.app = create_app(gma_settings(), GmaDeps(registry=toy_registry()))
        self.client = TestClient(self.app)
        self.addCleanup(self.app.state.gma.close)
        self.admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        self.lines = _Lines()
        root = logging.getLogger()
        root.addHandler(self.lines)
        previous = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous)
        self.addCleanup(root.removeHandler, self.lines)

    def put(self, site: str, url: Any, *, versioned: bool = True, headers: dict[str, str] | None = None):
        path = self.PAIRS["put"][1 if versioned else 0].format(site=site)
        body = {"url": url} if url is not None else {}
        return self.client.put(path, json=body, headers=self.admin if headers is None else headers)

    def assert_no_address_anywhere(self, *responses: Any) -> None:
        for response in responses:
            self.assertNotIn("127.0.0.1", response.text)
        self.assertFalse([line for line in self.lines.lines if "127.0.0.1" in line], self.lines.lines)

    def test_tokens(self) -> None:
        self.assertEqual(self.put("site-d", JOINABLE_URL, headers={}).status_code, 401)
        site_token = {"Authorization": f"Bearer {SITE_D_TOKEN}"}
        self.assertEqual(self.put("site-d", JOINABLE_URL, headers=site_token).status_code, 403)
        self.assertEqual(self.client.get(self.PAIRS["list"][1], headers=site_token).status_code, 403)
        self.assertEqual(
            self.client.delete(self.PAIRS["put"][1].format(site="site-d"), headers=site_token).status_code, 403
        )
        self.assertEqual(self.put("site-d", JOINABLE_URL).status_code, 200)

    def test_an_accepted_address_joins_the_network(self) -> None:
        response = self.put("site-d", JOINABLE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"site_id": "site-d", "source": "joined"})
        self.assertIn(lma_agent("site-d"), self.app.state.gma.network.agents)
        listed = self.client.get(self.PAIRS["list"][1], headers=self.admin)
        self.assertEqual(
            listed.json(),
            {
                "joinable": ["site-d"],
                "sites": [{"site_id": "site-a", "source": "configured"}, {"site_id": "site-d", "source": "joined"}],
            },
        )
        self.assert_no_address_anywhere(response, listed)

    def test_each_refusal_has_its_status_and_code(self) -> None:
        cases = (
            ("site-z", JOINABLE_URL, 403, "not_joinable"),
            ("site-d", OTHER_URL, 403, "address_differs"),
            ("site-a", OTHER_URL, 409, "configured_elsewhere"),
        )
        responses = []
        for site, url, status, code in cases:
            with self.subTest(site=site, url=url):
                response = self.put(site, url)
                responses.append(response)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["code"], code)
        self.assertFalse(self.app.state.gma.network.has_lma("site-d"))
        self.assert_no_address_anywhere(*responses)

    def test_a_bad_body_or_address_is_a_422(self) -> None:
        responses = []
        for url in (None, "", "h" * 2049, "http://user:pw@127.0.0.1:9/site-d", JOINABLE_URL + "?q=1",
                    JOINABLE_URL + "#f", "ftp://127.0.0.1:9/site-d"):
            with self.subTest(url=None if url is None else url[:40]):
                response = self.put("site-d", url)
                responses.append(response)
                self.assertEqual(response.status_code, 422)
        self.assertNotIn("user:pw", " ".join(response.text for response in responses))
        self.assertFalse(self.app.state.gma.network.has_lma("site-d"))

    def test_the_short_and_versioned_paths_answer_alike(self) -> None:
        for site, url in (("site-d", JOINABLE_URL), ("site-z", JOINABLE_URL), ("site-a", OTHER_URL), ("site-a", CONFIGURED_URL)):
            with self.subTest(site=site, url=url):
                short, versioned = self.put(site, url, versioned=False), self.put(site, url)
                self.assertEqual((short.status_code, short.json()), (versioned.status_code, versioned.json()))
        short = self.client.get(self.PAIRS["list"][0], headers=self.admin)
        versioned = self.client.get(self.PAIRS["list"][1], headers=self.admin)
        self.assertEqual((short.status_code, short.json()), (versioned.status_code, versioned.json()))

    def test_delete_removes_a_joined_address_and_refuses_the_others(self) -> None:
        self.put("site-d", JOINABLE_URL)
        for versioned in (True, False):
            with self.subTest(versioned=versioned):
                path = self.PAIRS["put"][1 if versioned else 0]
                self.assertEqual(self.client.delete(path.format(site="site-d"), headers=self.admin).status_code, 204)
                configured = self.client.delete(path.format(site="site-a"), headers=self.admin)
                self.assertEqual((configured.status_code, configured.json()["code"]), (409, "configured"))
                unknown = self.client.delete(path.format(site="site-z"), headers=self.admin)
                self.assertEqual((unknown.status_code, unknown.json()["code"]), (403, "not_joinable"))
        self.assertFalse(self.app.state.gma.network.has_lma("site-d"))
        self.assertTrue(self.app.state.gma.network.has_lma("site-a"))

    def test_a_gma_with_an_in_process_network_lists_nothing_and_refuses_every_address(self) -> None:
        network = AgentNetwork.in_process()
        self.addCleanup(network.close)
        app = create_app(Settings(engine="http"), GmaDeps(registry=toy_registry(), network=network))
        self.addCleanup(app.state.gma.close)
        client = TestClient(app)
        self.assertEqual(client.get(self.PAIRS["list"][1]).json(), {"joinable": [], "sites": []})
        response = client.put(self.PAIRS["put"][1].format(site="site-d"), json={"url": JOINABLE_URL})
        self.assertEqual((response.status_code, response.json()["code"]), (403, "not_joinable"))


class JoinableSettingsTests(unittest.TestCase):
    def test_none_by_default_and_read_from_the_environment(self) -> None:
        self.assertEqual(Settings.from_env({}).joinable_site_endpoints, {})
        settings = Settings.from_env({"GMA_JOINABLE_SITE_ENDPOINTS": f"site-d={JOINABLE_URL}"})
        self.assertEqual(settings.joinable_site_endpoints, {"site-d": JOINABLE_URL})
        self.assertNotIn(JOINABLE_URL, repr(settings))

    def test_joinable_sites_alone_build_the_http_engine_with_an_empty_network(self) -> None:
        app = create_app(gma_settings(site_endpoints={}), GmaDeps(registry=toy_registry()))
        self.addCleanup(app.state.gma.close)
        self.assertEqual(app.state.gma.engine.name, "http")
        self.assertEqual(app.state.gma.network.agents, [])

    def assert_refused(self, settings: Settings, *words: str, deps: GmaDeps | None = None) -> None:
        with self.assertRaises(ValueError) as raised:
            app = create_app(settings, deps or GmaDeps(registry=toy_registry()))
            app.state.gma.close()
        message = str(raised.exception)
        for word in words:
            self.assertIn(word, message)
        self.assertNotIn("127.0.0.1", message)

    def test_a_site_in_both_lists_is_refused(self) -> None:
        self.assert_refused(
            gma_settings(site_endpoints={"site-d": JOINABLE_URL}),
            "GMA_SITE_ENDPOINTS",
            "GMA_JOINABLE_SITE_ENDPOINTS",
            "site-d",
        )

    def test_an_address_that_is_not_a_base_url_is_refused(self) -> None:
        self.assert_refused(
            gma_settings(joinable_site_endpoints={"site-d": "ftp://127.0.0.1:9/site-d"}), "GMA_JOINABLE_SITE_ENDPOINTS"
        )

    def test_the_simulator_takes_no_joinable_site(self) -> None:
        self.assert_refused(gma_settings(engine="in-process"), "GMA_JOINABLE_SITE_ENDPOINTS", "GMA_ENGINE=http")

    def test_an_in_process_network_takes_no_joinable_site(self) -> None:
        network = AgentNetwork.in_process()
        self.addCleanup(network.close)
        self.assert_refused(
            gma_settings(site_endpoints={}),
            "GMA_JOINABLE_SITE_ENDPOINTS",
            deps=GmaDeps(registry=toy_registry(), network=network),
        )

    def test_with_tokens_a_joinable_site_needs_both_of_its_tokens(self) -> None:
        self.assert_refused(gma_settings(lma_tokens={"site-a": COORDINATOR_TOKENS["site-a"]}), "GMA_LMA_TOKENS", "site-d")
        self.assert_refused(gma_settings(site_tokens={"site-a": SITE_TOKENS["site-a"]}), "GMA_SITE_TOKENS", "site-d")

    def test_without_tokens_a_joinable_site_needs_none(self) -> None:
        app = create_app(
            Settings(engine="http", joinable_site_endpoints={"site-d": JOINABLE_URL}), GmaDeps(registry=toy_registry())
        )
        self.addCleanup(app.state.gma.close)
        self.assertEqual(app.state.gma.addresses.listing().joinable, ["site-d"])

    def test_the_http_engine_still_needs_some_lma(self) -> None:
        self.assert_refused(Settings(engine="http"), "GMA_SITE_ENDPOINTS", "GMA_JOINABLE_SITE_ENDPOINTS")


class _Served:
    """A fixture LMA served by uvicorn on a loopback port the operating system picks."""

    def __init__(self, app: Any) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        self.server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="warning", lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)
        self.sock.close()


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class OverHttpJoinTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = toy_registry()
        uploads = AgentNetwork.over_http({})
        self.addCleanup(uploads.close)
        self.lmas = {site: build_fixture_lma(site, registry=registry, network=uploads) for site in ("site-a", "site-b", "site-d")}
        self.served = {site: _Served(lma.app) for site, lma in self.lmas.items()}
        for served in self.served.values():
            self.addCleanup(served.stop)
        settings = Settings(
            engine="http",
            site_endpoints={site: self.served[site].url for site in ("site-a", "site-b")},
            joinable_site_endpoints={"site-d": self.served["site-d"].url},
            lma_tokens={site: COORDINATOR_TOKENS[site] for site in ("site-a", "site-b", "site-d")},
            admin_token=ADMIN_TOKEN,
            site_tokens={"site-a": SITE_TOKENS["site-a"], "site-b": SITE_TOKENS["site-b"], "site-d": SITE_D_TOKEN},
            require_tokens=True,
            summary_ttl_seconds=0,
        )
        self.gma_app = create_app(settings, GmaDeps(registry=registry))
        self.addCleanup(self.gma_app.state.gma.close)
        self.network = AgentNetwork.in_process({GMA_AGENT: self.gma_app})
        self.addCleanup(self.network.close)
        self.gma = self.network.gma(ADMIN_TOKEN)
        self.digests = register_models(self.gma)

    def analytics(self, round_id: str) -> Any:
        manifest = toy_manifest(
            round_id, "analytics", THRESHOLD_VERSION, self.digests[THRESHOLD_VERSION], invited=("site-a", "site-b", "site-d")
        )
        return self.gma.dispatch_round(manifest)

    def test_a_joined_site_is_enrolled_and_receives_the_job_until_its_address_is_removed(self) -> None:
        self.assertEqual([item.site_id for item in self.gma.refresh_sites()], ["site-a", "site-b"])
        self.assertEqual(self.gma.site_addresses().joinable, ["site-d"])

        state = self.gma.register_site_address("site-d", self.served["site-d"].url)
        self.assertEqual(state.source, "joined")
        self.assertEqual([item.site_id for item in self.gma.refresh_sites()], ["site-a", "site-b", "site-d"])
        summaries = self.gma.site_summaries()
        self.assertEqual([entry.site_id for entry in summaries], ["site-a", "site-b", "site-d"])
        self.assertTrue(all(entry.reachable for entry in summaries))

        record = self.analytics("join-1")
        self.assertIn("site-d", record.plan.selected_sites)
        self.assertEqual([job.round_id for job in self.lmas["site-d"].offers], ["join-1"])
        completed = {result.site_id: result.participation for result in record.results}
        self.assertEqual(completed["site-d"], "completed")

        self.gma.register_site(self.gma.refresh_sites()[-1].model_copy(update={"enrolled": False}))
        self.gma.remove_site_address("site-d")
        self.assertEqual([item.site_id for item in self.gma.refresh_sites()], ["site-a", "site-b"])
        record = self.analytics("join-2")
        self.assertNotIn("site-d", record.plan.selected_sites)
        self.assertEqual(len(self.lmas["site-d"].offers), 1, "the GMA called a site whose address it removed")
        self.assertEqual(json.loads(self.gma_app.state.gma.addresses.listing().model_dump_json())["sites"][-1]["site_id"], "site-b")


if __name__ == "__main__":
    unittest.main()
