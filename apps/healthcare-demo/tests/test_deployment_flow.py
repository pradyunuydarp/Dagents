"""Adding a hospital to the running demo, step by step, with fakes for everything outside the app.

The deployer runs seven steps: ``plan`` (core-service renders the new LMA's
Compose file), ``start`` (an applier starts that one service), ``answer`` (the
new LMA answers its health check and reports its site), ``attach`` (the GMA
accepts its address), ``enrol`` (the GMA enrols it), ``join`` (the app adds
the hospital to its services) and ``check`` (the GMA's planner plans the next
round). A failure in steps 2 to 6 undoes what the attempt did, newest first.

Here core-service, the applier, the new LMA and the GMA are fakes, so every
failure can be made to happen. The fake core-service writes the keys the
planner writes; ``test_deployment_spec.py`` checks the real planner's file,
and ``test_network_deploy.py`` runs the real processes. The routes, the flight
and the deployer are the app's own code.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace
from typing import Any, Callable
import unittest
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from fastapi.testclient import TestClient  # noqa: E402
import yaml  # noqa: E402

from agents.common.domain.federation import SiteAddressList, SiteAddressState, SiteRegistration  # noqa: E402
from agents.common.infrastructure.agent_client import AgentError, AgentUnavailableError  # noqa: E402
from agents.common.infrastructure.compose_apply import (  # noqa: E402
    AppliedService,
    ComposeApplyError,
    LocalProcessApplier,
)

import app.main as main  # noqa: E402
from tests.api_support import NO_LIMITS, default_limits  # noqa: E402
from app.domain.dataset import SITES, joining_site_for  # noqa: E402
from app.models import DEPLOYMENT_STEPS  # noqa: E402
from app.services.core_client import CoreServiceUnavailable  # noqa: E402
from app.services.deployment import DEPLOYMENTS_KEPT, build_hospital_deployer, framework_root  # noqa: E402

SITE_ID = "pl-290"
SERVICE = "lma-pl-290"
PROJECT = "healthcare-demo-lma-pl-290"
SITE_TOKEN = "the-new-lmas-site-token"
NOT_RUN_AFTER = {name: [step for step in DEPLOYMENT_STEPS[index + 1:]] for index, name in enumerate(DEPLOYMENT_STEPS)}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def rendered_yaml(request: dict[str, Any]) -> str:
    """The file core-service renders for ``request``: the keys the manifest compiler writes."""
    component = request["components"][0]
    service: dict[str, Any] = {"image": component["image"], "command": list(component["args"])}
    if component["env_files"]:
        service["env_file"] = list(component["env_files"])
    service["environment"] = {item["name"]: item["value"] for item in component["env"]}
    exposed = [str(port["container_port"]) for port in component["ports"] if port.get("host_port") is None]
    if exposed:
        service["expose"] = exposed
    health = component["health_check"]
    service["healthcheck"] = {
        "test": ["CMD", *health["command"]],
        "interval": f"{health['interval_seconds']}s",
        "timeout": f"{health['timeout_seconds']}s",
        "retries": health["retries"],
        "start_period": f"{health['start_period_seconds']}s",
    }
    service["networks"] = [request["network"]]
    service["restart"] = "unless-stopped"
    network = {"external": True} if request["external_network"] else {}
    document = {"services": {component["name"]: service}, "networks": {request["network"]: network}}
    return yaml.safe_dump(document, sort_keys=False)


class FakeCore:
    """core-service: renders the request, refuses an empty image as the planner does, or does not answer.

    ``healthy`` is its health check; ``up`` is whether it renders. A
    core-service whose health check answers can still fail to render.
    """

    def __init__(self) -> None:
        self.healthy = True
        self.up = True
        self.change: Callable[[dict[str, Any]], None] | None = None
        self.answers: list[dict[str, Any]] = []

    def health(self) -> bool:
        return self.healthy

    def render_compose(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.up:
            raise CoreServiceUnavailable("core-service did not answer")
        component = request["components"][0]
        if not component["image"]:
            answer = {
                "services": [component["name"]],
                "issues": [{"service": component["name"], "code": "empty_image", "value": None}],
                "compose_yaml": None,
            }
        else:
            text = rendered_yaml(request)
            if self.change is not None:
                document = yaml.safe_load(text)
                self.change(document["services"][component["name"]])
                text = yaml.safe_dump(document, sort_keys=False)
            answer = {"services": [component["name"]], "issues": [], "compose_yaml": text}
        self.answers.append(answer)
        return answer

    def close(self) -> None:
        pass


@dataclasses.dataclass
class FakeLma:
    """The new hospital's LMA: whether it runs, answers, and what it reports."""

    running: bool = False
    answers_once_started: bool = True
    answering: bool = False
    site_id: str = SITE_ID
    source_kind: str = "postgres"
    token: str = SITE_TOKEN
    takes_coordinator_token: bool = True


class FakeProbe:
    def __init__(self, lma: FakeLma, token: str) -> None:
        self.lma = lma
        self.token = token

    def answers(self, timeout_s: float) -> bool:
        return self.lma.running and self.lma.answering

    def site_info(self) -> Any:
        if self.token != self.lma.token:
            raise AgentError(
                service="lma", site_id=SITE_ID, path="/api/v1/site", status=401, code="unauthorized", detail="no"
            )
        return SimpleNamespace(site_id=self.lma.site_id, source=SimpleNamespace(kind=self.lma.source_kind))

    def close(self) -> None:
        pass


class FakeApplier:
    """Starts the fake LMA instead of a process. ``check`` is the real process applier's check."""

    name = "process"

    def __init__(self, lma: FakeLma, env_dir: Path) -> None:
        self.lma = lma
        self.env_dir = env_dir
        self.fail: ComposeApplyError | None = None
        self.state = "not_started"
        #: False: a running service is recreated, as docker compose does for one
        #: started from an older file or image, instead of kept.
        self.keep = True
        self.checked: list[str] = []
        self.applied: list[tuple[str, str, str, float]] = []
        self.stopped: list[tuple[str, str]] = []
        self._workdir = tempfile.TemporaryDirectory()
        self.log_path = str(Path(self._workdir.name) / f"{SERVICE}.log")

    def check(self, compose_yaml: str, service: str) -> None:
        self.checked.append(compose_yaml)
        LocalProcessApplier(
            Path(self._workdir.name),
            cwd=framework_root(),
            programs={sys.executable},
            base_env={},
            env_dirs=[self.env_dir],
        ).check(compose_yaml, service)

    def apply(self, compose_yaml: str, service: str, *, project: str, wait_seconds: float) -> AppliedService:
        self.applied.append((compose_yaml, service, project, wait_seconds))
        if self.state == "running":
            if not self.keep:
                return AppliedService(service, self.name, False, "recreated the container", self.log_path)
            return AppliedService(service, self.name, True, "already running as process 4242", self.log_path)
        if self.fail is not None:
            raise self.fail
        self.state = "running"
        self.lma.running = True
        self.lma.answering = self.lma.answers_once_started
        return AppliedService(service, self.name, False, "started process 4242", self.log_path)

    def status(self, service: str, *, project: str) -> str:
        return self.state

    def stop(self, service: str, *, project: str) -> None:
        self.stopped.append((service, project))
        self.state = "stopped"
        self.lma.running = False

    def close(self) -> None:
        self._workdir.cleanup()


def _gma_error(status: int | None, code: str, path: str = "/api/v1/federation/sites/{site_id}/address") -> AgentError:
    if status is None:
        return AgentUnavailableError(service="gma", site_id=None, path=path, status=None, code=code, detail="no answer")
    return AgentError(service="gma", site_id=None, path=path, status=status, code=code, detail="refused")


class FakeGma:
    """The GMA's address book, enrolments and round planner, in memory."""

    def __init__(self, lma: FakeLma, joinable_url: str) -> None:
        self.lma = lma
        self.joinable = {SITE_ID: joinable_url}
        self.addresses: dict[str, str] = {}
        self.enrolments: dict[str, SiteRegistration] = {}
        self.down: set[str] = set()
        self.calls: list[str] = []
        self.minimum_cohort = 20

    def register_site_address(self, site_id: str, url: str) -> SiteAddressState:
        self.calls.append("attach")
        if "attach" in self.down:
            raise _gma_error(None, "unavailable")
        if site_id not in self.joinable:
            raise _gma_error(403, "not_joinable")
        if url.rstrip("/") != self.joinable[site_id].rstrip("/"):
            raise _gma_error(403, "address_differs")
        self.addresses[site_id] = url
        return SiteAddressState(site_id=site_id, source="joined")

    def remove_site_address(self, site_id: str) -> None:
        self.calls.append("detach")
        self.addresses.pop(site_id, None)

    def site_addresses(self) -> SiteAddressList:
        sites = [SiteAddressState(site_id=site.site_id, source="configured") for site in SITES]
        sites += [SiteAddressState(site_id=site_id, source="joined") for site_id in self.addresses]
        return SiteAddressList(joinable=sorted(self.joinable), sites=sites)

    def _registration(self, site_id: str, cohort: int) -> SiteRegistration:
        return SiteRegistration(
            site_id=site_id, feature_contract_version="stroke-v1", cohort_size=cohort, enrolled=True,
            capabilities=["local_profiling"], approved_conditions=["acute-stroke"],
        )

    def refresh_sites(self) -> list[SiteRegistration]:
        self.calls.append("refresh")
        if "refresh" in self.down:
            raise _gma_error(None, "unavailable", "/api/v1/federation/sites:refresh")
        registrations = [self._registration(site.site_id, site.expected_rows) for site in SITES]
        if SITE_ID in self.addresses and self.lma.running and self.lma.takes_coordinator_token:
            registrations.append(self._registration(SITE_ID, joining_site_for(SITE_ID).expected_rows))
        for registration in registrations:
            self.enrolments[registration.site_id] = registration
        return sorted(registrations, key=lambda item: item.site_id)

    def register_site(self, registration: SiteRegistration) -> SiteRegistration:
        self.calls.append("register")
        self.enrolments[registration.site_id] = registration
        return registration

    def plan(self, invited: list[str]) -> Any:
        self.calls.append("plan")
        if "plan" in self.down:
            raise _gma_error(None, "unavailable", "/api/v1/federation/rounds:plan")
        selected, excluded = [], []
        for site_id in invited:
            enrolment = self.enrolments.get(site_id)
            if enrolment is None or not enrolment.enrolled:
                excluded.append(SimpleNamespace(site_id=site_id, reason="site is not enrolled"))
            elif enrolment.cohort_size < self.minimum_cohort:
                excluded.append(SimpleNamespace(site_id=site_id, reason="site cohort below minimum"))
            else:
                selected.append(site_id)
        return SimpleNamespace(selected_sites=selected, excluded_sites=excluded)


class FakeAgents:
    def __init__(self, gma: FakeGma) -> None:
        self._gma = gma

    def gma(self) -> FakeGma:
        return self._gma


class FakeDemo:
    """The app's services as the deployer sees them: the sites, the GMA client, the join and the next round."""

    def __init__(self, gma: FakeGma) -> None:
        self.sites = tuple(SITES)
        self.agents = FakeAgents(gma)
        self.gma = gma
        self.added: list[tuple[str, str, str]] = []
        self.joined_addresses: dict[str, str] = {}
        self.add_fails: Exception | None = None

    def has_site(self, site_id: str) -> bool:
        return any(site.site_id == site_id for site in self.sites)

    def joined(self) -> dict[str, str]:
        return dict(self.joined_addresses)

    def add_site(self, site: Any, url: str, token: str) -> None:
        if self.add_fails is not None:
            raise self.add_fails
        self.added.append((site.site_id, url, token))
        self.sites = (*self.sites, site)
        self.joined_addresses = {**self.joined_addresses, site.site_id: url}

    def preview_round(self, round_id: str) -> Any:
        return self.gma.plan([site.site_id for site in self.sites])


class DeploymentFlowTests(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.env_dir = Path(folder.name)
        (self.env_dir / "lma-pl-290.env").write_text(f"LMA_SITE_TOKEN={SITE_TOKEN}\n", encoding="utf-8")
        (self.env_dir / "lma-pl-290.env").chmod(0o600)
        # Nothing listens here, so the check that the port is free passes at once.
        self.url = f"http://127.0.0.1:{free_port()}"
        self.lma = FakeLma()
        self.core = FakeCore()
        self.gma = FakeGma(self.lma, self.url)
        self.demo = FakeDemo(self.gma)
        self.applier = FakeApplier(self.lma, self.env_dir)
        self.addCleanup(self.applier.close)
        self.use_deployer()
        # The pilot route's per-client limit would answer 429 before the flight's 409.
        main.rate_limits.configure(NO_LIMITS)
        self.addCleanup(main.rate_limits.configure, default_limits())
        self.client = TestClient(main.app)

    def use_deployer(self, **overrides: Any) -> None:
        settings = dataclasses.replace(
            main.settings,
            agents_mode="network",
            deployer="process",
            deploy_env_dir=str(self.env_dir),
            **{"deploy_timeout_seconds": 20.0, **overrides},
        )
        self.deployer = build_hospital_deployer(
            settings,
            demo=lambda: self.demo,
            core=self.core,
            applier=self.applier,  # type: ignore[arg-type]
            probe=lambda site_id, url, token, timeout_s: FakeProbe(self.lma, token),
            environ={"HEALTHCARE_DEMO_LMA_PL_290_URL": self.url, "HEALTHCARE_DEMO_LMA_PL_290_TOKEN": SITE_TOKEN},
        )
        patch = mock.patch.object(main, "deployer", self.deployer)
        patch.start()
        self.addCleanup(patch.stop)

    # --- helpers ------------------------------------------------------------------

    def deploy(self, status: int = 200) -> dict[str, Any]:
        response = self.client.post("/api/v1/deployments", json={"site_id": SITE_ID})
        self.assertEqual(status, response.status_code, response.text)
        body = response.json()
        # No answer holds the new LMA's address or a token.
        host_port = self.url.removeprefix("http://")
        self.assertNotIn(host_port, response.text)
        self.assertNotIn(SITE_TOKEN, response.text)
        return body

    @staticmethod
    def statuses(deployment: dict[str, Any]) -> dict[str, str]:
        return {step["step"]: step["status"] for step in deployment["steps"]}

    @staticmethod
    def detail(deployment: dict[str, Any], step: str) -> str:
        return next(row["detail"] for row in deployment["steps"] if row["step"] == step)

    def assert_failed_at(self, deployment: dict[str, Any], step: str, expected: dict[str, str]) -> None:
        self.assertEqual("failed", deployment["status"])
        self.assertEqual(list(DEPLOYMENT_STEPS), [row["step"] for row in deployment["steps"]])
        self.assertEqual({**expected, **{name: "not_run" for name in NOT_RUN_AFTER[step]}}, self.statuses(deployment))
        self.assertEqual("failed", self.statuses(deployment)[step])
        self.assertIsNone(deployment["next_round"])
        self.assertFalse(self.demo.has_site(SITE_ID))
        self.assertFalse(main.pilot_flight.busy)

    # --- joining --------------------------------------------------------------------

    def test_a_hospital_joins_and_the_next_round_selects_it(self) -> None:
        view = self.client.get("/api/v1/deployments").json()
        self.assertTrue(view["available"], view)
        self.assertIsNone(view["reason"])
        self.assertEqual("process", view["applier"])
        self.assertEqual(
            [(SITE_ID, False, "not_started")],
            [(h["site_id"], h["in_consortium"], h["service"]) for h in view["hospitals"]],
        )

        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertEqual({step: "done" for step in DEPLOYMENT_STEPS}, self.statuses(deployment))
        self.assertTrue(deployment["deployment_id"].startswith("dep-"))
        self.assertEqual(16, len(deployment["deployment_id"]))
        self.assertEqual(
            ("lma", SITE_ID, SERVICE, "process"),
            (deployment["kind"], deployment["site_id"], deployment["service"], deployment["applier"]),
        )
        self.assertFalse(deployment["adopted"])
        self.assertEqual([], deployment["issues"])
        self.assertEqual({"selected": True, "reason": None}, deployment["next_round"])
        self.assertEqual([], deployment["framework_calls"])
        self.assertEqual(
            {
                "plan": "core-service rendered the Compose file for lma-pl-290.",
                "start": "Started process 4242.",
                "answer": "The new LMA answers and reports site pl-290, read from its schema in Postgres.",
                "attach": "The GMA accepted the new LMA's address for pl-290.",
                "enrol": "The GMA enrolled pl-290 with 263 records.",
                "join": "Hospital 290 is in the consortium. Its pages now come from its LMA.",
                "check": "The next round selects Hospital 290.",
            },
            {step: self.detail(deployment, step) for step in DEPLOYMENT_STEPS},
        )
        self.assertEqual([(SERVICE, PROJECT, 20.0)], [entry[1:] for entry in self.applier.applied])
        self.assertEqual([(SITE_ID, self.url, SITE_TOKEN)], self.demo.added)
        self.assertEqual({SITE_ID: self.url}, self.gma.addresses)
        self.assertTrue(self.gma.enrolments[SITE_ID].enrolled)
        self.assertEqual(["attach", "refresh", "plan"], self.gma.calls)

        view = self.client.get("/api/v1/deployments").json()
        self.assertEqual(
            [(SITE_ID, True, "running")],
            [(h["site_id"], h["in_consortium"], h["service"]) for h in view["hospitals"]],
        )
        self.assertEqual([deployment["deployment_id"]], [row["deployment_id"] for row in view["deployments"]])

    def test_the_applier_gets_the_exact_file_core_service_returned(self) -> None:
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertEqual(1, len(self.core.answers))
        returned = self.core.answers[0]["compose_yaml"]
        self.assertIs(returned, self.applier.checked[0])
        self.assertIs(returned, self.applier.applied[0][0])
        self.assertEqual(returned, deployment["compose_yaml"])

    def test_attempts_are_kept_newest_first(self) -> None:
        self.core.up = False
        with self.assertLogs("app.services.deployment", "WARNING"):
            ids = [self.deploy()["deployment_id"] for _ in range(DEPLOYMENTS_KEPT + 2)]
        self.core.up = True
        listed = [row["deployment_id"] for row in self.client.get("/api/v1/deployments").json()["deployments"]]
        self.assertEqual(list(reversed(ids))[:DEPLOYMENTS_KEPT], listed)

    # --- failures at plan --------------------------------------------------------------

    def test_the_planner_refuses_the_request(self) -> None:
        self.use_deployer(deploy_image="")
        deployment = self.deploy()
        self.assert_failed_at(deployment, "plan", {"plan": "failed"})
        self.assertEqual([{"service": SERVICE, "code": "empty_image", "value": None}], deployment["issues"])
        self.assertEqual("The planner refused the request: empty_image for lma-pl-290.", self.detail(deployment, "plan"))
        self.assertIsNone(deployment["compose_yaml"])
        self.assertEqual([], self.applier.applied)

    def test_a_file_that_differs_from_the_request_is_never_applied(self) -> None:
        # Whatever answers at HEALTHCARE_DEMO_CORE_SERVICE_URL writes the file the
        # applier starts, so each part that decides what runs, which secrets it
        # reads, or where it listens must be the one the app asked for.
        changes = {
            "command": lambda service: service.update(command=[sys.executable, "-c", "print('not the LMA')"]),
            "environment": lambda service: service["environment"].update(LMA_SITE_ID="uk-134"),
            "env_file": lambda service: service.update(env_file=["/etc/other.env"]),
            "image": lambda service: service.update(image="example/other:1"),
            "ports": lambda service: service.update(ports=["0.0.0.0:8085:8085"]),
            "expose": lambda service: service.update(expose=["9999"]),
            "healthcheck": lambda service: service["healthcheck"].update(test=["CMD", "sh", "-c", "true"]),
            "networks": lambda service: service.update(networks=["other"]),
            "privileged": lambda service: service.update(privileged=True),
            "volumes": lambda service: service.update(volumes=["/:/host"]),
        }
        for name, change in changes.items():
            with self.subTest(change=name):
                self.core.change = change
                with self.assertLogs("app.services.deployment", "WARNING") as logs:
                    deployment = self.deploy()
                self.assertIn("does not match the request", logs.output[0])
                self.assert_failed_at(deployment, "plan", {"plan": "failed"})
                self.assertEqual(
                    "The core service returned a file this app cannot use.", self.detail(deployment, "plan")
                )
                self.assertIsNone(deployment["compose_yaml"])
                self.assertEqual([], self.applier.checked)
                self.assertEqual([], self.applier.applied)

    def test_core_service_that_does_not_answer(self) -> None:
        self.core.up = False
        with self.assertLogs("app.services.deployment", "WARNING"):
            deployment = self.deploy()
        self.assert_failed_at(deployment, "plan", {"plan": "failed"})
        self.assertEqual("The core service did not answer.", self.detail(deployment, "plan"))

    def test_a_file_the_applier_cannot_apply(self) -> None:
        (self.env_dir / "lma-pl-290.env").unlink()
        deployment = self.deploy()
        self.assert_failed_at(deployment, "plan", {"plan": "failed"})
        self.assertTrue(
            self.detail(deployment, "plan").startswith("The process applier cannot apply the file: "),
            self.detail(deployment, "plan"),
        )
        self.assertEqual([], self.applier.applied)

    # --- failures at start ----------------------------------------------------------

    def test_a_service_that_does_not_start(self) -> None:
        # The page shows the last line of the output, which names the cause,
        # and in process mode the log file.
        self.applier.fail = ComposeApplyError(
            "the process exited with status 1",
            "Traceback (most recent call last):\n  ...\nModuleNotFoundError: No module named 'agents'\n",
            log_path="/tmp/deploy/lma-pl-290.log",
        )
        with self.assertLogs("app.services.deployment", "WARNING") as logs:
            deployment = self.deploy()
        self.assertIn("Traceback (most recent call last)", logs.output[0])
        self.assert_failed_at(deployment, "start", {"plan": "done", "start": "failed"})
        self.assertEqual(
            "The process exited with status 1: ModuleNotFoundError: No module named 'agents'. "
            "Its log is /tmp/deploy/lma-pl-290.log.",
            self.detail(deployment, "start"),
        )
        self.assertEqual([], self.applier.stopped)

    def test_a_container_that_does_not_start_shows_dockers_reason(self) -> None:
        self.applier.fail = ComposeApplyError(
            "docker compose could not start lma-pl-290",
            " Network healthcare-demo_default  Error\n"
            "network healthcare-demo_default declared as external, but could not be found\n",
        )
        with self.assertLogs("app.services.deployment", "WARNING"):
            deployment = self.deploy()
        self.assertEqual(
            "Docker compose could not start lma-pl-290: network healthcare-demo_default declared as external, "
            "but could not be found.",
            self.detail(deployment, "start"),
        )

    def test_a_port_that_answers_for_another_service(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen()
            self.url = f"http://127.0.0.1:{held.getsockname()[1]}"
            self.gma.joinable = {SITE_ID: self.url}
            self.use_deployer()
            deployment = self.deploy()
        self.assert_failed_at(deployment, "start", {"plan": "done", "start": "failed"})
        self.assertEqual(
            "The new LMA's port answers, but not for a service this app started. Stop what is using it, or "
            "change HEALTHCARE_DEMO_LMA_PL_290_URL.",
            self.detail(deployment, "start"),
        )
        self.assertEqual([], self.applier.applied)

    # --- failures at answer ---------------------------------------------------------

    def test_a_hospital_that_never_answers_is_stopped_again(self) -> None:
        self.lma.answers_once_started = False
        self.use_deployer(deploy_timeout_seconds=1.5)
        deployment = self.deploy()
        self.assert_failed_at(deployment, "answer", {"plan": "done", "start": "undone", "answer": "failed"})
        self.assertEqual(
            f"The new LMA did not answer its health check within 1.5 seconds. Its log is {self.applier.log_path}.",
            self.detail(deployment, "answer"),
        )
        self.assertEqual("Stopped the service this attempt started.", self.detail(deployment, "start"))
        self.assertEqual([(SERVICE, PROJECT)], self.applier.stopped)
        self.assertEqual([], self.gma.calls)

    def test_a_hospital_that_stops_before_it_answers(self) -> None:
        self.lma.answers_once_started = False
        original = self.applier.apply

        def apply_then_exit(*args: Any, **kwargs: Any) -> AppliedService:
            applied = original(*args, **kwargs)
            self.applier.state = "stopped"
            return applied

        self.applier.apply = apply_then_exit  # type: ignore[method-assign]
        deployment = self.deploy()
        self.assertEqual("failed", self.statuses(deployment)["answer"])
        self.assertEqual(
            f"The new LMA stopped before it answered its health check. Its log is {self.applier.log_path}.",
            self.detail(deployment, "answer"),
        )

    def test_an_lma_that_reports_another_site(self) -> None:
        self.lma.site_id = "uk-134"
        deployment = self.deploy()
        self.assert_failed_at(deployment, "answer", {"plan": "done", "start": "undone", "answer": "failed"})
        self.assertEqual("The new LMA reports site uk-134, not pl-290.", self.detail(deployment, "answer"))
        self.assertEqual([(SERVICE, PROJECT)], self.applier.stopped)

    def test_an_lma_that_refuses_the_apps_token(self) -> None:
        self.lma.token = "another-token"
        deployment = self.deploy()
        self.assert_failed_at(deployment, "answer", {"plan": "done", "start": "undone", "answer": "failed"})
        self.assertEqual(
            "The new LMA answered its health check, but refused the app's site token (401).",
            self.detail(deployment, "answer"),
        )

    def test_an_lma_that_does_not_read_its_schema(self) -> None:
        self.lma.source_kind = "in-memory"
        deployment = self.deploy()
        self.assert_failed_at(deployment, "answer", {"plan": "done", "start": "undone", "answer": "failed"})
        self.assertEqual(
            "The new LMA reads an in-memory source, not its schema in Postgres.", self.detail(deployment, "answer")
        )

    # --- a service that already runs --------------------------------------------------

    def test_a_running_service_that_does_not_answer_is_replaced(self) -> None:
        # The applier still runs the hospital's process, but it does not answer,
        # for example because it hangs. Keeping it would wait the whole timeout
        # and then keep it, because a rollback leaves a kept service alone.
        self.applier.state = "running"
        self.lma.running, self.lma.answering = True, False
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertFalse(deployment["adopted"])
        self.assertEqual([(SERVICE, PROJECT)], self.applier.stopped)
        self.assertEqual(
            "The service ran but did not answer, so the app stopped it. Started process 4242.",
            self.detail(deployment, "start"),
        )

    def test_a_running_service_that_answers_is_kept(self) -> None:
        # The LMA runs and answers, but the app does not list the hospital, as
        # after the app restarted while a container kept running.
        self.applier.state = "running"
        self.lma.running, self.lma.answering = True, True
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertTrue(deployment["adopted"])
        self.assertEqual("Already running; kept.", self.detail(deployment, "start"))
        self.assertEqual([], self.applier.stopped)

    def test_a_running_service_the_applier_recreates_is_not_kept(self) -> None:
        self.applier.state = "running"
        self.lma.running, self.lma.answering = True, True
        self.applier.keep = False
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertFalse(deployment["adopted"])
        self.assertEqual("Recreated the container.", self.detail(deployment, "start"))

    def test_a_kept_service_is_not_stopped_by_a_rollback(self) -> None:
        self.applier.state = "running"
        self.lma.running, self.lma.answering = True, True
        self.gma.joinable = {SITE_ID: f"http://127.0.0.1:{free_port()}"}
        deployment = self.deploy()
        self.assertEqual("failed", deployment["status"])
        self.assertEqual("done", self.statuses(deployment)["start"])
        self.assertEqual([], self.applier.stopped)

    # --- failures at attach, enrol and join ---------------------------------------------

    def test_a_refused_address_stops_the_service(self) -> None:
        # The GMA lists pl-290 as joinable, at another address.
        self.gma.joinable = {SITE_ID: f"http://127.0.0.1:{free_port()}"}
        deployment = self.deploy()
        self.assert_failed_at(
            deployment, "attach", {"plan": "done", "start": "undone", "answer": "done", "attach": "failed"}
        )
        self.assertEqual(
            "The GMA accepts pl-290 only at the address in its GMA_JOINABLE_SITE_ENDPOINTS, and that differs from "
            "HEALTHCARE_DEMO_LMA_PL_290_URL.",
            self.detail(deployment, "attach"),
        )
        self.assertEqual([(SERVICE, PROJECT)], self.applier.stopped)
        self.assertEqual({}, self.gma.addresses)

    def test_a_site_the_gma_does_not_accept(self) -> None:
        # The view's check found the hospital joinable; the GMA restarted
        # without it before the POST, while the check was still reused.
        self.assertTrue(self.client.get("/api/v1/deployments").json()["available"])
        self.gma.joinable = {}
        deployment = self.deploy()
        self.assertEqual("failed", self.statuses(deployment)["attach"])
        self.assertEqual(
            "The GMA does not accept an address for pl-290. Start the GMA with GMA_JOINABLE_SITE_ENDPOINTS naming it.",
            self.detail(deployment, "attach"),
        )

    def test_a_gma_that_does_not_answer_the_address(self) -> None:
        self.gma.down.add("attach")
        deployment = self.deploy()
        self.assertEqual("failed", self.statuses(deployment)["attach"])
        self.assertEqual("The GMA did not answer.", self.detail(deployment, "attach"))
        self.assertEqual([(SERVICE, PROJECT)], self.applier.stopped)

    def test_an_enrolment_that_misses_the_site_removes_the_address_and_stops_the_service(self) -> None:
        # The LMA refuses the GMA's coordinator token, so the GMA's refresh leaves it out.
        self.lma.takes_coordinator_token = False
        deployment = self.deploy()
        self.assert_failed_at(
            deployment,
            "enrol",
            {"plan": "done", "start": "undone", "answer": "done", "attach": "undone", "enrol": "failed"},
        )
        self.assertEqual(
            "The GMA's refresh did not enrol pl-290. Check that the GMA holds the new LMA's coordinator token.",
            self.detail(deployment, "enrol"),
        )
        self.assertEqual("The GMA no longer holds the new LMA's address.", self.detail(deployment, "attach"))
        self.assertEqual({}, self.gma.addresses)
        self.assertNotIn(SITE_ID, self.gma.enrolments)
        self.assertEqual([(SERVICE, PROJECT)], self.applier.stopped)

    def test_a_refresh_the_gma_does_not_answer(self) -> None:
        self.gma.down.add("refresh")
        deployment = self.deploy()
        self.assertEqual("failed", self.statuses(deployment)["enrol"])
        self.assertEqual("The GMA did not answer.", self.detail(deployment, "enrol"))
        self.assertEqual("undone", self.statuses(deployment)["attach"])

    def test_a_join_that_fails_withdraws_the_enrolment_and_the_address(self) -> None:
        self.demo.add_fails = RuntimeError("no room at postgresql://demo-user:secret@db.example.invalid/x")
        with self.assertLogs("app.services.deployment", "ERROR") as logs:
            deployment = self.deploy()
        self.assertNotIn("secret@", "\n".join(logs.output))
        self.assert_failed_at(
            deployment,
            "join",
            {
                "plan": "done",
                "start": "undone",
                "answer": "done",
                "attach": "undone",
                "enrol": "undone",
                "join": "failed",
            },
        )
        self.assertEqual("Enrolment withdrawn.", self.detail(deployment, "enrol"))
        self.assertEqual(
            "The step failed (RuntimeError). The app's log has the cause.", self.detail(deployment, "join")
        )
        self.assertFalse(self.gma.enrolments[SITE_ID].enrolled)
        self.assertEqual({}, self.gma.addresses)
        self.assertEqual([(SERVICE, PROJECT)], self.applier.stopped)
        self.assertEqual(["attach", "refresh", "register", "detach"], self.gma.calls)

    # --- the check ------------------------------------------------------------------------

    def test_a_hospital_the_planner_does_not_select_stays_joined(self) -> None:
        self.gma.minimum_cohort = 1000
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertEqual({**{step: "done" for step in DEPLOYMENT_STEPS[:-1]}, "check": "failed"}, self.statuses(deployment))
        self.assertEqual({"selected": False, "reason": "site cohort below minimum"}, deployment["next_round"])
        self.assertEqual(
            "The next round would not select Hospital 290: site cohort below minimum.", self.detail(deployment, "check")
        )
        # A failed check keeps the hospital: nothing is rolled back.
        self.assertTrue(self.demo.has_site(SITE_ID))
        self.assertEqual([], self.applier.stopped)
        self.assertTrue(self.gma.enrolments[SITE_ID].enrolled)
        self.assertEqual({SITE_ID: self.url}, self.gma.addresses)

    def test_a_check_the_gma_does_not_answer_keeps_the_hospital(self) -> None:
        self.gma.down.add("plan")
        deployment = self.deploy()
        self.assertEqual("joined", deployment["status"], deployment)
        self.assertEqual("failed", self.statuses(deployment)["check"])
        self.assertEqual("The GMA did not answer.", self.detail(deployment, "check"))
        self.assertIsNone(deployment["next_round"])
        self.assertTrue(self.demo.has_site(SITE_ID))

    # --- one at a time --------------------------------------------------------------

    def test_a_pilot_and_an_addition_never_run_together(self) -> None:
        flight = main.pilot_flight
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
        flight = main.pilot_flight
        self.assertTrue(flight.try_enter("pilot"))
        flight._holder = None  # the holder left between the refusal and the read
        try:
            response = self.client.post("/api/v1/deployments", json={"site_id": SITE_ID})
        finally:
            flight.leave()
        self.assertEqual("A hospital is already being added.", response.json()["detail"])

    def test_a_second_addition_of_a_running_hospital_is_refused(self) -> None:
        self.assertEqual("joined", self.deploy()["status"])
        response = self.client.post("/api/v1/deployments", json={"site_id": SITE_ID})
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("Hospital 290 is already in the consortium.", response.json()["detail"])
        self.assertEqual(1, len(self.applier.applied))
        self.assertFalse(main.pilot_flight.busy)

    def test_a_hospital_whose_lma_stopped_is_started_again(self) -> None:
        self.assertEqual("joined", self.deploy()["status"])
        # The LMA stops, outside the app. The hospital stays in the consortium.
        self.applier.stop(SERVICE, project=PROJECT)
        self.applier.stopped.clear()
        again = self.deploy()
        self.assertEqual("joined", again["status"], again)
        self.assertEqual("skipped", self.statuses(again)["join"])
        self.assertEqual("Hospital 290 is already in the consortium.", self.detail(again, "join"))
        self.assertEqual("done", self.statuses(again)["check"])
        self.assertEqual(1, len(self.demo.added))
        self.assertEqual(2, len(self.client.get("/api/v1/deployments").json()["deployments"]))

    def test_a_failed_restart_keeps_the_joined_hospitals_address_and_enrolment(self) -> None:
        """The address and the enrolment belong to the joined hospital, not to the attempt that failed."""
        self.assertEqual("joined", self.deploy()["status"])
        self.applier.stop(SERVICE, project=PROJECT)
        self.applier.stopped.clear()
        self.gma.down.add("refresh")
        again = self.deploy()
        self.assertEqual("failed", again["status"])
        self.assertEqual("failed", self.statuses(again)["enrol"])
        self.assertEqual("done", self.statuses(again)["attach"])
        self.assertEqual("undone", self.statuses(again)["start"])
        self.assertEqual({SITE_ID: self.url}, self.gma.addresses)
        self.assertTrue(self.gma.enrolments[SITE_ID].enrolled)
        self.assertTrue(self.demo.has_site(SITE_ID))


if __name__ == "__main__":
    unittest.main()
