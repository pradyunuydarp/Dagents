"""Apply one service of a Compose file that core-service rendered.

core-service renders a Docker Compose file from a workload spec
(``POST /api/v1/manifests/compose``). Rendering is a planner decision; starting
the service is a side effect, so it lives here. An applier takes the rendered
text exactly as core-service returned it and starts the one service it names.
It never writes Compose itself.

Two appliers implement :class:`ComposeApplier`:

- :class:`LocalProcessApplier` runs the service's ``command`` as a local
  process, with its ``environment``. It needs no Docker, so tests and CI can
  use it. It starts only programs on an allowlist, and refuses any key it
  cannot apply.
- :class:`DockerComposeApplier` runs ``docker compose up`` on the file, for the
  one service, without its dependencies (``--no-deps``) and without building or
  pulling an image. Every command goes through one runner, so tests replace it
  with a fake.

Both apply a file that holds exactly one service. Neither checks the service's
health beyond what Compose's ``--wait`` does: the caller asks the service
itself whether it answers.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

#: How many lines of a tool's output an error keeps.
TAIL_LINES = 20

#: The top-level keys a rendered file may hold.
_TOP_LEVEL_KEYS = frozenset({"services", "networks"})

#: The service keys the process applier reads or knows it may ignore.
_PROCESS_KEYS = frozenset(
    {"image", "command", "environment", "ports", "expose", "healthcheck", "depends_on", "networks", "restart"}
)

#: The service keys the manifest compiler renders. The Compose applier refuses
#: every other key. Keys such as ``privileged``, ``pid``, ``volumes`` or
#: ``network_mode`` would give the container control of the machine that runs
#: Docker, and the planner never writes them.
_COMPOSE_KEYS = frozenset(
    {
        "image",
        "command",
        "env_file",
        "environment",
        "expose",
        "ports",
        "healthcheck",
        "depends_on",
        "networks",
        "restart",
    }
)

#: What a top-level network may say. The compiler renders ``{}`` for the
#: file's own network and ``external: true`` for a network that exists.
_NETWORK_DEFINITIONS = ({}, {"external": True})

ServiceState = Literal["running", "stopped", "not_started", "unknown"]


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(text.strip("\n").splitlines()[-lines:])


class ComposeApplyError(RuntimeError):
    """An applier could not check, start or stop a service.

    ``message`` is one plain sentence for a person. ``output_tail`` holds at
    most the last 20 lines of the tool's output, or of the process's log.
    """

    def __init__(self, message: str, output_tail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.output_tail = _tail(output_tail)


@dataclass(frozen=True)
class AppliedService:
    """What an applier did with one service."""

    #: The service's name in the Compose file.
    service: str
    #: ``"process"`` or ``"compose"``.
    applier: str
    #: True when a running service this applier started was kept, not started again.
    adopted: bool
    #: One plain line, such as ``"started process 12345"``.
    detail: str
    #: The process applier's log file. None for the Compose applier.
    log_path: str | None


@runtime_checkable
class ComposeApplier(Protocol):
    """Starts, checks and stops one service of a rendered Compose file."""

    name: str

    def check(self, compose_yaml: str, service: str) -> None:
        """Raise :class:`ComposeApplyError` when this applier cannot apply the file."""

    def apply(self, compose_yaml: str, service: str, *, project: str, wait_seconds: float) -> AppliedService:
        """Start the service, or keep it when this applier already runs it."""

    def status(self, service: str, *, project: str) -> ServiceState:
        """Say whether the service runs."""

    def stop(self, service: str, *, project: str) -> None:
        """Stop the service and remove what this applier started for it."""

    def close(self) -> None:
        """Release what the applier holds when its owner stops."""


def _compose_document(compose_yaml: str) -> dict[str, Any]:
    import yaml  # PyYAML is needed only where a file is parsed.

    try:
        document = yaml.safe_load(compose_yaml)
    except yaml.YAMLError as error:
        raise ComposeApplyError("the file is not valid YAML", str(error)) from error
    if not isinstance(document, dict):
        raise ComposeApplyError("the file is not a Compose mapping")
    return document


def compose_service(compose_yaml: str, service: str) -> dict[str, Any]:
    """Return the one service of a rendered file, or raise :class:`ComposeApplyError`.

    The file must be a mapping with a ``services`` mapping that holds exactly
    one service, named ``service``. A top-level ``networks`` key is allowed;
    any other top-level key is refused.
    """
    document = _compose_document(compose_yaml)
    unknown = sorted(str(key) for key in document if key not in _TOP_LEVEL_KEYS)
    if unknown:
        raise ComposeApplyError(f"the file has a top-level key this applier does not apply: {unknown[0]}")
    services = document.get("services")
    if not isinstance(services, dict) or len(services) != 1:
        count = len(services) if isinstance(services, dict) else 0
        raise ComposeApplyError(f"the file must hold exactly one service, not {count}")
    if service not in services:
        raise ComposeApplyError(f"the file's service is {next(iter(services))}, not {service}")
    definition = services[service]
    if not isinstance(definition, dict):
        raise ComposeApplyError(f"the service {service} is not a mapping")
    return definition


def unescape_compose(value: str) -> str:
    """Turn Compose's ``$$`` into ``$``.

    A ``$`` that is not followed by another ``$`` starts a variable, which
    would take its value from the environment of whoever runs Compose. The
    appliers apply the file as rendered, so they refuse it.
    """
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "$":
            if value[index + 1 : index + 2] != "$":
                raise ComposeApplyError("the file uses variable interpolation, which this applier does not support")
            out.append("$")
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


# --- a local process ---------------------------------------------------------


class LocalProcessApplier:
    """Runs one rendered service as a local process.

    The process runs ``command`` with ``base_env`` plus the service's
    ``environment``, in ``cwd``, in a new session, so stopping it stops every
    process it started. Its output goes to ``workdir/<service>.log``. While it
    runs, its process id is in ``workdir/<service>.pid``: the new session also
    means the process outlives a parent that dies without calling ``close``,
    so whatever supervises the parent can stop it from that file. Only
    ``command[0]`` values in ``programs`` may start. ``image``, ``ports``,
    ``expose``, ``healthcheck``, ``networks`` and ``restart`` are read and
    ignored: the process listens where its command says, and the caller checks
    its health. A service that depends on another is refused.

    Params:
    - `workdir`: where the log files go. Created when missing.
    - `cwd`: the directory the process starts in.
    - `programs`: the exact programs this applier may start.
    - `base_env`: the environment the service's ``environment`` is added to.
    - `stop_seconds`: how long ``stop`` waits after SIGTERM before SIGKILL.
    """

    name = "process"

    def __init__(
        self,
        workdir: Path,
        *,
        cwd: Path,
        programs: Collection[str],
        base_env: Mapping[str, str],
        stop_seconds: float = 10.0,
    ) -> None:
        self._workdir = Path(workdir)
        self._cwd = Path(cwd)
        self._programs = frozenset(programs)
        self._base_env = dict(base_env)
        self._stop_seconds = stop_seconds
        self._children: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()

    def log_path(self, service: str) -> Path:
        """Where the process for ``service`` writes its output."""
        return self._workdir / f"{service}.log"

    def pid_path(self, service: str) -> Path:
        """Where the process id of ``service`` is kept while this applier runs it."""
        return self._workdir / f"{service}.pid"

    def _plan(self, compose_yaml: str, service: str) -> tuple[list[str], dict[str, str]]:
        definition = compose_service(compose_yaml, service)
        for key in definition:
            if key not in _PROCESS_KEYS:
                raise ComposeApplyError(f"the process applier cannot apply `{key}`")
        if definition.get("depends_on"):
            raise ComposeApplyError("the process applier cannot apply `depends_on`; it starts one service alone")
        command = definition.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise ComposeApplyError("the process applier needs `command` as a list of strings")
        argv = [unescape_compose(item) for item in command]
        if argv[0] not in self._programs:
            raise ComposeApplyError(f"`{argv[0]}` is not a program this applier may start")
        environment = definition.get("environment", {})
        if environment is None:
            environment = {}
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
        ):
            raise ComposeApplyError("the process applier needs `environment` as a mapping of names to text")
        return argv, {key: unescape_compose(value) for key, value in environment.items()}

    def check(self, compose_yaml: str, service: str) -> None:
        self._plan(compose_yaml, service)

    def apply(self, compose_yaml: str, service: str, *, project: str, wait_seconds: float) -> AppliedService:
        argv, environment = self._plan(compose_yaml, service)
        log_path = self.log_path(service)
        with self._lock:
            child = self._children.get(service)
            if child is not None and child.poll() is None:
                return AppliedService(
                    service, self.name, True, f"already running as process {child.pid}", str(log_path)
                )
            self._workdir.mkdir(parents=True, exist_ok=True)
            try:
                with open(log_path, "ab") as log:
                    child = subprocess.Popen(
                        argv,
                        cwd=self._cwd,
                        env={**self._base_env, **environment},
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except OSError as error:
                raise ComposeApplyError(f"`{argv[0]}` could not be started", str(error)) from error
            self._children[service] = child
            self.pid_path(service).write_text(f"{child.pid}\n", encoding="utf-8")
        try:
            code = child.wait(timeout=min(1.0, wait_seconds))
        except subprocess.TimeoutExpired:
            return AppliedService(service, self.name, False, f"started process {child.pid}", str(log_path))
        with self._lock:
            if self._children.get(service) is child:
                del self._children[service]
                self.pid_path(service).unlink(missing_ok=True)
        raise ComposeApplyError(f"the process exited with status {code}", self._read_log(log_path))

    @staticmethod
    def _read_log(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def status(self, service: str, *, project: str) -> ServiceState:
        with self._lock:
            child = self._children.get(service)
        if child is None:
            return "not_started"
        return "running" if child.poll() is None else "stopped"

    def stop(self, service: str, *, project: str) -> None:
        with self._lock:
            child = self._children.pop(service, None)
        if child is None:
            return
        # start_new_session made the child the leader of its own process
        # group, so the group id is its pid and the signal reaches every
        # process it started.
        try:
            os.killpg(child.pid, signal.SIGTERM)
            # A stopped process (SIGSTOP) that handles SIGTERM acts on it only
            # when it continues.
            os.killpg(child.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=self._stop_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
        self.pid_path(service).unlink(missing_ok=True)

    def close(self) -> None:
        with self._lock:
            services = list(self._children)
        for service in services:
            self.stop(service, project="")


# --- docker compose -----------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """What a command printed and how it exited."""

    returncode: int
    stdout: str
    stderr: str


#: Runs one command and returns its result: ``runner(args, timeout=seconds)``.
Runner = Callable[..., CommandResult]


def run_command(args: list[str], *, timeout: float) -> CommandResult:
    """Run ``args`` and capture its output. The only place this module runs Docker.

    A missing program raises ``FileNotFoundError`` and a slow one
    ``subprocess.TimeoutExpired``, as ``subprocess.run`` does.
    """
    completed = subprocess.run(
        args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


class DockerComposeApplier:
    """Runs one rendered service with ``docker compose``.

    The service starts in its own Compose project, without its dependencies
    and without building or pulling an image. A container outlives the
    applier, so ``close`` leaves it running. ``apply`` always runs ``docker
    compose up``: Compose keeps a running container whose settings and image
    are unchanged, which the applier reports as adopted, and recreates one
    started from an older file or image.

    It applies only what the manifest compiler renders: a service key the
    compiler never writes, such as ``privileged`` or ``volumes``, and a
    top-level network other than ``{}`` or ``external: true`` are refused
    before Docker sees the file.

    Params:
    - `workdir`: where the files passed to ``docker compose`` are written.
    - `docker`: the Docker command.
    - `runner`: runs each command; tests pass a fake.
    - `timeout`: seconds for each command other than ``up``.
    """

    name = "compose"

    def __init__(
        self,
        workdir: Path,
        *,
        docker: str = "docker",
        runner: Runner = run_command,
        timeout: float = 120.0,
    ) -> None:
        self._workdir = Path(workdir)
        self._docker = docker
        self._runner = runner
        self._timeout = timeout

    def _run(self, args: list[str], timeout: float) -> CommandResult:
        try:
            return self._runner([self._docker, *args], timeout=timeout)
        except FileNotFoundError as error:
            raise ComposeApplyError("the docker command was not found") from error
        except subprocess.TimeoutExpired as error:
            stderr = _text(error.stderr)
            return CommandResult(-1, _text(error.stdout), f"{stderr}\ntimed out after {timeout:g} seconds")

    def _write(self, name: str, compose_yaml: str) -> Path:
        self._workdir.mkdir(parents=True, exist_ok=True)
        path = self._workdir / name
        path.write_text(compose_yaml, encoding="utf-8")
        return path

    def _filters(self, service: str, project: str) -> list[str]:
        return [
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            f"label=com.docker.compose.service={service}",
        ]

    def check(self, compose_yaml: str, service: str) -> None:
        definition = compose_service(compose_yaml, service)
        for key in definition:
            if key not in _COMPOSE_KEYS:
                raise ComposeApplyError(f"the compose applier cannot apply `{key}`")
        networks = _compose_document(compose_yaml).get("networks") or {}
        if not isinstance(networks, dict) or any(
            network not in _NETWORK_DEFINITIONS for network in networks.values()
        ):
            raise ComposeApplyError("the compose applier applies a network only as {} or external: true")
        path = self._write(f"check-{service}.yml", compose_yaml)
        result = self._run(["compose", "--file", str(path), "config", "--quiet", service], self._timeout)
        if result.returncode != 0:
            raise ComposeApplyError("docker compose refused the file", result.stderr)

    def _running_ids(self, service: str, project: str) -> list[str]:
        """The ids of the service's running containers. Empty when Docker cannot list them."""
        result = self._run(
            ["ps", "--quiet", "--filter", "status=running", *self._filters(service, project)], self._timeout
        )
        if result.returncode != 0:
            return []
        return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())

    def apply(self, compose_yaml: str, service: str, *, project: str, wait_seconds: float) -> AppliedService:
        self.check(compose_yaml, service)
        path = self._write(f"{project}.yml", compose_yaml)
        before = self._running_ids(service, project)
        base = ["compose", "--project-name", project, "--file", str(path)]
        result = self._run(
            [
                *base,
                "up",
                "--detach",
                "--no-build",
                "--pull",
                "never",
                "--no-deps",
                "--wait",
                "--wait-timeout",
                str(max(1, math.ceil(wait_seconds))),
                service,
            ],
            wait_seconds + 30,
        )
        if result.returncode != 0:
            # Remove only a container this call created. One that ran before
            # was not started here, so it is left as Compose left it.
            if not before:
                try:
                    self._run([*base, "rm", "--stop", "--force", service], self._timeout)
                except ComposeApplyError:
                    pass
            raise ComposeApplyError(f"docker compose could not start {service}", result.stderr)
        after = self._running_ids(service, project)
        if before and after == before:
            return AppliedService(service, self.name, True, "already running", None)
        if before:
            detail = f"recreated the container for {service} in project {project}, because its file or image changed"
            return AppliedService(service, self.name, False, detail, None)
        return AppliedService(
            service, self.name, False, f"started container for {service} in project {project}", None
        )

    def status(self, service: str, *, project: str) -> ServiceState:
        try:
            result = self._run(
                ["ps", "--all", *self._filters(service, project), "--format", "{{.State}}"], self._timeout
            )
        except ComposeApplyError:
            return "unknown"
        if result.returncode != 0:
            return "unknown"
        states = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not states:
            return "not_started"
        return "running" if "running" in states else "stopped"

    def stop(self, service: str, *, project: str) -> None:
        listed = self._run(["ps", "--all", "--quiet", *self._filters(service, project)], self._timeout)
        if listed.returncode != 0:
            raise ComposeApplyError(f"docker could not list the containers of {service}", listed.stderr)
        ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
        if not ids:
            return
        removed = self._run(["rm", "--force", *ids], self._timeout)
        if removed.returncode != 0:
            raise ComposeApplyError(f"docker could not remove the containers of {service}", removed.stderr)

    def close(self) -> None:
        """Leave every container running; the next applier adopts it."""


__all__ = [
    "AppliedService",
    "CommandResult",
    "ComposeApplier",
    "ComposeApplyError",
    "DockerComposeApplier",
    "LocalProcessApplier",
    "Runner",
    "ServiceState",
    "compose_service",
    "run_command",
    "unescape_compose",
]
