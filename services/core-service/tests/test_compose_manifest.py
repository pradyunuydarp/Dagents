"""Tests for the Docker Compose target of core-service.

The planner (`dagentsc manifest compile`) renders a Compose file, and
`app/services/compose_renderer.py` renders it when the planner cannot be
started. The two must give the same answer, so the golden files in
`fixtures/compose/` are checked against both: `ComposeFallbackTests` runs the
Python renderer on every file, and `ComposePlannerTests` runs the planner on
the same files when `DAGENTSC_BIN` is set. CI runs this file with and without
the planner.

Each golden file is `{"payload": ..., "expected": ...}`: the snake_case payload
that `run_dagentsc` sends, and the snake_case dict it returns. The OCaml tests
in `bindings/ocaml/test/test_compilers.ml` check the same cases from the
camelCase side.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tempfile
import typing
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from agents.common.infrastructure.dagents_runner import PlannerUnavailableError, run_dagentsc
from app.main import app
from app.models import ComposeIssue, ComposeManifestRequest
from app.services.compose_renderer import quote, render_compose_plan
from app.services.manifest_service import ManifestService

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "compose"
REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON_IR = REPO_ROOT / "bindings" / "ocaml" / "lib" / "common_ir" / "dagents_common_ir.ml"

FIXTURE_NAMES = (
    "lma-merges-into-project",
    "job-on-external-network",
    "quoting",
    "yaml-typed-names",
    "every-issue",
    "no-services",
)


def load_fixtures() -> dict[str, dict[str, typing.Any]]:
    """Read every golden file, keyed by its name without `.json`."""
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURE_DIR.glob("*.json"))
    }


FIXTURES = load_fixtures()

PLANNER_COMMAND = ["manifest", "compile", "--input", "-", "--output", "json"]

# The first golden file as an HTTP request body: one LMA merged into the
# running healthcare demo project.
LMA_REQUEST: dict[str, typing.Any] = {
    "placement": "merge",
    "existing_services": [
        "gma",
        "lma-uk-134",
        "lma-it-002",
        "lma-ch-245",
        "healthcare-demo-backend",
        "healthcare-demo-frontend",
    ],
    "components": [
        {
            "name": "lma-hospital-d",
            "image": "dagents/healthcare-demo-backend:local",
            "args": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"],
            "env_files": ["../../env/.env.shared", "env/.env.healthcare-demo"],
            "env": [
                {"name": "HEALTHCARE_DEMO_ROLE", "value": "lma"},
                {"name": "HEALTHCARE_DEMO_SITE_ID", "value": "hospital-d"},
                {"name": "HEALTHCARE_DEMO_API_PORT", "value": "8085"},
            ],
            "ports": [{"container_port": 8085, "host_port": 8085}],
            "health_check": {
                "command": [
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/api/v1/health')",
                ]
            },
            "depends_on": [{"service": "gma", "condition": "service_healthy"}],
        }
    ],
}
LMA_RESPONSE = {
    "services": ["lma-hospital-d"],
    "issues": [],
    "compose_yaml": FIXTURES["lma-merges-into-project"]["expected"]["compose_yaml"],
}


def lma_request(edit: typing.Callable[[dict, dict], typing.Any]) -> dict[str, typing.Any]:
    """A copy of `LMA_REQUEST` with one change, made by `edit(body, component)`."""
    body = copy.deepcopy(LMA_REQUEST)
    edit(body, body["components"][0])
    return body


def shape_error_payloads() -> dict[str, dict[str, typing.Any]]:
    """Payloads the planner refuses with exit status 2 and the fallback with ValueError.

    Each changes one thing in the first golden file. The HTTP models refuse the
    same mistakes with a 422, so neither renderer sees them from the route.
    """
    base = FIXTURES["lma-merges-into-project"]["payload"]
    cases: dict[str, dict[str, typing.Any]] = {}

    def case(name: str, edit: typing.Callable[[dict, dict], typing.Any]) -> None:
        payload = copy.deepcopy(base)
        edit(payload, payload["components"][0])
        cases[name] = payload

    case("health check with command and shell", lambda spec, component: component["health_check"].update(shell="true"))
    case("health check with neither", lambda spec, component: component["health_check"].pop("command"))
    case("unknown condition", lambda spec, component: component["depends_on"][0].update(condition="started"))
    case("unknown placement", lambda spec, component: spec["target"].update(placement="stack"))
    case("unknown target kind", lambda spec, component: spec["target"].update(kind="helm"))
    return cases


def surrogate_payloads() -> dict[str, dict[str, typing.Any]]:
    """Payloads with a lone surrogate, which is not UTF-8, in a string the planner reads.

    `json.loads` reads one from an escape such as `\\ud800`. Yojson refuses a
    lone high surrogate, and the planner's codec refuses the bytes Yojson reads
    from a lone low one, so the planner exits with status 2 and the fallback
    raises ValueError.
    """
    base = FIXTURES["lma-merges-into-project"]["payload"]
    cases: dict[str, dict[str, typing.Any]] = {}

    def case(name: str, edit: typing.Callable[[dict, dict], typing.Any]) -> None:
        payload = copy.deepcopy(base)
        edit(payload, payload["components"][0])
        cases[name] = payload

    case("high surrogate in the image", lambda spec, component: component.update(image="\ud800"))
    case("low surrogate in the image", lambda spec, component: component.update(image="a\udc80b"))
    case("in an argument", lambda spec, component: component["args"].append("\udfff"))
    case("in an env file", lambda spec, component: component["env_files"].append("\udc80.env"))
    case("in an environment value", lambda spec, component: component["env"][0].update(value="\udc80"))
    case("in a health check", lambda spec, component: component["health_check"]["command"].append("\udc80"))
    case("in a dependency", lambda spec, component: component["depends_on"][0].update(service="\udc80"))
    case("in a running service", lambda spec, component: spec["target"]["existing_services"].append("\udc80"))
    case("in the plan id", lambda spec, component: spec.update(plan_id="\udc80"))
    return cases


# The planner's integers are OCaml's 63-bit `int`: -2**62 to 2**62 - 1.
LARGEST_PLANNER_INT = 2**62 - 1


def number_payloads(value: int) -> dict[str, dict[str, typing.Any]]:
    """Payloads with `value` in each integer field the Compose target reads.

    Each changes one number in the first golden file. A value outside the
    planner's 63-bit integers makes the planner exit with status 2 and the
    fallback raise ValueError, so the route answers 500 on both paths.
    """
    base = FIXTURES["lma-merges-into-project"]["payload"]
    cases: dict[str, dict[str, typing.Any]] = {}

    def case(name: str, edit: typing.Callable[[dict], typing.Any]) -> None:
        payload = copy.deepcopy(base)
        edit(payload["components"][0])
        cases[name] = payload

    case("replicas", lambda component: component.update(replicas=value))
    case("container port", lambda component: component["ports"][0].update(container_port=value))
    case("host port", lambda component: component["ports"][0].update(host_port=value))
    case("health check retries", lambda component: component["health_check"].update(retries=value))
    return cases


def largest_number_issues() -> list[dict[str, typing.Any]]:
    """The issues both renderers report for each `number_payloads(LARGEST_PLANNER_INT)` case, in its order."""
    service = "lma-hospital-d"
    value = str(LARGEST_PLANNER_INT)
    return [
        [{"service": service, "code": "unsupported_replicas", "value": value}],
        [{"service": service, "code": "invalid_port", "value": value}],
        [{"service": service, "code": "invalid_port", "value": value}],
        [],
    ]


# The most seconds a Compose duration can hold (a Go time.Duration).
LONGEST_DURATION = 9_223_372_036


def health_timing_payloads() -> dict[str, tuple[dict[str, typing.Any], list[dict[str, typing.Any]]]]:
    """Payloads at and past the limits of each health check duration, with the issues both renderers report.

    Each changes one timing field of the first golden file's health check.
    """
    base = FIXTURES["lma-merges-into-project"]["payload"]
    refused = [{"service": "lma-hospital-d", "code": "invalid_health_check_timing", "value": None}]
    cases: dict[str, tuple[dict[str, typing.Any], list[dict[str, typing.Any]]]] = {}
    for field, lowest in (("interval_seconds", 1), ("timeout_seconds", 1), ("start_period_seconds", 0)):
        for value, issues in (
            (lowest - 1, refused),
            (lowest, []),
            (LONGEST_DURATION, []),
            (LONGEST_DURATION + 1, refused),
        ):
            payload = copy.deepcopy(base)
            payload["components"][0]["health_check"][field] = value
            cases[f"{field}={value}"] = (payload, issues)
    return cases


class ComposeFallbackTests(unittest.TestCase):
    """The Python renderer gives the golden answer for every fixture."""

    def test_every_golden_file_is_present(self) -> None:
        self.assertEqual(sorted(FIXTURES), sorted(FIXTURE_NAMES))

    def test_renders_each_fixture(self) -> None:
        for name, fixture in FIXTURES.items():
            with self.subTest(fixture=name):
                self.assertEqual(render_compose_plan(fixture["payload"]), fixture["expected"])

    def test_refuses_each_shape_error(self) -> None:
        for name, payload in shape_error_payloads().items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                render_compose_plan(payload)

    def test_refuses_text_that_is_not_utf_8(self) -> None:
        for name, payload in surrogate_payloads().items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                render_compose_plan(payload)

    def test_refuses_a_number_the_planner_cannot_read(self) -> None:
        for value in (LARGEST_PLANNER_INT + 1, -(LARGEST_PLANNER_INT + 2)):
            for name, payload in number_payloads(value).items():
                with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                    render_compose_plan(payload)

    def test_reads_the_largest_planner_number(self) -> None:
        for (name, payload), issues in zip(number_payloads(LARGEST_PLANNER_INT).items(), largest_number_issues()):
            with self.subTest(field=name):
                self.assertEqual(render_compose_plan(payload)["issues"], issues)

    def test_bounds_health_check_durations(self) -> None:
        for name, (payload, issues) in health_timing_payloads().items():
            with self.subTest(case=name):
                self.assertEqual(render_compose_plan(payload)["issues"], issues)

    def test_refuses_a_kubernetes_target(self) -> None:
        payload = copy.deepcopy(FIXTURES["lma-merges-into-project"]["payload"])
        for target in (None, "kubernetes", {"kind": "kubernetes"}):
            payload["target"] = target
            with self.subTest(target=target), self.assertRaises(ValueError):
                render_compose_plan(payload)

    def test_quotes_values(self) -> None:
        cases = {
            "plain": '"plain"',
            "back\\slash": '"back\\\\slash"',
            'say "hi"': '"say \\"hi\\""',
            "$HOME and ${X}": '"$$HOME and $${X}"',
            "a\nb\tc\rd": '"a\\nb\\tc\\rd"',
            "\x00\x01\x1f\x7f": '"\\x00\\x01\\x1F\\x7F"',
            "zürich 日本": '"zürich 日本"',
            "": '""',
            "nel\x85x": '"nel\\Nx"',
            "ls\u2028x": '"ls\\Lx"',
            "ps\u2029x": '"ps\\Px"',
            "c1\x80\x9f": '"c1\\x80\\x9F"',
            "nbsp\xa0": '"nbsp\xa0"',
            "nonchar\ufffe\uffff": '"nonchar\\uFFFE\\uFFFF"',
            "replacement\ufffd": '"replacement\ufffd"',
            "emoji\U0001F600": '"emoji\U0001F600"',
            "lone\udc80": '"lone\\uFFFD"',
        }
        for value, quoted in cases.items():
            with self.subTest(value=value):
                self.assertEqual(quote(value), quoted)


class ComposeVocabularyTests(unittest.TestCase):
    """The issue codes are the same in the fixtures, the response model and the planner."""

    def model_codes(self) -> tuple[str, ...]:
        return typing.get_args(ComposeIssue.model_fields["code"].annotation)

    def test_fixtures_use_every_code_of_the_model(self) -> None:
        used = {issue["code"] for fixture in FIXTURES.values() for issue in fixture["expected"]["issues"]}
        self.assertEqual(used, set(self.model_codes()))

    def test_model_codes_equal_the_planner_codes(self) -> None:
        # Read from the OCaml source, so this test needs no dagentsc build.
        source = COMMON_IR.read_text(encoding="utf-8")
        match = re.search(r"let compose_issue_code_and_value\b(.*?)(?:\n\n|\Z)", source, re.DOTALL)
        self.assertIsNotNone(match, f"compose_issue_code_and_value is missing from {COMMON_IR}")
        planner_codes = tuple(re.findall(r'\("([a-z_]+)",', match.group(1)))
        self.assertEqual(self.model_codes(), planner_codes)


class ComposePlannerTests(unittest.TestCase):
    """dagentsc gives the golden answer for every fixture. Runs when DAGENTSC_BIN is set."""

    @classmethod
    def setUpClass(cls) -> None:
        binary = os.environ.get("DAGENTSC_BIN")
        if not binary:
            raise unittest.SkipTest("dagentsc binary is not available; set DAGENTSC_BIN to test the planner path")
        # A planner built before the Compose target ignores --target and prints
        # Kubernetes YAML. Fail the class instead of skipping it, so that such a
        # binary cannot pass as a tested planner path.
        result = subprocess.run(
            [binary, "manifest", "compile", "--target", "compose"], capture_output=True, text=True, check=False
        )
        if result.returncode != 0 or not result.stdout.startswith("services:"):
            raise AssertionError(
                f"DAGENTSC_BIN={binary} cannot render a Compose file (exit {result.returncode}): "
                f"{(result.stdout or result.stderr)[:300]!r}"
            )
        cls.binary = binary

    def compile_yaml(self, spec: dict[str, typing.Any], *flags: str) -> subprocess.CompletedProcess:
        """Run `dagentsc manifest compile` on a camelCase spec, as a shell script would."""
        return subprocess.run(
            [self.binary, "manifest", "compile", "--input", "-", *flags],
            input=json.dumps(spec),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_yaml_output_with_issues_exits_3(self) -> None:
        # Docker Compose merges a file of comments as nothing when it follows a
        # project's own file, so the YAML output must not exit 0 without a file.
        spec = {"target": {"kind": "compose", "placement": "merge"}, "components": [{"name": "lma-x", "image": ""}]}
        yaml = self.compile_yaml(spec)
        self.assertEqual(yaml.returncode, 3, yaml.stderr)
        self.assertEqual(yaml.stdout, "# No Compose file: the spec has issues.\n# lma-x: empty_image\n")
        self.assertEqual(yaml.stderr, "")
        plan = self.compile_yaml(spec, "--output", "json")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertIsNone(json.loads(plan.stdout)["composeYaml"])
        spec["components"][0]["image"] = "x"
        rendered = self.compile_yaml(spec)
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertTrue(rendered.stdout.startswith('services:\n  "lma-x":\n'), rendered.stdout)

    def test_renders_each_fixture(self) -> None:
        for name, fixture in FIXTURES.items():
            with self.subTest(fixture=name):
                self.assertEqual(run_dagentsc(PLANNER_COMMAND, fixture["payload"]), fixture["expected"])

    def test_refuses_each_shape_error(self) -> None:
        for name, payload in shape_error_payloads().items():
            with self.subTest(case=name), self.assertRaises(PlannerUnavailableError) as raised:
                run_dagentsc(PLANNER_COMMAND, payload)
            self.assertIn("Invalid_argument", str(raised.exception))

    def test_refuses_text_that_is_not_utf_8(self) -> None:
        for name, payload in surrogate_payloads().items():
            with self.subTest(case=name), self.assertRaises(PlannerUnavailableError) as raised:
                run_dagentsc(PLANNER_COMMAND, payload)
            self.assertTrue(
                "Expected UTF-8 text" in str(raised.exception) or "surrogate" in str(raised.exception),
                str(raised.exception),
            )

    def test_refuses_a_number_it_cannot_read(self) -> None:
        for value in (LARGEST_PLANNER_INT + 1, -(LARGEST_PLANNER_INT + 2)):
            for name, payload in number_payloads(value).items():
                with self.subTest(field=name, value=value), self.assertRaises(PlannerUnavailableError) as raised:
                    run_dagentsc(PLANNER_COMMAND, payload)
                self.assertIn("int_of_string", str(raised.exception))

    def test_bounds_health_check_durations(self) -> None:
        for name, (payload, issues) in health_timing_payloads().items():
            with self.subTest(case=name):
                self.assertEqual(run_dagentsc(PLANNER_COMMAND, payload)["issues"], issues)

    def test_reads_the_largest_number(self) -> None:
        for (name, payload), issues in zip(number_payloads(LARGEST_PLANNER_INT).items(), largest_number_issues()):
            with self.subTest(field=name):
                self.assertEqual(run_dagentsc(PLANNER_COMMAND, payload)["issues"], issues)


class ComposeApiTests(unittest.TestCase):
    """POST /api/v1/manifests/compose, through whichever renderer is active.

    With DAGENTSC_BIN set this is the planner; without it, the Python renderer.
    """

    def setUp(self) -> None:
        self.client = TestClient(app)

    def post(self, body: dict[str, typing.Any]):
        return self.client.post("/api/v1/manifests/compose", json=body)

    def test_renders_an_lma_merged_into_a_project(self) -> None:
        response = self.post(LMA_REQUEST)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), LMA_RESPONSE)

    def test_reports_issues_with_a_200(self) -> None:
        response = self.post(lma_request(lambda body, component: body.update(placement="own")))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {
                "services": ["lma-hospital-d"],
                "issues": [{"service": "lma-hospital-d", "code": "dependency_outside_project", "value": "gma"}],
                "compose_yaml": None,
            },
        )

    def test_reports_an_empty_request_as_an_issue(self) -> None:
        response = self.post({})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json(),
            {"services": [], "issues": [{"service": None, "code": "no_services", "value": None}], "compose_yaml": None},
        )

    def test_refuses_a_wrong_shape_with_422(self) -> None:
        cases = {
            "kind without a Compose equivalent": lambda body, component: component.update(kind="CronJob"),
            "health check with command and shell": lambda body, component: component["health_check"].update(
                shell="true"
            ),
            "health check with neither": lambda body, component: component.update(health_check={}),
            "unknown condition": lambda body, component: component["depends_on"][0].update(condition="started"),
            "unknown placement": lambda body, component: body.update(placement="stack"),
            "a field the route does not read": lambda body, component: component.update(replicas=2),
        }
        for name, edit in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.post(lma_request(edit)).status_code, 422)

    def test_answers_500_for_a_number_the_planner_cannot_read(self) -> None:
        # The models set no numeric range, and the planner's integers have 63
        # bits. Both renderers refuse a larger number, so the answer is 500 with
        # or without the planner.
        body = lma_request(lambda body, component: component["ports"][0].update(host_port=10**20))
        response = TestClient(app, raise_server_exceptions=False).post("/api/v1/manifests/compose", json=body)
        self.assertEqual(response.status_code, 500)

    def test_answers_500_for_text_that_is_not_utf_8(self) -> None:
        # The models accept a lone surrogate, which `json.loads` reads from an
        # escape. Both renderers refuse it, so the answer is 500 with or
        # without the planner. The body is sent as text, because the client
        # cannot encode a lone surrogate.
        client = TestClient(app, raise_server_exceptions=False)
        bodies = {
            "high surrogate in a field the answer does not hold": '{"components": [{"name": "A", "image": "\\ud800"}]}',
            "low surrogate in a field the answer does not hold": '{"components": [{"name": "A", "image": "\\udc80"}]}',
            "low surrogate in an environment value": (
                '{"components": [{"name": "a", "image": "x", "env": [{"name": "V", "value": "\\udc80"}]}]}'
            ),
        }
        for name, body in bodies.items():
            with self.subTest(case=name):
                response = client.post(
                    "/api/v1/manifests/compose", content=body, headers={"Content-Type": "application/json"}
                )
                self.assertEqual(response.status_code, 500)

    def test_kubernetes_routes_still_answer(self) -> None:
        component = {"name": "lma-hospital-d", "image": "dagents/healthcare-demo-backend:local"}
        pods = self.client.post("/api/v1/manifests/pods", json={"components": [component]})
        self.assertEqual(pods.status_code, 200, pods.text)
        self.assertIn("kind: Deployment", pods.json()["combined_yaml"])
        compiled = self.client.post(
            "/api/v1/workloads:compile", json={"plan_id": "compose-neighbour", "components": [component]}
        )
        self.assertEqual(compiled.status_code, 200, compiled.text)
        self.assertEqual(compiled.json()["plan_id"], "compose-neighbour")
        self.assertIn("kind: Deployment", compiled.json()["combined_yaml"])


class ComposeFallbackRuleTests(unittest.TestCase):
    """The service renders in Python only when the planner cannot be started."""

    def request(self) -> ComposeManifestRequest:
        return ComposeManifestRequest.model_validate(LMA_REQUEST)

    def test_falls_back_when_the_binary_is_missing(self) -> None:
        with mock.patch.dict(os.environ, {"DAGENTSC_BIN": "/nonexistent/dagentsc"}):
            response = ManifestService().render_compose(self.request())
        self.assertEqual(response.model_dump(), LMA_RESPONSE)

    def test_falls_back_when_the_binary_cannot_be_run(self) -> None:
        with tempfile.NamedTemporaryFile(suffix="-dagentsc") as not_executable:
            os.chmod(not_executable.name, 0o644)
            with mock.patch.dict(os.environ, {"DAGENTSC_BIN": not_executable.name}):
                response = ManifestService().render_compose(self.request())
        self.assertEqual(response.model_dump(), LMA_RESPONSE)

    def test_does_not_fall_back_when_the_planner_fails(self) -> None:
        with mock.patch.dict(os.environ, {"DAGENTSC_BIN": "/bin/false"}):
            with self.assertRaises(PlannerUnavailableError):
                ManifestService().render_compose(self.request())
            response = TestClient(app, raise_server_exceptions=False).post(
                "/api/v1/manifests/compose", json=LMA_REQUEST
            )
        self.assertEqual(response.status_code, 500)


if __name__ == "__main__":
    unittest.main()
