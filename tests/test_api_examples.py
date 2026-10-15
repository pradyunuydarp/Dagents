"""Tests for the example requests and responses in the API reference.

Every example on the reference pages is a real call, captured by
`scripts/capture_api_examples.py`. Two kinds of test keep them correct:

- **File tests** need no planner and no network. Each stored example must
  match the request its request file declares, name an endpoint that exists,
  use a path that fits that endpoint's template, and send and receive bodies
  that fit the schemas in `docs/reference/api-reference.json`.
- **Replay tests** make every request again, with the planner, and fail if an
  answer changed (apart from the values an example marks as volatile). They
  skip when `dagentsc` is not built; CI provides it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import capture_api_examples as capture  # noqa: E402  (needs the sys.path entry above)

REFERENCE_PATH = REPO_ROOT / "docs" / "reference" / "api-reference.json"

_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def schema_errors(instance: Any, schema: Any, schemas: dict[str, Any], *, strict: bool, path: str = "$") -> list[str]:
    """Check a JSON value against the subset of JSON Schema that OpenAPI uses here.

    `strict` adds the checks a trimmed response could fail (string length and
    pattern, list length), so it is used for requests only.
    """
    if not isinstance(schema, dict) or not schema:
        return []
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        if name not in schemas:
            return [f"{path}: unknown schema {name}"]
        return schema_errors(instance, schemas[name], schemas, strict=strict, path=path)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            branches = [schema_errors(instance, branch, schemas, strict=strict, path=path) for branch in schema[key]]
            if any(not errors for errors in branches):
                return []
            return [f"{path}: matches none of {key}: " + "; ".join(errors[0] for errors in branches if errors)]
    errors: list[str] = []
    for branch in schema.get("allOf", []):
        errors += schema_errors(instance, branch, schemas, strict=strict, path=path)
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: must be {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} is not one of {schema['enum']}")
    expected = schema.get("type")
    if expected:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(instance, name) for name in allowed):
            return errors + [f"{path}: expected {expected}, got {type(instance).__name__}"]
    if isinstance(instance, bool):
        return errors
    if isinstance(instance, (int, float)):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below the minimum {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above the maximum {schema['maximum']}")
    if isinstance(instance, str) and strict:
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(instance, dict):
        for name in schema.get("required", []):
            if name not in instance:
                errors.append(f"{path}: missing {name}")
        properties = schema.get("properties", {})
        for name, value in instance.items():
            if name in properties:
                errors += schema_errors(value, properties[name], schemas, strict=strict, path=f"{path}.{name}")
            elif isinstance(schema.get("additionalProperties"), dict):
                errors += schema_errors(
                    value, schema["additionalProperties"], schemas, strict=strict, path=f"{path}.{name}"
                )
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected field {name}")
    if isinstance(instance, list):
        if strict and "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        for index, item in enumerate(instance):
            errors += schema_errors(item, schema.get("items", {}), schemas, strict=strict, path=f"{path}[{index}]")
    return errors


def _is_type(instance: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    return isinstance(instance, _JSON_TYPES.get(name, object))


def template_pattern(path: str) -> re.Pattern[str]:
    """A regex for an endpoint's path template: `{plan_id}` matches one segment."""
    parts = re.split(r"(\{[^}]+\})", path)
    return re.compile("^" + "".join("[^/]+" if part.startswith("{") else re.escape(part) for part in parts) + "$")


class ApiExampleFileTests(unittest.TestCase):
    """The stored examples are current, real and consistent with the schemas."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.reference = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
        cls.services = {service["name"]: service for service in cls.reference["services"]}
        cls.commands = {" ".join(command["words"]) for command in cls.reference["planner"]["commands"]}
        cls.files = capture.request_files()

    def stored(self, service: str) -> dict[str, Any]:
        path = capture.captured_path(service)
        self.assertTrue(path.exists(), f"{path.name} is missing. Run: .venv/bin/python scripts/capture_api_examples.py")
        return json.loads(path.read_text(encoding="utf-8"))

    def endpoint(self, service: str, key: str) -> dict[str, Any] | None:
        method, _, path = key.partition(" ")
        return next(
            (e for e in self.services[service]["endpoints"] if e["method"] == method and e["path"] == path),
            None,
        )

    def test_there_are_examples(self) -> None:
        self.assertTrue(self.files, "no docs/reference/api/examples/*.requests.json files")

    def test_every_request_file_has_a_current_capture(self) -> None:
        for path in self.files:
            service = capture.service_of(path)
            declared = capture.load_requests(service)
            stored = self.stored(service)
            with self.subTest(service=service):
                self.assertEqual(
                    [example["id"] for example in declared["examples"]],
                    [example["id"] for example in stored["examples"]],
                    "the stored examples are not the declared ones; capture again",
                )
            for want, have in zip(declared["examples"], stored["examples"]):
                with self.subTest(service=service, example=want["id"]):
                    self.assertEqual(want["endpoint"], have["endpoint"])
                    self.assertEqual(want.get("title"), have.get("title"))
                    self.assertEqual(want.get("app"), have.get("app"), "the example's app changed; capture again")
                    expected_request = dict(want["request"])
                    if service != capture.PLANNER:
                        expected_request = {"method": want["endpoint"].split()[0], **expected_request}
                    have_request = dict(have["request"])
                    if "request_template" in have:
                        # The path holds an id from an earlier answer ({from:...}); the template is what was declared.
                        self.assertRegex(want["request"]["path"], capture.FROM_EARLIER)
                        have_request["path"] = have["request_template"]["path"]
                    self.assertEqual(expected_request, have_request, "the request changed; capture again")
                    if "status" in want:
                        self.assertEqual(want["status"], have["response"]["status"])

    def test_every_example_names_an_existing_endpoint(self) -> None:
        for path in self.files:
            service = capture.service_of(path)
            for example in self.stored(service)["examples"]:
                with self.subTest(service=service, example=example["id"]):
                    if service == capture.PLANNER:
                        words = example["endpoint"].split()
                        self.assertEqual(capture.PLANNER, words[0])
                        self.assertIn(" ".join(words[1:]), self.commands)
                        self.assertEqual(words[1:], example["request"]["args"][: len(words) - 1])
                        continue
                    self.assertIn(service, self.services)
                    entry = self.endpoint(service, example["endpoint"])
                    self.assertIsNotNone(entry, f"{example['endpoint']} is not an endpoint of {service}")
                    self.assertEqual(entry["method"], example["request"]["method"])
                    self.assertRegex(example["request"]["path"], template_pattern(entry["path"]))

    def test_requests_and_responses_fit_the_schemas(self) -> None:
        for path in self.files:
            service = capture.service_of(path)
            if service == capture.PLANNER:
                continue
            schemas = self.services[service]["schemas"]
            for example in self.stored(service)["examples"]:
                entry = self.endpoint(service, example["endpoint"])
                if entry is None:
                    continue
                with self.subTest(service=service, example=example["id"], part="request"):
                    body = entry["request_body"]
                    if "json" in example["request"] and body:
                        errors = schema_errors(example["request"]["json"], body["schema"], schemas, strict=True)
                        if example["response"]["status"] != 422:
                            self.assertEqual([], errors)
                with self.subTest(service=service, example=example["id"], part="response"):
                    response = example["response"]
                    documented = next(
                        (r for r in entry["responses"] if r["status"] == str(response["status"])), None
                    )
                    content_type = response["headers"].get("content-type", "")
                    if documented and documented["schema"] and "json" in content_type:
                        self.assertEqual(
                            [], schema_errors(response["body"], documented["schema"], schemas, strict=False)
                        )


class ApiExampleAppTests(unittest.TestCase):
    """An example can run against an app its request file names, instead of the service's own app."""

    def test_every_named_app_is_declared_and_importable_from_its_path(self) -> None:
        for path in capture.request_files():
            document = capture.load_requests(capture.service_of(path))
            apps = document.get("apps") or {}
            for name, entry in apps.items():
                with self.subTest(service=document["service"], app=name):
                    self.assertRegex(entry["app"], r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")
                    for directory in entry.get("pythonpath", []):
                        self.assertTrue((REPO_ROOT / directory).is_dir(), f"{directory} is not a directory")
            for example in document["examples"]:
                if "app" in example:
                    with self.subTest(service=document["service"], example=example["id"]):
                        self.assertIn(example["app"], apps)

    def test_an_example_naming_an_undeclared_app_is_refused(self) -> None:
        document = {
            "service": "lma",
            "examples": [
                {"id": "x", "app": "missing", "endpoint": "GET /api/v1/health", "request": {"path": "/api/v1/health"}}
            ],
        }
        with self.assertRaisesRegex(ValueError, "missing"):
            capture.check_apps("lma.requests.json", document)

    def test_an_example_runs_against_the_app_it_names(self) -> None:
        import tempfile
        import textwrap

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "override_app_for_test.py").write_text(
                textwrap.dedent(
                    """
                    from fastapi import FastAPI

                    def build():
                        app = FastAPI()

                        @app.get("/api/v1/health")
                        def health():
                            return {"status": "ok", "agent_type": "override"}

                        return app
                    """
                ),
                encoding="utf-8",
            )
            document = {
                "service": "lma",
                "apps": {"other": {"app": "override_app_for_test:build", "pythonpath": [directory]}},
                "examples": [
                    {"id": "plain", "endpoint": "GET /api/v1/health", "request": {"path": "/api/v1/health"}},
                    {
                        "id": "named",
                        "app": "other",
                        "endpoint": "GET /api/v1/health",
                        "request": {"path": "/api/v1/health"},
                    },
                ],
            }
            service = next(entry for entry in capture.inventory.PYTHON_SERVICES if entry.name == "lma")
            results = capture.run_fastapi(service, document)
        self.assertEqual("LMA", results[0]["response"]["body"]["agent_type"])
        self.assertNotIn("app", results[0])
        self.assertEqual("override", results[1]["response"]["body"]["agent_type"])
        self.assertEqual("other", results[1]["app"])


def planner_available() -> bool:
    return capture.planner_binary() is not None


class ApiExampleReplayTests(unittest.TestCase):
    """Making every request again gives the stored answers."""

    @unittest.skipUnless(
        planner_available(),
        "dagentsc binary is not available; build it or set DAGENTSC_BIN to replay the API examples",
    )
    def test_the_examples_match_a_fresh_run(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "capture_api_examples.py"), "--check"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=dict(os.environ),
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)


if __name__ == "__main__":
    unittest.main()
