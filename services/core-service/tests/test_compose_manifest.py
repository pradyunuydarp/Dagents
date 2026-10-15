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
import re
import typing
import unittest
from pathlib import Path

from app.models import ComposeIssue
from app.services.compose_renderer import quote, render_compose_plan

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "compose"
REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON_IR = REPO_ROOT / "bindings" / "ocaml" / "lib" / "common_ir" / "dagents_common_ir.ml"

FIXTURE_NAMES = (
    "lma-merges-into-project",
    "job-on-external-network",
    "quoting",
    "every-issue",
    "no-services",
)


def load_fixtures() -> dict[str, dict[str, typing.Any]]:
    """Read every golden file, keyed by its name without `.json`."""
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURE_DIR.glob("*.json"))
    }


FIXTURES = load_fixtures()


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


if __name__ == "__main__":
    unittest.main()
