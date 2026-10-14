#!/usr/bin/env python3
"""Generate and verify the data behind the API reference.

The API reference on the framework site (`#/api`) has two sources:

- **Facts from the code**, written by this script to
  `docs/reference/api-reference.json`: every endpoint's parameters, request
  body and responses with their types, defaults and constraints, the handler
  function and the classes it calls, the schemas of the documented objects,
  and the commands of the planner program `dagentsc`.
- **Prose and structure**, written by people in `docs/reference/api/*.md`:
  which interface each endpoint belongs to, what it does, and its side effects.

Where the facts come from:

- **FastAPI services**: each app is imported in its own subprocess (the
  services each define an `app` package, so two cannot share an interpreter)
  and `app.openapi()` gives the parameters, schemas and constraints. The
  routing table gives the handler, and reading the handler's source gives the
  classes it calls.
- **Spring services**: the controllers and the request and response records
  are parsed from source, as `scripts/service_inventory.py` does, so this runs
  without Maven.
- **dagentsc**: the commands are parsed from `bindings/ocaml/bin/dagentsc.ml`:
  its usage text, its dispatch and the library functions each command calls.

The service list, ports and roles come from `scripts/service_inventory.py`, so
the two generated files cannot disagree about which services exist.

Usage::

    .venv/bin/python scripts/api_reference.py --write   # regenerate the file
    .venv/bin/python scripts/api_reference.py --check   # fail if it is stale
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
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import service_inventory as inventory  # noqa: E402  (needs the sys.path entry above)

OUTPUT_PATH = REPO_ROOT / "docs" / "reference" / "api-reference.json"
API_DOCS_DIR = REPO_ROOT / "docs" / "reference" / "api"
EXAMPLES_DIR = API_DOCS_DIR / "examples"

#: Files in docs/reference/api/ that are not module pages.
NOT_MODULES = frozenset({"README.md", "overview.md"})

DAGENTSC_SOURCE = REPO_ROOT / "bindings" / "ocaml" / "bin" / "dagentsc.ml"
OCAML_LIB_DIR = REPO_ROOT / "bindings" / "ocaml" / "lib"

#: The name the hierarchy and the examples use for the planner program.
PLANNER = "dagentsc"


# --------------------------------------------------------------------------- #
# Anchors
# --------------------------------------------------------------------------- #


def endpoint_id(service: str, method: str, path: str) -> str:
    """The anchor of one endpoint on the site, for example
    `core-service-post-api-v1-workloads-compile`. The site reads it from the
    generated file, so it is computed in one place."""
    return re.sub(r"[^a-z0-9]+", "-", f"{service} {method} {path}".lower()).strip("-")


def command_id(words: list[str] | tuple[str, ...]) -> str:
    """The anchor of one dagentsc command, for example `dagentsc-governance-restrict`."""
    return re.sub(r"[^a-z0-9]+", "-", " ".join([PLANNER, *words]).lower()).strip("-")


def heading_anchor(text: str) -> str:
    """The id the site's Markdown renderer gives a heading (`site/src/markdown.ts`).

    It drops backticks, asterisks and underscores, then follows GitHub: lower
    case, punctuation removed, each space a hyphen.
    """
    cleaned = re.sub(r"[`*_]", "", text)
    cleaned = re.sub(r"<[^>]+>", "", cleaned.lower().strip())
    cleaned = re.sub(r"[^\w\s-]", "", cleaned).replace("_", "")
    return re.sub(r"\s", "-", cleaned)


# --------------------------------------------------------------------------- #
# The Markdown hierarchy
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"^(```+|~~~+)\s*([\w-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_ENDPOINT_LINE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)\s+(/\S*)$")
_CLASS_ENTRY = re.compile(r"^(?P<name>[A-Za-z_][\w.]*)\s*\((?P<file>[^)]+)\)$")


@dataclass
class EndpointRef:
    """One `METHOD /path` or `dagentsc <words>` line in the hierarchy."""

    kind: str  # "http" or "command"
    method: str = ""
    path: str = ""
    words: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.method} {self.path}" if self.kind == "http" else " ".join((PLANNER, *self.words))


def parse_endpoint_ref(text: str) -> EndpointRef:
    """Parse a hierarchy line. Raises ValueError for anything else."""
    text = text.strip()
    match = _ENDPOINT_LINE.match(text)
    if match:
        return EndpointRef(kind="http", method=match.group(1), path=match.group(2))
    words = text.split()
    if len(words) >= 2 and words[0] == PLANNER:
        return EndpointRef(kind="command", words=tuple(words[1:]))
    raise ValueError(f"not an endpoint line: {text!r} (write 'METHOD /path' or 'dagentsc <command words>')")


def parse_block(lines: list[str]) -> dict[str, Any]:
    """Parse the body of an `api-*` block.

    The format is deliberately small: `key: value` lines, and `key:` followed
    by `- item` lines for a list. A line that is neither is kept under
    `"_lines"`, which is how an `api-endpoint` block holds its one endpoint.
    """
    fields: dict[str, Any] = {"_lines": []}
    current: str | None = None
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("- ") and current is not None:
            fields[current].append(stripped[2:].strip())
            continue
        match = re.match(r"^([a-z_]+):\s*(.*)$", stripped)
        if match and not _ENDPOINT_LINE.match(stripped):
            key, value = match.group(1), match.group(2).strip()
            if value:
                fields[key] = value
                current = None
            else:
                fields[key] = []
                current = key
            continue
        fields["_lines"].append(stripped)
        current = None
    return fields


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [part.strip() for part in str(value).split(",") if part.strip()]


@dataclass
class EndpointDoc:
    ref: EndpointRef
    heading: str
    prose: str
    line: int


@dataclass
class Interface:
    heading: str
    anchor: str
    service: str
    status: str
    code: list[str]
    classes: list[tuple[str, str]]
    endpoints: list[EndpointRef]
    prose: str
    docs: list[EndpointDoc] = field(default_factory=list)
    line: int = 0


@dataclass
class ObjectDoc:
    heading: str
    anchor: str
    service: str
    class_path: str
    example: str | None
    prose: str
    line: int = 0

    @property
    def name(self) -> str:
        return self.class_path.rsplit(".", 1)[-1]


@dataclass
class ModuleDoc:
    slug: str
    path: Path
    title: str
    interfaces: list[Interface]
    objects: list[ObjectDoc]
    headings: list[tuple[int, str, str]]  # (level, text, anchor), outside fences
    errors: list[str]


def module_paths() -> list[Path]:
    """Every module page in docs/reference/api/, in file-name order."""
    return sorted(path for path in API_DOCS_DIR.glob("*.md") if path.name not in NOT_MODULES)


def parse_module(path: Path) -> ModuleDoc:
    """Read one module page into its interfaces, endpoint prose and objects.

    The rules the site follows too (`site/src/api/parse.ts`):

    - A `##` heading directly followed by an `api-interface` block starts an
      interface. The heading names it, and its anchor is the heading's id.
    - A `###` heading directly followed by an `api-endpoint` block holds the
      prose for one endpoint of the interface above it.
    - A `###` heading directly followed by an `api-object` block documents one
      object.
    - The prose of each runs to the next `##` or `###` heading.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    errors: list[str] = []

    # First pass: headings and blocks, outside other fenced code.
    items: list[tuple[str, int, Any]] = []  # (kind, line index, payload)
    headings: list[tuple[int, str, str]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line)
        if fence:
            marker, info = fence.group(1), fence.group(2)
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith(marker):
                body.append(lines[index])
                index += 1
            if info in ("api-interface", "api-endpoint", "api-object"):
                items.append((info, index, parse_block(body)))
            index += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            headings.append((level, title, heading_anchor(title)))
            items.append(("heading", index, (level, title)))
        else:
            items.append(("text", index, line))
        index += 1

    title = next((title for level, title, _ in headings if level == 1), path.stem)
    interfaces: list[Interface] = []
    objects: list[ObjectDoc] = []

    def following_block(position: int) -> tuple[str, int, Any] | None:
        for kind, line_index, payload in items[position + 1 :]:
            if kind == "text" and not str(payload).strip():
                continue
            return (kind, line_index, payload) if kind.startswith("api-") else None
        return None

    def prose_after(position: int) -> str:
        """The text after a heading's block, up to the next `##` or `###` heading."""
        collected: list[str] = []
        seen_block = False
        for kind, line_index, payload in items[position + 1 :]:
            if kind == "heading" and payload[0] <= 3:
                break
            if kind.startswith("api-") and not seen_block:
                seen_block = True
                continue
            if kind.startswith("api-"):
                errors.append(f"{path.name}:{line_index}: a second api block in one section")
                continue
            if kind == "heading":
                collected.append(lines[line_index])
            elif kind == "text":
                collected.append(str(payload))
        return "\n".join(collected).strip()

    current: Interface | None = None
    for position, (kind, line_index, payload) in enumerate(items):
        if kind.startswith("api-"):
            previous = next(
                (item for item in reversed(items[:position]) if not (item[0] == "text" and not str(item[2]).strip())),
                None,
            )
            if previous is None or previous[0] != "heading":
                errors.append(f"{path.name}:{line_index}: an {kind} block must directly follow a heading")
            continue
        if kind != "heading":
            continue
        level, heading_text = payload
        block = following_block(position)
        if level <= 2:
            current = None
        if block is None:
            continue
        block_kind, block_line, fields = block
        if block_kind == "api-interface":
            if level != 2:
                errors.append(f"{path.name}:{block_line}: an api-interface block needs a ## heading")
                continue
            service = fields.get("service")
            if not service:
                errors.append(f"{path.name}:{block_line}: the api-interface block has no service")
                continue
            classes: list[tuple[str, str]] = []
            for entry in _as_list(fields.get("classes")):
                match = _CLASS_ENTRY.match(entry)
                if not match:
                    errors.append(f"{path.name}:{block_line}: write a class as 'Name (path/to/file)', not {entry!r}")
                    continue
                classes.append((match.group("name"), match.group("file").strip()))
            refs: list[EndpointRef] = []
            for entry in _as_list(fields.get("endpoints")):
                try:
                    refs.append(parse_endpoint_ref(entry))
                except ValueError as error:
                    errors.append(f"{path.name}:{block_line}: {error}")
            status = str(fields.get("status", "draft"))
            if status not in ("draft", "complete"):
                errors.append(f"{path.name}:{block_line}: status must be draft or complete, not {status!r}")
            current = Interface(
                heading=heading_text,
                anchor=heading_anchor(heading_text),
                service=str(service),
                status=status,
                code=_as_list(fields.get("code")),
                classes=classes,
                endpoints=refs,
                prose=prose_after(position),
                line=block_line,
            )
            interfaces.append(current)
        elif block_kind == "api-endpoint":
            if level != 3 or current is None:
                errors.append(f"{path.name}:{block_line}: an api-endpoint block needs a ### heading inside an interface")
                continue
            entries = fields["_lines"]
            if len(entries) != 1:
                errors.append(f"{path.name}:{block_line}: an api-endpoint block names exactly one endpoint")
                continue
            try:
                ref = parse_endpoint_ref(entries[0])
            except ValueError as error:
                errors.append(f"{path.name}:{block_line}: {error}")
                continue
            current.docs.append(EndpointDoc(ref=ref, heading=heading_text, prose=prose_after(position), line=block_line))
        elif block_kind == "api-object":
            if level != 3:
                errors.append(f"{path.name}:{block_line}: an api-object block needs a ### heading")
                continue
            if not fields.get("class") or not fields.get("service"):
                errors.append(f"{path.name}:{block_line}: an api-object block needs class and service")
                continue
            objects.append(
                ObjectDoc(
                    heading=heading_text,
                    anchor=heading_anchor(heading_text),
                    service=str(fields["service"]),
                    class_path=str(fields["class"]),
                    example=str(fields["example"]) if fields.get("example") else None,
                    prose=prose_after(position),
                    line=block_line,
                )
            )
    return ModuleDoc(
        slug=path.stem,
        path=path,
        title=title,
        interfaces=interfaces,
        objects=objects,
        headings=headings,
        errors=errors,
    )


def parse_modules() -> list[ModuleDoc]:
    return [parse_module(path) for path in module_paths()]


# --------------------------------------------------------------------------- #
# FastAPI extraction (runs in a subprocess per service)
# --------------------------------------------------------------------------- #

_PARAM_DOC = re.compile(r"^\s*[-*]\s*`(?P<name>\w+)`\s*:\s*(?P<text>.+)$")


def _worker(service_name: str, module_name: str, attribute: str, requested_objects: list[str]) -> dict[str, Any]:
    """Describe one FastAPI app. Runs inside the service's own interpreter."""
    import ast
    import importlib
    import inspect
    import textwrap
    import typing

    from pydantic import BaseModel, TypeAdapter

    def repo_file(obj: Any) -> str | None:
        try:
            source = inspect.getsourcefile(obj)
        except TypeError:
            return None
        if not source:
            return None
        resolved = Path(source).resolve()
        if REPO_ROOT not in resolved.parents or {".venv", "site-packages"} & set(resolved.parts):
            return None
        return resolved.relative_to(REPO_ROOT).as_posix()

    def own_doc(obj: Any) -> str | None:
        """The object's own docstring; a class without one does not inherit BaseModel's."""
        if inspect.isclass(obj) and "__doc__" not in vars(obj):
            return None
        raw = obj.__doc__ if inspect.isclass(obj) else inspect.getdoc(obj)
        return inspect.cleandoc(raw) if raw else None

    def docstring_parts(obj: Any) -> tuple[str | None, str | None]:
        doc = own_doc(obj)
        if not doc:
            return None, None
        first, _, rest = doc.partition("\n")
        return first.strip() or None, rest.strip() or None

    def docstring_field_docs(cls: Any) -> dict[str, str]:
        """`- \\`name\\`: text` lines in a class docstring, as field descriptions."""
        doc = own_doc(cls) or ""
        found: dict[str, str] = {}
        name: str | None = None
        for line in doc.splitlines():
            match = _PARAM_DOC.match(line)
            if match:
                name = match.group("name")
                found[name] = match.group("text").strip()
            elif name and line.startswith("  ") and line.strip():
                found[name] += " " + line.strip()
            else:
                name = None
        return found

    def reachable_classes(root: Any, found: dict[str, Any]) -> None:
        """Every pydantic model and dataclass reachable from a type, by name."""
        origin = typing.get_origin(root)
        if origin is not None:
            for argument in typing.get_args(root):
                reachable_classes(argument, found)
            return
        if not inspect.isclass(root) or root.__name__ in found:
            return
        is_model = issubclass(root, BaseModel)
        is_dataclass = hasattr(root, "__dataclass_fields__")
        if not (is_model or is_dataclass):
            return
        found[root.__name__] = root
        try:
            hints = typing.get_type_hints(root)
        except Exception:
            hints = {}
        for hint in hints.values():
            reachable_classes(hint, found)

    def fill_descriptions(schema: dict[str, Any], cls: Any) -> None:
        docs = docstring_field_docs(cls)
        for name, prop in (schema.get("properties") or {}).items():
            if isinstance(prop, dict) and not prop.get("description") and docs.get(name):
                prop["description"] = docs[name]

    module = importlib.import_module(module_name)
    app = getattr(module, attribute)

    # Record which classes each route's types reach, so the component schemas
    # can take field descriptions from class docstrings where Field() has none.
    classes: dict[str, Any] = {}
    routes = [
        route
        for route in app.routes
        if getattr(route, "methods", None) and getattr(route, "path", None) not in inventory.FASTAPI_BUILTIN_PATHS
    ]
    route_functions = {getattr(route, "endpoint", None) for route in routes}
    for route in routes:
        endpoint = getattr(route, "endpoint", None)
        try:
            hints = typing.get_type_hints(endpoint)
        except Exception:
            hints = {}
        for hint in hints.values():
            reachable_classes(hint, classes)
        response_model = getattr(route, "response_model", None)
        if response_model is not None:
            reachable_classes(response_model, classes)

    document = app.openapi()
    schemas = document.get("components", {}).get("schemas", {})
    for name, schema in schemas.items():
        base = re.sub(r"-(Input|Output)$", "", name)
        if base in classes:
            fill_descriptions(schema, classes[base])

    def method_entry(cls: Any, method: str) -> dict[str, Any] | None:
        for klass in inspect.getmro(cls):
            if method in vars(klass):
                file = repo_file(klass)
                if file is None:
                    return None
                return {"kind": "method", "class": klass.__name__, "module": klass.__module__, "file": file, "name": method}
        return None

    def function_entry(function: Any) -> dict[str, Any] | None:
        file = repo_file(function)
        if file is None:
            return None
        return {"kind": "function", "class": None, "module": function.__module__, "file": file, "name": function.__name__}

    def calls_of(function: Any) -> list[dict[str, Any]]:
        """The repository classes and functions a handler calls, read from its source."""
        try:
            tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        except (OSError, TypeError, SyntaxError):
            return []
        try:
            hints = typing.get_type_hints(function)
        except Exception:
            hints = {}
        scope = getattr(function, "__globals__", {})
        found: list[dict[str, Any]] = []

        def add(entry: dict[str, Any] | None) -> None:
            if entry is not None and entry not in found:
                found.append(entry)

        # The body only: a decorator such as a rate-limit dependency runs when
        # the module is imported, not when the handler is called.
        definition = tree.body[0] if tree.body else None
        statements = getattr(definition, "body", [])
        nodes = [node for statement in statements for node in ast.walk(statement)]
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                owner, method = func.value, func.attr
                cls = None
                if isinstance(owner, ast.Name):
                    if owner.id in hints and inspect.isclass(hints[owner.id]):
                        cls = hints[owner.id]
                    elif owner.id in scope:
                        target = scope[owner.id]
                        if inspect.ismodule(target):
                            member = getattr(target, method, None)
                            if inspect.isfunction(member):
                                add(function_entry(member))
                            continue
                        cls = target if inspect.isclass(target) else type(target)
                elif isinstance(owner, ast.Call) and isinstance(owner.func, ast.Name):
                    factory = scope.get(owner.func.id)
                    if callable(factory):
                        try:
                            returned = typing.get_type_hints(factory).get("return")
                        except Exception:
                            returned = None
                        if inspect.isclass(returned):
                            cls = returned
                if cls is not None:
                    add(method_entry(cls, method))
            elif isinstance(func, ast.Name):
                target = scope.get(func.id)
                if target in route_functions and target is not function:
                    # Another route's handler: this endpoint answers as that one does.
                    add(
                        {
                            "kind": "route",
                            "class": None,
                            "module": target.__module__,
                            "file": repo_file(target),
                            "name": target.__name__,
                        }
                    )
                elif inspect.isfunction(target) and not target.__name__.startswith("_"):
                    add(function_entry(target))
        return found

    endpoints: list[dict[str, Any]] = []
    for route in routes:
        endpoint = route.endpoint
        operations = document.get("paths", {}).get(route.path, {})
        summary, description = docstring_parts(endpoint)
        for method in sorted(m for m in route.methods if m not in {"HEAD", "OPTIONS"}):
            operation = operations.get(method.lower(), {})
            body = operation.get("requestBody")
            request_body = None
            if body:
                content_type, media = next(iter(body.get("content", {}).items()), ("application/json", {}))
                request_body = {
                    "required": bool(body.get("required", False)),
                    "content_type": content_type,
                    "schema": media.get("schema", {}),
                }
            responses = []
            for status, response in operation.get("responses", {}).items():
                content = response.get("content") or {}
                content_type, media = next(iter(content.items()), (None, {}))
                responses.append(
                    {
                        "status": str(status),
                        "description": response.get("description", ""),
                        "content_type": content_type,
                        "schema": media.get("schema") if media else None,
                    }
                )
            endpoints.append(
                {
                    "id": endpoint_id(service_name, method, route.path),
                    "method": method,
                    "path": route.path,
                    "summary": summary,
                    "description": description,
                    "status_code": getattr(route, "status_code", None) or 200,
                    "deprecated": bool(operation.get("deprecated", False)),
                    "handler": {
                        "function": route.name,
                        "module": endpoint.__module__,
                        "file": repo_file(endpoint),
                        "calls": calls_of(endpoint),
                    },
                    "parameters": [
                        {
                            "name": parameter.get("name"),
                            "in": parameter.get("in"),
                            "required": bool(parameter.get("required", False)),
                            "description": parameter.get("description"),
                            "schema": parameter.get("schema", {}),
                        }
                        for parameter in operation.get("parameters", [])
                    ],
                    "request_body": request_body,
                    "responses": responses,
                }
            )

    objects: dict[str, Any] = {}
    for class_path in requested_objects:
        module_path, _, class_name = class_path.rpartition(".")
        target = getattr(importlib.import_module(module_path), class_name)
        if inspect.isclass(target) and issubclass(target, BaseModel):
            schema = target.model_json_schema(ref_template="#/$defs/{model}")
        else:
            schema = TypeAdapter(target).json_schema(ref_template="#/$defs/{model}")
        defs = schema.pop("$defs", {})
        found: dict[str, Any] = {}
        reachable_classes(target, found)
        fill_descriptions(schema, target)
        for name, definition in defs.items():
            if name in found:
                fill_descriptions(definition, found[name])
        summary, description = docstring_parts(target)
        objects[class_path] = {
            "class": class_path,
            "name": class_name,
            "module": module_path,
            "file": repo_file(target),
            "summary": summary,
            "description": description,
            "schema": schema,
            "defs": defs,
        }

    return {
        "title": app.title,
        "version": app.version,
        "description": app.description or None,
        "app": {"module": module_name, "attribute": attribute, "file": repo_file(module)},
        "endpoints": endpoints,
        "schemas": schemas,
        "objects": objects,
    }


def extract_python_service(service: inventory.PythonService, requested_objects: list[str]) -> dict[str, Any]:
    """Run the worker for one FastAPI service in its own interpreter."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / entry) for entry in service.python_path]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            service.name,
            service.module,
            service.attribute,
            json.dumps(requested_objects),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Could not describe {service.name} ({service.module}):\n{completed.stderr.strip()}")
    payload = json.loads(completed.stdout)
    _attach_aliases(payload["endpoints"])
    by_function = {endpoint["handler"]["function"]: endpoint["id"] for endpoint in payload["endpoints"]}
    for endpoint in payload["endpoints"]:
        for call in endpoint["handler"]["calls"]:
            if call["kind"] == "route":
                call["endpoint"] = by_function.get(call["name"])
    payload["endpoints"].sort(key=lambda item: (item["path"], item["method"]))
    return payload


def _attach_aliases(endpoints: list[dict[str, Any]]) -> None:
    """Pair each `<name>_v1` handler with the `<name>` handler it calls.

    This is the convention `scripts/service_inventory.py` records; the pairs
    are computed the same way so both files agree.
    """
    by_handler = {endpoint["handler"]["function"]: endpoint for endpoint in endpoints}
    for endpoint in endpoints:
        endpoint["alias_of"] = None
    for endpoint in endpoints:
        handler = endpoint["handler"]["function"] or ""
        if handler.endswith("_v1"):
            legacy = by_handler.get(handler[: -len("_v1")])
            if legacy is not None:
                endpoint["alias_of"] = {"method": legacy["method"], "path": legacy["path"], "id": legacy["id"]}


# --------------------------------------------------------------------------- #
# Spring extraction
# --------------------------------------------------------------------------- #

_JAVA_CLASS = re.compile(r"public\s+(?:final\s+)?class\s+(\w+)")
_JAVA_FIELD = re.compile(r"private\s+final\s+([\w<>, .]+?)\s+(\w+)\s*;")
_JAVA_RECORD = re.compile(r"public\s+record\s+(\w+)\s*\((?P<body>.*?)\)\s*(?:implements[^{]*)?\{", re.S)
_JAVA_STATUS = re.compile(r"@ResponseStatus\(\s*(?:code\s*=\s*|value\s*=\s*)?HttpStatus\.(\w+)")
_HTTP_STATUS = {"OK": 200, "CREATED": 201, "ACCEPTED": 202, "NO_CONTENT": 204}
_JAVA_PRIMITIVES = frozenset({"int", "long", "double", "float", "boolean", "short", "byte", "char"})
_JAVADOC = re.compile(r"/\*\*(?P<body>.*?)\*/", re.S)


def _javadoc_before(source: str, position: int) -> str | None:
    """The Javadoc comment that ends just before `position`, if only annotations and space lie between."""
    best = None
    for match in _JAVADOC.finditer(source, 0, position):
        best = match
    if best is None:
        return None
    between = source[best.end() : position]
    if re.sub(r"@\w+(?:\([^)]*\))?|\s", "", between):
        return None
    return best.group("body")


def _javadoc_text(text: str) -> str:
    """Javadoc markup as the site's Markdown: `{@code x}` becomes `x` in backticks, `<p>` a paragraph."""
    text = re.sub(r"\{@code\s+([^}]*)\}", r"`\1`", text)
    text = re.sub(r"\{@link\s+([^}]*)\}", r"`\1`", text)
    paragraphs = [" ".join(part.split()) for part in re.split(r"\s*<p>\s*", text)]
    return "\n\n".join(part for part in paragraphs if part)


def parse_javadoc(body: str | None) -> dict[str, Any]:
    """Split a Javadoc comment into its summary, the rest of its text, and its `@param` lines."""
    if not body:
        return {"summary": None, "description": None, "params": {}}
    lines = [re.sub(r"^\s*\*\s?", "", line) for line in body.splitlines()]
    text_lines: list[str] = []
    params: dict[str, str] = {}
    current: str | None = None
    for line in lines:
        tag = re.match(r"^@(\w+)\s*(.*)$", line.strip())
        if tag:
            current = None
            if tag.group(1) == "param":
                name, _, rest = tag.group(2).partition(" ")
                params[name] = rest.strip()
                current = name
            continue
        if current is not None:
            params[current] += " " + line.strip()
        else:
            text_lines.append(line)
    text = _javadoc_text("\n".join(text_lines))
    summary, _, description = text.partition("\n\n")
    cleaned = {name: _javadoc_text(value) for name, value in params.items()}
    # A record component's description starts lower case in Javadoc style; the reference shows sentences.
    cleaned = {name: (value[:1].upper() + value[1:]).rstrip(".") + "." for name, value in cleaned.items() if value}
    return {"summary": summary or None, "description": description or None, "params": cleaned}

_JAVA_SCALARS: dict[str, dict[str, Any]] = {
    "String": {"type": "string"},
    "int": {"type": "integer"},
    "Integer": {"type": "integer"},
    "long": {"type": "integer"},
    "Long": {"type": "integer"},
    "double": {"type": "number"},
    "Double": {"type": "number"},
    "float": {"type": "number"},
    "Float": {"type": "number"},
    "boolean": {"type": "boolean"},
    "Boolean": {"type": "boolean"},
    "Object": {},
    "Instant": {"type": "string", "format": "date-time"},
}


def _split_generic_arguments(text: str) -> list[str]:
    depth, current, parts = 0, "", []
    for char in text:
        if char == "<":
            depth += 1
        elif char == ">":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def java_type_schema(java_type: str, records: set[str]) -> dict[str, Any]:
    """A JSON-schema shape for a Java type, with records as `$ref`s."""
    java_type = java_type.strip()
    generic = re.match(r"^([\w.]+)\s*<(.+)>$", java_type)
    if generic:
        outer, inner = generic.group(1).split(".")[-1], _split_generic_arguments(generic.group(2))
        if outer in ("List", "Set", "Collection") and inner:
            return {"type": "array", "items": java_type_schema(inner[0], records)}
        if outer == "Map" and len(inner) == 2:
            return {"type": "object", "additionalProperties": java_type_schema(inner[1], records)}
        if outer in ("ResponseEntity", "Optional") and inner:
            return java_type_schema(inner[0], records)
        return {}
    if java_type.endswith("[]"):
        return {"type": "array", "items": java_type_schema(java_type[:-2], records)}
    name = java_type.split(".")[-1]
    if name in _JAVA_SCALARS:
        return dict(_JAVA_SCALARS[name])
    if name in records:
        return {"$ref": f"#/components/schemas/{name}"}
    return {"title": name}


def _record_components(body: str) -> list[tuple[list[str], str, str]]:
    """`(annotations, type, name)` for each component of a record header."""
    components: list[tuple[list[str], str, str]] = []
    for part in _split_generic_arguments(" ".join(body.split())):
        annotations = re.findall(r"@(\w+)(?:\([^)]*\))?", part)
        bare = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", part).strip()
        java_type, _, name = bare.rpartition(" ")
        components.append((annotations, java_type.strip(), name.strip()))
    return components


def extract_java_records(roots: list[Path]) -> dict[str, dict[str, Any]]:
    """Schemas for every `public record` under the given source roots."""
    sources: dict[str, tuple[Path, str]] = {}
    javadocs: dict[str, dict[str, Any]] = {}
    for root in roots:
        for path in sorted(root.rglob("*.java")):
            text = path.read_text(encoding="utf-8")
            for match in _JAVA_RECORD.finditer(text):
                sources[match.group(1)] = (path, match.group("body"))
                javadocs[match.group(1)] = parse_javadoc(_javadoc_before(text, match.start()))
    names = set(sources)
    schemas: dict[str, dict[str, Any]] = {}
    for name, (path, body) in sorted(sources.items()):
        properties: dict[str, Any] = {}
        required: list[str] = []
        for annotations, java_type, field_name in _record_components(body):
            properties[field_name] = java_type_schema(java_type, names)
            if java_type.strip() not in _JAVA_PRIMITIVES:
                # Jackson writes a reference-typed component that is not set as null.
                properties[field_name] = {"anyOf": [properties[field_name], {"type": "null"}]}
            if javadocs[name]["params"].get(field_name):
                properties[field_name]["description"] = javadocs[name]["params"][field_name]
            if {"NotBlank", "NotNull", "NotEmpty"} & set(annotations):
                required.append(field_name)
            constraints = [a for a in annotations if a in {"NotBlank", "NotNull", "NotEmpty", "Valid", "Positive", "Min", "Max"}]
            if constraints:
                properties[field_name]["x-java-annotations"] = constraints
        schema: dict[str, Any] = {
            "title": name,
            "type": "object",
            "properties": properties,
            "x-java-file": path.relative_to(REPO_ROOT).as_posix(),
        }
        if javadocs[name]["summary"]:
            schema["description"] = "\n\n".join(
                part for part in (javadocs[name]["summary"], javadocs[name]["description"]) if part
            )
        if required:
            schema["required"] = required
        schemas[name] = schema
    return schemas


def _java_parameters(signature: str, records: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Path, query and header parameters and the request body of one handler."""
    parameters: list[dict[str, Any]] = []
    body: dict[str, Any] | None = None
    for part in _split_generic_arguments(" ".join(signature.split())):
        annotations = dict(
            (match.group(1), match.group(2) or "") for match in re.finditer(r"@(\w+)(?:\(([^)]*)\))?", part)
        )
        bare = re.sub(r"@\w+(?:\([^)]*\))?\s*", "", part).strip()
        java_type, _, name = bare.rpartition(" ")
        schema = java_type_schema(java_type, records)
        if "RequestBody" in annotations:
            body = {"required": True, "content_type": "application/json", "schema": schema}
            continue
        for annotation, location in (("PathVariable", "path"), ("RequestParam", "query"), ("RequestHeader", "header")):
            if annotation in annotations:
                explicit = re.search(r'"([^"]+)"', annotations[annotation])
                required_flag = re.search(r"required\s*=\s*false", annotations[annotation]) is None
                default = re.search(r'defaultValue\s*=\s*"([^"]*)"', annotations[annotation])
                if default:
                    schema = {**schema, "default": default.group(1)}
                parameters.append(
                    {
                        "name": explicit.group(1) if explicit else name,
                        "in": location,
                        "required": location == "path" or (required_flag and not default),
                        "description": None,
                        "schema": schema,
                    }
                )
    return parameters, body


def extract_java_service(service: inventory.JavaService, records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Endpoints of one Spring service, with parameters, bodies and the service class called."""
    root = REPO_ROOT / service.source_root
    record_names = set(records)
    handled = _java_handled_exceptions(root)
    endpoints: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.java")):
        source = path.read_text(encoding="utf-8")
        if not inventory._REST_CONTROLLER.search(source):
            continue
        class_match = _JAVA_CLASS.search(source)
        controller = class_match.group(1) if class_match else path.stem
        fields = {name: java_type for java_type, name in _JAVA_FIELD.findall(source)}
        package = re.search(r"^package\s+([\w.]+);", source, re.M)
        prefix_match = inventory._CLASS_MAPPING.search(source)
        prefix = prefix_match.group(1) if prefix_match else ""
        start = prefix_match.end() if prefix_match else 0
        for match in inventory._METHOD_MAPPING.finditer(source, start):
            signature = re.compile(r"public\s+([\w<>,\[\]\s.?]+?)\s+(\w+)\s*\(").search(source, match.end())
            if signature is None:
                continue
            open_paren = signature.end() - 1
            depth, close = 0, open_paren
            for close in range(open_paren, len(source)):
                if source[close] == "(":
                    depth += 1
                elif source[close] == ")":
                    depth -= 1
                    if depth == 0:
                        break
            body_start = source.index("{", close)
            depth, body_end = 0, body_start
            for body_end in range(body_start, len(source)):
                if source[body_end] == "{":
                    depth += 1
                elif source[body_end] == "}":
                    depth -= 1
                    if depth == 0:
                        break
            method_body = source[body_start:body_end]
            annotations_text = source[match.start() : signature.start()]
            status = _JAVA_STATUS.search(annotations_text)
            parameters, request_body = _java_parameters(source[open_paren + 1 : close], record_names)
            doc = parse_javadoc(_javadoc_before(source, match.start()))
            for parameter in parameters:
                parameter["description"] = doc["params"].get(parameter["name"])
            if request_body is not None:
                body_name = next(
                    (part.strip().split()[-1] for part in _split_generic_arguments(" ".join(source[open_paren + 1 : close].split()))
                     if "@RequestBody" in part),
                    None,
                )
                if body_name and doc["params"].get(body_name):
                    request_body["description"] = doc["params"][body_name]
            http_method = inventory._HTTP_METHOD[match.group(1)]
            full_path = inventory._join_paths(prefix, match.group(2) or "")
            return_type = signature.group(1).strip()
            calls = []
            for field_name, field_type in fields.items():
                for call in re.findall(rf"\b{field_name}\.(\w+)\(", method_body):
                    entry = {
                        "kind": "method",
                        "class": field_type.strip(),
                        "module": None,
                        "file": _java_class_file(field_type.strip(), root),
                        "name": call,
                    }
                    if entry not in calls:
                        calls.append(entry)
            status_code = _HTTP_STATUS.get(status.group(1), 200) if status else 200
            endpoints.append(
                {
                    "id": endpoint_id(service.name, http_method, full_path),
                    "method": http_method,
                    "path": full_path,
                    "summary": doc["summary"],
                    "description": doc["description"],
                    "status_code": status_code,
                    "deprecated": False,
                    "handler": {
                        "function": signature.group(2),
                        "module": f"{package.group(1)}.{controller}" if package else controller,
                        "file": path.relative_to(REPO_ROOT).as_posix(),
                        "calls": calls,
                    },
                    "parameters": parameters,
                    "request_body": request_body,
                    "responses": [
                        {
                            "status": str(status_code),
                            "description": f"Returns {return_type}",
                            "content_type": "application/json",
                            "schema": java_type_schema(return_type, record_names),
                        },
                        *_java_error_responses(
                            method_body,
                            " ".join(source[open_paren + 1 : close].split()),
                            calls,
                            handled,
                            request_body is not None,
                        ),
                    ],
                    "alias_of": None,
                }
            )
    endpoints.sort(key=lambda item: (item["path"], item["method"]))
    used = _referenced_schemas([endpoint for endpoint in endpoints], records)
    return {
        "title": service.name,
        "version": None,
        "description": None,
        "app": {"module": None, "attribute": None, "file": service.source_root},
        "endpoints": endpoints,
        "schemas": {name: records[name] for name in sorted(used)},
        "objects": {},
    }


def _java_class_file(name: str, root: Path) -> str | None:
    for path in root.rglob(f"{name}.java"):
        return path.relative_to(REPO_ROOT).as_posix()
    return None


#: The body each Spring service's `ApiExceptionHandler` writes for the exceptions it handles.
_SPRING_HANDLED_ERROR = {
    "type": "object",
    "title": "ApiError",
    "properties": {
        "code": {"type": "string", "description": "Always `validation_error`."},
        "message": {
            "type": "string",
            "description": "The exception's message: the rule that failed, or Spring's list of fields that are not valid.",
        },
    },
    "required": ["code", "message"],
}

#: The body Spring Boot writes itself when no handler matches, such as a body that is not JSON.
_SPRING_DEFAULT_ERROR = {
    "type": "object",
    "title": "SpringError",
    "properties": {
        "timestamp": {"type": "string", "description": "When the error happened, in ISO 8601."},
        "status": {"type": "integer", "description": "The HTTP status code."},
        "error": {"type": "string", "description": "The status code's reason phrase."},
        "path": {"type": "string", "description": "The request path."},
    },
    "required": ["timestamp", "status", "error", "path"],
}


def _java_handled_exceptions(root: Path) -> set[str]:
    """Exception classes a `@RestControllerAdvice` under `root` maps to a response."""
    handled: set[str] = set()
    for path in sorted(root.rglob("*.java")):
        source = path.read_text(encoding="utf-8")
        if "@RestControllerAdvice" in source or "@ControllerAdvice" in source:
            handled.update(re.findall(r"@ExceptionHandler\(\s*(\w+)\.class", source))
    return handled


def _java_method_body(source: str, name: str) -> str:
    """The body of every method called `name` in a Java source file, joined."""
    bodies = []
    for match in re.finditer(rf"\b{re.escape(name)}\s*\([^;{{]*\)\s*(?:throws[^{{]*)?\{{", source):
        depth = 0
        for end in range(match.end() - 1, len(source)):
            if source[end] == "{":
                depth += 1
            elif source[end] == "}":
                depth -= 1
                if depth == 0:
                    bodies.append(source[match.end() : end])
                    break
    return "\n".join(bodies)


def _java_error_responses(
    method_body: str, signature: str, calls: list[dict[str, Any]], handled: set[str], has_body: bool
) -> list[dict[str, Any]]:
    """The error responses a Spring handler can return, found from its code and the exception handler."""
    responses: list[dict[str, Any]] = []
    if has_body:
        responses.append(
            {
                "status": "400",
                "description": "The body is not JSON, or a field has the wrong JSON type. Spring Boot writes this body itself.",
                "content_type": "application/json",
                "schema": _SPRING_DEFAULT_ERROR,
            }
        )
    reasons = []
    if "MethodArgumentNotValidException" in handled and re.search(r"@Valid\b[^,]*@RequestBody|@RequestBody[^,]*@Valid\b", signature):
        reasons.append("a field of the body breaks a rule such as @NotBlank")
    called_code = method_body
    for call in calls:
        if call["file"]:
            called_code += _java_method_body((REPO_ROOT / call["file"]).read_text(encoding="utf-8"), call["name"])
    if "IllegalArgumentException" in handled and "new IllegalArgumentException" in called_code:
        reasons.append("the handler or the service it calls rejects the request")
    if reasons:
        responses.append(
            {
                "status": "422",
                "description": "The request is not valid: " + ", or ".join(reasons) + ".",
                "content_type": "application/json",
                "schema": _SPRING_HANDLED_ERROR,
            }
        )
    return responses


def _referenced_schemas(endpoints: list[dict[str, Any]], records: dict[str, dict[str, Any]]) -> set[str]:
    """Every record reachable from the endpoints' parameters, bodies and responses."""
    pending: list[Any] = []
    for endpoint in endpoints:
        pending.extend(parameter["schema"] for parameter in endpoint["parameters"])
        if endpoint["request_body"]:
            pending.append(endpoint["request_body"]["schema"])
        pending.extend(response["schema"] for response in endpoint["responses"])
    found: set[str] = set()
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.rsplit("/", 1)[-1]
                if name not in found and name in records:
                    found.add(name)
                    pending.append(records[name])
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    return found


def java_object(class_path: str, records: dict[str, dict[str, Any]], service: str) -> dict[str, Any]:
    """An object page entry for a Java record."""
    name = class_path.rsplit(".", 1)[-1]
    if name not in records:
        raise RuntimeError(f"{class_path} is not a record under the Spring sources ({service})")
    used = _referenced_schemas(
        [{"parameters": [], "request_body": None, "responses": [{"schema": {"$ref": f"#/components/schemas/{name}"}}]}],
        records,
    )
    schema = json.loads(json.dumps(records[name]).replace("#/components/schemas/", "#/$defs/"))
    defs = {
        other: json.loads(json.dumps(records[other]).replace("#/components/schemas/", "#/$defs/"))
        for other in sorted(used)
        if other != name
    }
    summary, _, description = (records[name].get("description") or "").partition("\n\n")
    return {
        "class": class_path,
        "name": name,
        "module": class_path.rpartition(".")[0],
        "file": records[name]["x-java-file"],
        "summary": summary or None,
        "description": description or None,
        "schema": schema,
        "defs": defs,
    }


# --------------------------------------------------------------------------- #
# dagentsc extraction
# --------------------------------------------------------------------------- #

_USAGE = re.compile(r'prerr_endline\s+"\s+dagentsc\s+([^"]+)"')
_DISPATCH = re.compile(r'\|\s+((?:"[\w-]+"\s*::\s*)+)(action\s*::\s*)?rest\s*->\s*(\w+)')
_OCAML_LET = re.compile(r"^let\s+(\(\)|\w+)", re.M)
_LIBRARY_CALL = re.compile(r"\b(Dagents_\w+)\.(\w+)")
_CODEC_CALL = re.compile(r"\bJson_codec\.(\w+)")


def _ocaml_libraries() -> dict[str, str]:
    """Library module name (`Dagents_dataset_compiler`) to its source folder."""
    found: dict[str, str] = {}
    for dune in sorted(OCAML_LIB_DIR.glob("*/dune")):
        match = re.search(r"\(name\s+(\w+)\)", dune.read_text(encoding="utf-8"))
        if match:
            found[match.group(1).capitalize()] = dune.parent.relative_to(REPO_ROOT).as_posix()
    return found


def _clean_ocaml_doc(text: str) -> str:
    text = re.sub(r"\{\[.*?\]\}", "", text, flags=re.S)
    lines = [line.strip() for line in text.strip().splitlines()]
    paragraphs, current = [], []
    for line in lines:
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    cleaned = "\n\n".join(paragraphs)
    return re.sub(r"\[([^\[\]]+)\]", r"`\1`", cleaned)


def _planner_flags(text: str, helpers: list[str]) -> list[dict[str, Any]]:
    """The flags one command reads, from its argument parsing, in the order the code reads them.

    The usage text leaves flags out (`--plan-id`, `--step`, ...), so the flags come
    from the handler: `arg_value "--flag" args "default"`, `read_json_from_flag
    "--flag"`, `read_json_input` (which reads `--input`) and the `"--step" :: raw`
    pattern that collects a repeated flag.
    """
    flags: dict[str, dict[str, Any]] = {}

    def add(name: str, default: str | None, repeated: bool = False) -> None:
        if name not in flags:
            flags[name] = {"name": name, "default": default, "repeated": repeated}

    code = text + "\n".join(helpers)
    pattern = re.compile(
        r'arg_value\s+"(--[\w-]+)"\s+args\s+"([^"]*)"'
        r'|read_json_from_flag\s+"(--[\w-]+)"'
        r'|\bread_json_input\b'
        r'|"(--[\w-]+)"\s*::\s*\w+\s*::\s*rest'
        r'|List\.mem\s+"(--[\w-]+)"'
    )
    for match in pattern.finditer(code):
        if match.group(1):
            add(match.group(1), match.group(2))
        elif match.group(3):
            add(match.group(3), "-")
        elif match.group(4):
            add(match.group(4), None, repeated=True)
        elif match.group(5):
            add(match.group(5), "-")
        else:
            add("--input", "-")
    return list(flags.values())


def extract_planner() -> dict[str, Any]:
    """The commands of `dagentsc`, from its source."""
    source = DAGENTSC_SOURCE.read_text(encoding="utf-8")
    libraries = _ocaml_libraries()

    usages: dict[tuple[str, ...], str] = {}
    for line in _USAGE.findall(source):
        words: list[str] = []
        for token in line.split():
            if token.startswith("[") or token.startswith("--"):
                break
            words.append(token)
        flags = line[len(" ".join(words)) :].strip()
        choices = words[-1].split("|")
        for choice in choices:
            usages[tuple([*words[:-1], choice])] = flags

    # Each handler's body and its preceding doc comment.
    lets = list(_OCAML_LET.finditer(source))
    bodies: dict[str, tuple[str, str]] = {}
    for index, match in enumerate(lets):
        end = lets[index + 1].start() if index + 1 < len(lets) else len(source)
        body = source[match.start() : end]
        # The doc comment directly above the let, if there is one.
        doc = ""
        opening = source.rfind("(**", 0, match.start())
        closing = source.find("*)", opening) if opening >= 0 else -1
        if opening >= 0 and closing >= 0 and source[closing + 2 : match.start()].strip() == "":
            doc = source[opening + 3 : closing]
        # The doc comment of the next let sits at the end of this body.
        body = re.sub(r"\(\*\*.*?\*\)\s*$", "", body, flags=re.S)
        bodies[match.group(1)] = (body, _clean_ocaml_doc(doc))

    dispatch = _DISPATCH.findall(source)
    handlers = {handler for _, _, handler in dispatch}
    commands: list[dict[str, Any]] = []
    for words_text, action, handler in dispatch:
        fixed = tuple(re.findall(r'"([\w-]+)"', words_text))
        body, doc = bodies.get(handler, ("", ""))
        if action:
            prelude, _, arms_text = body.partition("match action with")
            arms = re.split(r'\n\s*\|\s*"([\w-]+)"\s*->', arms_text)
            arm_bodies = {arms[i]: arms[i + 1] for i in range(1, len(arms) - 1, 2)}
            variants = [(name, prelude + text) for name, text in arm_bodies.items()]
        else:
            variants = [(None, body)]
        for name, text in variants:
            words = (*fixed, name) if name else fixed
            library_calls = []
            for module, function in _LIBRARY_CALL.findall(text):
                entry = {"module": module, "function": function, "folder": libraries.get(module)}
                if entry not in library_calls:
                    library_calls.append(entry)
            # Helpers such as `manifest_from_payload` read part of the input.
            helpers = [
                helper_body
                for helper, (helper_body, _) in bodies.items()
                if helper not in handlers and helper != "()" and re.search(rf"\b{helper}\b", text)
            ]
            flags = _planner_flags(text, helpers)
            codecs = list(dict.fromkeys(_CODEC_CALL.findall(text + "".join(helpers))))
            reads = [c.removesuffix("_of_yojson") for c in codecs if c.endswith("_of_yojson")]
            writes = [
                c.removeprefix("yojson_of_").removesuffix("_to_yojson")
                for c in codecs
                if c.startswith("yojson_of_") or c.endswith("_to_yojson")
            ]
            commands.append(
                {
                    "id": command_id(words),
                    "words": list(words),
                    "usage": " ".join([PLANNER, *words, usages.get(tuple(words), "")]).strip(),
                    "flags": flags,
                    "handler": {"function": handler, "file": DAGENTSC_SOURCE.relative_to(REPO_ROOT).as_posix()},
                    "doc": doc or None,
                    "calls": library_calls,
                    "reads": reads,
                    "writes": writes,
                }
            )
    missing = sorted(set(usages) - {tuple(command["words"]) for command in commands})
    if missing:
        raise RuntimeError(f"dagentsc usage lists commands the dispatch does not handle: {missing}")
    if not commands:
        raise RuntimeError("no dagentsc commands were parsed; the parser in scripts/api_reference.py needs updating")
    commands.sort(key=lambda command: command["words"])
    return {
        "name": PLANNER,
        "source": DAGENTSC_SOURCE.relative_to(REPO_ROOT).as_posix(),
        "commands": commands,
    }


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def requested_objects(modules: list[ModuleDoc]) -> dict[str, list[str]]:
    """The object classes the module pages document, per service."""
    found: dict[str, list[str]] = {}
    for module in modules:
        for obj in module.objects:
            found.setdefault(obj.service, [])
            if obj.class_path not in found[obj.service]:
                found[obj.service].append(obj.class_path)
    return found


def build_reference() -> dict[str, Any]:
    """Collect every service, the documented objects and the planner commands."""
    modules = parse_modules()
    wanted = requested_objects(modules)
    known = {service.name for service in inventory.PYTHON_SERVICES} | {
        service.name for service in inventory.JAVA_SERVICES
    }
    unknown = sorted(set(wanted) - known)
    if unknown:
        raise RuntimeError(f"api-object blocks name services that do not exist: {unknown}")

    services: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    for service in inventory.PYTHON_SERVICES:
        extracted = extract_python_service(service, sorted(wanted.get(service.name, [])))
        for class_path, obj in sorted(extracted.pop("objects").items()):
            objects.append({"service": service.name, **obj})
        services.append(
            {
                "name": service.name,
                "language": "python",
                "framework": "fastapi",
                "role": service.role,
                "port": service.port,
                **extracted,
            }
        )
    # source_root is <spring-services>/<service>/src/main/java, so parents[3] is the
    # folder that also holds dagents-spring-common, where the shared records live.
    java_roots = sorted({(REPO_ROOT / service.source_root).parents[3] for service in inventory.JAVA_SERVICES})
    records = extract_java_records(
        [REPO_ROOT / service.source_root for service in inventory.JAVA_SERVICES]
        + [path / "dagents-spring-common" / "src" / "main" / "java" for path in java_roots]
    )
    for java_service in inventory.JAVA_SERVICES:
        extracted = extract_java_service(java_service, records)
        extracted.pop("objects")
        for class_path in sorted(wanted.get(java_service.name, [])):
            objects.append({"service": java_service.name, **java_object(class_path, records, java_service.name)})
        services.append(
            {
                "name": java_service.name,
                "language": "java",
                "framework": "spring-boot",
                "role": java_service.role,
                "port": java_service.port,
                **extracted,
            }
        )
    services.sort(key=lambda item: item["name"])
    objects.sort(key=lambda item: (item["name"], item["service"]))
    return {
        "generated_by": "scripts/api_reference.py",
        "regenerate": ".venv/bin/python scripts/api_reference.py --write",
        "endpoint_count": sum(len(service["endpoints"]) for service in services),
        "services": services,
        "objects": objects,
        "planner": extract_planner(),
    }


def serialize(reference: dict[str, Any]) -> str:
    return json.dumps(reference, indent=1, sort_keys=False, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--worker":
        _, service_name, module_name, attribute, objects_json = argv
        payload = _worker(service_name, module_name, attribute, json.loads(objects_json))
        sys.stdout.write(json.dumps(payload))
        return 0

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="regenerate docs/reference/api-reference.json")
    group.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    group.add_argument("--print", action="store_true", help="write the JSON to stdout")
    args = parser.parse_args(argv)

    serialized = serialize(build_reference())
    if args.print:
        sys.stdout.write(serialized)
        return 0
    if args.write:
        OUTPUT_PATH.write_text(serialized, encoding="utf-8")
        print(f"wrote {OUTPUT_PATH.relative_to(REPO_ROOT)}")
        return 0
    actual = OUTPUT_PATH.read_text(encoding="utf-8") if OUTPUT_PATH.exists() else None
    if actual != serialized:
        print(
            f"{OUTPUT_PATH.relative_to(REPO_ROOT)} is stale.\n"
            "Regenerate it with: .venv/bin/python scripts/api_reference.py --write",
            file=sys.stderr,
        )
        return 1
    print("API reference data is current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
