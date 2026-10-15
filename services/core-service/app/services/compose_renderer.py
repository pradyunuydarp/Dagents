"""Render a Docker Compose file in Python, for when the planner cannot start.

The Compose file is the OCaml manifest compiler's decision: `dagentsc manifest
compile` reads the request, lists every issue and renders the file. This module
is a port of that code, used by `ManifestService.render_compose` only when the
`dagentsc` binary cannot be started (outside a container, without
`DAGENTSC_BIN`). When the planner runs and fails, the service does not use it.

Each function names the OCaml function it copies:

- `bindings/ocaml/lib/common_ir/json_codec.ml` reads the request;
- `bindings/ocaml/lib/manifest_compiler/dagents_manifest_compiler.ml` lists the
  issues and renders the file.

Both renderers are checked against the same golden files in
`services/core-service/tests/fixtures/compose/`. `ComposeFallbackTests` in
`tests/test_compose_manifest.py` runs this module on them, and
`ComposePlannerTests` runs the planner. A change to a rule changes the OCaml
module, this module and the golden files in one commit.

The module is pure: it reads no file, no clock and no environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.models import ComposeManifestRequest

# The words `workload_kind_of_string` (dagents_common_ir.ml) accepts for each kind.
_KIND_WORDS = {
    "Deployment": "Deployment",
    "deployment": "Deployment",
    "Job": "Job",
    "job": "Job",
    "CronJob": "CronJob",
    "cronjob": "CronJob",
    "cron_job": "CronJob",
    "Service": "Service",
    "service": "Service",
    "ConfigMap": "ConfigMap",
    "configmap": "ConfigMap",
    "config_map": "ConfigMap",
    "ServiceAccount": "ServiceAccount",
    "serviceaccount": "ServiceAccount",
    "service_account": "ServiceAccount",
}
_DEPENDENCY_CONDITIONS = ("service_started", "service_healthy", "service_completed_successfully")

# Character classes for the name rules, written out so that no Unicode letter or
# digit passes, as in the OCaml byte loops. Never use str.isalnum or str.isdigit.
_LOWER_OR_DIGIT = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")
_COMPOSE_NAME_CHARS = _LOWER_OR_DIGIT | frozenset("-_.")
_ENV_FIRST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
_DIGITS = frozenset("0123456789")
_ENV_CHARS = _ENV_FIRST_CHARS | _DIGITS

_DEFAULT_HOST_IP = "127.0.0.1"

# The planner's integers are OCaml's 63-bit `int` (`min_int` and `max_int` on a
# 64-bit machine). The codec's `int_of_string` fails on a JSON number outside
# this range, and dagentsc exits with status 2.
_PLANNER_INT_MIN = -(2**62)
_PLANNER_INT_MAX = 2**62 - 1


@dataclass(frozen=True)
class _Target:
    """`compose_target`: where the file runs and what already runs there."""

    merge: bool
    network: str
    external_network: bool
    existing_services: list[str]


@dataclass(frozen=True)
class _Port:
    """`port`: `host_port` is None when the port is not published (`NotPublished`)."""

    container_port: int
    host_ip: str
    host_port: int | None


@dataclass(frozen=True)
class _HealthCheck:
    """`health_check`: exactly one of `command` (`ExecTest`) and `shell` (`ShellTest`) is set."""

    command: list[str] | None
    shell: str | None
    interval_seconds: int
    timeout_seconds: int
    retries: int
    start_period_seconds: int


@dataclass(frozen=True)
class _Component:
    """The fields of `workload_component` that the Compose target reads."""

    name: str
    image: str
    kind: str
    replicas: int
    args: list[str]
    env_files: list[str]
    env: list[tuple[str, str]]
    ports: list[_Port]
    health_check: _HealthCheck | None
    depends_on: list[tuple[str, str]]


def planner_payload(request: ComposeManifestRequest) -> dict[str, Any]:
    """Build the snake_case payload sent to `dagentsc manifest compile`.

    `run_dagentsc` converts its keys to camelCase. The payload has no
    `plan_id`, so the planner answers with its default, `dagents-plan`. Null
    values (`host_port`, `health_check`, `command`, `shell`) mean "absent" to
    the planner's JSON codec.
    """
    return {
        "target": {
            "kind": "compose",
            "placement": request.placement,
            "network": request.network,
            "external_network": request.external_network,
            "existing_services": list(request.existing_services),
        },
        "components": [component.model_dump() for component in request.components],
    }


def quote(value: str) -> str:
    """Write a caller value as a double-quoted YAML scalar. Copies `quote_compose_scalar`.

    `$` becomes `$$`, so Docker Compose passes one `$` to the container instead
    of reading a variable. Control characters become escapes, so a value cannot
    end its line. OCaml escapes byte by byte and this function character by
    character; both leave every character from U+0080 up unchanged, so the
    results are equal.
    """
    out = ['"']
    for char in value:
        code = ord(char)
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "$":
            out.append("$$")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\x{code:02X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def valid_compose_name(name: str) -> bool:
    """A Compose service or network name: `[a-z0-9][a-z0-9_.-]*`. Copies `valid_compose_name`."""
    return name != "" and name[0] in _LOWER_OR_DIGIT and all(char in _COMPOSE_NAME_CHARS for char in name)


def valid_environment_name(name: str) -> bool:
    """An environment variable name: `[A-Za-z_][A-Za-z0-9_]*`. Copies `valid_env_name`."""
    return name != "" and name[0] in _ENV_FIRST_CHARS and all(char in _ENV_CHARS for char in name)


def valid_ipv4(address: str) -> bool:
    """A dotted IPv4 address: four parts, each `0` or `[1-9][0-9]{0,2}`, at most 255. Copies `valid_ipv4`."""
    parts = address.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not 1 <= len(part) <= 3 or not all(char in _DIGITS for char in part):
            return False
        if len(part) > 1 and part[0] == "0":
            return False
        if int(part) > 255:
            return False
    return True


def valid_port(port: int) -> bool:
    """A TCP port: 1 to 65535. Copies `valid_port`."""
    return 1 <= port <= 65535


# Reading the payload. These helpers copy the field readers at the top of
# json_codec.ml: the same defaults, and a ValueError where the codec raises
# Invalid_argument (the planner then exits with status 2).


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _planner_int(value: int, key: str) -> int:
    """Copies the codec's `int_of_string` on a large JSON number: refuse what does not fit in 63 bits."""
    if not _PLANNER_INT_MIN <= value <= _PLANNER_INT_MAX:
        raise ValueError(f"Integer field does not fit in the planner's int: {key}")
    return value


def _required_string(fields: dict[str, Any], key: str) -> str:
    """Copies `string_field`."""
    if key not in fields:
        raise ValueError(f"Missing JSON field: {key}")
    value = fields[key]
    if not isinstance(value, str):
        raise ValueError(f"Expected string field: {key}")
    return value


def _optional_string(fields: dict[str, Any], key: str) -> str | None:
    """Copies `string_option_field`: absent or null gives None."""
    value = fields.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"Expected nullable string field: {key}")


def _int_with_default(fields: dict[str, Any], key: str, default: int) -> int:
    """Copies `int_field_with_default`: absent gives the default, null is an error."""
    if key not in fields:
        return default
    value = fields[key]
    if not _is_int(value):
        raise ValueError(f"Expected int field: {key}")
    return _planner_int(value, key)


def _optional_int(fields: dict[str, Any], key: str) -> int | None:
    """Copies `int_option_field`: absent or null gives None."""
    value = fields.get(key)
    if value is None:
        return None
    if _is_int(value):
        return _planner_int(value, key)
    raise ValueError(f"Expected nullable int field: {key}")


def _bool_with_default(fields: dict[str, Any], key: str, default: bool) -> bool:
    """Copies `bool_field_with_default`: absent gives the default, null is an error."""
    if key not in fields:
        return default
    value = fields[key]
    if not isinstance(value, bool):
        raise ValueError(f"Expected bool field: {key}")
    return value


def _string_list(fields: dict[str, Any], key: str) -> list[str]:
    """Copies `string_list_field`: absent gives an empty list, null is an error."""
    if key not in fields:
        return []
    values = fields[key]
    if not isinstance(values, list):
        raise ValueError(f"Expected list field: {key}")
    if not all(isinstance(value, str) for value in values):
        raise ValueError(f"Expected string list field: {key}")
    return list(values)


def _object_list(fields: dict[str, Any], key: str) -> list[Any]:
    """The codec's `match ... with Some (`List values) -> values | _ -> []` for `env` and `ports`."""
    values = fields.get(key)
    return values if isinstance(values, list) else []


def _as_object(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Expected {what} object")
    return value


def _read_target(payload: dict[str, Any]) -> _Target:
    """Read a Compose `target`. Copies `deployment_target_of_yojson`.

    Raises ValueError for a target that is not Compose, because this module
    renders the Compose target only.
    """
    raw = payload.get("target")
    fields: dict[str, Any] = {}
    if isinstance(raw, dict):
        kind = _required_string(raw, "kind")
        fields = raw
    elif isinstance(raw, str):
        kind = raw
    elif raw is None:
        kind = "kubernetes"
    else:
        raise ValueError("Expected deployment target")
    if kind == "kubernetes":
        raise ValueError("render_compose_plan renders the Compose target only")
    if kind != "compose":
        raise ValueError(f"Unknown deployment target: {kind}")

    placement = _optional_string(fields, "placement")
    if placement not in (None, "own", "merge"):
        raise ValueError(f"Unknown compose placement: {placement}")
    network = _optional_string(fields, "network")
    return _Target(
        merge=placement == "merge",
        network="default" if network is None else network,
        external_network=_bool_with_default(fields, "external_network", False),
        existing_services=_string_list(fields, "existing_services"),
    )


def _read_port(raw: Any) -> _Port:
    """Copies `port_of_yojson`. `host_ip` is read only when `host_port` is set."""
    fields = _as_object(raw, "port")
    host_port = _optional_int(fields, "host_port")
    host_ip = _optional_string(fields, "host_ip") if host_port is not None else None
    return _Port(
        container_port=_int_with_default(fields, "container_port", 0),
        host_ip=_DEFAULT_HOST_IP if host_ip is None else host_ip,
        host_port=host_port,
    )


def _read_health_check(raw: Any) -> _HealthCheck | None:
    """Copies `health_check_of_yojson`: exactly one of `command` and `shell`."""
    if raw is None:
        return None
    fields = _as_object(raw, "health check")
    command = None if fields.get("command") is None else _string_list(fields, "command")
    shell = _optional_string(fields, "shell")
    if (command is None) == (shell is None):
        raise ValueError("Expected exactly one of healthCheck.command and healthCheck.shell")
    return _HealthCheck(
        command=command,
        shell=shell,
        interval_seconds=_int_with_default(fields, "interval_seconds", 30),
        timeout_seconds=_int_with_default(fields, "timeout_seconds", 5),
        retries=_int_with_default(fields, "retries", 3),
        start_period_seconds=_int_with_default(fields, "start_period_seconds", 10),
    )


def _read_dependency(raw: Any) -> tuple[str, str]:
    """Copies `service_dependency_of_yojson`: no condition means `service_started`."""
    fields = _as_object(raw, "dependency")
    service = _required_string(fields, "service")
    condition = _optional_string(fields, "condition")
    if condition is None:
        return service, "service_started"
    if condition not in _DEPENDENCY_CONDITIONS:
        raise ValueError(f"Unknown dependency condition: {condition}")
    return service, condition


def _read_component(raw: Any) -> _Component:
    """Copies the fields of `workload_component_of_yojson` that the Compose target reads."""
    fields = _as_object(raw, "workload component")
    kind_word = fields.get("kind")
    if isinstance(kind_word, str):
        if kind_word not in _KIND_WORDS:
            raise ValueError(f"Unknown workload kind: {kind_word}")
        kind = _KIND_WORDS[kind_word]
    else:
        kind = "Deployment"
    env = []
    for item in _object_list(fields, "env"):
        variable = _as_object(item, "env var")
        env.append((_required_string(variable, "name"), _required_string(variable, "value")))
    depends_on = fields.get("depends_on")
    if depends_on is not None and not isinstance(depends_on, list):
        raise ValueError("Expected list field: dependsOn")
    return _Component(
        name=_required_string(fields, "name"),
        image=_required_string(fields, "image"),
        kind=kind,
        replicas=_int_with_default(fields, "replicas", 1),
        args=_string_list(fields, "args"),
        env_files=_string_list(fields, "env_files"),
        env=env,
        ports=[_read_port(port) for port in _object_list(fields, "ports")],
        health_check=_read_health_check(fields.get("health_check")),
        depends_on=[_read_dependency(dependency) for dependency in depends_on or []],
    )


# Issues.


def _in_cycle(components: list[_Component], component: _Component) -> bool:
    """Whether `component` reaches its own name through in-plan dependencies. Copies `in_cycle`.

    An edge goes to the first component with the dependency's name, and an edge
    from a component to itself is skipped (that is a `self_dependency`). A
    component that only leads into a cycle is not in it.
    """

    def first_named(name: str) -> _Component | None:
        return next((candidate for candidate in components if candidate.name == name), None)

    def successors(current: _Component) -> list[_Component]:
        found = [first_named(name) for name, _ in current.depends_on if name != current.name]
        return [successor for successor in found if successor is not None]

    visited: list[str] = []
    pending = successors(component)
    while pending:
        current = pending.pop(0)
        if current.name == component.name:
            return True
        if current.name in visited:
            continue
        visited.append(current.name)
        pending = successors(current) + pending
    return False


def compose_issues(target: _Target, components: list[_Component]) -> list[dict[str, str | None]]:
    """List every reason the spec cannot become a Compose file. Copies `compose_issues`.

    The order is part of the contract: issues with the whole spec first, then
    each component's issues in request order, each in the order the API
    reference lists them. Each issue is `{"service", "code", "value"}`, as
    `yojson_of_compose_issue` writes it.
    """
    issues: list[dict[str, str | None]] = []

    def spec_issue(code: str, value: str | None = None) -> None:
        issues.append({"service": None, "code": code, "value": value})

    if not components:
        spec_issue("no_services")
    if not valid_compose_name(target.network):
        spec_issue("invalid_network_name", target.network)
    for name in target.existing_services:
        if not valid_compose_name(name):
            spec_issue("invalid_service_name", name)

    names = [component.name for component in components]
    used_host_ports: list[int] = []
    for index, component in enumerate(components):

        def issue(code: str, value: str | None = None, service: str = component.name) -> None:
            issues.append({"service": service, "code": code, "value": value})

        if not valid_compose_name(component.name):
            issue("invalid_service_name", component.name)
        if component.name in names[:index]:
            issue("duplicate_service_name")
        if component.name in target.existing_services:
            issue("service_name_in_use")
        if component.image == "":
            issue("empty_image")
        if component.kind not in ("Deployment", "Job"):
            issue("no_compose_equivalent", component.kind)
        if component.replicas > 1:
            issue("unsupported_replicas", str(component.replicas))

        env_names = [name for name, _ in component.env]
        for name in env_names:
            if not valid_environment_name(name):
                issue("invalid_environment_name", name)
        for position, name in enumerate(env_names):
            if name in env_names[:position]:
                issue("duplicate_environment_name", name)

        for port in component.ports:
            if not valid_port(port.container_port):
                issue("invalid_port", str(port.container_port))
            if port.host_port is None:
                continue
            if not valid_ipv4(port.host_ip):
                issue("invalid_host_ip", port.host_ip)
            if not valid_port(port.host_port):
                issue("invalid_port", str(port.host_port))
            elif port.host_port in used_host_ports:
                issue("host_port_conflict", str(port.host_port))
            else:
                used_host_ports.append(port.host_port)

        health = component.health_check
        if health is not None:
            if health.command == [] or health.shell == "":
                issue("empty_health_check")
            if (
                health.interval_seconds < 1
                or health.timeout_seconds < 1
                or health.retries < 1
                or health.start_period_seconds < 0
            ):
                issue("invalid_health_check_timing")

        seen: list[str] = []
        for name, _ in component.depends_on:
            if name == component.name:
                issue("self_dependency")
            elif name in seen:
                issue("duplicate_dependency", name)
            elif name in names:
                pass
            elif name in target.existing_services:
                if not target.merge:
                    issue("dependency_outside_project", name)
            else:
                issue("unknown_dependency", name)
            seen.append(name)

        if _in_cycle(components, component):
            issue("dependency_cycle")
    return issues


# Rendering.


def _flow_list(values: list[str]) -> str:
    """Copies `flow_list`: a YAML flow sequence of values that are already quoted."""
    return "[" + ", ".join(values) + "]"


def _service_lines(component: _Component, network: str) -> list[str]:
    """Render one component as the lines of its Compose service, unindented. Copies `render_compose_service`.

    Names are quoted like values: YAML reads a bare `134`, `null` or `True` as
    a number, null or a boolean, and Docker Compose refuses such a key.
    """
    lines = [f"{quote(component.name)}:", f"  image: {quote(component.image)}"]
    if component.args:
        lines.append("  command: " + _flow_list([quote(arg) for arg in component.args]))
    if component.env_files:
        lines.append("  env_file:")
        lines.extend(f"    - {quote(path)}" for path in component.env_files)
    if component.env:
        lines.append("  environment:")
        lines.extend(f"    {quote(name)}: {quote(value)}" for name, value in component.env)
    exposed = [port for port in component.ports if port.host_port is None]
    published = [port for port in component.ports if port.host_port is not None]
    if exposed:
        lines.append("  expose:")
        lines.extend(f'    - "{port.container_port}"' for port in exposed)
    if published:
        lines.append("  ports:")
        lines.extend(f'    - "{port.host_ip}:{port.host_port}:{port.container_port}"' for port in published)
    health = component.health_check
    if health is not None:
        if health.command is not None:
            test = _flow_list(['"CMD"', *(quote(part) for part in health.command)])
        else:
            test = _flow_list(['"CMD-SHELL"', quote(health.shell or "")])
        lines.extend(
            [
                "  healthcheck:",
                f"    test: {test}",
                f"    interval: {health.interval_seconds}s",
                f"    timeout: {health.timeout_seconds}s",
                f"    retries: {health.retries}",
                f"    start_period: {health.start_period_seconds}s",
            ]
        )
    if component.depends_on:
        lines.append("  depends_on:")
        for name, condition in component.depends_on:
            lines.extend([f"    {quote(name)}:", f"      condition: {condition}"])
    lines.extend(["  networks:", f"    - {quote(network)}"])
    lines.append('  restart: "no"' if component.kind == "Job" else "  restart: unless-stopped")
    return lines


def render_compose_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Render a Compose plan from a planner payload. Copies `compile_compose_plan`.

    Params:
    - `payload`: the snake_case payload sent to `run_dagentsc`, with a Compose
      `target` (the word `"compose"` or an object). Absent keys take the
      planner's defaults, and `kind` accepts the same words as the planner.

    Returns the snake_case dict that `run_dagentsc` returns for the same payload:
    `plan_id`, `services`, `issues` and `compose_yaml`, which is None exactly
    when `issues` is not empty.

    Raises ValueError where the planner exits with status 2: an unknown word,
    a missing required field, a number outside the planner's 63-bit integers,
    or a health check with both or neither of `command` and `shell`. It reads
    only the fields the Compose target uses, so it does not check the fields
    only the Kubernetes target reads (`schedule`, `resources` and others); the
    route's request models refuse those fields.
    """
    target = _read_target(payload)
    raw_components = payload.get("components")
    components = [_read_component(raw) for raw in raw_components] if isinstance(raw_components, list) else []
    plan_id = payload.get("plan_id")
    issues = compose_issues(target, components)

    compose_yaml = None
    if not issues:
        lines = ["services:"]
        for component in components:
            lines.extend("  " + line for line in _service_lines(component, target.network))
        if target.external_network:
            lines.extend(["networks:", f"  {quote(target.network)}:", "    external: true"])
        else:
            lines.extend(["networks:", f"  {quote(target.network)}: {{}}"])
        compose_yaml = "\n".join(lines)

    return {
        "plan_id": plan_id if isinstance(plan_id, str) else "dagents-plan",
        "services": [component.name for component in components],
        "issues": issues,
        "compose_yaml": compose_yaml,
    }
