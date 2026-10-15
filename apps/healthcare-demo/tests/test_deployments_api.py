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
from app.api.limits import SingleFlight  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.services.deployment import DeployerSettingError, check_deployer  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
