#!/usr/bin/env python3
"""Capture the example requests and responses shown in the API reference.

Every example on the reference pages comes from a real call. Content writers
declare the requests in `docs/reference/api/examples/<service>.requests.json`;
this script makes them and stores each request with its response in
`docs/reference/api/examples/<service>.json`. The site renders the curl,
Python and JavaScript code from the stored request, so the code and the
response always belong to the same call.

How each service is called:

- **FastAPI services** run in process, in their own interpreter, through
  FastAPI's `TestClient`. No port is opened and no network is used. An
  example can name another app of the same service in the request file's
  `apps`, such as an LMA started for a site: `{"app": "module:attribute",
  "pythonpath": [...]}`, where the attribute is an app or a function that
  returns one. Each app is built once, the first time an example names it.
- **dagentsc** is run as a subprocess with the request's arguments and its
  JSON on standard input.
- **Any other service** (the Spring services) needs a `runner` in its request
  file: a command that starts the service on a port between 9400 and 9499.
  The script starts it, waits until the ready path answers, sends the
  requests and stops it.

The planner must be available. Without it the Ethical Guard denies every
request and the core service falls back to its Python renderer, so the
examples would show behaviour that no deployment has. The script refuses to
run without it.

Usage::

    .venv/bin/python scripts/capture_api_examples.py                       # every request file
    .venv/bin/python scripts/capture_api_examples.py --service core-service
    .venv/bin/python scripts/capture_api_examples.py --check               # replay and compare, write nothing

`--check` replays every request and compares the answer with the stored one,
ignoring the JSON pointers an example lists as `volatile` (ids made from the
clock, timestamps). `tests/test_api_examples.py` runs it when the planner is
available.

A request path may use a value from the answer to an earlier example in the
same file, written `{from:<example id>:<JSON pointer>}`, for example
`/api/v1/runs/{from:run-pipeline:/run_id}`. That is how a fetch-by-id example
names a run or a job whose id comes from the clock. The stored example keeps
the real path, for the code samples, and the template in `request_template`,
which `--check` compares instead.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import service_inventory as inventory  # noqa: E402  (needs the sys.path entry above)

EXAMPLES_DIR = REPO_ROOT / "docs" / "reference" / "api" / "examples"
PLANNER = "dagentsc"

#: Default trimming. A list keeps its first `items` entries and a string its
#: first `chars` characters; the stored response lists every cut it made.
DEFAULT_ITEMS = 5
DEFAULT_CHARS = 1200

#: Response headers worth showing. Others (date, content-length) change on
#: every call or say nothing about the API.
KEPT_HEADERS = ("content-type", "retry-after", "etag", "x-dagents-app", "location", "cache-control")

PORT_RANGE = range(9400, 9500)

#: `{from:<example id>:<JSON pointer>}` in a request path.
FROM_EARLIER = re.compile(r"\{from:([A-Za-z0-9_.-]+):(/[^}]*)\}")


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #


def request_files() -> list[Path]:
    return sorted(EXAMPLES_DIR.glob("*.requests.json"))


def service_of(path: Path) -> str:
    return path.name.removesuffix(".requests.json")


def captured_path(service: str) -> Path:
    return EXAMPLES_DIR / f"{service}.json"


def load_requests(service: str) -> dict[str, Any]:
    path = EXAMPLES_DIR / f"{service}.requests.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("service") != service:
        raise ValueError(f"{path.name}: 'service' must be {service!r}")
    ids = [example["id"] for example in document.get("examples", [])]
    duplicates = sorted({example_id for example_id in ids if ids.count(example_id) > 1})
    if duplicates:
        raise ValueError(f"{path.name}: example ids must be unique, repeated: {duplicates}")
    check_apps(path.name, document)
    return document


#: `module:attribute` in an app entry.
APP_REFERENCE = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


def check_apps(name: str, document: dict[str, Any]) -> None:
    """Check the `apps` a request file declares, and that each example names one of them.

    An entry is `{"app": "module:attribute", "pythonpath": [...], "description": ...}`.
    The attribute is an ASGI app, or a function without arguments that returns
    one. Raises ``ValueError`` naming the file and the problem.
    """
    apps = document.get("apps") or {}
    if not isinstance(apps, dict):
        raise ValueError(f"{name}: 'apps' must map names to app entries")
    for app_name, entry in apps.items():
        if not isinstance(entry, dict) or not APP_REFERENCE.match(str(entry.get("app", ""))):
            raise ValueError(f"{name}: app {app_name!r} needs 'app' written as 'module:attribute'")
        paths = entry.get("pythonpath", [])
        if not isinstance(paths, list) or not all(isinstance(item, str) and item for item in paths):
            raise ValueError(f"{name}: app {app_name!r} needs 'pythonpath' as a list of directories")
    for example in document.get("examples", []):
        if "app" in example and example["app"] not in apps:
            raise ValueError(f"{name}: example {example.get('id')!r} names the app {example['app']!r}, which 'apps' does not declare")


def planner_binary() -> str | None:
    """The planner the services would use: `DAGENTSC_BIN`, the dune build, or PATH."""
    configured = os.environ.get("DAGENTSC_BIN")
    if configured:
        return configured if Path(configured).exists() else None
    built = REPO_ROOT / "bindings" / "ocaml" / "_build" / "default" / "bin" / "dagentsc.exe"
    if built.exists():
        return str(built)
    return shutil.which(PLANNER)


# --------------------------------------------------------------------------- #
# Trimming and comparison
# --------------------------------------------------------------------------- #


def _pointer_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def trim(value: Any, items: int, chars: int, pointer: str = "") -> tuple[Any, list[dict[str, str]]]:
    """Shorten long lists and strings, and say what was cut."""
    notes: list[dict[str, str]] = []
    if isinstance(value, str) and len(value) > chars:
        notes.append({"pointer": pointer or "/", "note": f"{len(value)} characters, cut to {chars}"})
        return value[:chars] + "…", notes
    if isinstance(value, list):
        kept = value[:items]
        if len(value) > items:
            notes.append({"pointer": pointer or "/", "note": f"{len(value)} items, cut to {items}"})
        result = []
        for index, item in enumerate(kept):
            trimmed, inner = trim(item, items, chars, f"{pointer}/{index}")
            result.append(trimmed)
            notes.extend(inner)
        return result, notes
    if isinstance(value, dict):
        result_dict = {}
        for key, item in value.items():
            trimmed, inner = trim(item, items, chars, f"{pointer}/{_pointer_token(str(key))}")
            result_dict[key] = trimmed
            notes.extend(inner)
        return result_dict, notes
    return value, notes


def blank_volatile(value: Any, pointers: list[str]) -> Any:
    """Replace the values at the given JSON pointers (`*` matches any list item or key)."""
    result = copy.deepcopy(value)
    for pointer in pointers:
        tokens = [token.replace("~1", "/").replace("~0", "~") for token in pointer.strip("/").split("/")] if pointer.strip("/") else []
        _blank(result, tokens)
    return result


def _blank(node: Any, tokens: list[str]) -> None:
    if not tokens:
        return
    head, rest = tokens[0], tokens[1:]
    if isinstance(node, dict):
        keys = list(node) if head == "*" else ([head] if head in node else [])
        for key in keys:
            if rest:
                _blank(node[key], rest)
            else:
                node[key] = "<volatile>"
    elif isinstance(node, list):
        indexes = range(len(node)) if head == "*" else ([int(head)] if head.isdigit() and int(head) < len(node) else [])
        for index in indexes:
            if rest:
                _blank(node[index], rest)
            else:
                node[index] = "<volatile>"


def normalized(example: dict[str, Any]) -> dict[str, Any]:
    """An example with its volatile values replaced, for comparison."""
    volatile = example.get("volatile", [])
    result = copy.deepcopy(example)
    if "request_template" in result:
        # The real path holds an id from an earlier answer; compare the template.
        result["request"]["path"] = result["request_template"]["path"]
    result["response"]["body"] = blank_volatile(result["response"]["body"], volatile)
    headers = result["response"].get("headers", {})
    if "etag" in headers:
        headers["etag"] = "<volatile>"
    return result


# --------------------------------------------------------------------------- #
# Running requests
# --------------------------------------------------------------------------- #


def resolve_path(path: str, answers: dict[str, Any]) -> str:
    """Fill each `{from:<example id>:<pointer>}` with that earlier answer's value."""

    def value(match: re.Match[str]) -> str:
        example_id, pointer = match.group(1), match.group(2)
        if example_id not in answers:
            raise ValueError(f"{path}: no earlier example {example_id!r} answered with JSON")
        node = answers[example_id]
        for token in pointer.strip("/").split("/"):
            token = token.replace("~1", "/").replace("~0", "~")
            node = node[int(token)] if isinstance(node, list) else node[token]
        return urllib.parse.quote(str(node), safe="")

    return FROM_EARLIER.sub(value, path)


def _remember(answers: dict[str, Any], example: dict[str, Any], content_type: str, raw: bytes) -> None:
    """Keep an answer's full JSON (before trimming) for later `{from:...}` paths."""
    if "json" in content_type:
        try:
            answers[example["id"]] = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass


def _endpoint_method(example: dict[str, Any]) -> str:
    return example["endpoint"].split()[0]


def _response_record(status: int, headers: dict[str, str], raw: bytes | str, example: dict[str, Any]) -> dict[str, Any]:
    content_type = headers.get("content-type", "")
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    body: Any
    if "json" in content_type:
        try:
            body = json.loads(text) if text else None
        except json.JSONDecodeError:
            body = text
    else:
        body = text
    limits = example.get("trim", {})
    body, notes = trim(body, int(limits.get("items", DEFAULT_ITEMS)), int(limits.get("chars", DEFAULT_CHARS)))
    return {
        "status": status,
        "headers": {name: headers[name] for name in KEPT_HEADERS if name in headers},
        "body": body,
        "trimmed": notes,
    }


def _record(
    example: dict[str, Any], response: dict[str, Any], planner_calls: list[str], path: str | None = None
) -> dict[str, Any]:
    record = {key: example[key] for key in ("id", "endpoint", "title") if key in example}
    for key in ("description", "app", "show", "volatile", "trim"):
        if key in example:
            record[key] = example[key]
    record["request"] = {"method": _endpoint_method(example), **example["request"]}
    if path is not None and path != example["request"]["path"]:
        record["request"]["path"] = path
        record["request_template"] = {"path": example["request"]["path"]}
    record["response"] = response
    record["planner_calls"] = planner_calls
    return record


def _load_app(reference: str) -> Any:
    """The ASGI app that `module:attribute` names, calling the attribute when it is a function."""
    import importlib
    import inspect

    module_name, _, attribute = reference.partition(":")
    target = getattr(importlib.import_module(module_name), attribute)
    if inspect.isfunction(target) or inspect.ismethod(target):
        target = target()
    return target


#: Runs inside the service's own interpreter, so its `app` package is the only one.
def _fastapi_worker(module_name: str, attribute: str, document_json: str) -> list[dict[str, Any]]:
    from contextlib import ExitStack

    document = json.loads(document_json)
    for key, value in (document.get("env") or {}).items():
        os.environ[key] = str(value)

    from agents.common.infrastructure import dagents_runner

    calls: list[str] = []
    original = dagents_runner._execute

    def recording(full_cmd: list[str], input_str: str | None = None) -> Any:
        words = []
        for token in full_cmd[1:]:
            if token.startswith("-"):
                break
            words.append(token)
        name = " ".join(words)
        if name not in calls:
            calls.append(name)
        return original(full_cmd, input_str)

    dagents_runner._execute = recording

    from fastapi.testclient import TestClient

    # The service's own app, and each app the request file names, built the
    # first time an example needs it. All of them stay open until the end, so
    # a later example sees what an earlier one stored in the same app.
    references = {None: f"{module_name}:{attribute}"}
    references.update({name: entry["app"] for name, entry in (document.get("apps") or {}).items()})
    results = []
    answers: dict[str, Any] = {}
    with ExitStack() as stack:
        clients: dict[str | None, Any] = {}
        for example in document.get("examples", []):
            app_name = example.get("app")
            if app_name not in clients:
                clients[app_name] = stack.enter_context(TestClient(_load_app(references[app_name])))
            client = clients[app_name]
            request = example["request"]
            calls.clear()
            kwargs: dict[str, Any] = {"params": request.get("query"), "headers": request.get("headers")}
            if "json" in request:
                kwargs["json"] = request["json"]
            path = resolve_path(request["path"], answers)
            response = client.request(_endpoint_method(example), path, **kwargs)
            headers = {name.lower(): value for name, value in response.headers.items()}
            _remember(answers, example, headers.get("content-type", ""), response.content)
            record = _response_record(response.status_code, headers, response.content, example)
            results.append(_record(example, record, list(calls), path))
    return results


def run_fastapi(service: inventory.PythonService, document: dict[str, Any]) -> list[dict[str, Any]]:
    env = dict(os.environ)
    extra = [entry for app in (document.get("apps") or {}).values() for entry in app.get("pythonpath", [])]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / entry) for entry in (*service.python_path, *extra)]
        + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", service.module, service.attribute],
        input=json.dumps(document),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"capturing {service.name} failed:\n{completed.stderr.strip()[-4000:]}")
    return json.loads(completed.stdout)


def run_planner(binary: str, document: dict[str, Any]) -> list[dict[str, Any]]:
    """Run each example's dagentsc command with its JSON on standard input."""
    results = []
    for example in document.get("examples", []):
        request = example["request"]
        stdin = request.get("stdin")
        completed = subprocess.run(
            [binary, *request["args"]],
            input=json.dumps(stdin) if stdin is not None else "",
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )
        output: Any = completed.stdout
        try:
            output = json.loads(completed.stdout)
        except json.JSONDecodeError:
            pass
        limits = example.get("trim", {})
        body, notes = trim(output, int(limits.get("items", DEFAULT_ITEMS)), int(limits.get("chars", DEFAULT_CHARS)))
        response = {
            "exit_code": completed.returncode,
            "status": completed.returncode,
            "headers": {},
            "body": body,
            "stderr": completed.stderr.strip()[:DEFAULT_CHARS],
            "trimmed": notes,
        }
        record = {key: example[key] for key in ("id", "endpoint", "title") if key in example}
        for key in ("description", "show", "volatile", "trim"):
            if key in example:
                record[key] = example[key]
        record["request"] = dict(request)
        record["response"] = response
        words = []
        for token in request["args"]:
            if token.startswith("-"):
                break
            words.append(token)
        record["planner_calls"] = [" ".join(words)]
        results.append(record)
    return results


def run_process(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Start a service from its `runner`, send the requests over HTTP, stop it."""
    runner = document["runner"]
    port = int(runner["port"])
    if port not in PORT_RANGE:
        raise ValueError(f"runner port {port} must be between 9400 and 9499")
    env = {**os.environ, **{key: str(value) for key, value in (runner.get("env") or {}).items()}}
    env = {key: value.replace("{port}", str(port)) for key, value in env.items()}
    command = [part.replace("{port}", str(port)) for part in runner["command"]]
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT / runner.get("cwd", "."),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + float(runner.get("timeout_seconds", 90))
        while True:
            try:
                opener.open(base + runner.get("ready", "/"), timeout=2).read()
                break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError(f"{' '.join(command)} did not answer on port {port}") from None
                time.sleep(0.5)
        results = []
        answers: dict[str, Any] = {}
        for example in document.get("examples", []):
            request = example["request"]
            path = resolve_path(request["path"], answers)
            url = base + path
            if request.get("query"):
                url += "?" + urllib.parse.urlencode(request["query"])
            data = json.dumps(request["json"]).encode() if "json" in request else None
            headers = dict(request.get("headers") or {})
            if data is not None:
                headers.setdefault("Content-Type", "application/json")
            outgoing = urllib.request.Request(url, data=data, headers=headers, method=_endpoint_method(example))
            try:
                with opener.open(outgoing, timeout=30) as answer:
                    status, raw, answer_headers = answer.status, answer.read(), answer.headers
            except urllib.error.HTTPError as error:
                status, raw, answer_headers = error.code, error.read(), error.headers
            lowered = {name.lower(): value for name, value in answer_headers.items()}
            _remember(answers, example, lowered.get("content-type", ""), raw)
            results.append(_record(example, _response_record(status, lowered, raw, example), [], path))
        return results
    finally:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()


def capture(service: str, binary: str) -> dict[str, Any]:
    """Make every request in one service's request file and build the stored document."""
    document = load_requests(service)
    python_services = {entry.name: entry for entry in inventory.PYTHON_SERVICES}
    if service == PLANNER:
        examples = run_planner(binary, document)
    elif service in python_services:
        examples = run_fastapi(python_services[service], document)
    elif "runner" in document:
        examples = run_process(document)
    else:
        raise ValueError(f"{service}: not a FastAPI service and no 'runner' in its request file")
    for example, declared in zip(examples, document.get("examples", [])):
        expected = declared.get("status")
        if expected is not None and example["response"]["status"] != expected:
            raise RuntimeError(
                f"{service} example {declared['id']!r}: expected status {expected}, "
                f"got {example['response']['status']}: {json.dumps(example['response']['body'])[:400]}"
            )
    return {
        "service": service,
        "generated_by": "scripts/capture_api_examples.py",
        "note": document.get("note"),
        "examples": examples,
    }


def serialize(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=1, ensure_ascii=False) + "\n"


def differences(service: str, fresh: dict[str, Any]) -> list[str]:
    """How a fresh capture differs from the stored one, ignoring volatile values."""
    path = captured_path(service)
    if not path.exists():
        return [f"{path.relative_to(REPO_ROOT)} does not exist"]
    stored = json.loads(path.read_text(encoding="utf-8"))
    problems: list[str] = []
    stored_by_id = {example["id"]: example for example in stored.get("examples", [])}
    fresh_by_id = {example["id"]: example for example in fresh["examples"]}
    if list(stored_by_id) != list(fresh_by_id):
        problems.append(f"{service}: stored examples {list(stored_by_id)} != requested {list(fresh_by_id)}")
    for example_id, example in fresh_by_id.items():
        before = stored_by_id.get(example_id)
        if before is None:
            continue
        if normalized(before) != normalized(example):
            for part in ("request", "response", "planner_calls"):
                if normalized(before).get(part) != normalized(example).get(part):
                    problems.append(f"{service} example {example_id!r}: the {part} changed")
    return problems


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--worker":
        results = _fastapi_worker(argv[1], argv[2], sys.stdin.read())
        sys.stdout.write(json.dumps(results))
        return 0

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--service", action="append", help="capture only this service (repeatable)")
    parser.add_argument("--check", action="store_true", help="replay and compare with the stored examples")
    args = parser.parse_args(argv)

    binary = planner_binary()
    if binary is None:
        print(
            "dagentsc binary is not available. Build it (cd bindings/ocaml && opam exec -- dune build "
            "./bin/dagentsc.exe) or set DAGENTSC_BIN. Without it the Guard denies every request, so the "
            "examples would be wrong.",
            file=sys.stderr,
        )
        return 2
    os.environ["DAGENTSC_BIN"] = binary

    services = args.service or [service_of(path) for path in request_files()]
    problems: list[str] = []
    for service in services:
        fresh = capture(service, binary)
        if args.check:
            problems.extend(differences(service, fresh))
            continue
        path = captured_path(service)
        path.write_text(serialize(fresh), encoding="utf-8")
        print(f"wrote {len(fresh['examples'])} examples to {path.relative_to(REPO_ROOT)}")
    if args.check:
        if problems:
            print("The stored API examples no longer match the code:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print("Capture them again: .venv/bin/python scripts/capture_api_examples.py", file=sys.stderr)
            return 1
        print(f"API examples match the code ({len(services)} services)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
