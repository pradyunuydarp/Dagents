"""The routes that add a hospital to a running consortium, and the flight they share with the pilot.

A pilot and the addition of a hospital share one :class:`SingleFlight`, so the
two never run at the same time, and a refused request says which one runs.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.api.deploy_routes import FOREIGN_ORIGIN_MESSAGE  # noqa: E402
from app.api.limits import SingleFlight  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.domain.dataset import JOINING_SITES  # noqa: E402
from app.services.deployment import (  # noqa: E402
    AVAILABILITY_CACHE_SECONDS,
    REASON_NO_CORE,
    REASON_NO_GMA,
    REASON_NOT_APP,
    REASON_NOT_JOINABLE,
    REASON_OFF,
    DeployerSettingError,
    build_hospital_deployer,
    check_deployer,
)


class SingleFlightTests(unittest.TestCase):
    def test_one_holder_at_a_time_and_its_name(self) -> None:
        flight = SingleFlight()
        self.assertFalse(flight.busy)
        self.assertIsNone(flight.holder)
        self.assertTrue(flight.try_enter("pilot"))
        self.assertTrue(flight.busy)
        self.assertEqual("pilot", flight.holder)
        self.assertFalse(flight.try_enter("deployment"))
        self.assertEqual("pilot", flight.holder)
        flight.leave()
        self.assertFalse(flight.busy)
        self.assertIsNone(flight.holder)
        self.assertTrue(flight.try_enter("deployment"))
        self.assertEqual("deployment", flight.holder)
        flight.leave()

    def test_the_holder_defaults_to_no_name(self) -> None:
        flight = SingleFlight()
        self.assertTrue(flight.try_enter())
        self.assertEqual("", flight.holder)
        flight.leave()

    def test_two_threads_never_both_enter(self) -> None:
        flight = SingleFlight()
        entered: list[str] = []
        start = threading.Barrier(8)

        def attempt(name: str) -> None:
            start.wait()
            if flight.try_enter(name):
                entered.append(name)

        threads = [threading.Thread(target=attempt, args=(f"caller-{n}",)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, len(entered))
        self.assertEqual(entered[0], flight.holder)
        flight.leave()


class PilotBusyMessageTests(unittest.TestCase):
    """The pilot route names what holds the flight."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main.app)

    def refused(self, holder: str) -> dict:
        self.assertTrue(main.pilot_flight.try_enter(holder))
        try:
            response = self.client.post("/api/v1/pilot:run", json={"round_prefix": "ui"})
        finally:
            main.pilot_flight.leave()
        self.assertEqual(409, response.status_code, response.text)
        return response.json()

    def test_a_running_pilot(self) -> None:
        self.assertEqual("A pilot is already running. Try again when it finishes.", self.refused("pilot")["detail"])

    def test_a_hospital_being_added(self) -> None:
        self.assertEqual(
            "A hospital is being added. Run the pilot when it has joined.", self.refused("deployment")["detail"]
        )

    def test_a_holder_that_left_reads_as_a_pilot(self) -> None:
        """The holder is read after the refusal; if it left, the pilot's own message is used."""
        self.assertTrue(main.pilot_flight.try_enter("deployment"))
        main.pilot_flight._holder = None  # the holder left between the refusal and the read
        try:
            response = self.client.post("/api/v1/pilot:run", json={"round_prefix": "ui"})
        finally:
            main.pilot_flight.leave()
        self.assertEqual("A pilot is already running. Try again when it finishes.", response.json()["detail"])


class DeployerSettingTests(unittest.TestCase):
    """The app refuses to start with a deploy setting that is unsafe or cannot work (``check_deployer``)."""

    LMA_URLS = "uk-134=http://127.0.0.1:8082,it-002=http://127.0.0.1:8083,ch-245=http://127.0.0.1:8084"

    def settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "role": "app",
            "deployer": "process",
            "lma_urls": self.LMA_URLS,
            "joining_lma_urls": "pl-290=http://127.0.0.1:8085",
            "coordinator_url": "http://127.0.0.1:8081",
        }
        values.update(overrides)
        return dataclasses.replace(main.settings, **values)

    def refused(self, message: str, environ: dict[str, str] | None = None, **overrides: object) -> None:
        with self.assertRaises(DeployerSettingError) as caught:
            check_deployer(self.settings(**overrides), environ or {})
        self.assertEqual(message, str(caught.exception))
        self.assertIsInstance(caught.exception, ValueError)

    def test_process_and_compose_settings_that_work(self) -> None:
        check_deployer(self.settings(), {})
        check_deployer(self.settings(joining_lma_urls="pl-290=http://localhost:8085"), {})
        check_deployer(
            self.settings(
                deployer="compose",
                joining_lma_urls="pl-290=http://lma-pl-290:8085",
                coordinator_url="http://healthcare-demo-backend:8081",
            ),
            {},
        )

    def test_1_an_unknown_value(self) -> None:
        self.refused("HEALTHCARE_DEMO_DEPLOYER must be off, process or compose, not 'docker'.", deployer="docker")

    def test_2_off_needs_nothing_else(self) -> None:
        for role in ("combined", "lma", "app"):
            with self.subTest(role=role):
                check_deployer(self.settings(deployer="off", role=role, joining_lma_urls=""), {"RENDER": "true"})

    def test_the_setting_is_read_stripped_and_in_lower_case(self) -> None:
        with mock.patch.dict(os.environ, {"HEALTHCARE_DEMO_DEPLOYER": "  Process "}):
            self.assertEqual("process", Settings().deployer)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEALTHCARE_DEMO_DEPLOYER", None)
            self.assertEqual("off", Settings().deployer)

    def test_3_only_the_app_role(self) -> None:
        for role in ("combined", "lma"):
            with self.subTest(role=role):
                self.refused(
                    "HEALTHCARE_DEMO_DEPLOYER=process needs HEALTHCARE_DEMO_ROLE=app. Only the app of a split run "
                    "adds hospitals.",
                    role=role,
                )

    def test_4_never_on_render(self) -> None:
        for deployer in ("process", "compose"):
            with self.subTest(deployer=deployer):
                self.refused(
                    "HEALTHCARE_DEMO_DEPLOYER must be off on Render. The public demo never starts a process or a "
                    "container.",
                    environ={"RENDER": "true"},
                    deployer=deployer,
                )

    def test_5_no_joining_hospital(self) -> None:
        self.refused("HEALTHCARE_DEMO_JOINING_LMA_URLS names no hospital.", joining_lma_urls=" , ")

    def test_6_a_hospital_that_cannot_join(self) -> None:
        self.refused("xx-999 is not a hospital that can join. Known: pl-290.", joining_lma_urls="xx-999=http://127.0.0.1:8085")
        self.refused(
            "uk-134 is not a hospital that can join. Known: pl-290.", joining_lma_urls="uk-134=http://127.0.0.1:8085"
        )

    def test_7_a_clash_with_the_configured_hospitals(self) -> None:
        self.refused(
            "pl-290 clashes with HEALTHCARE_DEMO_LMA_URLS.",
            lma_urls=f"{self.LMA_URLS},pl-290=http://127.0.0.1:8086",
        )
        self.refused("pl-290 clashes with HEALTHCARE_DEMO_LMA_URLS.", joining_lma_urls="pl-290=http://127.0.0.1:8084/")

    def test_8_http_with_an_explicit_port(self) -> None:
        for url in ("https://127.0.0.1:8085", "http://127.0.0.1", "127.0.0.1:8085", "http://127.0.0.1:port"):
            with self.subTest(url=url):
                self.refused("The address of pl-290 needs http and an explicit port.", joining_lma_urls=f"pl-290={url}")

    def test_9_compose_needs_the_container_name(self) -> None:
        self.refused(
            "In compose mode the address of pl-290 must be http://lma-pl-290:<port>.",
            deployer="compose",
            joining_lma_urls="pl-290=http://127.0.0.1:8085",
            coordinator_url="http://healthcare-demo-backend:8081",
        )

    def test_10_compose_needs_the_apps_address_on_the_network(self) -> None:
        for host in ("127.0.0.1", "localhost"):
            with self.subTest(host=host):
                self.refused(
                    "In compose mode set HEALTHCARE_DEMO_COORDINATOR_URL to the app's internal address on the "
                    "Compose network, such as http://healthcare-demo-backend:8081.",
                    deployer="compose",
                    joining_lma_urls="pl-290=http://lma-pl-290:8085",
                    coordinator_url=f"http://{host}:8081",
                )

    def test_11_process_mode_stays_on_this_machine(self) -> None:
        self.refused(
            "In process mode the address of pl-290 must be on 127.0.0.1 or localhost.",
            joining_lma_urls="pl-290=http://lma-pl-290:8085",
        )

    def test_the_lma_list_refuses_a_joining_hospital(self) -> None:
        from app.services.roles import build_app_consortium

        with self.assertRaises(ValueError) as caught:
            build_app_consortium(self.settings(lma_urls=f"{self.LMA_URLS},pl-290=http://127.0.0.1:8086"))
        self.assertEqual(
            "pl-290 joins at run time. List it in HEALTHCARE_DEMO_JOINING_LMA_URLS, not HEALTHCARE_DEMO_LMA_URLS.",
            str(caught.exception),
        )

    def test_the_app_does_not_start_with_a_refused_setting(self) -> None:
        from tests.split_support import load_main

        with self.assertRaises(DeployerSettingError):
            load_main("combined", deployer="process")
        with mock.patch.dict(os.environ, {"RENDER": "true"}):
            with self.assertRaises(DeployerSettingError):
                load_main("app", deployer="process")



class CombinedRoleTests(unittest.TestCase):
    """The hosted demo runs the combined role. It lists the joining hospital and never adds it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main.app)

    def test_the_view_says_why_and_lists_the_hospital(self) -> None:
        response = self.client.get("/api/v1/deployments")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            {
                "available": False,
                "reason": REASON_NOT_APP,
                "applier": "off",
                "hospitals": [
                    {
                        "site_id": "pl-290",
                        "display_name": "Hospital 290",
                        "country": "Poland",
                        "records": 263,
                        "records_source": "dataset",
                        "in_consortium": False,
                        "service": "unknown",
                    }
                ],
                "deployments": [],
            },
            response.json(),
        )
        self.assertIn('"Add a hospital while the demo runs"', REASON_NOT_APP)

    def test_the_post_is_refused_with_the_reason(self) -> None:
        response = self.client.post("/api/v1/deployments", json={"site_id": "pl-290"})
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual({"detail": REASON_NOT_APP}, response.json())
        self.assertFalse(main.pilot_flight.busy)

    def test_an_unknown_hospital_is_a_422_in_the_apps_shape(self) -> None:
        cases = {
            "uk-134": {"site_id": "must be one of pl-290"},
            "xx-999": {"site_id": "must be one of pl-290"},
        }
        for site_id, errors in cases.items():
            with self.subTest(site_id=site_id):
                response = self.client.post("/api/v1/deployments", json={"site_id": site_id})
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual({"detail": "Some fields are not valid.", "errors": errors}, response.json())
        response = self.client.post("/api/v1/deployments", json={"site_id": "pl-290", "image": "x"})
        self.assertEqual(422, response.status_code, response.text)
        self.assertIn("image", response.json()["errors"])

    def test_the_api_gains_exactly_two_operations(self) -> None:
        from fastapi.openapi.utils import get_openapi

        def operations(routes: list) -> set[tuple[str, str]]:
            document = get_openapi(title="t", version="v", routes=routes)
            return {(path, method) for path, item in document["paths"].items() for method in item}

        routes = list(main.app.routes)
        without = [route for route in routes if getattr(route, "path", None) != "/api/v1/deployments"]
        self.assertEqual(
            {("/api/v1/deployments", "get"), ("/api/v1/deployments", "post")},
            operations(routes) - operations(without),
        )
        self.assertEqual(2, len(routes) - len(without))

    def test_the_pilots_409_is_the_one_existing_operation_that_changed(self) -> None:
        # A pilot and an addition share one flight, so the pilot can now be
        # refused while a hospital is being added. Its 409 says so; this is a
        # deliberate change to an operation the combined API already had.
        pilot = main.app.openapi()["paths"]["/api/v1/pilot:run"]["post"]
        self.assertEqual(
            "A pilot is already running, or a hospital is being added. One of them runs at a time.",
            pilot["responses"]["409"]["description"],
        )

    def test_the_joining_hospital_has_no_pages_here(self) -> None:
        self.assertEqual(404, self.client.get("/api/v1/hospitals/pl-290").status_code)
        sites = [row["site_id"] for row in self.client.get("/api/v1/hospitals").json()]
        self.assertNotIn("pl-290", sites)


class OriginGuardTests(unittest.TestCase):
    """Only a page on this machine, or a client that sends no Origin, may ask to add a hospital."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(main.app)

    def post(self, origin: str | None) -> dict:
        headers = {} if origin is None else {"Origin": origin}
        response = self.client.post("/api/v1/deployments", json={"site_id": "pl-290"}, headers=headers)
        self.assertEqual(403, response.status_code, response.text)
        return response.json()

    def test_another_host_is_refused(self) -> None:
        for origin in (
            "http://192.168.1.20:5174",
            "https://example.github.io",
            "http://localhost.example.org",
            "http://127.0.0.1.example.org:5174",
        ):
            with self.subTest(origin=origin):
                self.assertEqual({"detail": FOREIGN_ORIGIN_MESSAGE}, self.post(origin))

    def test_a_null_origin_is_refused(self) -> None:
        self.assertEqual({"detail": "Adding a hospital is accepted only from this machine."}, self.post("null"))

    def test_this_machine_passes_the_guard(self) -> None:
        # The combined role then refuses for its own reason, which shows that the guard let it through.
        for origin in ("http://localhost:5174", "http://127.0.0.1:5174", "http://[::1]:5174", None):
            with self.subTest(origin=origin):
                self.assertEqual({"detail": REASON_NOT_APP}, self.post(origin))

    def test_the_guard_is_checked_before_the_body(self) -> None:
        response = self.client.post(
            "/api/v1/deployments", json={"site_id": "xx-999"}, headers={"Origin": "http://192.168.1.20:5174"}
        )
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual({"detail": FOREIGN_ORIGIN_MESSAGE}, response.json())

    def test_the_view_has_no_guard(self) -> None:
        response = self.client.get("/api/v1/deployments", headers={"Origin": "http://192.168.1.20:5174"})
        self.assertEqual(200, response.status_code, response.text)


class FakeCore:
    def __init__(self) -> None:
        self.up = True
        self.health_checks = 0

    def health(self) -> bool:
        self.health_checks += 1
        return self.up

    def render_compose(self, request: dict) -> dict:
        from app.services.remote import RemoteServiceUnavailable

        raise RemoteServiceUnavailable("The Dagents core service cannot be reached right now.", "test")


class FakeGma:
    def __init__(self) -> None:
        self.up = True
        self.joinable = ["pl-290"]

    def list_site_addresses(self):
        from agents.common.domain.federation import SiteAddressList
        from app.services.remote import RemoteServiceUnavailable

        if not self.up:
            raise RemoteServiceUnavailable("The Dagents GMA cannot be reached right now.", "test")
        return SiteAddressList(joinable=self.joinable, addresses=[])


class FakeApplier:
    name = "process"

    def status(self, service: str, *, project: str) -> str:
        return "not_started"

    def close(self) -> None:
        pass


class AvailabilityTests(unittest.TestCase):
    """Why adding a hospital is not available: the first rule that fails wins."""

    def setUp(self) -> None:
        self.now = 1000.0
        self.core = FakeCore()
        self.gma = FakeGma()

    def deployer(self, **overrides: object):
        values: dict[str, object] = {
            "role": "app",
            "deployer": "process",
            "joining_lma_urls": "pl-290=http://127.0.0.1:8085",
        }
        values.update(overrides)
        return build_hospital_deployer(
            dataclasses.replace(main.settings, **values),
            consortium=mock.Mock(hospitals={}),
            forwarder=None,
            gma=self.gma,
            core=self.core,
            applier=FakeApplier(),  # type: ignore[arg-type]
            clock=lambda: self.now,
        )

    def test_each_reason_in_order(self) -> None:
        self.core.up = False
        self.gma.up = False
        self.gma.joinable = []
        steps = [
            (REASON_NOT_APP, {"role": "combined"}, None),
            (REASON_NOT_APP, {"role": "lma", "deployer": "off"}, None),
            (REASON_OFF, {"deployer": "off"}, None),
            (REASON_NO_CORE, {}, lambda: None),
            (REASON_NO_GMA, {}, lambda: setattr(self.core, "up", True)),
            (REASON_NOT_JOINABLE.format(site_id="pl-290"), {}, lambda: setattr(self.gma, "up", True)),
            (None, {}, lambda: setattr(self.gma, "joinable", ["pl-290"])),
        ]
        for reason, overrides, change in steps:
            with self.subTest(reason=reason):
                if change is not None:
                    change()
                # A GMA that does not answer is logged as a warning.
                with mock.patch("app.services.deployment.logger"):
                    view = self.deployer(**overrides).view()
                self.assertEqual(reason, view.reason)
                self.assertEqual(reason is None, view.available)
        self.assertEqual(
            "The GMA does not accept an address for pl-290. Start the GMA with GMA_FEDERATION_JOINABLE_SITES naming it.",
            REASON_NOT_JOINABLE.format(site_id="pl-290"),
        )

    def test_off_and_other_roles_have_no_applier_and_check_nothing(self) -> None:
        for overrides in ({"deployer": "off"}, {"role": "combined"}, {"role": "lma"}):
            with self.subTest(**overrides):
                deployer = self.deployer(**overrides)
                view = deployer.view()
                self.assertEqual("off", view.applier)
                self.assertEqual(["unknown"], [hospital.service for hospital in view.hospitals])
        self.assertEqual(0, self.core.health_checks)

    def test_the_checks_are_reused_for_15_seconds(self) -> None:
        self.assertEqual(15, AVAILABILITY_CACHE_SECONDS)
        deployer = self.deployer()
        self.assertTrue(deployer.view().available)
        self.core.up = False
        self.now += AVAILABILITY_CACHE_SECONDS - 0.1
        self.assertTrue(deployer.view().available)
        self.assertEqual(1, self.core.health_checks)
        self.now += 0.1
        self.assertEqual(REASON_NO_CORE, deployer.view().reason)
        self.assertEqual(2, self.core.health_checks)

    def test_a_post_clears_the_cache(self) -> None:
        deployer = self.deployer()
        self.assertTrue(deployer.view().available)
        self.gma.joinable = []
        with mock.patch.object(main, "deployer", deployer):
            client = TestClient(main.app)
            # Refused before it starts anything, and then the next view checks again.
            with self.assertLogs("app.services.deployment", "WARNING"):
                response = client.post("/api/v1/deployments", json={"site_id": "pl-290"})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("failed", response.json()["status"])
            self.assertEqual("The core service did not answer.", response.json()["steps"][0]["detail"])
            view = client.get("/api/v1/deployments").json()
        self.assertEqual(REASON_NOT_JOINABLE.format(site_id="pl-290"), view["reason"])
        self.assertEqual(2, self.core.health_checks)

    def test_a_refused_post_also_clears_the_cache(self) -> None:
        deployer = self.deployer()
        self.core.up = False
        self.assertEqual(REASON_NO_CORE, deployer.view().reason)
        self.core.up = True
        with mock.patch.object(main, "deployer", deployer):
            client = TestClient(main.app)
            response = client.post("/api/v1/deployments", json={"site_id": "pl-290"})
            self.assertEqual(403, response.status_code, response.text)
            self.assertEqual({"detail": REASON_NO_CORE}, response.json())
            self.assertTrue(client.get("/api/v1/deployments").json()["available"])

    def test_the_view_lists_every_joining_hospital_from_the_dataset_module(self) -> None:
        view = self.deployer().view()
        self.assertEqual([site.site_id for site in JOINING_SITES], [hospital.site_id for hospital in view.hospitals])
        self.assertEqual(["not_started"], [hospital.service for hospital in view.hospitals])
        self.assertEqual("process", view.applier)


class LmaRoleTests(unittest.TestCase):
    """A hospital's LMA answers 404 for both routes: only the app adds hospitals."""

    def test_both_routes_are_404(self) -> None:
        from tests.split_support import load_main

        lma = load_main("lma", site_id="uk-134")
        try:
            client = TestClient(lma.app)
            self.assertEqual(404, client.get("/api/v1/deployments").status_code)
            self.assertEqual(404, client.post("/api/v1/deployments", json={"site_id": "pl-290"}).status_code)
            self.assertEqual(200, client.get("/api/v1/health").status_code)
        finally:
            lma.consortium.close()


if __name__ == "__main__":
    unittest.main()
