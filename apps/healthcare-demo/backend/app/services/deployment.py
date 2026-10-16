"""Add a hospital to the running stroke demo.

A person who runs the demo on their own machine, with the agents as their own
processes (``HEALTHCARE_DEMO_AGENTS=network``) and started with ``--deploy``,
can add a hospital that is not in the consortium at start (``JOINING_SITES``)
while the demo runs. The app runs seven steps, in order. The framework makes
every decision and does every side effect except the join:

1. ``plan``: core-service renders the new LMA's Compose file
   (``POST /api/v1/manifests/compose``, the OCaml manifest compiler). The app
   never writes Compose. It checks that the file holds only the service it
   asked for, with the image, command, environment, env file, health check and
   network it asked for.
2. ``start``: a :class:`~agents.common.infrastructure.compose_apply.ComposeApplier`
   starts the one service in that exact file, as a local process or with
   ``docker compose``. The new LMA is a stock Dagents LMA; it reads its tokens,
   its database address and the GMA's address from the env file the Compose
   file names, so the file holds no secret.
3. ``answer``: the new LMA answers ``/api/v1/health`` and reports its site,
   read from its own schema in Postgres.
4. ``attach``: the Dagents GMA accepts the LMA's address
   (``PUT /api/v1/federation/sites/{site_id}/address``), which it allows only
   for the pair in its ``GMA_JOINABLE_SITE_ENDPOINTS``.
5. ``enrol``: the GMA enrols the site from the registration the LMA reports
   (``POST /api/v1/federation/sites:refresh``).
6. ``join``: the app adds the hospital to its services
   (:meth:`~app.services.agents.HealthcareDemo.add_site`), so its pages come
   from its LMA and every later round invites it.
7. ``check``: the GMA's planner plans the next round and says whether it
   selects the hospital (``POST /api/v1/federation/rounds:plan``). The app
   never decides eligibility.

A failure in steps 2 to 6 undoes what this attempt did, newest first: it
withdraws the enrolment, has the GMA drop the address and stops the service it
started. :meth:`HealthcareDemo.add_site` undoes its own work when it fails, so
nothing undoes a finished join. A failed ``check`` keeps the hospital and
gives the planner's reason. For a hospital that had joined already, whose LMA
is started again, the address and the enrolment belong to that hospital and
stay.

Adding a hospital starts a process or a container, so it is off unless
``HEALTHCARE_DEMO_DEPLOYER`` turns it on, and :func:`check_deployer` refuses
every setting that would let a visitor of a public demo start one. No answer
or log line holds a token. Any page can read ``GET /api/v1/deployments``, so
no answer holds the new LMA's port, the interpreter's path or a folder of this
machine either: the answers show ``<port>``, ``<python>``, ``<env folder>``
and ``<deploy folder>`` in their place (:class:`HiddenValues`).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping
from urllib.parse import urlsplit

import yaml

from agents.common.infrastructure.compose_apply import (
    ComposeApplier,
    ComposeApplyError,
    DockerComposeApplier,
    LocalProcessApplier,
    compose_service,
    format_env_file,
    unescape_compose,
)

from app.core.config import Settings, lma_token_variable, lma_url_variable, site_value
from app.domain.dataset import (
    JOINING_SITES,
    SITES,
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
from app.services.agents import AgentError, AgentUnavailableError, FrameworkCalls, LmaProbe, lma_environment, loggable
from app.services.core_client import CoreServiceClient, CoreServiceUnavailable

logger = logging.getLogger(__name__)

#: The values ``HEALTHCARE_DEMO_DEPLOYER`` may take.
DEPLOYERS = ("off", "process", "compose")

#: Hosts that mean "this machine" in an address.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost")

#: Hosts an API that listens on them can be reached at from this machine.
_THIS_MACHINE = ("127.0.0.1", "localhost", "0.0.0.0", "::", "")

#: The services ``docker-compose.yml`` and ``docker-compose.deploy.yml`` run. A
#: new hospital's container joins their network, so its name must differ from
#: each of them.
COMPOSE_SERVICES: tuple[str, ...] = (
    "postgres",
    "setup",
    "gma",
    *(f"lma-{site.site_id}" for site in SITES),
    "healthcare-demo-backend",
    "healthcare-demo-frontend",
    "core-service",
)

#: The only variables a new LMA process takes from the app's environment.
#: Everything else it needs is in its Compose file or its env file, so the
#: app's own tokens and any secret a person exported never reach it.
CHILD_ENVIRONMENT = ("PATH", "PYTHONPATH", "DAGENTSC_BIN", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")

#: How the new LMA's container checks its own health. It reads the port from
#: ``PORT``, so the file holds no ``host:port``.
HEALTH_CHECK: dict[str, Any] = {
    "command": [
        "python",
        "-c",
        "import os, urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ['PORT'], timeout=3)",
    ],
    "interval_seconds": 5,
    "timeout_seconds": 5,
    "retries": 12,
    "start_period_seconds": 10,
}

#: Seconds the checks of core-service and the GMA are reused by
#: ``GET /api/v1/deployments``. Each one can wait for a timeout.
AVAILABILITY_CACHE_SECONDS = 15

#: Attempts kept in memory, newest first.
DEPLOYMENTS_KEPT = 20

#: Seconds between two health checks of a new LMA.
POLL_SECONDS = 0.5

#: Seconds a check that a port answers may take.
PORT_CHECK_SECONDS = 0.5

#: Seconds an LMA that is already in the consortium has to answer before it
#: counts as gone, so the hospital can be started again.
RUNNING_CHECK_SECONDS = 2.0

#: Seconds the new LMA has to send its site report.
SITE_REPORT_SECONDS = 30.0

#: Characters of a tool's last output line a step's detail shows.
CAUSE_CHARACTERS = 300

#: What the answers show in place of each value of this machine.
PORT_SHOWN = "<port>"
PYTHON_SHOWN = "<python>"
ENV_FOLDER_SHOWN = "<env folder>"
DEPLOY_FOLDER_SHOWN = "<deploy folder>"

README_SECTION = '"Add a hospital while the demo runs" in the demo\'s README'
REASON_IN_PROCESS = (
    "This server runs the Dagents agents inside the API process. Adding a hospital starts a new LMA process, "
    f"which only a network-mode run started with --deploy on your own machine does. See {README_SECTION}."
)
REASON_OFF = "Adding a hospital is turned off. A network-mode run started with --deploy turns it on."
REASON_NO_CORE = (
    "The Dagents core service does not answer, so the app cannot ask it for the new LMA's Compose file."
)
REASON_NO_GMA = "The Dagents GMA does not answer."
REASON_NOT_JOINABLE = (
    "The GMA does not accept an address for {site_id}. Start the GMA with GMA_JOINABLE_SITE_ENDPOINTS naming it."
)


class DeployerSettingError(ValueError):
    """A deploy setting the app refuses to start with. The message names variables, never their values."""


class DeploymentUnavailable(RuntimeError):
    """Adding a hospital is not available now. The message is the reason."""


class DeploymentConflict(RuntimeError):
    """The hospital is in the consortium and its LMA answers. The message says so."""


# --- Settings ----------------------------------------------------------------------


def env_folder(settings: Settings) -> str:
    """The folder that holds each new LMA's env file: ``HEALTHCARE_DEMO_DEPLOY_ENV_DIR``, else the deploy folder."""
    return settings.deploy_env_dir.strip() or settings.deploy_dir.strip()


def env_file_path(site: Site, settings: Settings) -> Path:
    """Where the env file of ``site``'s LMA is: ``lma-<site_id>.env`` in :func:`env_folder`.

    Raises ``ValueError`` when neither folder is set.
    """
    folder = env_folder(settings)
    if not folder:
        raise ValueError("HEALTHCARE_DEMO_DEPLOY_ENV_DIR or HEALTHCARE_DEMO_DEPLOY_DIR is not set")
    return Path(folder) / f"lma-{site.site_id}.env"


def _host_port(url: str) -> tuple[str, int] | None:
    """``(host, port)`` of an address that is ``http://host:port`` and nothing else, or ``None``."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "http"
        or not parts.hostname
        or port is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        return None
    return parts.hostname, port


def _machine(host: str) -> str:
    """One name for every host that means this machine, so two addresses on it compare equal."""
    return "127.0.0.1" if host in _THIS_MACHINE else host


def check_deployer(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """Refuse deploy settings that are unsafe or cannot work. Called when the app starts.

    Raises :class:`DeployerSettingError` at the first rule that fails:

    1. ``HEALTHCARE_DEMO_DEPLOYER`` is ``off``, ``process`` or ``compose``.
    2. ``off`` needs nothing else.
    3. The agents run as their own processes (``HEALTHCARE_DEMO_AGENTS=network``).
    4. Never on Render: the public demo never starts a process or a container.
    5. ``HEALTHCARE_DEMO_DATABASE_URL`` is set: the new LMA reads its own schema.
    6. Each joining hospital has its LMA address and the app's ``site`` token
       for it (``HEALTHCARE_DEMO_LMA_<SITE>_URL`` and ``_TOKEN``).
    7. Each address is ``http://host:port`` and nothing else, and no other
       agent or the API uses that host and port.
    8. In process mode each address is on 127.0.0.1 or localhost, and
       ``HEALTHCARE_DEMO_API_HOST`` is 127.0.0.1 or localhost; the start
       scripts bind the API to that host. The setting does not decide where a
       uvicorn started by hand listens, so the route also refuses a client
       that is not this machine (:func:`app.api.deploy_routes.is_this_machine`).
    9. In compose mode each address is ``http://lma-<site_id>:<port>``, the
       container's name on the Compose network.
    10. ``HEALTHCARE_DEMO_CORE_SERVICE_URL`` is set.
    11. The env folder exists and holds each new LMA's env file, readable by
        its owner only.
    """
    environ = os.environ if environ is None else environ
    value = settings.deployer
    if value not in DEPLOYERS:
        raise DeployerSettingError(f"HEALTHCARE_DEMO_DEPLOYER must be off, process or compose, not {value!r}.")
    if value == "off":
        return
    if settings.agents_mode != "network":
        raise DeployerSettingError(
            f"HEALTHCARE_DEMO_DEPLOYER={value} needs HEALTHCARE_DEMO_AGENTS=network. Agents inside the API process "
            "cannot reach a new LMA process."
        )
    if environ.get("RENDER"):
        raise DeployerSettingError(
            "HEALTHCARE_DEMO_DEPLOYER must be off on Render. The public demo never starts a process or a container."
        )
    if not settings.database_url.strip():
        raise DeployerSettingError(
            f"HEALTHCARE_DEMO_DEPLOYER={value} needs HEALTHCARE_DEMO_DATABASE_URL. A new hospital's LMA reads its "
            "records from its own schema."
        )
    addresses: dict[str, tuple[str, int]] = {}
    for site in JOINING_SITES:
        url_name, token_name = lma_url_variable(site.site_id), lma_token_variable(site.site_id)
        for name in (url_name, token_name):
            if not site_value(name, environ):
                raise DeployerSettingError(f"HEALTHCARE_DEMO_DEPLOYER={value} needs {name}.")
        address = _host_port(site_value(url_name, environ))
        if address is None:
            raise DeployerSettingError(f"{url_name} needs http, a host and an explicit port, and nothing else.")
        addresses[site.site_id] = address
    taken = {(_machine(settings.api_host.strip()), settings.api_port)}
    for url in (settings.gma_url, *(site_value(lma_url_variable(site.site_id), environ) for site in SITES)):
        other = _host_port(url) if url else None
        if other is not None:
            taken.add((_machine(other[0]), other[1]))
    for site_id, (host, port) in addresses.items():
        if (_machine(host), port) in taken:
            raise DeployerSettingError(
                f"{lma_url_variable(site_id)} uses the host and port of the GMA, an LMA or the API."
            )
    if value == "process":
        for site_id, (host, _port) in addresses.items():
            if host not in LOOPBACK_HOSTS:
                raise DeployerSettingError(
                    f"In process mode {lma_url_variable(site_id)} must be on 127.0.0.1 or localhost."
                )
        if settings.api_host.strip() not in LOOPBACK_HOSTS:
            raise DeployerSettingError(
                "In process mode HEALTHCARE_DEMO_API_HOST must be 127.0.0.1 or localhost, so that only this machine "
                "can ask the API to start a process."
            )
    else:
        for site_id, (host, _port) in addresses.items():
            if host != f"lma-{site_id}":
                raise DeployerSettingError(
                    f"In compose mode {lma_url_variable(site_id)} must be http://lma-{site_id}:<port>, the "
                    "container's name on the Compose network."
                )
    if not settings.core_service_url.strip():
        raise DeployerSettingError(f"HEALTHCARE_DEMO_DEPLOYER={value} needs HEALTHCARE_DEMO_CORE_SERVICE_URL.")
    folder = env_folder(settings)
    if not folder:
        raise DeployerSettingError(
            "HEALTHCARE_DEMO_DEPLOY_ENV_DIR or HEALTHCARE_DEMO_DEPLOY_DIR must name the folder that holds each new "
            "LMA's env file."
        )
    if not Path(folder).is_dir():
        raise DeployerSettingError(
            "The env folder in HEALTHCARE_DEMO_DEPLOY_ENV_DIR or HEALTHCARE_DEMO_DEPLOY_DIR does not exist."
        )
    for site in JOINING_SITES:
        path = env_file_path(site, settings)
        if not path.is_file():
            raise DeployerSettingError(
                f"The env folder has no {path.name}. The start script writes it; see {README_SECTION}."
            )
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise DeployerSettingError(
                f"{path.name} can be read by other users. Make it readable by its owner only (chmod 600)."
            )


def write_env_file(path: Path, values: Mapping[str, str], *, owner: tuple[int, int] | None = None, mode: int = 0o600) -> None:
    """Write ``values`` as a new LMA's env file that only its owner can read.

    The file is written beside ``path`` with mode 0600, given to ``owner``
    (``(uid, gid)``) when one is named, set to ``mode``, then moved into place,
    so no other user can read it at any moment. Values are written in the form
    the appliers and Docker Compose read the same way
    (:func:`~agents.common.infrastructure.compose_apply.format_env_file`). Raises
    ``ValueError`` for a value outside that form; the message names the
    variable, never the value.
    """
    text = format_env_file(values)
    temporary = path.with_name(f".{path.name}.new")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    if owner is not None:
        os.chown(temporary, *owner)
    temporary.chmod(mode)
    temporary.replace(path)


# --- The Compose request -------------------------------------------------------------


def framework_root() -> Path:
    """The Dagents checkout a new LMA process starts in: ``DAGENTS_HOME``, or the folder that holds ``agents/``."""
    home = os.environ.get("DAGENTS_HOME", "").strip()
    if home:
        return Path(home).resolve()
    return Path(sys.modules[LocalProcessApplier.__module__].__file__ or "").resolve().parents[3]


def lma_compose_request(
    site: Site, url: str, settings: Settings, mode: Literal["process", "compose"]
) -> dict[str, Any]:
    """The workload spec core-service renders for a new hospital's LMA. Pure.

    The LMA is a stock Dagents LMA (``agents.lma.main:app``) with the
    hospital's ``LMA_*`` settings (:func:`~app.services.agents.lma_environment`)
    and ``PORT``, which its health check reads. Its tokens, its database
    address and the GMA's address are in its env file (:func:`env_file_path`),
    so the request holds no secret. It holds the port, this Python's path and
    the env file's path, which the answers hide (:class:`HiddenValues`).

    - ``process``: this Python runs uvicorn on the host and port of ``url``,
      on its own network (``default``), with no port published.
    - ``compose``: the image's uvicorn listens on every interface of the
      container, on the running demo's network, which is external to the new
      file. The port is exposed on that network, not published on the host.
    """
    address = _host_port(url)
    if address is None:
        raise ValueError(f"{lma_url_variable(site.site_id)} needs http, a host and an explicit port")
    host, port = address
    environment = {**lma_environment(site, database=True), "PORT": str(port)}
    serve = ["agents.lma.main:app", "--host", host if mode == "process" else "0.0.0.0", "--port", str(port)]
    serve += ["--log-level", "warning"]
    if mode == "process":
        args = [sys.executable, "-m", "uvicorn", *serve]
        network, external, existing, ports = "default", False, [], []
    else:
        args = ["uvicorn", *serve]
        network, external = settings.deploy_network, True
        existing, ports = list(COMPOSE_SERVICES), [{"container_port": port}]
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
                "env_files": [str(env_file_path(site, settings))],
                "env": [{"name": name, "value": value} for name, value in environment.items()],
                "ports": ports,
                "health_check": HEALTH_CHECK,
                "depends_on": [],
            }
        ],
    }


#: The service keys the file core-service renders from :func:`lma_compose_request`.
#: ``ports`` is not one: the request never publishes a port.
RENDERED_KEYS = frozenset(
    {"image", "command", "env_file", "environment", "expose", "healthcheck", "networks", "restart"}
)


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
    answers at ``HEALTHCARE_DEMO_CORE_SERVICE_URL``. So the app compares the
    parts that decide what runs, which secrets it reads and where it listens
    with the component it asked for: the keys, the image, the command, the env
    file, the environment, the exposed port, the health check's command and
    the network. It compares parsed values, not text, so a change in how the
    planner formats the file does not matter.
    """
    component = request["components"][0]
    unknown = sorted(str(key) for key in definition if key not in RENDERED_KEYS)
    if unknown:
        return f"the key {unknown[0]}"
    exposed = [str(port["container_port"]) for port in component["ports"] if port.get("host_port") is None]
    expected: dict[str, Any] = {
        "image": component["image"],
        "command": list(component["args"]),
        "env_file": list(component["env_files"]) or None,
        "environment": {item["name"]: item["value"] for item in component["env"]},
        "expose": exposed or None,
        "healthcheck": ["CMD", *component["health_check"]["command"]],
        "networks": [request["network"]],
    }
    healthcheck = definition.get("healthcheck")
    expose = definition.get("expose")
    actual = {
        **{key: definition.get(key) for key in expected},
        "expose": [str(port) for port in expose] if isinstance(expose, list) else expose,
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


# --- What the answers show ---------------------------------------------------------


@dataclass(frozen=True)
class HiddenValues:
    """Values of this machine that no answer shows, and the placeholder each answer shows instead.

    Any page can read ``GET /api/v1/deployments``, because the app answers
    every origin. So the answers never show the new LMA's port (with its
    host it is the LMA's address), the interpreter's path or a folder on this
    machine, which name the account and the folder that holds the env files.

    - `port`: the new LMA's port. It is hidden only where it stands as a whole
      number, so ``19123`` keeps its digits when the port is ``9123``.
    - `paths`: each path and its placeholder, longest first, so a folder
      inside another one gets its own placeholder.
    """

    port: int | None
    paths: tuple[tuple[str, str], ...]

    def _port_pattern(self) -> re.Pattern[str] | None:
        return None if self.port is None else re.compile(rf"(?<![0-9]){self.port}(?![0-9])")

    def hide(self, text: str) -> str:
        """``text`` with each path and the port replaced by its placeholder."""
        for path, placeholder in self.paths:
            text = text.replace(path, placeholder)
        pattern = self._port_pattern()
        return text if pattern is None else pattern.sub(PORT_SHOWN, text)

    def shown_in(self, text: str) -> bool:
        """Whether ``text`` still holds a path or the port."""
        pattern = self._port_pattern()
        return any(path in text for path, _ in self.paths) or (pattern is not None and bool(pattern.search(text)))


def hidden_values(url: str, settings: Settings, folders: Iterable[str] = ()) -> HiddenValues:
    """The values of this machine an attempt for the LMA at ``url`` must not show.

    They are the LMA's port, this Python's path, the env folder, the deploy
    folder and ``folders``, such as the folder a log file is in.
    """
    address = _host_port(url)
    paths: dict[str, str] = {sys.executable: PYTHON_SHOWN}
    named = [(env_folder(settings), ENV_FOLDER_SHOWN), (settings.deploy_dir.strip(), DEPLOY_FOLDER_SHOWN)]
    named += [(folder, DEPLOY_FOLDER_SHOWN) for folder in folders]
    for folder, placeholder in named:
        path = Path(folder) if folder else None
        # The root folder would turn every "/" into a placeholder.
        if path is not None and path.is_absolute() and path != Path(path.anchor):
            paths.setdefault(str(path), placeholder)
    ordered = tuple(sorted(paths.items(), key=lambda item: (-len(item[0]), item[0])))
    return HiddenValues(port=None if address is None else address[1], paths=ordered)


def _texts(node: Any) -> Iterable[str]:
    """Every key and value of a parsed YAML document, as text, with Compose's ``$$`` read as ``$``."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _texts(key)
            yield from _texts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _texts(item)
    elif node is not None:
        text = str(node)
        yield text
        yield text.replace("$$", "$")


def shown_compose_file(compose_yaml: str, hidden: HiddenValues) -> str | None:
    """The Compose file as the answers show it: ``compose_yaml`` with ``hidden``'s placeholders.

    The applier gets the file exactly as core-service returned it. The answers
    show the same text with ``<port>``, ``<python>`` and the folder
    placeholders in place of this machine's values. The result is parsed
    again, and when any value still holds one of them, for example because
    the file writes it escaped, the answers show no file (``None``).
    """
    shown = hidden.hide(compose_yaml)
    try:
        document = yaml.safe_load(shown)
    except yaml.YAMLError:
        return None
    if not isinstance(document, Mapping) or any(hidden.shown_in(text) for text in _texts(document)):
        return None
    return shown


# --- One attempt -----------------------------------------------------------------------


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
    text = str(issue.get("code"))
    if issue.get("value") is not None:
        text += f" ({issue.get('value')})"
    service = issue.get("service")
    return text + (f" for {service}" if service else " for the request")


def _sentence(text: str) -> str:
    text = f"{text[:1].upper()}{text[1:]}"
    return text if text.endswith(".") else f"{text}."


def _log_sentence(log_path: str | None) -> str:
    """Where the process's log is: its file name in the deploy folder. The folder is never shown."""
    return f" Its log is {Path(log_path).name} in the deploy folder." if log_path else ""


def _apply_error_detail(error: ComposeApplyError, lead: str = "") -> str:
    """A step's detail for an applier error: ``lead``, its message, the last line
    of the tool's output, which usually names the cause, and the process's log file."""
    text = lead + (error.message if lead else _sentence(error.message).rstrip("."))
    cause = next((line.strip() for line in reversed(error.output_tail.splitlines()) if line.strip()), "")
    if cause:
        text += f": {cause[:CAUSE_CHARACTERS]}"
    return _sentence(text) + _log_sentence(error.log_path)


def _project(site_id: str) -> str:
    return f"healthcare-demo-lma-{site_id}"


def child_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The part of ``environ`` a new LMA process starts from: only the names in :data:`CHILD_ENVIRONMENT`."""
    return {name: environ[name] for name in CHILD_ENVIRONMENT if name in environ}


ProbeFactory = Callable[[str, str, str, float], Any]


class HospitalDeployer:
    """Adds hospitals to the running consortium, one at a time.

    The route holds the pilot's flight while :meth:`deploy` runs, so a pilot
    and an addition never run at the same time.

    Params:
    - `settings`: the app's settings.
    - `demo`: returns the app's services
      (:class:`~app.services.agents.HealthcareDemo`) when called. It is called
      on each use, so tests can replace the services.
    - `core`: the core-service client. ``None`` when adding is off.
    - `applier`: what starts the service. ``None`` when adding is off.
    - `addresses`: each joining hospital's LMA address and the app's ``site``
      token for it. ``repr`` shows neither.
    - `probe`: builds a :class:`~app.services.agents.LmaProbe` from ``(site_id,
      url, token, timeout_s)``. Tests pass a fake.
    - `clock`: seconds, for the availability cache. Tests pass a fake.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        demo: Callable[[], Any],
        core: Any,
        applier: ComposeApplier | None,
        addresses: Mapping[str, tuple[str, str]],
        probe: ProbeFactory,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._demo = demo
        self._core = core
        self._applier = applier
        self._addresses = dict(addresses)
        self._probe = probe
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, str | None] | None = None
        self._deployments: list[Deployment] = []

    def __repr__(self) -> str:
        return f"HospitalDeployer(applier={self.applier_name!r})"

    # --- What GET /api/v1/deployments shows ----------------------------------------------

    @property
    def applier_name(self) -> Literal["off", "process", "compose"]:
        if self._applier is None or self._settings.deployer not in ("process", "compose"):
            return "off"
        return self._settings.deployer  # type: ignore[return-value]

    def unavailable_reason(self) -> str | None:
        """Why adding a hospital is not available now, or ``None`` when it is.

        The agents' mode and the setting are read each time. The checks of
        core-service and the GMA are reused for :data:`AVAILABILITY_CACHE_SECONDS`.
        """
        if self._settings.agents_mode != "network":
            return REASON_IN_PROCESS
        if self.applier_name == "off":
            return REASON_OFF
        now = self._clock()
        with self._lock:
            if self._cached is not None and now - self._cached[0] < AVAILABILITY_CACHE_SECONDS:
                return self._cached[1]
        reason = self._check_services()
        with self._lock:
            self._cached = (now, reason)
        return reason

    def _check_services(self) -> str | None:
        if self._core is None or not self._core.health():
            return REASON_NO_CORE
        demo = self._demo()
        try:
            gma = demo.agents.gma()
            listing = gma.site_addresses()
        except AgentError as error:
            logger.warning("the GMA's site addresses could not be read: %s", error.without_detail())
            return REASON_NO_GMA
        for site_id in self._addresses:
            if site_id not in listing.joinable:
                return REASON_NOT_JOINABLE.format(site_id=site_id)
        # A GMA that restarted forgot the addresses it accepted: send them again.
        held = {state.site_id for state in listing.sites if state.source == "joined"}
        for site_id, url in demo.joined().items():
            if site_id in held:
                continue
            try:
                gma.register_site_address(site_id, url)
            except AgentError as error:
                logger.warning("the GMA did not take the address of %s again: %s", site_id, error.without_detail())
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
            logger.warning("the state of lma-%s could not be read: %s", site.site_id, loggable(error))
            return "unknown"

    def view(self) -> DeploymentsView:
        """What ``GET /api/v1/deployments`` returns. It reads no hospital's records."""
        reason = self.unavailable_reason()
        demo = self._demo()
        hospitals = [
            JoinableHospital(
                site_id=site.site_id,
                display_name=site.display_name,
                country=site.country,
                records=expected_record_count(site),
                records_source=records_source(site),
                in_consortium=demo.has_site(site.site_id),
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
            hospitals=hospitals,
            deployments=deployments,
        )

    # --- POST /api/v1/deployments --------------------------------------------------------

    def lma_answers(self, site_id: str, url: str, token: str, timeout_s: float) -> bool:
        """Whether the LMA at ``url`` answers its health check within ``timeout_s``."""
        probe = self._probe(site_id, url, token, timeout_s)
        try:
            return bool(probe.answers(timeout_s))
        finally:
            probe.close()

    def deploy(self, site_id: str) -> Deployment:
        """Add one hospital to the running consortium, and report every step.

        Raises :class:`DeploymentUnavailable` when adding is not available,
        and :class:`DeploymentConflict` when the hospital is in the consortium
        and its LMA answers. Any other failure is a step with status
        ``failed`` in the returned :class:`~app.models.Deployment`, whose
        ``framework_calls`` lists the agent calls the attempt made.
        """
        reason = self.unavailable_reason()
        if reason is not None:
            raise DeploymentUnavailable(reason)
        site = joining_site_for(site_id)
        if site_id not in self._addresses:
            raise DeploymentUnavailable(
                f"{site.display_name} has no {lma_url_variable(site_id)} and {lma_token_variable(site_id)}, so it "
                "cannot be added."
            )
        url, token = self._addresses[site_id]
        demo = self._demo()
        if demo.has_site(site_id) and self.lma_answers(site_id, url, token, RUNNING_CHECK_SECONDS):
            raise DeploymentConflict(f"{site.display_name} is already in the consortium.")
        recorder = FrameworkCalls.current()
        before = len(recorder.entries()) if recorder is not None else 0
        try:
            deployment = _Attempt(self, demo, site, url, token).run()
        finally:
            self.clear_availability()
        calls = recorder.entries()[before:] if recorder is not None else []
        deployment = Deployment.model_validate({**deployment.model_dump(mode="json"), "framework_calls": calls})
        with self._lock:
            self._deployments = [deployment, *self._deployments][:DEPLOYMENTS_KEPT]
        return deployment

    def close(self) -> None:
        """Stop what the applier started, when it stops services with the app (process mode), and close the client."""
        try:
            if self._applier is not None:
                self._applier.close()
        finally:
            if self._core is not None:
                self._core.close()


class _Attempt:
    """The seven steps of one addition, with the rollback of steps 2 to 6."""

    def __init__(self, deployer: HospitalDeployer, demo: Any, site: Site, url: str, token: str) -> None:
        self.d = deployer
        self.demo = demo
        self.site = site
        self.url = url
        self.token = token
        self.service = f"lma-{site.site_id}"
        self.project = _project(site.site_id)
        self.deployment_id = f"dep-{secrets.token_hex(6)}"
        self.steps = {name: _Step(name) for name in DEPLOYMENT_STEPS}
        self.compose_yaml: str | None = None
        self.issues: list[DeploymentIssue] = []
        self.adopted = False
        self.started_service = False
        self.log_path: str | None = None
        #: Folders of this machine the attempt saw, such as a log file's.
        self.folders: set[str] = set()
        self.registration: Any = None
        self.next_round: NextRound | None = None
        self.deadline = 0.0
        # A hospital that joined earlier, whose LMA is started again: its
        # address and enrolment are the hospital's, and a rollback keeps them.
        self.was_joined = bool(demo.has_site(site.site_id))

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
            logger.error("step %s of adding %s failed: %s: %s", name, self.site.site_id, type(error).__name__, loggable(error))
            step.status, step.detail = "failed", f"The step failed ({type(error).__name__}). The app's log has the cause."
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
        hidden = hidden_values(self.url, self.settings, self.folders)
        steps = [self.steps[name].model() for name in DEPLOYMENT_STEPS]
        return Deployment(
            deployment_id=self.deployment_id,
            kind="lma",
            site_id=self.site.site_id,
            service=self.service,
            applier=self.applier.name,  # type: ignore[arg-type]
            status="joined" if joined else "failed",
            adopted=self.adopted,
            compose_yaml=None if self.compose_yaml is None else shown_compose_file(self.compose_yaml, hidden),
            issues=[
                issue.model_copy(update={"value": None if issue.value is None else hidden.hide(issue.value)})
                for issue in self.issues
            ],
            steps=[step.model_copy(update={"detail": hidden.hide(step.detail)}) for step in steps],
            next_round=self.next_round,
            started_at=started_at,
            finished_at=_utc_now(),
        )

    # --- The steps -------------------------------------------------------------------

    def _plan(self) -> tuple[str, str]:
        request = lma_compose_request(self.site, self.url, self.settings, self.applier.name)  # type: ignore[arg-type]
        try:
            answer = self.d._core.render_compose(request)
        except CoreServiceUnavailable as error:
            logger.warning("core-service did not render the Compose file of %s: %s", self.service, error)
            raise _StepFailed("The core service did not answer.") from None
        issues = answer.get("issues")
        if isinstance(issues, list) and issues:
            self.issues = [
                DeploymentIssue(service=issue.get("service"), code=str(issue.get("code")), value=issue.get("value"))
                for issue in issues
                if isinstance(issue, Mapping)
            ]
            listed = "; ".join(_issue_text(issue) for issue in issues if isinstance(issue, Mapping))
            raise _StepFailed(f"The planner refused the request: {listed}.")
        compose_yaml = answer.get("compose_yaml")
        unusable = "The core service returned a file this app cannot use."
        if issues != [] or not isinstance(compose_yaml, str) or not compose_yaml or answer.get("services") != [self.service]:
            logger.warning("the answer for %s does not match the request: it is not one rendered file", self.service)
            raise _StepFailed(unusable)
        try:
            definition = compose_service(compose_yaml, self.service)
        except ComposeApplyError as error:
            logger.warning("the Compose file for %s does not match the request: %s", self.service, error.message)
            raise _StepFailed(unusable) from None
        mismatch = rendered_service_mismatch(definition, request)
        if mismatch is not None:
            logger.warning("the Compose file for %s does not match the request: %s differs", self.service, mismatch)
            raise _StepFailed(unusable)
        self.compose_yaml = compose_yaml
        try:
            self.applier.check(compose_yaml, self.service)
        except ComposeApplyError as error:
            self.compose_yaml = None
            lead = f"The {self.applier.name} applier cannot apply the file: "
            raise _StepFailed(_apply_error_detail(error, lead)) from None
        return "done", f"core-service rendered the Compose file for {self.service}."

    def _start(self) -> tuple[str, str]:
        assert self.compose_yaml is not None
        wait = self.settings.deploy_timeout_seconds
        self.deadline = time.monotonic() + wait
        replaced = ""
        if self.applier.status(self.service, project=self.project) == "running":
            if self.d.lma_answers(self.site.site_id, self.url, self.token, RUNNING_CHECK_SECONDS):
                # The applier keeps it, or, with docker compose, recreates it
                # when it was started from an older file or image.
                try:
                    applied = self.applier.apply(self.compose_yaml, self.service, project=self.project, wait_seconds=wait)
                except ComposeApplyError as error:
                    self._saw_log(error.log_path)
                    raise _StepFailed(_apply_error_detail(error)) from None
                self._saw_log(applied.log_path)
                self.adopted = applied.adopted
                self.started_service = not applied.adopted
                self.log_path = applied.log_path
                if applied.adopted:
                    return "done", "Already running; kept."
                return "done", _sentence(applied.detail)
            # It runs but does not answer, for example because it hangs. Kept,
            # it would fail the answer step after the whole timeout, and a
            # rollback leaves a kept service alone, so it would never be
            # replaced. This applier started it, so it may stop it.
            logger.warning("%s runs but does not answer; stopping it to start it again", self.service)
            try:
                self.applier.stop(self.service, project=self.project)
            except ComposeApplyError as error:
                raise _StepFailed(
                    f"The service runs but does not answer, and it could not be stopped: {error.message}."
                ) from None
            replaced = "The service ran but did not answer, so the app stopped it. "
        address = _host_port(self.url)
        if self.applier.name == "process" and address is not None and _port_answers(*address):
            raise _StepFailed(
                "The new LMA's port answers, but not for a service this app started. Stop what is using it, or "
                f"change {lma_url_variable(self.site.site_id)}."
            )
        try:
            applied = self.applier.apply(self.compose_yaml, self.service, project=self.project, wait_seconds=wait)
        except ComposeApplyError as error:
            if error.output_tail:
                logger.warning("%s did not start: %s\n%s", self.service, error.message, loggable(error.output_tail))
            else:
                logger.warning("%s did not start: %s", self.service, error.message)
            self._saw_log(error.log_path)
            raise _StepFailed(_apply_error_detail(error)) from None
        self._saw_log(applied.log_path)
        self.adopted = applied.adopted
        self.started_service = not applied.adopted
        self.log_path = applied.log_path
        return "done", f"{replaced}{_sentence(applied.detail)}"

    def _saw_log(self, log_path: str | None) -> None:
        if log_path:
            self.folders.add(str(Path(log_path).parent))

    def _with_log(self, text: str) -> str:
        if self.applier.name == "process":
            return text + _log_sentence(self.log_path)
        return text

    def _answer(self) -> tuple[str, str]:
        site_id = self.site.site_id
        probe = self.d._probe(site_id, self.url, self.token, SITE_REPORT_SECONDS)
        try:
            while True:
                remaining = self.deadline - time.monotonic()
                if probe.answers(max(0.1, min(2.0, remaining))):
                    break
                if self.applier.name == "process" and self.applier.status(self.service, project=self.project) == "stopped":
                    raise _StepFailed(self._with_log("The new LMA stopped before it answered its health check."))
                if time.monotonic() + POLL_SECONDS > self.deadline:
                    seconds = f"{self.settings.deploy_timeout_seconds:g}"
                    raise _StepFailed(
                        self._with_log(f"The new LMA did not answer its health check within {seconds} seconds.")
                    )
                time.sleep(POLL_SECONDS)
            try:
                info = probe.site_info()
            except AgentError as error:
                if error.status in (401, 403):
                    raise _StepFailed(
                        f"The new LMA answered its health check, but refused the app's site token ({error.status})."
                    ) from None
                logger.warning("the new LMA of %s sent no site report: %s", site_id, loggable(error))
                raise _StepFailed("The new LMA answered its health check, but sent no site report.") from None
        finally:
            probe.close()
        if info.site_id != site_id:
            raise _StepFailed(f"The new LMA reports site {info.site_id}, not {site_id}.")
        kind = str(info.source.kind)
        if kind != "postgres":
            article = "an" if kind[:1] in "aeiou" else "a"
            raise _StepFailed(f"The new LMA reads {article} {kind} source, not its schema in Postgres.")
        return "done", f"The new LMA answers and reports site {site_id}, read from its schema in Postgres."

    def _gma(self) -> Any:
        return self.demo.agents.gma()

    def _attach(self) -> tuple[str, str]:
        site_id = self.site.site_id
        try:
            self._gma().register_site_address(site_id, self.url)
        except AgentUnavailableError:
            raise _StepFailed("The GMA did not answer.") from None
        except AgentError as error:
            if error.code == "not_joinable":
                raise _StepFailed(REASON_NOT_JOINABLE.format(site_id=site_id)) from None
            if error.code == "address_differs":
                raise _StepFailed(
                    f"The GMA accepts {site_id} only at the address in its GMA_JOINABLE_SITE_ENDPOINTS, and that "
                    f"differs from {lma_url_variable(site_id)}."
                ) from None
            if error.code == "configured_elsewhere":
                raise _StepFailed(f"The GMA lists {site_id} in its GMA_SITE_ENDPOINTS with another address.") from None
            raise _StepFailed(f"The GMA refused the address ({error.status}, {error.code}).") from None
        return "done", f"The GMA accepted the new LMA's address for {site_id}."

    def _enrol(self) -> tuple[str, str]:
        site_id = self.site.site_id
        try:
            registrations = self._gma().refresh_sites()
        except AgentUnavailableError:
            raise _StepFailed("The GMA did not answer.") from None
        except AgentError as error:
            raise _StepFailed(f"The GMA refused the refresh ({error.status}, {error.code}).") from None
        registration = next((item for item in registrations if item.site_id == site_id), None)
        if registration is None:
            raise _StepFailed(
                f"The GMA's refresh did not enrol {site_id}. Check that the GMA holds the new LMA's coordinator token."
            )
        self.registration = registration
        return "done", f"The GMA enrolled {site_id} with {registration.cohort_size} records."

    def _join(self) -> tuple[str, str]:
        name = self.site.display_name
        if self.demo.has_site(self.site.site_id):
            return "skipped", f"{name} is already in the consortium."
        self.demo.add_site(self.site, self.url, self.token)
        return "done", f"{name} is in the consortium. Its pages now come from its LMA."

    def _check(self) -> tuple[str, str]:
        try:
            plan = self.demo.preview_round(f"preview-{self.deployment_id}")
        except AgentUnavailableError:
            raise _StepFailed("The GMA did not answer.") from None
        except AgentError as error:
            raise _StepFailed(f"The GMA could not plan the next round ({error.status}, {error.code}).") from None
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

    def _undo(self, name: str, action: Callable[[], None], detail: str) -> None:
        step = self.steps[name]
        began = time.monotonic()
        try:
            action()
            step.status, step.detail = "undone", detail
        except Exception as error:  # noqa: BLE001 - the rollback reports what it could not undo
            logger.warning("step %s of adding %s could not be undone: %s", name, self.site.site_id, loggable(error))
            step.detail = f"{step.detail} It could not be undone."
        step.seconds = round(step.seconds + time.monotonic() - began, 2)

    def _roll_back(self) -> None:
        """Undo what steps 2 to 5 did, newest first. A failed join undoes its own work."""
        site_id = self.site.site_id
        if self.steps["enrol"].status == "done" and self.registration is not None and not self.was_joined:
            withdrawn = self.registration.model_copy(update={"enrolled": False})
            self._undo("enrol", lambda: self._gma().register_site(withdrawn), "Enrolment withdrawn.")
        if self.steps["attach"].status == "done" and not self.was_joined:
            self._undo(
                "attach",
                lambda: self._gma().remove_site_address(site_id),
                "The GMA no longer holds the new LMA's address.",
            )
        if self.steps["start"].status == "done" and self.started_service:
            self._undo(
                "start",
                lambda: self.applier.stop(self.service, project=self.project),
                "Stopped the service this attempt started.",
            )


# --- Building one ----------------------------------------------------------------------


def joining_addresses(environ: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """Each joining hospital with both its LMA address and the app's ``site`` token for it set."""
    found: dict[str, tuple[str, str]] = {}
    for site in JOINING_SITES:
        url = site_value(lma_url_variable(site.site_id), environ)
        token = site_value(lma_token_variable(site.site_id), environ)
        if url and token:
            found[site.site_id] = (url, token)
    return found


def build_hospital_deployer(
    settings: Settings,
    *,
    demo: Callable[[], Any],
    core: Any = None,
    applier: ComposeApplier | None = None,
    probe: ProbeFactory | None = None,
    clock: Callable[[], float] = time.monotonic,
    environ: Mapping[str, str] | None = None,
) -> HospitalDeployer:
    """The app's deployer. With the deployer off, or the agents in process, it has no applier and no client.

    With ``process``, a :class:`LocalProcessApplier` that may start only this
    Python, in the Dagents checkout (:func:`framework_root`), from the
    allow-listed part of the environment (:func:`child_environment`), and read
    env files only in the env folder. With ``compose``, a
    :class:`DockerComposeApplier` with the same env folder. Both write to
    ``HEALTHCARE_DEMO_DEPLOY_DIR``, or a new temporary folder.
    """
    environ = os.environ if environ is None else environ
    on = settings.deployer in ("process", "compose") and settings.agents_mode == "network"
    if not on:
        applier = None
        core = None
    else:
        if applier is None:
            folder = settings.deploy_dir.strip()
            workdir = Path(folder) if folder else Path(tempfile.mkdtemp(prefix="healthcare-demo-deploy-"))
            env_dirs = [Path(env_folder(settings) or workdir)]
            if settings.deployer == "process":
                applier = LocalProcessApplier(
                    workdir,
                    cwd=framework_root(),
                    programs={sys.executable},
                    base_env=child_environment(environ),
                    env_dirs=env_dirs,
                )
            else:
                applier = DockerComposeApplier(workdir, env_dirs=env_dirs)
        if core is None and settings.core_service_url.strip():
            core = CoreServiceClient(settings.core_service_url.strip())

    def lma_probe(site_id: str, url: str, token: str, timeout_s: float) -> LmaProbe:
        return LmaProbe(site_id, url, token, timeout_s=timeout_s)

    return HospitalDeployer(
        settings,
        demo=demo,
        core=core,
        applier=applier,
        addresses=joining_addresses(environ),
        probe=probe or lma_probe,
        clock=clock,
    )


__all__ = [
    "AVAILABILITY_CACHE_SECONDS",
    "CHILD_ENVIRONMENT",
    "COMPOSE_SERVICES",
    "DEPLOYERS",
    "DEPLOYMENTS_KEPT",
    "DEPLOY_FOLDER_SHOWN",
    "DeployerSettingError",
    "DeploymentConflict",
    "DeploymentUnavailable",
    "ENV_FOLDER_SHOWN",
    "HEALTH_CHECK",
    "HiddenValues",
    "HospitalDeployer",
    "PORT_SHOWN",
    "PYTHON_SHOWN",
    "REASON_IN_PROCESS",
    "REASON_NOT_JOINABLE",
    "REASON_NO_CORE",
    "REASON_NO_GMA",
    "REASON_OFF",
    "RENDERED_KEYS",
    "build_hospital_deployer",
    "check_deployer",
    "child_environment",
    "env_file_path",
    "env_folder",
    "framework_root",
    "hidden_values",
    "joining_addresses",
    "lma_compose_request",
    "rendered_service_mismatch",
    "shown_compose_file",
    "write_env_file",
]
