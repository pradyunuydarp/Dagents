"""HTTP behaviour of pipeline validation and runs.

The OCaml pipeline compiler decides whether a pipeline graph is valid and the
order its steps run in. These tests run against the real `dagentsc` binary,
because a stub would only show that the service calls something. They skip,
with a message, when the binary is missing.
"""

from __future__ import annotations

import os
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agents.tests.test_governance import dagentsc_available
from app.api.routes import PLANNER_UNAVAILABLE_DETAIL, get_pipeline_service
from app.main import app
from app.models import PipelineDefinition
from app.pipeline.engine import PipelineExecutionEngine
from app.services.pipeline_service import (
    InMemoryPipelineDefinitionRepository,
    InMemoryPipelineRunRepository,
    PipelineService,
)

REGISTER_PATHS = ("/api/v1/pipelines", "/api/v1/pipeline-definitions")


def step(step_id: str, depends_on: list[str] | None = None, kind: str = "enrich_context", **config: object) -> dict:
    return {"step_id": step_id, "kind": kind, "depends_on": depends_on or [], "config": dict(config)}


class PipelineApiTestCase(unittest.TestCase):
    """Each test gets its own service, so registrations do not leak between tests."""

    def setUp(self) -> None:
        self.service = PipelineService(
            definitions=InMemoryPipelineDefinitionRepository(),
            runs=InMemoryPipelineRunRepository(),
            engine=PipelineExecutionEngine(),
        )
        app.dependency_overrides[get_pipeline_service] = lambda: self.service
        self.addCleanup(app.dependency_overrides.clear)
        # The routes log why the planner was unavailable; keep that out of the test output.
        logger = patch("app.api.routes.logger")
        self.logger = logger.start()
        self.addCleanup(logger.stop)
        self.client = TestClient(app, raise_server_exceptions=False)

    def register(self, pipeline_id: str, steps: list[dict], path: str = REGISTER_PATHS[0]):
        return self.client.post(path, json={"pipeline_id": pipeline_id, "steps": steps})

    def assert_registered(self, pipeline_id: str, steps: list[dict]) -> None:
        response = self.register(pipeline_id, steps)
        self.assertEqual(200, response.status_code, response.text)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PlannerDecidesTheGraphTests(PipelineApiTestCase):
    """A graph the planner rejects is a 422 with the planner's message, and is not stored."""

    CASES = {
        "cycle": (
            [step("a", ["b"]), step("b", ["a"])],
            "Cyclic dependency: a -> b -> a (each step depends on the next)",
        ),
        "self-dependency": ([step("a", ["a"])], "Cyclic dependency: a -> a (each step depends on the next)"),
        "duplicate id": ([step("a"), step("a")], "Duplicate step id: a"),
        "unknown dependency": (
            [step("a", ["missing"])],
            "Step a depends on missing, which is not a step of this pipeline",
        ),
    }

    def test_each_graph_error_is_a_422_with_the_planner_message(self) -> None:
        for path in REGISTER_PATHS:
            for name, (steps, message) in self.CASES.items():
                with self.subTest(path=path, case=name):
                    response = self.register("broken", steps, path)
                    self.assertEqual(422, response.status_code, response.text)
                    self.assertEqual({"detail": message}, response.json())
                    stored = self.client.get("/api/v1/pipeline-definitions/broken")
                    self.assertEqual(404, stored.status_code)

    def test_a_valid_graph_is_stored(self) -> None:
        for path in REGISTER_PATHS:
            with self.subTest(path=path):
                response = self.register("valid", [step("b", ["a"]), step("a")], path)
                self.assertEqual(200, response.status_code, response.text)
                self.assertIsNotNone(response.json()["registered_at"])

    def test_steps_run_in_the_planner_order(self) -> None:
        self.assert_registered(
            "ordered",
            [
                step("c", ["b"], target_field="c", values={"seen": True}),
                step("b", ["a"], target_field="b", values={"seen": True}),
                step("a", target_field="a", values={"seen": True}),
            ],
        )
        response = self.client.post("/api/v1/pipelines/ordered/runs", json={"payload": {}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(["a", "b", "c"], [result["step_id"] for result in response.json()["step_results"]])

    def test_validating_a_stored_pipeline(self) -> None:
        self.assert_registered("stored", [step("a")])
        response = self.client.post("/api/v1/pipeline-definitions/stored:validate")
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual("stored", response.json()["pipeline_id"])
        missing = self.client.post("/api/v1/pipeline-definitions/nope:validate")
        self.assertEqual(404, missing.status_code)
        self.assertEqual({"detail": "Unknown pipeline: nope"}, missing.json())


class PlannerFollowedTests(PipelineApiTestCase):
    """The engine runs the order the planner returns, and refuses an answer that does not fit."""

    def plan(self, *step_ids: str) -> dict:
        return {"pipeline_id": "p", "steps": [{"step_id": step_id} for step_id in step_ids]}

    def test_the_engine_uses_the_planner_order_rather_than_its_own(self) -> None:
        # b and c both depend only on a, so either order is valid. The engine
        # must run the one the planner chose.
        self.service._definitions.save(
            PipelineDefinition.model_validate(
                {"pipeline_id": "p", "steps": [step("a"), step("b", ["a"]), step("c", ["a"])]}
            )
        )
        with patch("app.pipeline.engine.run_dagentsc", return_value=self.plan("a", "c", "b")):
            response = self.client.post("/api/v1/pipelines/p/runs", json={"payload": {}})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(["a", "c", "b"], [result["step_id"] for result in response.json()["step_results"]])

    def test_a_plan_that_does_not_list_each_step_once_is_a_503(self) -> None:
        for answer in (self.plan("a"), self.plan("a", "a"), self.plan("a", "zzz"), {"steps": "a"}, {}):
            with self.subTest(answer=answer):
                with patch("app.pipeline.engine.run_dagentsc", return_value=answer):
                    response = self.register("p", [step("a"), step("b", ["a"])])
                self.assertEqual(503, response.status_code, response.text)
                self.assertEqual({"detail": PLANNER_UNAVAILABLE_DETAIL}, response.json())


class PlannerUnavailableTests(PipelineApiTestCase):
    """Without the planner the service cannot check or order a pipeline, so it answers 503."""

    def setUp(self) -> None:
        super().setUp()
        missing = patch.dict(os.environ, {"DAGENTSC_BIN": "/nonexistent/dagentsc"})
        missing.start()
        self.addCleanup(missing.stop)

    def store(self, pipeline_id: str) -> None:
        # Stored directly, as if it was registered while the planner was up.
        self.service._definitions.save(
            PipelineDefinition.model_validate({"pipeline_id": pipeline_id, "steps": [step("a")]})
        )

    def test_registration_is_a_503_and_stores_nothing(self) -> None:
        for path in REGISTER_PATHS:
            with self.subTest(path=path):
                response = self.register("valid", [step("a")], path)
                self.assertEqual(503, response.status_code, response.text)
                self.assertEqual({"detail": PLANNER_UNAVAILABLE_DETAIL}, response.json())
                self.assertEqual(404, self.client.get("/api/v1/pipeline-definitions/valid").status_code)

    def test_the_reason_goes_to_the_log_and_not_to_the_caller(self) -> None:
        response = self.register("valid", [step("a")])
        self.assertNotIn("/nonexistent/dagentsc", response.text)
        logged = " ".join(str(arg) for call in self.logger.warning.call_args_list for arg in call.args)
        self.assertIn("/nonexistent/dagentsc", logged)

    def test_runs_and_validation_are_503(self) -> None:
        self.store("stored")
        requests = {
            "run": ("/api/v1/pipelines/stored/runs", {"payload": {}}),
            "submit": ("/api/v1/pipeline-runs", {"pipeline_id": "stored", "payload": {}}),
            "validate": ("/api/v1/pipeline-definitions/stored:validate", None),
        }
        for name, (path, body) in requests.items():
            with self.subTest(request=name):
                response = self.client.post(path, json=body)
                self.assertEqual(503, response.status_code, response.text)
                self.assertEqual({"detail": PLANNER_UNAVAILABLE_DETAIL}, response.json())
        # The submission failed before it was queued, so no run was recorded.
        self.assertEqual([], self.client.get("/api/v1/pipeline-runs").json()["runs"])

    def test_an_unknown_pipeline_is_still_a_404(self) -> None:
        response = self.client.post("/api/v1/pipelines/nope/runs", json={"payload": {}})
        self.assertEqual(404, response.status_code)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RunErrorTests(PipelineApiTestCase):
    """A step that fails because of its config or payload is a 422 naming the step; 404 means no such pipeline."""

    def test_step_errors_in_a_synchronous_run_are_422(self) -> None:
        cases = {
            "sum without a field": (
                step("s", kind="summarize_items", aggregations=[{"operation": "sum"}]),
                "Step s (summarize_items) failed: Aggregation sum requires a field",
            ),
            "unknown operator": (
                step("f", kind="filter_items", conditions=[{"field": "x", "operator": "like", "value": 1}]),
                "Step f (filter_items) failed: operator: Input should be 'eq', 'ne', 'gt', 'gte', 'lt', "
                "'lte', 'contains' or 'in'",
            ),
            "number compared with a string": (
                step("f", kind="filter_items", conditions=[{"field": "x", "operator": "gt", "value": "a"}]),
                "Step f (filter_items) failed: '>' not supported between instances of 'int' and 'str'",
            ),
        }
        for name, (bad_step, message) in cases.items():
            with self.subTest(case=name):
                self.assert_registered(name, [bad_step])
                response = self.client.post(f"/api/v1/pipelines/{name}/runs", json={"payload": {"items": [{"x": 1}]}})
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual({"detail": message}, response.json())

    def test_an_unknown_pipeline_is_a_404(self) -> None:
        for path, body in (
            ("/api/v1/pipelines/nope/runs", {"payload": {}}),
            ("/api/v1/pipeline-runs", {"pipeline_id": "nope", "payload": {}}),
        ):
            with self.subTest(path=path):
                response = self.client.post(path, json=body)
                self.assertEqual(404, response.status_code)
                self.assertEqual({"detail": "Unknown pipeline: nope"}, response.json())

    def test_a_submission_without_a_pipeline_id_is_422(self) -> None:
        for body in ({"payload": {}}, {"pipeline_id": "", "payload": {}}):
            with self.subTest(body=body):
                response = self.client.post("/api/v1/pipeline-runs", json=body)
                self.assertEqual(422, response.status_code, response.text)
                self.assertEqual(["body", "pipeline_id"], response.json()["detail"][0]["loc"])

    def test_a_queued_run_records_the_failing_step(self) -> None:
        self.assert_registered("bad-sum", [step("s", kind="summarize_items", aggregations=[{"operation": "sum"}])])
        queued = self.client.post("/api/v1/pipeline-runs", json={"pipeline_id": "bad-sum", "payload": {}})
        self.assertEqual(202, queued.status_code, queued.text)
        run = queued.json()
        for _ in range(100):
            run = self.client.get(f"/api/v1/pipeline-runs/{run['run_id']}").json()
            if run["status"] == "failed":
                break
            time.sleep(0.01)
        self.assertEqual("failed", run["status"])
        self.assertEqual("Step s (summarize_items) failed: Aggregation sum requires a field", run["error"])


class OpenApiTests(unittest.TestCase):
    """The schema lists the errors each route returns, and every reference in it resolves."""

    schema = TestClient(app).get("/openapi.json").json()

    def test_routes_declare_their_errors(self) -> None:
        expected = {
            ("/api/v1/pipelines", "post"): {"422", "503"},
            ("/api/v1/pipeline-definitions", "post"): {"422", "503"},
            ("/api/v1/pipeline-definitions/{pipeline_id}:validate", "post"): {"404", "422", "503"},
            ("/api/v1/pipelines/{pipeline_id}/runs", "post"): {"404", "422", "503"},
            ("/api/v1/pipeline-runs", "post"): {"404", "422", "503"},
        }
        for (path, method), statuses in expected.items():
            with self.subTest(path=path, method=method):
                declared = set(self.schema["paths"][path][method]["responses"])
                self.assertLessEqual(statuses, declared)

    def test_every_reference_resolves(self) -> None:
        components = self.schema["components"]["schemas"]

        def refs(node: object):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "$ref":
                        yield value
                    else:
                        yield from refs(value)
            elif isinstance(node, list):
                for value in node:
                    yield from refs(value)

        for ref in refs(self.schema):
            with self.subTest(ref=ref):
                self.assertIn(ref.removeprefix("#/components/schemas/"), components)


if __name__ == "__main__":
    unittest.main()
