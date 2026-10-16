"""Adding a hospital to a running split consortium, step by step, in one test process.

The split run is :class:`tests.split_support.SplitRun`: three LMAs, the real
Dagents GMA app and the app, wired through a :class:`HostRouter`. The GMA
accepts Hospital 290's address (its ``GMA_FEDERATION_JOINABLE_SITES``). A
real core-service renders the Compose file, in its own process
(:class:`tests.test_deployment_spec.CoreServiceProcess`). The applier is a
fake: instead of starting a process, it attaches Hospital 290's LMA
(``load_main("lma", site_id="pl-290")``) to the router, at the address the
app was told.

Everything else is the app's own code: the deployer, the route, the
consortium, the forwarding, and the GMA's planner, which decides whether the
next round selects the new hospital.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
import sys
import tempfile
from types import ModuleType
from typing import Any
import unittest
from unittest import mock

import httpx

from tests.split_support import APP_INTERNAL_URL, GMA_URL, LMA_URLS, HostRouter, SplitRun, load_main  # noqa: E402
from tests.support import dagentsc_available  # noqa: E402
from tests.test_deployment_spec import BACKEND_DIR, CoreServiceProcess, free_port  # noqa: E402

from agents.common.application.federation import FederatedRoundController  # noqa: E402
from agents.common.infrastructure.compose_apply import (  # noqa: E402
    AppliedService,
    ComposeApplyError,
    LocalProcessApplier,
)
from agents.common.infrastructure.federation_http import HttpFederationEngine  # noqa: E402

from app.domain import dataset  # noqa: E402
from app.models import DEPLOYMENT_STEPS  # noqa: E402
from app.services.deployment import build_hospital_deployer  # noqa: E402
from app.services.framework_client import CoreServiceClient  # noqa: E402
from app.services.remote import RemoteServiceUnavailable  # noqa: E402
from app.services.roles import build_site_node, round_timeout  # noqa: E402

SITE_ID = "pl-290"
SERVICE = "lma-pl-290"


class RouterTransport(httpx.AsyncBaseTransport):
    """The app's forwarding reaches the hosts of a :class:`HostRouter`, as an ``httpx`` transport."""

    def __init__(self, router: HostRouter) -> None:
        self.router = router

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host in self.router.down or host not in self.router.apps:
            raise httpx.ConnectError(f"cannot connect to {host}", request=request)
        transport = httpx.ASGITransport(app=self.router.apps[host].app)
        return await transport.handle_async_request(request)


class FakeApplier:
    """Starts Hospital 290's LMA in this process, on the router, instead of as a process.

    ``check`` is the real process applier's check, so the file must be one
    that :class:`LocalProcessApplier` could start.
    """

    name = "process"

    def __init__(self, router: HostRouter, url: str, *, attach: bool = True, fail: ComposeApplyError | None = None) -> None:
        self.router = router
        self.url = url
        self.attach = attach
        self.fail = fail
        self.state = "not_started"
        #: False: a running service is recreated, as docker compose does for one
        #: started from an older file or image, instead of kept.
        self.keep = True
        self.checked: list[str] = []
        self.applied: list[tuple[str, str, str, float]] = []
        self.stopped: list[tuple[str, str]] = []
        self.lmas: list[ModuleType] = []
        self._workdir = tempfile.TemporaryDirectory()
        self.log_path = str(Path(self._workdir.name) / f"{SERVICE}.log")

    def check(self, compose_yaml: str, service: str) -> None:
        self.checked.append(compose_yaml)
        LocalProcessApplier(
            Path(self._workdir.name), cwd=BACKEND_DIR, programs={sys.executable}, base_env={}
        ).check(compose_yaml, service)

    def apply(self, compose_yaml: str, service: str, *, project: str, wait_seconds: float) -> AppliedService:
        self.applied.append((compose_yaml, service, project, wait_seconds))
        if self.state == "running":
            if not self.keep:
                return AppliedService(service, self.name, False, "recreated the container", self.log_path)
            return AppliedService(service, self.name, True, "already running as process 4242", self.log_path)
        if self.fail is not None:
            raise self.fail
        if self.attach:
            lma = load_main("lma", site_id=SITE_ID, coordinator_url=APP_INTERNAL_URL)
            lma.consortium = build_site_node(lma.settings, client=self.router)  # type: ignore[arg-type]
            self.lmas.append(lma)
            self.router.attach(self.url, lma.app)
        self.state = "running"
        return AppliedService(service, self.name, False, "started process 4242", self.log_path)

    def status(self, service: str, *, project: str) -> str:
        return self.state

    def stop(self, service: str, *, project: str) -> None:
        self.stopped.append((service, project))
        self.state = "stopped"
        self.router.apps.pop(httpx.URL(self.url).host, None)

    def close(self) -> None:
        for lma in self.lmas:
            lma.consortium.close()
        self._workdir.cleanup()


class RecordingCore(CoreServiceClient):
    """core-service's client, keeping each answer it returned."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.answers: list[dict[str, Any]] = []

    def render_compose(self, request: dict[str, Any]) -> dict[str, Any]:
        answer = super().render_compose(request)
        self.answers.append(answer)
        return answer


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class DeploymentFlowTests(unittest.TestCase):
    core: CoreServiceProcess
    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.core = CoreServiceProcess().start()
        # Nothing listens here, so the deployer's check that the port is free passes at once.
        cls.url = f"http://127.0.0.1:{free_port()}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.core.close()

    def setUp(self) -> None:
        self.split = SplitRun()
        self.addCleanup(self.split.close)
        self.router = self.split.router
        self.app = self.split.app
        # The GMA, started with GMA_FEDERATION_JOINABLE_SITES=pl-290=<url>.
        self.use_gma(self.url)
        # The app's forwarding reaches the LMAs through the router.
        forwarder = self.app.app.state.forwarder
        forwarder._transport = RouterTransport(self.router)
        forwarder._client = None
        self.applier = FakeApplier(self.router, self.url)
        self.addCleanup(self.applier.close)
        self.core_client = RecordingCore(self.core.url, timeout=30)
        self.use_deployer()
        self.client = self.split.app_client

    def use_gma(self, joinable_url: str) -> FederatedRoundController:
        """Give the GMA a new round controller: a restarted GMA that accepts ``joinable_url`` for pl-290."""
        import agents.gma.main as gma_main

        rounds = FederatedRoundController(
            engine=HttpFederationEngine(LMA_URLS, joinable={SITE_ID: joinable_url}, client=self.router)  # type: ignore[arg-type]
        )
        patch = mock.patch.object(gma_main, "rounds", rounds)
        patch.start()
        self.addCleanup(patch.stop)
        self.split.gma_rounds = rounds
        return rounds

    def use_deployer(self, **overrides: Any) -> None:
        settings = dataclasses.replace(
            self.app.settings,
            deployer="process",
            joining_lma_urls=f"{SITE_ID}={self.url}",
            **{"deploy_timeout_seconds": 20.0, **overrides},
        )
        self.app.deployer = build_hospital_deployer(
            settings,
            consortium=self.app.consortium,
            forwarder=self.app.app.state.forwarder,
            gma=self.app.consortium.controller,
            core=self.core_client,
            applier=self.applier,
            client=self.router,  # type: ignore[arg-type]
        )

    # --- helpers ------------------------------------------------------------------

    def deploy(self, status: int = 200) -> dict[str, Any]:
        response = self.client.post("/api/v1/deployments", json={"site_id": SITE_ID})
        self.assertEqual(status, response.status_code, response.text)
        return response.json()

    @staticmethod
    def statuses(deployment: dict[str, Any]) -> dict[str, str]:
        return {step["step"]: step["status"] for step in deployment["steps"]}

    @staticmethod
    def detail(deployment: dict[str, Any], step: str) -> str:
        return next(row["detail"] for row in deployment["steps"] if row["step"] == step)

    def gma_addresses(self) -> dict[str, dict[str, Any]]:
        response = self.split.gma_client.get("/api/v1/federation/site-addresses")
        self.assertEqual(200, response.status_code, response.text)
        return {row["site_id"]: row for row in response.json()["addresses"]}

    def gma_sites(self) -> dict[str, dict[str, Any]]:
        return {
            registration.site_id: registration.model_dump(mode="json")
            for registration in self.split.gma_rounds.list_sites()
        }

    def assert_failed_at(self, deployment: dict[str, Any], step: str, expected: dict[str, str]) -> None:
        self.assertEqual("failed", deployment["status"])
        self.assertEqual(list(DEPLOYMENT_STEPS), [row["step"] for row in deployment["steps"]])
        self.assertEqual(expected, self.statuses(deployment))
        self.assertEqual("failed", self.statuses(deployment)[step])
        self.assertIsNone(deployment["next_round"])
        self.assertNotIn(SITE_ID, self.app.consortium.hospitals)
        self.assertEqual(404, self.client.get(f"/api/v1/hospitals/{SITE_ID}").status_code)

    # --- joining --------------------------------------------------------------------

    def test_a_hospital_joins_and_the_next_round_selects_it(self) -> None:
        view = self.client.get("/api/v1/deployments").json()
        self.assertTrue(view["available"], view)
        self.assertIsNone(view["reason"])
        self.assertEqual("process", view["applier"])
        self.assertEqual([(SITE_ID, False, "not_started")], [(h["site_id"], h["in_consortium"], h["service"]) for h in view["hospitals"]])

        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertEqual({step: "done" for step in DEPLOYMENT_STEPS}, self.statuses(deployment))
        self.assertTrue(deployment["deployment_id"].startswith("dep-"))
        self.assertEqual(16, len(deployment["deployment_id"]))
        self.assertEqual(("lma", SITE_ID, SERVICE, "process"), (deployment["kind"], deployment["site_id"], deployment["service"], deployment["applier"]))
        self.assertFalse(deployment["adopted"])
        self.assertEqual([], deployment["issues"])
        self.assertEqual({"selected": True, "reason": None}, deployment["next_round"])
        self.assertEqual("core-service rendered the Compose file for lma-pl-290.", self.detail(deployment, "plan"))
        self.assertEqual("Started process 4242.", self.detail(deployment, "start"))
        self.assertEqual(f"The GMA accepted {self.url} for pl-290 (joined).", self.detail(deployment, "attach"))
        self.assertEqual("Enrolled with 263 records.", self.detail(deployment, "enrol"))
        self.assertEqual("Hospital 290 is in the consortium. Its pages now go to its LMA.", self.detail(deployment, "join"))
        self.assertEqual("The next round selects Hospital 290.", self.detail(deployment, "check"))
        self.assertEqual(
            [("lma-pl-290", "healthcare-demo-lma-pl-290", 20.0)], [entry[1:] for entry in self.applier.applied]
        )

        # The app: four hospitals, and Hospital 290's pages come from its LMA.
        hospitals = self.client.get("/api/v1/hospitals").json()
        self.assertEqual([*LMA_URLS, SITE_ID], [row["site_id"] for row in hospitals])
        dashboard = self.client.get(f"/api/v1/hospitals/{SITE_ID}")
        self.assertEqual(200, dashboard.status_code, dashboard.text)
        self.assertEqual(SITE_ID, dashboard.json()["site_id"])
        self.assertEqual(263, dashboard.json()["counts"]["total"])
        # Where the records are stored is reported for the configured hospitals only.
        status = self.client.get("/api/v1/framework/status").json()
        self.assertEqual(list(LMA_URLS), [row["site_id"] for row in status["hospitals"]])
        # The GMA: the address it accepted, and the site enrolled.
        self.assertEqual({"site_id": SITE_ID, "url": self.url, "source": "joined"}, self.gma_addresses()[SITE_ID])
        self.assertTrue(self.gma_sites()[SITE_ID]["enrolled"])
        self.assertEqual(263, self.gma_sites()[SITE_ID]["cohort_size"])

        view = self.client.get("/api/v1/deployments").json()
        self.assertEqual([(SITE_ID, True, "running")], [(h["site_id"], h["in_consortium"], h["service"]) for h in view["hospitals"]])
        self.assertEqual([deployment["deployment_id"]], [row["deployment_id"] for row in view["deployments"]])

        # A pilot includes the new hospital.
        response = self.client.post("/api/v1/pilot:run", json={"round_prefix": "ui"})
        self.assertEqual(200, response.status_code, response.text)
        report = response.json()
        analytics = report["rounds"]["analytics"]
        self.assertIn(SITE_ID, analytics["plan"]["selected_sites"])
        self.assertIn((SITE_ID, "completed"), [(row["site_id"], row["participation"]) for row in analytics["results"]])
        for training in report["rounds"]["training"]:
            self.assertIn(SITE_ID, training["plan"]["selected_sites"])
        self.assertIn(SITE_ID, report["site_audits"])

    def test_the_applier_gets_the_exact_file_core_service_returned(self) -> None:
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertEqual(1, len(self.core_client.answers))
        returned = self.core_client.answers[0]["compose_yaml"]
        self.assertIs(returned, self.applier.checked[0])
        self.assertIs(returned, self.applier.applied[0][0])
        self.assertEqual(returned, deployment["compose_yaml"])

    def test_the_round_timeout_grows_with_the_hospital_that_joined(self) -> None:
        consortium = self.app.consortium
        before = consortium.controller.round_timeout_for(consortium.manifest("r-before", "analytics"))
        self.assertEqual("joined", self.deploy()["status"])
        manifest = consortium.manifest("r-after", "analytics")
        self.assertEqual(sorted([*LMA_URLS, SITE_ID]), sorted(manifest.invited_sites))
        after = consortium.controller.round_timeout_for(manifest)
        timeout = self.app.settings.federation_timeout_seconds
        self.assertEqual(round_timeout(timeout, 3), before)
        self.assertEqual(round_timeout(timeout, 4), after)
        self.assertGreater(after, before)

    def test_a_restarted_gma_learns_the_address_and_the_enrolment_again(self) -> None:
        self.assertEqual("joined", self.deploy()["status"])
        # The GMA restarts: it knows only its configured sites again.
        restarted = self.use_gma(self.url)
        self.assertNotIn(SITE_ID, self.gma_addresses())
        self.assertEqual([], restarted.list_sites())
        report = self.app.consortium.run_round("r-after-restart", "analytics")
        self.assertEqual({"site_id": SITE_ID, "url": self.url, "source": "joined"}, self.gma_addresses()[SITE_ID])
        self.assertTrue(self.gma_sites()[SITE_ID]["enrolled"])
        self.assertIn(SITE_ID, report["plan"]["selected_sites"])
        results = {row["site_id"]: row for row in report["results"]}
        self.assertEqual("completed", results[SITE_ID]["participation"])

    def test_an_address_the_gma_refuses_again_does_not_stop_the_round(self) -> None:
        self.assertEqual("joined", self.deploy()["status"])
        # A GMA restarted with another address for pl-290.
        self.use_gma(f"http://127.0.0.1:{free_port()}")
        with self.assertLogs("agents.gma.main", "WARNING"), self.assertLogs("app.services.consortium", "WARNING") as logs:
            manifest = self.app.consortium.manifest("r-refused-again", "analytics")
        self.assertIn("the GMA did not take the address of pl-290 again", logs.output[0])
        self.assertIn(SITE_ID, manifest.invited_sites)
        self.assertEqual(set(LMA_URLS) | {SITE_ID}, set(self.gma_sites()))

    # --- failures -------------------------------------------------------------------

    def test_the_planner_refuses_the_request(self) -> None:
        self.use_deployer(deploy_image="")
        deployment = self.deploy()
        self.assert_failed_at(
            deployment,
            "plan",
            {"plan": "failed", **{step: "not_run" for step in DEPLOYMENT_STEPS[1:]}},
        )
        self.assertEqual([{"service": SERVICE, "code": "empty_image", "value": None}], deployment["issues"])
        self.assertEqual("The planner refused the request: empty_image for lma-pl-290", self.detail(deployment, "plan"))
        self.assertIsNone(deployment["compose_yaml"])
        self.assertEqual([], self.applier.applied)

    def test_a_file_that_differs_from_the_request_is_never_applied(self) -> None:
        # Whatever answers at CORE_SERVICE_PUBLIC_URL writes the file the applier
        # starts, so each part that decides what runs, or where it listens, must
        # be the one the app asked for.
        import yaml

        real = self.core_client.render_compose

        def tampered(change):
            def render(request: dict[str, Any]) -> dict[str, Any]:
                answer = dict(real(request))
                document = yaml.safe_load(answer["compose_yaml"])
                change(document["services"][SERVICE])
                answer["compose_yaml"] = yaml.safe_dump(document, sort_keys=False)
                return answer

            return render

        changes = {
            "command": lambda service: service.update(command=[sys.executable, "-c", "print('not the LMA')"]),
            "environment": lambda service: service["environment"].update(HEALTHCARE_DEMO_SITE_ID="uk-134"),
            "image": lambda service: service.update(image="example/other:1"),
            "ports": lambda service: service.update(ports=["0.0.0.0:8085:8085"]),
            "healthcheck": lambda service: service["healthcheck"].update(test=["CMD", "sh", "-c", "true"]),
            "networks": lambda service: service.update(networks=["other"]),
            "privileged": lambda service: service.update(privileged=True),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                with mock.patch.object(self.core_client, "render_compose", side_effect=tampered(change)):
                    with self.assertLogs("app.services.deployment", "WARNING") as logs:
                        deployment = self.deploy()
                self.assertIn("does not match the request", logs.output[0])
                self.assert_failed_at(
                    deployment, "plan", {"plan": "failed", **{step: "not_run" for step in DEPLOYMENT_STEPS[1:]}}
                )
                self.assertEqual("The core service returned a file this app cannot use.", self.detail(deployment, "plan"))
                self.assertIsNone(deployment["compose_yaml"])
                self.assertEqual([], self.applier.checked)
                self.assertEqual([], self.applier.applied)

    def test_core_service_that_does_not_answer(self) -> None:
        # Its health check answers, and then it does not render the file.
        unavailable = RemoteServiceUnavailable("The Dagents core service cannot be reached right now.", "test")
        with mock.patch.object(self.core_client, "render_compose", side_effect=unavailable):
            with self.assertLogs("app.services.deployment", "WARNING"):
                deployment = self.deploy()
        self.assert_failed_at(
            deployment, "plan", {"plan": "failed", **{step: "not_run" for step in DEPLOYMENT_STEPS[1:]}}
        )
        self.assertEqual("The core service did not answer.", self.detail(deployment, "plan"))

    def test_a_service_that_does_not_start(self) -> None:
        self.applier.fail = ComposeApplyError("the process exited with status 1", "Traceback ...")
        with self.assertLogs("app.services.deployment", "WARNING") as logs:
            deployment = self.deploy()
        self.assertIn("Traceback ...", logs.output[0])
        self.assert_failed_at(
            deployment,
            "start",
            {"plan": "done", "start": "failed", **{step: "not_run" for step in DEPLOYMENT_STEPS[2:]}},
        )
        self.assertEqual("The process exited with status 1.", self.detail(deployment, "start"))
        self.assertEqual([], self.applier.stopped)

    def test_a_port_that_answers_for_another_service(self) -> None:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen()
            port = held.getsockname()[1]
            self.url = f"http://127.0.0.1:{port}"
            self.use_gma(self.url)
            self.use_deployer()
            deployment = self.deploy()
        self.assert_failed_at(
            deployment,
            "start",
            {"plan": "done", "start": "failed", **{step: "not_run" for step in DEPLOYMENT_STEPS[2:]}},
        )
        self.assertEqual(
            f"Port {port} answers, but not for a service this app started. Stop what is using it, or change "
            "HEALTHCARE_DEMO_JOINING_LMA_URLS.",
            self.detail(deployment, "start"),
        )
        self.assertEqual([], self.applier.applied)

    def test_a_hospital_that_never_answers_is_stopped_again(self) -> None:
        self.applier.attach = False
        self.use_deployer(deploy_timeout_seconds=1.5)
        deployment = self.deploy()
        self.assert_failed_at(
            deployment,
            "answer",
            {"plan": "done", "start": "undone", "answer": "failed", **{step: "not_run" for step in DEPLOYMENT_STEPS[3:]}},
        )
        self.assertEqual(
            f"The new hospital did not answer at /api/v1/health within 1.5 seconds. Its log is {self.applier.log_path}.",
            self.detail(deployment, "answer"),
        )
        self.assertEqual("Stopped the service this deployment started.", self.detail(deployment, "start"))
        self.assertEqual([(SERVICE, "healthcare-demo-lma-pl-290")], self.applier.stopped)

    def test_a_running_service_that_does_not_answer_is_replaced(self) -> None:
        # The applier still runs the hospital's process, but it does not answer,
        # for example because it hangs. Adopting it would wait the whole timeout
        # and then keep it, because a rollback leaves an adopted service alone.
        self.applier.state = "running"
        self.use_deployer(deploy_timeout_seconds=3.0)
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertFalse(deployment["adopted"])
        self.assertEqual([(SERVICE, "healthcare-demo-lma-pl-290")], self.applier.stopped)
        self.assertEqual(
            "The service ran but did not answer, so the app stopped it. Started process 4242.",
            self.detail(deployment, "start"),
        )

    def test_a_running_service_that_answers_is_kept(self) -> None:
        self.assertEqual("joined", self.deploy()["status"])
        # The hospital leaves the consortium in the app only, as after a failed
        # join, while its LMA keeps running and answering.
        consortium = self.app.consortium
        consortium.hospitals = {key: value for key, value in consortium.hospitals.items() if key != SITE_ID}
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertTrue(deployment["adopted"])
        self.assertEqual("Already running; kept.", self.detail(deployment, "start"))
        self.assertEqual([], self.applier.stopped)

    def test_a_running_service_the_applier_recreates_is_not_kept(self) -> None:
        self.assertEqual("joined", self.deploy()["status"])
        consortium = self.app.consortium
        consortium.hospitals = {key: value for key, value in consortium.hospitals.items() if key != SITE_ID}
        self.applier.keep = False
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertFalse(deployment["adopted"])
        self.assertEqual("Recreated the container.", self.detail(deployment, "start"))

    def test_a_refused_address_stops_the_service(self) -> None:
        # The GMA lists pl-290 as joinable, at another address.
        self.use_gma(f"http://127.0.0.1:{free_port()}")
        with self.assertLogs("agents.gma.main", "WARNING") as logs:
            deployment = self.deploy()
        self.assertIn("refused an address for site pl-290: address_differs", logs.output[0])
        self.assert_failed_at(
            deployment,
            "attach",
            {
                "plan": "done",
                "start": "undone",
                "answer": "done",
                "attach": "failed",
                **{step: "not_run" for step in DEPLOYMENT_STEPS[4:]},
            },
        )
        self.assertEqual(
            "This GMA accepts site pl-290 only at the address in GMA_FEDERATION_JOINABLE_SITES.",
            self.detail(deployment, "attach"),
        )
        self.assertEqual([(SERVICE, "healthcare-demo-lma-pl-290")], self.applier.stopped)
        self.assertNotIn(SITE_ID, self.gma_addresses())

    def test_an_enrolment_that_fails_keeps_the_address_and_stops_the_service(self) -> None:
        controller = self.app.consortium.controller
        failing = RemoteServiceUnavailable("The Dagents GMA cannot be reached right now.", "test")
        with mock.patch.object(controller, "register_site", side_effect=failing):
            deployment = self.deploy()
        self.assert_failed_at(
            deployment,
            "enrol",
            {
                "plan": "done",
                "start": "undone",
                "answer": "done",
                "attach": "done",
                "enrol": "failed",
                "join": "not_run",
                "check": "not_run",
            },
        )
        self.assertEqual("The Dagents GMA cannot be reached right now.", self.detail(deployment, "enrol"))
        # There is no route to remove an address; without an enrolment no round uses it.
        self.assertEqual("joined", self.gma_addresses()[SITE_ID]["source"])
        self.assertNotIn(SITE_ID, self.gma_sites())

    def test_a_join_that_fails_withdraws_the_enrolment(self) -> None:
        with mock.patch.object(self.app.consortium, "add_hospital", side_effect=RuntimeError("no room")):
            with self.assertLogs("app.services.deployment", "ERROR"):
                deployment = self.deploy()
        self.assert_failed_at(
            deployment,
            "join",
            {
                "plan": "done",
                "start": "undone",
                "answer": "done",
                "attach": "done",
                "enrol": "undone",
                "join": "failed",
                "check": "not_run",
            },
        )
        self.assertEqual("Enrolment withdrawn.", self.detail(deployment, "enrol"))
        self.assertEqual("The step failed: RuntimeError: no room", self.detail(deployment, "join"))
        self.assertFalse(self.gma_sites()[SITE_ID]["enrolled"])
        self.assertEqual([(SERVICE, "healthcare-demo-lma-pl-290")], self.applier.stopped)

    def test_a_hospital_the_planner_does_not_select_stays_joined(self) -> None:
        empty = tuple(dataclasses.replace(site, extract=None, expected_rows=0) for site in dataset.JOINING_SITES)
        with mock.patch.object(dataset, "JOINING_SITES", empty):
            deployment = self.deploy()
            self.assertEqual("joined", deployment["status"], deployment)
            self.assertEqual(
                {**{step: "done" for step in DEPLOYMENT_STEPS[:-1]}, "check": "failed"}, self.statuses(deployment)
            )
            self.assertEqual("Enrolled with 0 records.", self.detail(deployment, "enrol"))
            next_round = deployment["next_round"]
            self.assertFalse(next_round["selected"])
            self.assertTrue(next_round["reason"])
            self.assertEqual(
                f"The next round would not select Hospital 290: {next_round['reason']}.",
                self.detail(deployment, "check"),
            )
            # A failed check keeps the hospital: nothing is rolled back.
            self.assertIn(SITE_ID, self.app.consortium.hospitals)
            self.assertEqual([], self.applier.stopped)
            self.assertTrue(self.gma_sites()[SITE_ID]["enrolled"])

    # --- one at a time --------------------------------------------------------------

    def test_a_pilot_and_an_addition_never_run_together(self) -> None:
        flight = self.app.pilot_flight
        cases = [
            ("pilot", "/api/v1/deployments", {"site_id": SITE_ID}, "A pilot is running. Add the hospital when it has finished."),
            ("deployment", "/api/v1/deployments", {"site_id": SITE_ID}, "A hospital is already being added."),
            ("deployment", "/api/v1/pilot:run", {"round_prefix": "ui"}, "A hospital is being added. Run the pilot when it has joined."),
            ("pilot", "/api/v1/pilot:run", {"round_prefix": "ui"}, "A pilot is already running. Try again when it finishes."),
        ]
        for holder, path, body, message in cases:
            with self.subTest(holder=holder, path=path):
                self.assertTrue(flight.try_enter(holder))
                try:
                    response = self.client.post(path, json=body)
                finally:
                    flight.leave()
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual(message, response.json()["detail"])
        self.assertEqual([], self.applier.applied)

    def test_a_holder_that_left_reads_as_an_addition(self) -> None:
        flight = self.app.pilot_flight
        self.assertTrue(flight.try_enter("pilot"))
        flight._holder = None  # the holder left between the refusal and the read
        try:
            response = self.client.post("/api/v1/deployments", json={"site_id": SITE_ID})
        finally:
            flight.leave()
        self.assertEqual("A hospital is already being added.", response.json()["detail"])

    def test_a_second_addition_of_a_running_hospital_is_refused(self) -> None:
        first = self.deploy()
        self.assertEqual("joined", first["status"])
        response = self.client.post("/api/v1/deployments", json={"site_id": SITE_ID})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("Hospital 290 is already in the consortium.", response.json()["detail"])
        self.assertEqual(1, len(self.applier.applied))
        self.assertFalse(self.app.pilot_flight.busy)

    def test_a_hospital_whose_lma_stopped_is_started_again(self) -> None:
        self.assertEqual("joined", self.deploy()["status"])
        # The LMA stops, outside the app. The hospital stays in the consortium.
        self.applier.stop(SERVICE, project="healthcare-demo-lma-pl-290")
        self.applier.stopped.clear()
        again = self.deploy()
        self.assertEqual("joined", again["status"], again)
        self.assertEqual("skipped", self.statuses(again)["join"])
        self.assertEqual("Hospital 290 is already in the consortium.", self.detail(again, "join"))
        self.assertEqual("done", self.statuses(again)["check"])
        self.assertEqual(2, len(self.client.get("/api/v1/deployments").json()["deployments"]))



class ConsortiumJoinTests(unittest.TestCase):
    """``Consortium.add_hospital`` replaces its maps, and the status leaves a joined hospital out."""

    def test_a_hospital_is_added_once_and_the_maps_are_replaced(self) -> None:
        from app.services.consortium import Consortium

        consortium = Consortium()
        self.addCleanup(consortium.close)
        hospitals, sites = consortium.hospitals, consortium.sites
        joining = dataset.joining_site_for(SITE_ID)
        # Its store kind and counts would break the status if it were read.
        hospital = mock.Mock(site_id=SITE_ID)
        consortium.add_hospital(joining, hospital, address="http://127.0.0.1:8085")

        self.assertNotIn(SITE_ID, hospitals)
        self.assertNotIn(joining, sites)
        self.assertIs(hospital, consortium.hospitals[SITE_ID])
        self.assertEqual(joining, consortium.sites[-1])
        addresses = consortium.joined_addresses
        addresses["xx"] = "changed"
        self.assertEqual({SITE_ID: "http://127.0.0.1:8085"}, consortium.joined_addresses)
        with self.assertRaises(ValueError):
            consortium.add_hospital(joining, hospital, address="http://127.0.0.1:8085")

        status = consortium.data_source_status()
        self.assertEqual("in-memory", status["cohort_source"])
        self.assertEqual([site.site_id for site in dataset.SITES], [row["site_id"] for row in status["hospitals"]])
        hospital.counts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
