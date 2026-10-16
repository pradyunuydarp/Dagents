"""The routes that add a hospital while the demo runs, their settings, and the flight they share with the pilot.

A pilot and the addition of a hospital share one :class:`SingleFlight`, so the
two never run at the same time, and a refused request says which one runs.

Adding a hospital starts a process or a container, so the app refuses to start
with a deploy setting that could let a visitor of a public demo start one
(:func:`check_deployer`), and the POST is accepted only from a page on this
machine. The hosted demo runs the agents in process, so its view says why it
cannot add a hospital. None of this needs the planner or a database.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1]).resolve()
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(FRAMEWORK_ROOT))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agents.common.domain.federation import SiteAddressList, SiteAddressState  # noqa: E402
from agents.common.infrastructure.agent_client import (  # noqa: E402
    GMA_AGENT,
    AgentNetwork,
    AgentUnavailableError,
)

import app.main as main  # noqa: E402
from app.api.deploy_routes import FOREIGN_ORIGIN_MESSAGE  # noqa: E402
from app.api.limits import SingleFlight  # noqa: E402
from app.core.config import Settings  # noqa: E402
from app.domain.dataset import JOINING_SITES, SITES  # noqa: E402
from app.services.deployment import (  # noqa: E402
    AVAILABILITY_CACHE_SECONDS,
    REASON_IN_PROCESS,
    REASON_NO_CORE,
    REASON_NO_GMA,
    REASON_NOT_JOINABLE,
    REASON_OFF,
    DeployerSettingError,
    build_hospital_deployer,
    check_deployer,
)

from tests.api_support import NO_LIMITS, ApiTestCase, default_limits  # noqa: E402

PL_URL = "HEALTHCARE_DEMO_LMA_PL_290_URL"
PL_TOKEN = "HEALTHCARE_DEMO_LMA_PL_290_TOKEN"
SECRET = "a-token-that-must-never-be-shown"
DATABASE = "postgresql://demo-user:demo-password@db.example.invalid:5432/demo"


def env_folder(test: unittest.TestCase, mode: int = 0o600) -> Path:
    """A new folder holding ``lma-pl-290.env`` with ``mode``. Removed after the test."""
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    path = Path(folder.name) / "lma-pl-290.env"
    path.write_text(f"LMA_SITE_TOKEN={SECRET}\n", encoding="utf-8")
    path.chmod(mode)
    return Path(folder.name)


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
    """The pilot route names what holds the flight. It is refused before it uses an agent."""

    @classmethod
    def setUpClass(cls) -> None:
        # The pilot route's per-client limit would answer 429 before the flight's 409.
        main.rate_limits.configure(NO_LIMITS)
        cls.addClassCleanup(main.rate_limits.configure, default_limits())
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

    def setUp(self) -> None:
        self.folder = env_folder(self)

    def environ(self, **changes: str | None) -> dict[str, str]:
        values: dict[str, str | None] = {
            "HEALTHCARE_DEMO_LMA_UK_134_URL": "http://127.0.0.1:8082",
            "HEALTHCARE_DEMO_LMA_IT_002_URL": "http://127.0.0.1:8083",
            "HEALTHCARE_DEMO_LMA_CH_245_URL": "http://127.0.0.1:8084",
            PL_URL: "http://127.0.0.1:8085",
            PL_TOKEN: SECRET,
        }
        values.update(changes)
        return {name: value for name, value in values.items() if value is not None}

    def compose_environ(self, **changes: str | None) -> dict[str, str]:
        values: dict[str, str | None] = {
            "HEALTHCARE_DEMO_LMA_UK_134_URL": "http://lma-uk-134:8082",
            "HEALTHCARE_DEMO_LMA_IT_002_URL": "http://lma-it-002:8083",
            "HEALTHCARE_DEMO_LMA_CH_245_URL": "http://lma-ch-245:8084",
            PL_URL: "http://lma-pl-290:8085",
            PL_TOKEN: SECRET,
        }
        values.update(changes)
        return {name: value for name, value in values.items() if value is not None}

    def settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "agents_mode": "network",
            "deployer": "process",
            "database_url": DATABASE,
            "api_host": "127.0.0.1",
            "api_port": 8080,
            "gma_url": "http://127.0.0.1:8081",
            "core_service_url": "http://127.0.0.1:8040",
            "deploy_dir": "",
            "deploy_env_dir": str(self.folder),
        }
        values.update(overrides)
        return dataclasses.replace(main.settings, **values)

    def compose_settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "deployer": "compose",
            "api_host": "0.0.0.0",
            "gma_url": "http://gma:8081",
            "core_service_url": "http://core-service:8040",
        }
        values.update(overrides)
        return self.settings(**values)

    def refused(self, message: str, settings: Settings, environ: dict[str, str]) -> None:
        with self.assertRaises(DeployerSettingError) as caught:
            check_deployer(settings, environ)
        self.assertEqual(message, str(caught.exception))
        self.assertIsInstance(caught.exception, ValueError)
        # A refusal names variables, never a value that could be a secret.
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn("demo-password", str(caught.exception))

    def test_process_and_compose_settings_that_work(self) -> None:
        check_deployer(self.settings(), self.environ())
        check_deployer(self.settings(api_host="localhost"), self.environ(**{PL_URL: "http://localhost:8085"}))
        check_deployer(self.settings(deploy_env_dir="", deploy_dir=str(self.folder)), self.environ())
        check_deployer(self.compose_settings(), self.compose_environ())

    def test_1_an_unknown_value(self) -> None:
        self.refused(
            "HEALTHCARE_DEMO_DEPLOYER must be off, process or compose, not 'docker'.",
            self.settings(deployer="docker"),
            self.environ(),
        )

    def test_2_off_needs_nothing_else(self) -> None:
        for mode in ("in-process", "network"):
            with self.subTest(mode=mode):
                check_deployer(
                    self.settings(deployer="off", agents_mode=mode, database_url="", core_service_url=""),
                    {"RENDER": "true"},
                )

    def test_the_setting_is_read_stripped_and_in_lower_case(self) -> None:
        with mock.patch.dict(os.environ, {"HEALTHCARE_DEMO_DEPLOYER": "  Process "}):
            self.assertEqual("process", Settings().deployer)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HEALTHCARE_DEMO_DEPLOYER", None)
            self.assertEqual("off", Settings().deployer)

    def test_3_only_with_the_agents_in_their_own_processes(self) -> None:
        for deployer in ("process", "compose"):
            with self.subTest(deployer=deployer):
                self.refused(
                    f"HEALTHCARE_DEMO_DEPLOYER={deployer} needs HEALTHCARE_DEMO_AGENTS=network. Agents inside the "
                    "API process cannot reach a new LMA process.",
                    self.settings(deployer=deployer, agents_mode="in-process"),
                    self.environ(),
                )

    def test_4_never_on_render(self) -> None:
        for deployer in ("process", "compose"):
            with self.subTest(deployer=deployer):
                self.refused(
                    "HEALTHCARE_DEMO_DEPLOYER must be off on Render. The public demo never starts a process or a "
                    "container.",
                    self.settings(deployer=deployer),
                    {**self.environ(), "RENDER": "true"},
                )

    def test_5_a_database(self) -> None:
        self.refused(
            "HEALTHCARE_DEMO_DEPLOYER=process needs HEALTHCARE_DEMO_DATABASE_URL. A new hospital's LMA reads its "
            "records from its own schema.",
            self.settings(database_url=" "),
            self.environ(),
        )

    def test_6_each_joining_hospital_has_an_address_and_a_token(self) -> None:
        for variable in (PL_URL, PL_TOKEN):
            for value in (None, "  "):
                with self.subTest(variable=variable, value=value):
                    self.refused(
                        f"HEALTHCARE_DEMO_DEPLOYER=process needs {variable}.",
                        self.settings(),
                        self.environ(**{variable: value}),
                    )

    def test_7_http_with_a_host_and_an_explicit_port(self) -> None:
        for url in (
            "https://127.0.0.1:8085",
            "http://127.0.0.1",
            "127.0.0.1:8085",
            "http://127.0.0.1:port",
            "http://127.0.0.1:8085/lma",
            "http://user@127.0.0.1:8085",
        ):
            with self.subTest(url=url):
                self.refused(
                    f"{PL_URL} needs http, a host and an explicit port, and nothing else.",
                    self.settings(),
                    self.environ(**{PL_URL: url}),
                )

    def test_7_an_address_another_agent_or_the_api_uses(self) -> None:
        message = f"{PL_URL} uses the host and port of the GMA, an LMA or the API."
        for url in ("http://127.0.0.1:8081", "http://127.0.0.1:8084", "http://localhost:8082", "http://127.0.0.1:8080"):
            with self.subTest(url=url):
                self.refused(message, self.settings(), self.environ(**{PL_URL: url}))
        # On the Compose network an agent's host is its container's name.
        self.refused(message, self.compose_settings(), self.compose_environ(**{PL_URL: "http://lma-uk-134:8082"}))
        self.refused(message, self.compose_settings(), self.compose_environ(**{PL_URL: "http://gma:8081"}))

    def test_8_process_mode_stays_on_this_machine(self) -> None:
        self.refused(
            f"In process mode {PL_URL} must be on 127.0.0.1 or localhost.",
            self.settings(),
            self.environ(**{PL_URL: "http://lma-pl-290:8085"}),
        )
        for host in ("0.0.0.0", "192.168.1.20", "::"):
            with self.subTest(host=host):
                self.refused(
                    "In process mode HEALTHCARE_DEMO_API_HOST must be 127.0.0.1 or localhost, so that only this "
                    "machine can ask the API to start a process.",
                    self.settings(api_host=host),
                    self.environ(),
                )

    def test_9_compose_mode_needs_the_containers_name(self) -> None:
        self.refused(
            f"In compose mode {PL_URL} must be http://lma-pl-290:<port>, the container's name on the Compose "
            "network.",
            self.compose_settings(),
            self.compose_environ(**{PL_URL: "http://127.0.0.1:8085"}),
        )

    def test_10_core_service(self) -> None:
        self.refused(
            "HEALTHCARE_DEMO_DEPLOYER=process needs HEALTHCARE_DEMO_CORE_SERVICE_URL.",
            self.settings(core_service_url=""),
            self.environ(),
        )

    def test_11_the_env_file(self) -> None:
        self.refused(
            "HEALTHCARE_DEMO_DEPLOY_ENV_DIR or HEALTHCARE_DEMO_DEPLOY_DIR must name the folder that holds each new "
            "LMA's env file.",
            self.settings(deploy_env_dir="", deploy_dir=""),
            self.environ(),
        )
        self.refused(
            "The env folder in HEALTHCARE_DEMO_DEPLOY_ENV_DIR or HEALTHCARE_DEMO_DEPLOY_DIR does not exist.",
            self.settings(deploy_env_dir=str(self.folder / "missing")),
            self.environ(),
        )
        (self.folder / "lma-pl-290.env").unlink()
        self.refused(
            'The env folder has no lma-pl-290.env. The start script writes it; see "Add a hospital while the '
            'demo runs" in the demo\'s README.',
            self.settings(),
            self.environ(),
        )
        for mode in (0o640, 0o604, 0o644):
            with self.subTest(mode=oct(mode)):
                folder = env_folder(self, mode)
                self.refused(
                    "lma-pl-290.env can be read by other users. Make it readable by its owner only (chmod 600).",
                    self.settings(deploy_env_dir=str(folder)),
                    self.environ(),
                )

    def test_the_app_does_not_start_with_a_refused_setting(self) -> None:
        """``app.main`` checks the setting when it is imported, before it serves a request."""
        backend = APP_ROOT / "backend"
        base = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith("HEALTHCARE_DEMO_") and name != "RENDER"
        }
        base["PYTHONPATH"] = os.pathsep.join([str(FRAMEWORK_ROOT), str(backend)])
        cases = {
            "in process": {"HEALTHCARE_DEMO_DEPLOYER": "process"},
            "on Render": {"HEALTHCARE_DEMO_DEPLOYER": "compose", "HEALTHCARE_DEMO_AGENTS": "network", "RENDER": "1"},
            "unknown": {"HEALTHCARE_DEMO_DEPLOYER": "docker"},
        }
        for name, changes in cases.items():
            with self.subTest(case=name):
                result = subprocess.run(
                    [sys.executable, "-c", "import app.main"],
                    cwd=backend,
                    env={**base, **changes},
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                self.assertNotEqual(0, result.returncode, result.stderr[-2000:])
                self.assertIn("DeployerSettingError", result.stderr)
        result = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=backend,
            env={**base, "HEALTHCARE_DEMO_DEPLOYER": "off"},
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(0, result.returncode, result.stderr[-2000:])


class InProcessViewTests(ApiTestCase):
    """The hosted demo runs the agents in process. It lists the joining hospital and never adds it."""

    def test_the_view_says_why_and_lists_the_hospital(self) -> None:
        response = self.client.get("/api/v1/deployments")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            {
                "available": False,
                "reason": REASON_IN_PROCESS,
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
                "framework_calls": [],
            },
            response.json(),
        )
        self.assertIn('"Add a hospital while the demo runs"', REASON_IN_PROCESS)

    def test_the_post_is_refused_with_the_reason(self) -> None:
        response = self.client.post("/api/v1/deployments", json={"site_id": "pl-290"})
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual({"detail": REASON_IN_PROCESS}, response.json())
        self.assertFalse(main.pilot_flight.busy)

    def test_an_unknown_hospital_is_a_422_in_the_apps_shape(self) -> None:
        for site_id in ("uk-134", "xx-999"):
            with self.subTest(site_id=site_id):
                response = self.client.post("/api/v1/deployments", json={"site_id": site_id})
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual(
                    {"detail": "Some fields are not valid.", "errors": {"site_id": "must be one of pl-290"}},
                    response.json(),
                )
        for body in ({"site_id": "pl-290", "image": "x"}, {"site_id": "pl-290", "url": "http://127.0.0.1:9"}):
            with self.subTest(body=body):
                response = self.client.post("/api/v1/deployments", json=body)
                self.assertEqual(422, response.status_code, response.text)

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
        # deliberate change to an operation the API already had.
        pilot = main.app.openapi()["paths"]["/api/v1/pilot:run"]["post"]
        self.assertEqual(
            "A pilot is already running, or a hospital is being added. One of them runs at a time.",
            pilot["responses"]["409"]["description"],
        )

    def test_the_joining_hospital_has_no_pages_before_it_joins(self) -> None:
        for path in ("/api/v1/hospitals/pl-290", "/api/v1/hospitals/pl-290/charts", "/api/v1/hospitals/pl-290/audit"):
            with self.subTest(path=path):
                self.assertEqual(404, self.client.get(path).status_code)
        sites = [row["site_id"] for row in self.ok(self.client.get("/api/v1/hospitals"))]
        self.assertEqual([site.site_id for site in SITES], sites)


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
            "not a url",
        ):
            with self.subTest(origin=origin):
                self.assertEqual({"detail": FOREIGN_ORIGIN_MESSAGE}, self.post(origin))

    def test_a_null_origin_is_refused(self) -> None:
        self.assertEqual({"detail": "Adding a hospital is accepted only from this machine."}, self.post("null"))

    def test_this_machine_passes_the_guard(self) -> None:
        # The in-process server then refuses for its own reason, which shows that the guard let it through.
        for origin in ("http://localhost:5174", "http://127.0.0.1:5174", "http://[::1]:5174", None):
            with self.subTest(origin=origin):
                self.assertEqual({"detail": REASON_IN_PROCESS}, self.post(origin))

    def test_the_guard_is_checked_before_the_body(self) -> None:
        response = self.client.post(
            "/api/v1/deployments", json={"site_id": "xx-999"}, headers={"Origin": "http://192.168.1.20:5174"}
        )
        self.assertEqual(403, response.status_code, response.text)
        self.assertEqual({"detail": FOREIGN_ORIGIN_MESSAGE}, response.json())

    def test_the_view_has_no_guard(self) -> None:
        response = self.client.get("/api/v1/deployments", headers={"Origin": "http://192.168.1.20:5174"})
        self.assertEqual(200, response.status_code, response.text)


# --- Fakes for the availability checks ------------------------------------------------


class FakeCore:
    """core-service that answers its health check, or does not, and renders nothing."""

    def __init__(self) -> None:
        self.up = True
        self.health_checks = 0

    def health(self) -> bool:
        self.health_checks += 1
        return self.up

    def render_compose(self, request: dict) -> dict:
        from app.services.core_client import CoreServiceUnavailable

        raise CoreServiceUnavailable("core-service did not answer")

    def close(self) -> None:
        pass


class FakeGma:
    """The GMA's address routes, answered from memory."""

    def __init__(self) -> None:
        self.up = True
        self.joinable = ["pl-290"]
        self.joined: list[str] = []
        self.registered: list[tuple[str, str]] = []

    def site_addresses(self) -> SiteAddressList:
        if not self.up:
            raise AgentUnavailableError(
                service="gma", site_id=None, path="/api/v1/federation/site-addresses", status=None,
                code="unavailable", detail="no answer",
            )
        sites = [SiteAddressState(site_id=site.site_id, source="configured") for site in SITES]
        sites += [SiteAddressState(site_id=site_id, source="joined") for site_id in self.joined]
        return SiteAddressList(joinable=self.joinable, sites=sites)

    def register_site_address(self, site_id: str, url: str) -> SiteAddressState:
        self.registered.append((site_id, url))
        self.joined.append(site_id)
        return SiteAddressState(site_id=site_id, source="joined")


class FakeAgents:
    def __init__(self, gma: object) -> None:
        self._gma = gma

    def gma(self) -> object:
        return self._gma


class FakeDemo:
    """What the deployer reads from the app's services, without agents or stores."""

    def __init__(self, gma: object) -> None:
        self.sites = tuple(SITES)
        self.agents = FakeAgents(gma)
        self.joined_addresses: dict[str, str] = {}

    def has_site(self, site_id: str) -> bool:
        return any(site.site_id == site_id for site in self.sites)

    def joined(self) -> dict[str, str]:
        return dict(self.joined_addresses)


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
        self.demo = FakeDemo(self.gma)
        self.folder = env_folder(self)

    def deployer(self, **overrides: object):
        values: dict[str, object] = {
            "agents_mode": "network",
            "deployer": "process",
            "deploy_env_dir": str(self.folder),
        }
        values.update(overrides)
        return build_hospital_deployer(
            dataclasses.replace(main.settings, **values),
            demo=lambda: self.demo,
            core=self.core,
            applier=FakeApplier(),  # type: ignore[arg-type]
            clock=lambda: self.now,
            environ={PL_URL: "http://127.0.0.1:8085", PL_TOKEN: SECRET},
        )

    def test_each_reason_in_order(self) -> None:
        self.core.up = False
        self.gma.up = False
        self.gma.joinable = []
        steps = [
            (REASON_IN_PROCESS, {"agents_mode": "in-process"}, None),
            (REASON_IN_PROCESS, {"agents_mode": "in-process", "deployer": "off"}, None),
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
            "The GMA does not accept an address for pl-290. Start the GMA with GMA_JOINABLE_SITE_ENDPOINTS naming it.",
            REASON_NOT_JOINABLE.format(site_id="pl-290"),
        )

    def test_off_and_in_process_have_no_applier_and_check_nothing(self) -> None:
        for overrides in ({"deployer": "off"}, {"agents_mode": "in-process"}):
            with self.subTest(**overrides):
                view = self.deployer(**overrides).view()
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
            # It fails at the plan step, and then the next view checks again.
            with self.assertLogs("app.services.deployment", "WARNING"):
                response = client.post("/api/v1/deployments", json={"site_id": "pl-290"})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual("failed", response.json()["status"])
            self.assertEqual("The core service did not answer.", response.json()["steps"][0]["detail"])
            view = client.get("/api/v1/deployments").json()
        self.assertEqual(REASON_NOT_JOINABLE.format(site_id="pl-290"), view["reason"])
        self.assertEqual(2, self.core.health_checks)
        self.assertFalse(main.pilot_flight.busy)

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
        self.assertEqual([False], [hospital.in_consortium for hospital in view.hospitals])
        self.assertEqual("process", view.applier)

    def test_the_view_sends_a_joined_address_the_gma_forgot(self) -> None:
        """A GMA that restarted forgets the addresses it accepted; the app sends them again."""
        self.demo.joined_addresses = {"pl-290": "http://127.0.0.1:8085"}
        deployer = self.deployer()
        self.assertTrue(deployer.view().available)
        self.assertEqual([("pl-290", "http://127.0.0.1:8085")], self.gma.registered)
        # Listed as joined now, so the next check sends nothing.
        self.now += AVAILABILITY_CACHE_SECONDS
        deployer.view()
        self.assertEqual(1, len(self.gma.registered))

    def test_the_deployer_never_shows_its_token(self) -> None:
        deployer = self.deployer()
        self.assertNotIn(SECRET, repr(deployer))
        self.assertNotIn(SECRET, deployer.view().model_dump_json())


class BuiltApplierTests(unittest.TestCase):
    """What the deployer builds from the settings, and what a new LMA process starts from."""

    ENVIRON = {
        "PATH": "/usr/bin",
        "PYTHONPATH": "/srv/dagents",
        "HOME": "/home/demo",
        "HEALTHCARE_DEMO_GMA_TOKEN": SECRET,
        "SUPABASE_DB_URL": DATABASE,
        "LMA_SITE_TOKEN": SECRET,
        "AWS_SECRET_ACCESS_KEY": SECRET,
        PL_URL: "http://127.0.0.1:8085",
        PL_TOKEN: SECRET,
    }

    def deployer(self, deployer: str):
        folder = env_folder(self)
        built = build_hospital_deployer(
            dataclasses.replace(
                main.settings,
                agents_mode="network",
                deployer=deployer,
                deploy_dir=str(folder),
                deploy_env_dir="",
                core_service_url="http://127.0.0.1:1",
            ),
            demo=lambda: FakeDemo(FakeGma()),
            environ=self.ENVIRON,
        )
        self.addCleanup(built.close)
        return built

    def test_a_new_lma_process_starts_from_the_allow_listed_names_only(self) -> None:
        from app.services.deployment import child_environment

        self.assertEqual(
            {"PATH": "/usr/bin", "PYTHONPATH": "/srv/dagents", "HOME": "/home/demo"}, child_environment(self.ENVIRON)
        )

    def test_process_mode_builds_a_process_applier_in_the_framework_checkout(self) -> None:
        from agents.common.infrastructure.compose_apply import LocalProcessApplier
        from app.services.deployment import child_environment, framework_root

        applier = self.deployer("process")._applier
        self.assertIsInstance(applier, LocalProcessApplier)
        self.assertEqual(child_environment(self.ENVIRON), applier._base_env)
        self.assertEqual(framework_root(), applier._cwd)
        self.assertEqual(frozenset({sys.executable}), applier._programs)
        self.assertTrue((framework_root() / "agents" / "lma" / "main.py").is_file())

    def test_compose_mode_builds_a_compose_applier(self) -> None:
        from agents.common.infrastructure.compose_apply import DockerComposeApplier

        self.assertIsInstance(self.deployer("compose")._applier, DockerComposeApplier)

    def test_off_builds_nothing(self) -> None:
        deployer = self.deployer("off")
        self.assertIsNone(deployer._applier)
        self.assertIsNone(deployer._core)


class FrameworkCallsTests(unittest.TestCase):
    """The view's GMA call is listed in its ``framework_calls``, by route, with no address or token."""

    def test_the_view_lists_the_gma_call(self) -> None:
        gma = FastAPI()

        @gma.get("/api/v1/federation/site-addresses")
        def listing() -> dict:
            return {"joinable": ["pl-290"], "sites": [{"site_id": "uk-134", "source": "configured"}]}

        network = AgentNetwork.in_process({GMA_AGENT: gma})
        self.addCleanup(network.close)
        agents = mock.Mock()
        agents.gma.return_value = network.gma(SECRET)
        demo = FakeDemo(None)
        demo.agents = agents
        deployer = build_hospital_deployer(
            dataclasses.replace(
                main.settings, agents_mode="network", deployer="process", deploy_env_dir=str(env_folder(self))
            ),
            demo=lambda: demo,
            core=FakeCore(),
            applier=FakeApplier(),  # type: ignore[arg-type]
            environ={PL_URL: "http://127.0.0.1:8085", PL_TOKEN: SECRET},
        )
        with mock.patch.object(main, "deployer", deployer):
            response = TestClient(main.app).get("/api/v1/deployments")
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertTrue(body["available"], body)
        self.assertEqual(
            [("gma", None, "GET", "/api/v1/federation/site-addresses", 200, "app")],
            [
                (call["service"], call["site_id"], call["method"], call["path"], call["status"], call["via"])
                for call in body["framework_calls"]
            ],
        )
        self.assertNotIn(SECRET, response.text)
        self.assertNotIn("127.0.0.1", response.text)


if __name__ == "__main__":
    unittest.main()
