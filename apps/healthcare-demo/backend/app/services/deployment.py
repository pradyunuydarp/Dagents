"""Add a hospital to the running consortium.

A person who runs the split demo on their own machine can add a hospital that
is not in the consortium at start (``JOINING_SITES``) while the demo runs. The
app uses the framework for every decision and does the side effects itself:

1. ``plan``: core-service renders the new LMA's Compose file
   (``POST /api/v1/manifests/compose``, the OCaml manifest compiler). The app
   never writes Compose.
2. ``start``: a :class:`~agents.common.infrastructure.compose_apply.ComposeApplier`
   starts the one service in that exact file, as a local process or with
   ``docker compose``.
3. ``answer``: the new LMA answers ``/api/v1/health`` and reports its site.
4. ``attach``: the Dagents GMA accepts the LMA's address
   (``PUT /api/v1/federation/sites/{id}/address``), which it allows only for
   the pair in its ``GMA_FEDERATION_JOINABLE_SITES``.
5. ``enrol``: the GMA enrols the site with the registration the LMA reports.
6. ``join``: the app adds the hospital to its consortium and forwards the
   hospital's pages to the LMA.
7. ``check``: the GMA's planner plans the next round, and says whether it
   selects the hospital. The app never decides eligibility.

A failure in steps 2 to 6 rolls back what this attempt did, in reverse order:
it withdraws the enrolment and stops the service it started. The GMA keeps an
accepted address in memory until it restarts; without an enrolment, no round
uses it.

Adding a hospital starts a process or a container, so it is off unless
``HEALTHCARE_DEMO_DEPLOYER`` turns it on, and :func:`check_deployer` refuses
every setting that would let a visitor of a public demo start one.
"""

from __future__ import annotations

import logging
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Mapping
from urllib.parse import urlsplit

import httpx

from agents.common.infrastructure.compose_apply import (
    ComposeApplier,
    ComposeApplyError,
    DockerComposeApplier,
    LocalProcessApplier,
    compose_service,
    unescape_compose,
)

from app.core.config import Settings
from app.domain.dataset import (
    JOINING_SITE_IDS,
    JOINING_SITES,
    Site,
    expected_record_count,
    joining_site_for,
    records_source,
)
from app.models import (
    DEPLOYMENT_STEPS,
    Deployment,
    DeploymentIssue,
    DeploymentStep,
    DeploymentsView,
    JoinableHospital,
    NextRound,
)
from app.services.remote import SITE_REPORT_PATH, RemoteServiceUnavailable

logger = logging.getLogger(__name__)

#: The values ``HEALTHCARE_DEMO_DEPLOYER`` may take.
DEPLOYERS = ("off", "process", "compose")

#: Hosts that mean "this machine" in an address.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")


class DeployerSettingError(ValueError):
    """A deploy setting the app refuses to start with."""


def check_deployer(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """Refuse deploy settings that are unsafe or cannot work. Called at start-up.

    Raises :class:`DeployerSettingError` at the first rule that fails:

    1. ``HEALTHCARE_DEMO_DEPLOYER`` is ``off``, ``process`` or ``compose``.
    2. ``off`` needs nothing else.
    3. Only the ``app`` role adds hospitals.
    4. Never on Render: the public demo never starts a process or a container.
    5. ``HEALTHCARE_DEMO_JOINING_LMA_URLS`` names at least one hospital.
    6. Each one is a hospital that can join.
    7. None is in ``HEALTHCARE_DEMO_LMA_URLS``, by id or by address.
    8. Each address is ``http`` with an explicit port.
    9. In compose mode each address is ``http://lma-{id}:<port>``, the
       container's name on the Compose network.
    10. In compose mode the hospitals reach the app's internal listener by its
        name on the Compose network, not by 127.0.0.1.
    11. In process mode each address is on 127.0.0.1 or localhost.
    """
    environ = os.environ if environ is None else environ
    value = settings.deployer
    if value not in DEPLOYERS:
        raise DeployerSettingError(f"HEALTHCARE_DEMO_DEPLOYER must be off, process or compose, not {value!r}.")
    if value == "off":
        return
    if settings.role != "app":
        raise DeployerSettingError(
            f"HEALTHCARE_DEMO_DEPLOYER={value} needs HEALTHCARE_DEMO_ROLE=app. Only the app of a split run adds "
            "hospitals."
        )
    if environ.get("RENDER"):
        raise DeployerSettingError(
            "HEALTHCARE_DEMO_DEPLOYER must be off on Render. The public demo never starts a process or a container."
        )
    try:
        joining = settings.joining_lma_url_map()
        configured = settings.lma_url_map()
    except ValueError as error:
        raise DeployerSettingError(str(error)) from None
    if not joining:
        raise DeployerSettingError("HEALTHCARE_DEMO_JOINING_LMA_URLS names no hospital.")
    for site_id in joining:
        if site_id not in JOINING_SITE_IDS:
            raise DeployerSettingError(
                f"{site_id} is not a hospital that can join. Known: {', '.join(JOINING_SITE_IDS)}."
            )
    configured_urls = set(configured.values())
    for site_id, url in joining.items():
        if site_id in configured or url in configured_urls:
            raise DeployerSettingError(f"{site_id} clashes with HEALTHCARE_DEMO_LMA_URLS.")
    hosts: dict[str, str] = {}
    for site_id, url in joining.items():
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError:
            port = None
        if parts.scheme != "http" or port is None or not parts.hostname:
            raise DeployerSettingError(f"The address of {site_id} needs http and an explicit port.")
        hosts[site_id] = parts.hostname
    if value == "compose":
        for site_id, host in hosts.items():
            if host != f"lma-{site_id}":
                raise DeployerSettingError(
                    f"In compose mode the address of {site_id} must be http://lma-{site_id}:<port>."
                )
        if urlsplit(settings.coordinator_url).hostname in LOOPBACK_HOSTS:
            raise DeployerSettingError(
                "In compose mode set HEALTHCARE_DEMO_COORDINATOR_URL to the app's internal address on the Compose "
                "network, such as http://healthcare-demo-backend:8081."
            )
    else:
        for site_id, host in hosts.items():
            if host not in LOOPBACK_HOSTS:
                raise DeployerSettingError(
                    f"In process mode the address of {site_id} must be on 127.0.0.1 or localhost."
                )


# --- What the page is told ---------------------------------------------------------

#: The services ``docker-compose.yml`` runs. A new hospital's container joins
#: their network, so its name must differ from each of them.
COMPOSE_SERVICES = (
    "gma",
    "lma-uk-134",
    "lma-it-002",
    "lma-ch-245",
    "healthcare-demo-backend",
    "healthcare-demo-frontend",
    "core-service",
)

#: Seconds the checks of core-service and the GMA are reused by
#: ``GET /api/v1/deployments``. Each one can wait for a timeout.
AVAILABILITY_CACHE_SECONDS = 15

#: Attempts kept in memory, newest first.
DEPLOYMENTS_KEPT = 20

#: Seconds between two health checks of a new hospital.
POLL_SECONDS = 0.5

#: Seconds a check that a port answers may take.
PORT_CHECK_SECONDS = 0.5

#: Seconds an LMA that is already in the consortium has to answer before it
#: counts as gone, so the hospital can be started again.
RUNNING_CHECK_SECONDS = 2.0

REASON_NOT_APP = (
    "This server runs the demo in one process. Adding a hospital starts a new hospital process, which only a split "
    'run on your own machine does. See "Add a hospital while the demo runs" in the demo\'s README.'
)
REASON_OFF = "Adding a hospital is turned off. A split run started with --deploy turns it on."
REASON_NO_CORE = (
    "The Dagents core service does not answer, so the app cannot ask it for the new hospital's Compose file."
)
REASON_NO_GMA = "The Dagents GMA does not answer."
REASON_NOT_JOINABLE = (
    "The GMA does not accept an address for {site_id}. Start the GMA with GMA_FEDERATION_JOINABLE_SITES naming it."
)


class DeploymentUnavailable(RuntimeError):
    """Adding a hospital is not available now. The message is the reason."""


class DeploymentConflict(RuntimeError):
    """The hospital is in the consortium and its LMA answers. The message says so."""


# --- The Compose request -----------------------------------------------------------


def _host_and_port(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.hostname is None or parts.port is None:
        raise ValueError(f"{url} has no host and port")
    return parts.hostname, parts.port


def lma_compose_request(
    site: Site, url: str, settings: Settings, mode: Literal["process", "compose"]
) -> dict[str, Any]:
    """The workload spec core-service renders for a new hospital's LMA. Pure.

    The LMA is this app's own backend with ``HEALTHCARE_DEMO_ROLE=lma``, on the
    port of ``url``, reaching the app's internal listener for models and
    updates. Its per-client limits are off, because every request reaches it
    from the app or the GMA.

    - ``process``: the command is this Python running uvicorn on the address
      of ``url``. The app's internal listener is on 127.0.0.1.
    - ``compose``: the command is the image's uvicorn on every interface of
      the container, on the running demo's network, which is external to the
      new file. The app's internal listener is ``HEALTHCARE_DEMO_COORDINATOR_URL``.

    There are no ``env_files``: the image holds the defaults, and the request
    sets every variable an LMA needs.
    """
    host, port = _host_and_port(url)
    if mode == "process":
        args = [sys.executable, "-m", "uvicorn", "app.main:app", "--host", host, "--port", str(port)]
        coordinator = f"http://127.0.0.1:{settings.internal_port}"
        network, external, existing = "default", False, []
    else:
        args = ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)]
        coordinator = settings.coordinator_url
        network, external, existing = settings.deploy_network, True, list(COMPOSE_SERVICES)
    environment = {
        "HEALTHCARE_DEMO_ROLE": "lma",
        "HEALTHCARE_DEMO_SITE_ID": site.site_id,
        "HEALTHCARE_DEMO_API_PORT": str(port),
        "HEALTHCARE_DEMO_COORDINATOR_URL": coordinator,
        "HEALTHCARE_DEMO_RATE_LIMIT_ENTRIES": "0",
        "HEALTHCARE_DEMO_RATE_LIMIT_PROBES": "0",
        "HEALTHCARE_DEMO_RATE_LIMIT_PILOTS": "0",
        "HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS": "0",
    }
    return {
        "placement": "own",
        "network": network,
        "external_network": external,
        "existing_services": existing,
        "components": [
            {
                "name": f"lma-{site.site_id}",
                "image": settings.deploy_image,
                "kind": "Deployment",
                "args": args,
                "env": [{"name": name, "value": value} for name, value in environment.items()],
                "ports": [{"container_port": port, "host_port": port, "host_ip": "127.0.0.1"}],
                "health_check": {
                    "command": [
                        "python",
                        "-c",
                        "import urllib.request; "
                        f"urllib.request.urlopen('http://127.0.0.1:{port}/api/v1/health', timeout=2)",
                    ],
                    "interval_seconds": 5,
                    "timeout_seconds": 3,
                    "retries": 12,
                    "start_period_seconds": 10,
                },
                "depends_on": [],
            }
        ],
    }


#: The service keys the file core-service renders from :func:`lma_compose_request`.
RENDERED_KEYS = frozenset({"image", "command", "environment", "ports", "healthcheck", "networks", "restart"})


def _unescaped(value: Any) -> Any:
    if isinstance(value, str):
        return unescape_compose(value)
    if isinstance(value, list):
        return [_unescaped(item) for item in value]
    if isinstance(value, dict):
        return {key: _unescaped(item) for key, item in value.items()}
    return value


def rendered_service_mismatch(definition: Mapping[str, Any], request: Mapping[str, Any]) -> str | None:
    """The first part of a rendered service that differs from the request, or ``None``.

    The applier starts what the file says, and the file comes from whatever
    answers at ``CORE_SERVICE_PUBLIC_URL``. So the app compares the parts that
    decide what runs and where it listens with the component it asked for:
    the keys, the image, the command, the environment, the published ports,
    the health check's command and the network. It compares parsed values,
    not text, so a change in how the planner formats the file does not matter.
    """
    component = request["components"][0]
    unknown = sorted(str(key) for key in definition if key not in RENDERED_KEYS)
    if unknown:
        return f"the key {unknown[0]}"
    expected: dict[str, Any] = {
        "image": component["image"],
        "command": list(component["args"]),
        "environment": {item["name"]: item["value"] for item in component["env"]},
        "ports": [f"{port['host_ip']}:{port['host_port']}:{port['container_port']}" for port in component["ports"]],
        "healthcheck": ["CMD", *component["health_check"]["command"]],
        "networks": [request["network"]],
    }
    healthcheck = definition.get("healthcheck")
    actual = {
        **{key: definition.get(key) for key in expected if key != "healthcheck"},
        "healthcheck": healthcheck.get("test") if isinstance(healthcheck, Mapping) else None,
    }
    for key, value in expected.items():
        try:
            found = _unescaped(actual[key])
        except ComposeApplyError:
            return f"the {key}"
        if found != value:
            return f"the {key}"
    return None


# --- One attempt -------------------------------------------------------------------


class _StepFailed(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass
class _Step:
    step: str
    status: str = "not_run"
    detail: str = ""
    seconds: float = 0.0

    def model(self) -> DeploymentStep:
        return DeploymentStep(step=self.step, status=self.status, detail=self.detail, seconds=self.seconds)  # type: ignore[arg-type]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _port_answers(host: str, port: int) -> bool:
    """Whether something accepts a TCP connection at ``host:port``."""
    try:
        with socket.create_connection((host, port), timeout=PORT_CHECK_SECONDS):
            return True
    except OSError:
        return False


def _issue_text(issue: Mapping[str, Any]) -> str:
    code = issue.get("code")
    value = issue.get("value")
    service = issue.get("service")
    text = str(code)
    if value is not None:
        text += f" ({value})"
    return text + (f" for {service}" if service else " for the request")


def _service_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment a new hospital's process starts from.

    The app's own settings are left out, so the hospital reads only what its
    Compose file sets and the defaults in the env files. ``PATH``,
    ``PYTHONPATH`` and ``DAGENTSC_BIN`` stay.
    """
    return {
        name: value
        for name, value in environ.items()
        if not name.startswith(("HEALTHCARE_DEMO_", "GMA_", "LMA_")) and name != "RENDER"
    }


#: The backend directory: the LMA process starts here, so ``app.main`` imports.
BACKEND_DIR = Path(__file__).resolve().parents[2]


class HospitalDeployer:
    """Adds hospitals to the running consortium, one at a time.

    The route holds the pilot's flight while :meth:`deploy` runs, so a pilot
    and an addition never run at the same time.

    Params:
    - `settings`: the app's settings.
    - `consortium`: the app's consortium. :meth:`deploy` adds the hospital to it.
    - `forwarder`: the app's :class:`~app.api.forward.Forwarder`, which then
      sends the hospital's pages to its LMA. ``None`` outside the ``app`` role.
    - `gma`: the app's client of the Dagents GMA
      (:class:`~app.services.remote.GmaRoundClient`). ``None`` outside the
      ``app`` role.
    - `core`: a :class:`~app.services.framework_client.CoreServiceClient`.
    - `applier`: what starts the service. ``None`` when adding is off.
    - `clock`: seconds, for the availability cache. Tests pass a fake.
    - `client`: an ``httpx.Client`` for the calls to the new LMA, for tests.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        consortium: Any,
        forwarder: Any,
        gma: Any,
        core: Any,
        applier: ComposeApplier | None,
        clock: Callable[[], float] = time.monotonic,
        client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings
        self._consortium = consortium
        self._forwarder = forwarder
        self._gma = gma
        self._core = core
        self._applier = applier
        self._clock = clock
        self._client = client
        self._lock = threading.Lock()
        self._cached: tuple[float, str | None] | None = None
        self._deployments: list[Deployment] = []

    # --- What GET /api/v1/deployments shows ------------------------------------------

    @property
    def applier_name(self) -> Literal["off", "process", "compose"]:
        if self._applier is None or self._settings.deployer not in ("process", "compose"):
            return "off"
        return self._settings.deployer  # type: ignore[return-value]

    def _urls(self) -> dict[str, str]:
        try:
            return self._settings.joining_lma_url_map()
        except ValueError:
            return {}

    def unavailable_reason(self) -> str | None:
        """Why adding a hospital is not available now, or ``None`` when it is.

        The role and the setting are read each time. The checks of core-service
        and the GMA are reused for :data:`AVAILABILITY_CACHE_SECONDS`.
        """
        if self._settings.role != "app":
            return REASON_NOT_APP
        if self.applier_name == "off":
            return REASON_OFF
        now = self._clock()
        with self._lock:
            if self._cached is not None and now - self._cached[0] < AVAILABILITY_CACHE_SECONDS:
                return self._cached[1]
        reason = self._probe()
        with self._lock:
            self._cached = (now, reason)
        return reason

    def _probe(self) -> str | None:
        if not self._core.health():
            return REASON_NO_CORE
        try:
            joinable = set(self._gma.list_site_addresses().joinable)
        except Exception as error:  # noqa: BLE001 - any failure means the GMA cannot be used
            logger.warning("the GMA's site addresses could not be read: %s", error)
            return REASON_NO_GMA
        for site_id in self._urls():
            if site_id not in joinable:
                return REASON_NOT_JOINABLE.format(site_id=site_id)
        return None

    def clear_availability(self) -> None:
        """Forget the cached checks, so the next view checks again."""
        with self._lock:
            self._cached = None

    def _service_state(self, site: Site) -> str:
        if self._applier is None:
            return "unknown"
        try:
            return self._applier.status(f"lma-{site.site_id}", project=_project(site.site_id))
        except Exception as error:  # noqa: BLE001 - the view must answer
            logger.warning("the state of lma-%s could not be read: %s", site.site_id, error)
            return "unknown"

    def view(self) -> DeploymentsView:
        """What ``GET /api/v1/deployments`` returns. It reads no hospital's records."""
        reason = self.unavailable_reason()
        hospitals = getattr(self._consortium, "hospitals", {})
        joinable = [
            JoinableHospital(
                site_id=site.site_id,
                display_name=site.display_name,
                country=site.country,
                records=expected_record_count(site),
                records_source=records_source(site),
                in_consortium=site.site_id in hospitals,
                service=self._service_state(site),  # type: ignore[arg-type]
            )
            for site in JOINING_SITES
        ]
        with self._lock:
            deployments = list(self._deployments)
        return DeploymentsView(
            available=reason is None,
            reason=reason,
            applier=self.applier_name,
            hospitals=joinable,
            deployments=deployments,
        )

    # --- POST /api/v1/deployments ----------------------------------------------------

    def _lma_answers(self, url: str, timeout: float) -> bool:
        try:
            if self._client is not None:
                response = self._client.get(f"{url}/api/v1/health", timeout=timeout)
            else:
                with httpx.Client(timeout=timeout) as client:
                    response = client.get(f"{url}/api/v1/health")
        except httpx.HTTPError:
            return False
        return response.status_code == 200

    def _get_json(self, url: str, timeout: float) -> Any:
        if self._client is not None:
            response = self._client.get(url, timeout=timeout)
        else:
            with httpx.Client(timeout=timeout) as client:
                response = client.get(url)
        response.raise_for_status()
        return response.json()

    def deploy(self, site_id: str) -> Deployment:
        """Add one hospital to the running consortium, and report every step.

        Raises :class:`DeploymentUnavailable` when adding is not available,
        and :class:`DeploymentConflict` when the hospital is in the consortium
        and its LMA answers. Any other failure is a step with status
        ``failed`` in the returned :class:`~app.models.Deployment`.
        """
        reason = self.unavailable_reason()
        if reason is not None:
            raise DeploymentUnavailable(reason)
        site = joining_site_for(site_id)
        urls = self._urls()
        if site_id not in urls:
            raise DeploymentUnavailable(
                f"{site.display_name} has no address in HEALTHCARE_DEMO_JOINING_LMA_URLS, so it cannot be added."
            )
        url = urls[site_id]
        if site_id in self._consortium.hospitals and self._lma_answers(url, RUNNING_CHECK_SECONDS):
            raise DeploymentConflict(f"{site.display_name} is already in the consortium.")
        try:
            deployment = _Attempt(self, site, url).run()
        finally:
            self.clear_availability()
        with self._lock:
            self._deployments = [deployment, *self._deployments][:DEPLOYMENTS_KEPT]
        return deployment

    def close(self) -> None:
        """Stop what the applier started, when it stops services with the app (process mode)."""
        if self._applier is not None:
            self._applier.close()


def _project(site_id: str) -> str:
    return f"healthcare-demo-lma-{site_id}"


class _Attempt:
    """The seven steps of one addition, with the rollback of steps 2 to 6."""

    def __init__(self, deployer: HospitalDeployer, site: Site, url: str) -> None:
        self.d = deployer
        self.site = site
        self.url = url
        self.service = f"lma-{site.site_id}"
        self.project = _project(site.site_id)
        self.deployment_id = f"dep-{secrets.token_hex(6)}"
        self.steps = {name: _Step(name) for name in DEPLOYMENT_STEPS}
        self.compose_yaml: str | None = None
        self.issues: list[DeploymentIssue] = []
        self.adopted = False
        self.started_service = False
        self.log_path: str | None = None
        self.hospital: Any = None
        self.registration: Any = None
        self.next_round: NextRound | None = None
        self.deadline = 0.0

    @property
    def settings(self) -> Settings:
        return self.d._settings

    @property
    def applier(self) -> ComposeApplier:
        assert self.d._applier is not None
        return self.d._applier

    def _run_step(self, name: str, action: Callable[[], tuple[str, str]]) -> bool:
        step = self.steps[name]
        began = time.monotonic()
        try:
            step.status, step.detail = action()
        except _StepFailed as failure:
            step.status, step.detail = "failed", failure.detail
        except Exception as error:  # noqa: BLE001 - every failure is reported as the step's
            logger.exception("step %s of adding %s failed", name, self.site.site_id)
            step.status, step.detail = "failed", f"The step failed: {type(error).__name__}: {error}"
        finally:
            step.seconds = round(time.monotonic() - began, 2)
        return step.status in ("done", "skipped")

    def run(self) -> Deployment:
        started_at = _utc_now()
        order = [
            ("plan", self._plan),
            ("start", self._start),
            ("answer", self._answer),
            ("attach", self._attach),
            ("enrol", self._enrol),
            ("join", self._join),
        ]
        joined = True
        for name, action in order:
            if not self._run_step(name, action):
                joined = False
                if name != "plan":
                    self._roll_back()
                break
        if joined:
            self._run_step("check", self._check)
        return Deployment(
            deployment_id=self.deployment_id,
            kind="lma",
            site_id=self.site.site_id,
            service=self.service,
            applier=self.applier.name,  # type: ignore[arg-type]
            status="joined" if joined else "failed",
            adopted=self.adopted,
            compose_yaml=self.compose_yaml,
            issues=self.issues,
            steps=[self.steps[name].model() for name in DEPLOYMENT_STEPS],
            next_round=self.next_round,
            started_at=started_at,
            finished_at=_utc_now(),
        )

    # --- The steps -------------------------------------------------------------------

    def _plan(self) -> tuple[str, str]:
        request = lma_compose_request(self.site, self.url, self.settings, self.applier.name)  # type: ignore[arg-type]
        try:
            answer = self.d._core.render_compose(request)
        except RemoteServiceUnavailable as error:
            logger.warning("core-service did not render the Compose file of %s: %s", self.service, error)
            raise _StepFailed("The core service did not answer.") from None
        issues = answer.get("issues")
        if isinstance(issues, list) and issues:
            self.issues = [
                DeploymentIssue(
                    service=issue.get("service"), code=str(issue.get("code")), value=issue.get("value")
                )
                for issue in issues
                if isinstance(issue, Mapping)
            ]
            listed = "; ".join(_issue_text(issue) for issue in issues if isinstance(issue, Mapping))
            raise _StepFailed(f"The planner refused the request: {listed}")
        compose_yaml = answer.get("compose_yaml")
        if issues != [] or not isinstance(compose_yaml, str) or not compose_yaml or answer.get("services") != [self.service]:
            raise _StepFailed("The core service returned a file this app cannot use.")
        try:
            definition = compose_service(compose_yaml, self.service)
        except ComposeApplyError:
            raise _StepFailed("The core service returned a file this app cannot use.") from None
        mismatch = rendered_service_mismatch(definition, request)
        if mismatch is not None:
            logger.warning(
                "the Compose file for %s does not match the request: %s differs", self.service, mismatch
            )
            raise _StepFailed("The core service returned a file this app cannot use.")
        self.compose_yaml = compose_yaml
        try:
            self.applier.check(compose_yaml, self.service)
        except ComposeApplyError as error:
            raise _StepFailed(f"The {self.applier.name} applier cannot apply the file: {error.message}.") from None
        return "done", f"core-service rendered the Compose file for {self.service}."

    def _start(self) -> tuple[str, str]:
        assert self.compose_yaml is not None
        wait = self.settings.deploy_timeout_seconds
        self.deadline = time.monotonic() + wait
        if self.applier.status(self.service, project=self.project) == "running":
            try:
                applied = self.applier.apply(self.compose_yaml, self.service, project=self.project, wait_seconds=wait)
            except ComposeApplyError as error:
                raise _StepFailed(f"{error.message}.") from None
            self.adopted = True
            self.log_path = applied.log_path
            return "done", "Already running; kept."
        host, port = _host_and_port(self.url)
        if _port_answers(host, port):
            raise _StepFailed(
                f"Port {port} answers, but not for a service this app started. Stop what is using it, or change "
                "HEALTHCARE_DEMO_JOINING_LMA_URLS."
            )
        try:
            applied = self.applier.apply(self.compose_yaml, self.service, project=self.project, wait_seconds=wait)
        except ComposeApplyError as error:
            if error.output_tail:
                logger.warning("%s did not start: %s\n%s", self.service, error.message, error.output_tail)
            raise _StepFailed(f"{error.message[:1].upper()}{error.message[1:]}.") from None
        self.adopted = applied.adopted
        self.started_service = not applied.adopted
        self.log_path = applied.log_path
        return "done", f"{applied.detail[:1].upper()}{applied.detail[1:]}."

    def _answer(self) -> tuple[str, str]:
        health = f"{self.url}/api/v1/health"
        while True:
            remaining = self.deadline - time.monotonic()
            if self.d._lma_answers(self.url, max(0.1, min(2.0, remaining))):
                break
            if self.applier.name == "process" and self.applier.status(self.service, project=self.project) == "stopped":
                raise _StepFailed(self._with_log("The new hospital stopped before it answered at /api/v1/health."))
            if time.monotonic() + POLL_SECONDS > self.deadline:
                seconds = f"{self.settings.deploy_timeout_seconds:g}"
                raise _StepFailed(
                    self._with_log(f"The new hospital did not answer at /api/v1/health within {seconds} seconds.")
                )
            time.sleep(POLL_SECONDS)
        try:
            report = self.d._get_json(f"{self.url}{SITE_REPORT_PATH}", timeout=self.settings.upstream_timeout_seconds)
        except (httpx.HTTPError, ValueError) as error:
            raise _StepFailed(f"The service at {self.url} answered {health} but sent no site report: {error}") from None
        reported = report.get("site_id") if isinstance(report, Mapping) else None
        if reported != self.site.site_id:
            raise _StepFailed(f"The service at {self.url} reports site {reported}, not {self.site.site_id}.")
        from app.services.roles import build_joining_hospital

        self.hospital = build_joining_hospital(self.settings, self.site, self.url, client=self.d._client)
        return "done", f"The LMA at {self.url} answers and reports site {self.site.site_id}."

    def _with_log(self, text: str) -> str:
        if self.applier.name == "process" and self.log_path:
            return f"{text} Its log is {self.log_path}."
        return text

    def _attach(self) -> tuple[str, str]:
        try:
            address = self.d._gma.register_site_address(self.site.site_id, self.url)
        except PermissionError as refused:
            raise _StepFailed(str(refused)) from None
        except RemoteServiceUnavailable as error:
            raise _StepFailed(error.public_message) from None
        return "done", f"The GMA accepted {address.url} for {self.site.site_id} ({address.source})."

    def _enrol(self) -> tuple[str, str]:
        try:
            registration = self.hospital.registration()
            self.registration = self.d._consortium.controller.register_site(registration)
        except RemoteServiceUnavailable as error:
            raise _StepFailed(error.public_message) from None
        except (KeyError, PermissionError, ValueError) as error:
            raise _StepFailed(f"The GMA did not enrol {self.site.site_id}: {error}") from None
        return "done", f"Enrolled with {registration.cohort_size} records."

    def _join(self) -> tuple[str, str]:
        if self.site.site_id in self.d._consortium.hospitals:
            return "skipped", f"{self.site.display_name} is already in the consortium."
        self.d._consortium.add_hospital(self.site, self.hospital, address=self.url)
        if self.d._forwarder is not None:
            self.d._forwarder.add_site(self.site.site_id, self.url)
        return "done", f"{self.site.display_name} is in the consortium. Its pages now go to its LMA."

    def _check(self) -> tuple[str, str]:
        try:
            plan = self.d._consortium.preview_round(f"preview-{self.deployment_id}")
        except RemoteServiceUnavailable as error:
            raise _StepFailed(error.public_message) from None
        name = self.site.display_name
        if self.site.site_id in plan.selected_sites:
            self.next_round = NextRound(selected=True, reason=None)
            return "done", f"The next round selects {name}."
        reason = next(
            (exclusion.reason for exclusion in plan.excluded_sites if exclusion.site_id == self.site.site_id),
            "the plan does not list it",
        )
        self.next_round = NextRound(selected=False, reason=reason)
        raise _StepFailed(f"The next round would not select {name}: {reason}.")

    # --- Rollback --------------------------------------------------------------------

    def _roll_back(self) -> None:
        """Undo what steps 2 to 6 did, newest first. The address stays with the GMA."""
        enrol = self.steps["enrol"]
        if enrol.status == "done" and self.registration is not None:
            began = time.monotonic()
            try:
                self.d._consortium.controller.register_site(
                    self.registration.model_copy(update={"enrolled": False})
                )
                enrol.status, enrol.detail = "undone", "Enrolment withdrawn."
            except Exception as error:  # noqa: BLE001 - the rollback reports what it could not undo
                logger.warning("the enrolment of %s could not be withdrawn: %s", self.site.site_id, error)
            enrol.seconds = round(enrol.seconds + time.monotonic() - began, 2)
        start = self.steps["start"]
        if start.status == "done" and self.started_service:
            began = time.monotonic()
            try:
                self.applier.stop(self.service, project=self.project)
                start.status, start.detail = "undone", "Stopped the service this deployment started."
            except Exception as error:  # noqa: BLE001 - the rollback reports what it could not undo
                logger.warning("%s could not be stopped: %s", self.service, error)
            start.seconds = round(start.seconds + time.monotonic() - began, 2)


# --- Building one ------------------------------------------------------------------


def build_hospital_deployer(
    settings: Settings,
    *,
    consortium: Any,
    forwarder: Any,
    gma: Any,
    core: Any,
    applier: ComposeApplier | None = None,
    clock: Callable[[], float] = time.monotonic,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
) -> HospitalDeployer:
    """The app's deployer. With the deployer off, or outside the ``app`` role, it has no applier.

    With ``process``, a :class:`LocalProcessApplier` that may start only this
    Python, in the backend directory, with the app's environment minus its own
    settings. With ``compose``, a :class:`DockerComposeApplier`. Both write to
    ``HEALTHCARE_DEMO_DEPLOY_DIR``, or a new temporary directory.
    """
    if settings.deployer not in ("process", "compose") or settings.role != "app":
        applier = None
    elif applier is None:
        workdir = Path(settings.deploy_dir) if settings.deploy_dir else Path(tempfile.mkdtemp(prefix="healthcare-demo-deploy-"))
        if settings.deployer == "process":
            applier = LocalProcessApplier(
                workdir,
                cwd=BACKEND_DIR,
                programs={sys.executable},
                base_env=_service_environment(os.environ if environ is None else environ),
            )
        else:
            applier = DockerComposeApplier(workdir)
    return HospitalDeployer(
        settings,
        consortium=consortium,
        forwarder=forwarder,
        gma=gma,
        core=core,
        applier=applier,
        clock=clock,
        client=client,
    )
