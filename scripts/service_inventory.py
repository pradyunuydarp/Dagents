#!/usr/bin/env python3
"""Generate and verify the Dagents service inventory.

Dagents is consumed by other backends, so "which endpoints does the framework
expose" is part of its contract. A hand-maintained list of that contract decays
the first time someone adds a route and forgets the doc, which is why this is a
generator and a checker rather than a document.

Where the endpoints come from:

- **Python services** are read from the live FastAPI app. Each service is
  imported in its own subprocess, because every service defines its own ``app``
  package (``app.core.config``, ``app.models``, ``app.api.routes``) and
  importing two of them into one interpreter would collide. Reading the routing
  table means the inventory reflects what the app actually serves, including
  the ``status_code`` overrides and the versioned aliases.
- **Java services** are read from the Spring controller sources: the
  class-level ``@RequestMapping`` prefix plus each method-level
  ``@GetMapping``/``@PostMapping``/``@PutMapping``/... suffix. Parsing source
  rather than booting Spring keeps this runnable without Maven, and the parser
  fails loudly (rather than reporting an empty service) if a ``@RestController``
  yields no routes.

Usage::

    scripts/service_inventory.py --write    # regenerate the committed files
    scripts/service_inventory.py --check    # fail if the code has drifted
    scripts/service_inventory.py --print    # dump the JSON to stdout

Run it with the repository's virtualenv, from the repository root::

    .venv/bin/python scripts/service_inventory.py --check
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = REPO_ROOT / "docs" / "reference" / "service-inventory.json"
MARKDOWN_PATH = REPO_ROOT / "docs" / "reference" / "service-inventory.md"

#: Routes FastAPI mounts on every app. They are not part of the framework's contract.
FASTAPI_BUILTIN_PATHS = frozenset(
    {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
)


@dataclass(frozen=True, slots=True)
class PythonService:
    """A FastAPI service whose routing table can be read by importing it.

    Params:
    - `name`: inventory key, matching the compose service name.
    - `role`: which layer of the framework this service belongs to.
    - `port`: the port from `env/`, recorded so consumers do not guess.
    - `module`: dotted module holding the ASGI app.
    - `attribute`: the app object's name inside that module.
    - `python_path`: entries prepended to `PYTHONPATH`, repo-root-relative.
    """

    name: str
    role: str
    port: int
    module: str
    attribute: str = "app"
    python_path: tuple[str, ...] = (".",)


@dataclass(frozen=True, slots=True)
class JavaService:
    """A Spring Boot service whose routes are parsed from its controllers.

    Params:
    - `name`: inventory key, matching the compose service name.
    - `role`: which layer of the framework this service belongs to.
    - `port`: the port from `env/`.
    - `source_root`: repo-root-relative directory scanned for controllers.
    """

    name: str
    role: str
    port: int
    source_root: str


PYTHON_SERVICES: tuple[PythonService, ...] = (
    PythonService(name="lma", role="agent", port=8010, module="agents.lma.main"),
    PythonService(name="gma", role="agent", port=8020, module="agents.gma.main"),
    PythonService(
        name="model-service",
        role="framework-service",
        port=8000,
        module="app.main",
        python_path=(".", "services/model-service"),
    ),
    PythonService(
        name="pipeline-service",
        role="framework-service",
        port=8030,
        module="app.main",
        python_path=(".", "services/pipeline-service"),
    ),
    PythonService(
        name="core-service",
        role="framework-service",
        port=8040,
        module="app.main",
        python_path=(".", "services/core-service"),
    ),
    PythonService(
        name="nl2sql-demo-backend",
        role="demo-app",
        port=8070,
        module="app.main",
        python_path=(".", "services/nl2sql-demo/backend"),
    ),
    PythonService(
        name="healthcare-demo-backend",
        role="demo-app",
        port=8080,
        module="app.main",
        python_path=(".", "apps/healthcare-demo/backend"),
    ),
)

JAVA_SERVICES: tuple[JavaService, ...] = (
    JavaService(
        name="spring-control-service",
        role="framework-service",
        port=8050,
        source_root="services/spring-services/spring-control-service/src/main/java",
    ),
    JavaService(
        name="spring-core-service",
        role="framework-service",
        port=8060,
        source_root="services/spring-services/spring-core-service/src/main/java",
    ),
)


# --------------------------------------------------------------------------- #
# Python extraction
# --------------------------------------------------------------------------- #

#: Runs inside the per-service subprocess. Kept as source text because each
#: service needs its own interpreter, and inlining it avoids a helper module
#: that would itself need to be on every service's import path.
_EXTRACTOR = r"""
import importlib, json, sys

module = importlib.import_module(sys.argv[1])
app = getattr(module, sys.argv[2])

routes = []
for route in app.routes:
    path = getattr(route, "path", None)
    methods = getattr(route, "methods", None)
    if path is None or not methods:
        continue
    routes.append(
        {
            "path": path,
            "methods": sorted(m for m in methods if m not in {"HEAD", "OPTIONS"}),
            "handler": getattr(route, "name", None),
            "status_code": getattr(route, "status_code", None),
            "summary": (getattr(route, "endpoint", None).__doc__ or "").strip().splitlines()[0]
            if getattr(getattr(route, "endpoint", None), "__doc__", None)
            else None,
        }
    )
print(json.dumps({"title": app.title, "routes": routes}))
"""


def extract_python_service(service: PythonService) -> dict[str, Any]:
    """Read one FastAPI service's routing table by importing it in a subprocess.

    Params:
    - `service`: the service descriptor, including its import path.

    What it does:
    - Runs the extractor with the service's own `PYTHONPATH`, so services that
      each define an `app` package cannot shadow one another.
    - Drops FastAPI's built-in doc routes and the `HEAD`/`OPTIONS` methods
      Starlette adds, neither of which is part of the framework's contract.

    Returns:
    - `{"title": ..., "endpoints": [...]}` sorted by path then method.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / entry) for entry in service.python_path]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    completed = subprocess.run(
        [sys.executable, "-c", _EXTRACTOR, service.module, service.attribute],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Could not import {service.name} ({service.module}):\n{completed.stderr.strip()}"
        )
    payload = json.loads(completed.stdout)

    endpoints = [
        {
            "method": method,
            "path": route["path"],
            "handler": route["handler"],
            "status_code": route["status_code"],
            "summary": route["summary"],
        }
        for route in payload["routes"]
        if route["path"] not in FASTAPI_BUILTIN_PATHS
        for method in route["methods"]
    ]
    if not endpoints:
        raise RuntimeError(f"{service.name} reported no endpoints; the extractor is broken")
    _attach_aliases(endpoints)
    endpoints.sort(key=lambda item: (item["path"], item["method"]))
    return {"title": payload["title"], "endpoints": endpoints}


def _attach_aliases(endpoints: list[dict[str, Any]]) -> None:
    """Pair each versioned alias with the legacy endpoint it delegates to.

    Params:
    - `endpoints`: one service's endpoints, mutated in place.

    What it does:
    - Uses the repo's own convention: an alias handler is named `<legacy>_v1`
      and calls `<legacy>`. Recording the pair makes the LMA/GMA duplication
      visible in the inventory instead of looking like an accident, and gives
      the inventory test something to assert parity against.

    Returns:
    - `None`; each endpoint gains an `alias_of` key (`None` when it has no twin).
    """
    by_handler = {endpoint["handler"]: endpoint for endpoint in endpoints}
    for endpoint in endpoints:
        endpoint["alias_of"] = None
    for endpoint in endpoints:
        handler = endpoint["handler"] or ""
        if not handler.endswith("_v1"):
            continue
        legacy = by_handler.get(handler[: -len("_v1")])
        if legacy is not None:
            endpoint["alias_of"] = {"method": legacy["method"], "path": legacy["path"]}


# --------------------------------------------------------------------------- #
# Java extraction
# --------------------------------------------------------------------------- #

#: `\b` after the annotation name is what keeps `@RestControllerAdvice` — an
#: exception handler, not a routed controller — out of the inventory.
_REST_CONTROLLER = re.compile(r"@RestController\b")
_CLASS_MAPPING = re.compile(r'@RequestMapping\(\s*(?:value\s*=\s*)?"([^"]*)"\s*\)')
_METHOD_MAPPING = re.compile(
    r'@(Get|Post|Put|Patch|Delete)Mapping(?:\(\s*(?:value\s*=\s*)?(?:"([^"]*)")?[^)]*\))?'
)
_METHOD_SIGNATURE = re.compile(r"public\s+[\w<>,\[\]\s.?]+?\s+(\w+)\s*\(")

_HTTP_METHOD = {
    "Get": "GET",
    "Post": "POST",
    "Put": "PUT",
    "Patch": "PATCH",
    "Delete": "DELETE",
}


def extract_java_service(service: JavaService) -> dict[str, Any]:
    """Parse one Spring service's endpoints out of its controller sources.

    Params:
    - `service`: the service descriptor, including its source root.

    What it does:
    - Finds every `@RestController`, joins its class-level `@RequestMapping`
      prefix to each method-level mapping suffix, and records the handler name.
    - Raises if a controller yields no endpoints, so a parser that stops
      understanding the source fails instead of silently reporting an empty
      service.

    Returns:
    - `{"title": ..., "endpoints": [...]}` sorted by path then method.
    """
    root = REPO_ROOT / service.source_root
    endpoints: list[dict[str, Any]] = []
    controllers = 0
    for path in sorted(root.rglob("*.java")):
        source = path.read_text(encoding="utf-8")
        if not _REST_CONTROLLER.search(source):
            continue
        controllers += 1
        parsed = _parse_controller(source)
        if not parsed:
            raise RuntimeError(
                f"{path.relative_to(REPO_ROOT)} is a @RestController but no mappings were parsed; "
                "the Java parser in scripts/service_inventory.py needs updating"
            )
        endpoints.extend(parsed)
    if controllers == 0:
        raise RuntimeError(f"No @RestController found under {service.source_root}")

    for endpoint in endpoints:
        endpoint["alias_of"] = None
    endpoints.sort(key=lambda item: (item["path"], item["method"]))
    return {"title": service.name, "endpoints": endpoints}


def _parse_controller(source: str) -> list[dict[str, Any]]:
    """Extract `(method, path, handler)` triples from one controller's source.

    Params:
    - `source`: the full Java source text.

    What it does:
    - Takes the first class-level `@RequestMapping` as the path prefix.
    - Walks each mapping annotation and reads the handler name from the `public`
      method signature that follows it.

    Returns:
    - A list of endpoint dicts, empty if the file has no mappings.
    """
    prefix_match = _CLASS_MAPPING.search(source)
    prefix = prefix_match.group(1) if prefix_match else ""
    prefix_end = prefix_match.end() if prefix_match else 0

    endpoints: list[dict[str, Any]] = []
    for match in _METHOD_MAPPING.finditer(source, prefix_end):
        suffix = match.group(2) or ""
        signature = _METHOD_SIGNATURE.search(source, match.end())
        endpoints.append(
            {
                "method": _HTTP_METHOD[match.group(1)],
                "path": _join_paths(prefix, suffix),
                "handler": signature.group(1) if signature else None,
                "status_code": None,
                "summary": None,
            }
        )
    return endpoints


def _join_paths(prefix: str, suffix: str) -> str:
    """Join a Spring class prefix and method suffix into one path."""
    joined = f"{prefix.rstrip('/')}/{suffix.lstrip('/')}" if suffix else prefix
    return joined or "/"


# --------------------------------------------------------------------------- #
# Inventory assembly
# --------------------------------------------------------------------------- #


def build_inventory() -> dict[str, Any]:
    """Collect every Python and Java service into one inventory document.

    Params:
    - None.

    What it does:
    - Extracts each service and records its role and port alongside its routes.
    - Keeps the output ordering fully deterministic so the committed files only
      change when the endpoints do.

    Returns:
    - The inventory document, ready to serialize.
    """
    services: list[dict[str, Any]] = []
    for service in PYTHON_SERVICES:
        extracted = extract_python_service(service)
        services.append(
            {
                "name": service.name,
                "language": "python",
                "framework": "fastapi",
                "role": service.role,
                "port": service.port,
                "title": extracted["title"],
                "endpoints": extracted["endpoints"],
            }
        )
    for java_service in JAVA_SERVICES:
        extracted = extract_java_service(java_service)
        services.append(
            {
                "name": java_service.name,
                "language": "java",
                "framework": "spring-boot",
                "role": java_service.role,
                "port": java_service.port,
                "title": extracted["title"],
                "endpoints": extracted["endpoints"],
            }
        )
    services.sort(key=lambda item: item["name"])
    return {
        "framework": "dagents",
        "generated_by": "scripts/service_inventory.py",
        "endpoint_count": sum(len(service["endpoints"]) for service in services),
        "services": services,
    }


def render_markdown(inventory: dict[str, Any]) -> str:
    """Render the inventory as the committed Markdown reference.

    Params:
    - `inventory`: the document returned by `build_inventory`.

    What it does:
    - Writes one table per service, with the alias column making the LMA/GMA
      legacy-plus-versioned duplication explicit.

    Returns:
    - The Markdown document as a string.
    """
    lines = [
        "# Dagents service inventory",
        "",
        "<!-- Generated by scripts/service_inventory.py. Do not edit by hand. -->",
        "",
        "Every HTTP endpoint the framework and its demo apps expose, extracted from the",
        "code: Python services from the live FastAPI routing table, Java services from",
        "their Spring controllers. Regenerate with:",
        "",
        "```bash",
        ".venv/bin/python scripts/service_inventory.py --write",
        "```",
        "",
        f"**{inventory['endpoint_count']} endpoints across {len(inventory['services'])} services.**",
        "",
        "Ports come from `env/`; never hardcode them — each service has distinct",
        "`*_PUBLIC_URL` (host) and `*_INTERNAL_URL` (compose network) values.",
        "",
        "## Services",
        "",
        "| Service | Language | Role | Port | Endpoints |",
        "|---|---|---|---|---|",
    ]
    for service in inventory["services"]:
        lines.append(
            f"| [`{service['name']}`](#{service['name']}) | {service['language']} "
            f"| {service['role']} | {service['port']} | {len(service['endpoints'])} |"
        )

    for service in inventory["services"]:
        lines += [
            "",
            f"## {service['name']}",
            "",
            f"`{service['language']}` · `{service['framework']}` · port `{service['port']}` · "
            f"{len(service['endpoints'])} endpoints",
            "",
            "| Method | Path | Handler | Notes |",
            "|---|---|---|---|",
        ]
        for endpoint in service["endpoints"]:
            notes: list[str] = []
            if endpoint.get("alias_of"):
                alias = endpoint["alias_of"]
                notes.append(f"versioned alias of `{alias['method']} {alias['path']}`")
            if endpoint.get("status_code"):
                notes.append(f"returns `{endpoint['status_code']}`")
            if endpoint.get("summary"):
                notes.append(endpoint["summary"].rstrip("."))
            handler = endpoint["handler"] or "—"
            lines.append(
                f"| {endpoint['method']} | `{endpoint['path']}` | `{handler}` | "
                f"{'; '.join(notes) or '—'} |"
            )

    lines += [
        "",
        "## Conventions this inventory records",
        "",
        "- LMA and GMA expose legacy short paths **and** `/api/v1/...` equivalents. The",
        "  versioned handler calls the legacy one, so they cannot drift; the alias column",
        "  above says which pairs exist.",
        "- The framework services (`core`, `pipeline`, `model`) are uniformly `/api/v1/...`.",
        "- `spring-control-service` and `spring-core-service` mirror control-plane and core",
        "  endpoints for consumers that integrate through the JVM.",
        "- A path ending in `:verb` (`/api/v1/sources/{source_id}:validate`) is a custom",
        "  method on a resource, not a sub-collection.",
        "",
        "`tests/test_service_inventory.py` regenerates this file and fails on drift, so an",
        "endpoint added without regenerating breaks the suite.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Entry point for `--write`, `--check`, and `--print`.

    Params:
    - `argv`: argument list, defaulting to `sys.argv[1:]`.

    What it does:
    - Builds the inventory, then writes, compares, or prints it.

    Returns:
    - `0` on success, `1` when `--check` finds drift.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="regenerate the committed files")
    group.add_argument("--check", action="store_true", help="fail if the committed files are stale")
    group.add_argument("--print", action="store_true", help="write the JSON to stdout")
    args = parser.parse_args(argv)

    inventory = build_inventory()
    serialized = json.dumps(inventory, indent=2, sort_keys=False) + "\n"
    markdown = render_markdown(inventory)

    if args.print:
        sys.stdout.write(serialized)
        return 0

    if args.write:
        JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        JSON_PATH.write_text(serialized, encoding="utf-8")
        MARKDOWN_PATH.write_text(markdown, encoding="utf-8")
        print(
            f"wrote {inventory['endpoint_count']} endpoints across "
            f"{len(inventory['services'])} services to "
            f"{JSON_PATH.relative_to(REPO_ROOT)} and {MARKDOWN_PATH.relative_to(REPO_ROOT)}"
        )
        return 0

    stale: list[str] = []
    for path, expected in ((JSON_PATH, serialized), (MARKDOWN_PATH, markdown)):
        actual = path.read_text(encoding="utf-8") if path.exists() else None
        if actual != expected:
            stale.append(str(path.relative_to(REPO_ROOT)))
    if stale:
        print(
            "Service inventory is stale: " + ", ".join(stale) + "\n"
            "Regenerate it with: .venv/bin/python scripts/service_inventory.py --write",
            file=sys.stderr,
        )
        return 1
    print(f"service inventory is current ({inventory['endpoint_count']} endpoints)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
