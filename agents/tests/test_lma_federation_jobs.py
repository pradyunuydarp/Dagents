"""The LMA's federated job routes: offer, execute, the update channel and the job log.

A coordinator offers a job; the site checks it before it reads any data, then
runs it on the Guard's payload when it is executed. A training job's clipped
update goes to the GMA's update store over HTTP, and the result carries only
its norm and digest.

The site, the fake GMA and the network come from ``test_lma_site_api.py``.
Every check that involves the Guard or the round digest uses the real
``dagentsc``; the classes skip with ``dagentsc binary is not available``
without it.
"""

from __future__ import annotations

import math
import os
import socket
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

from agents.common.domain.federation import AggregationMethod, FederatedJob, JobModel, RoundManifest
from agents.common.extensions import ExtensionRegistry
from agents.common.infrastructure.agent_client import GMA_AGENT, AgentNetwork
from agents.common.infrastructure.dagents_runner import round_digest
from agents.common.infrastructure.site_sources import StaticSiteSource
from agents.lma.application.federation_jobs import SourceDataSource
from agents.tests.fixtures.toy_extension import (
    TOY_CONDITION_ID,
    TOY_CONTRACT,
    TOY_LABEL_FIELD,
    ToyExtension,
    ToyLinearKind,
    ToyThresholdKind,
    toy_records,
)
from agents.tests.test_governance import dagentsc_available
from agents.tests.test_lma_site_api import (
    COORDINATOR_HEADERS,
    INITIAL,
    SITE,
    TRAINED,
    LmaHarness,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TASKS = {"analytics": "profile", "evaluation": "evaluate", "training": "train"}
RULE = ToyThresholdKind()


class SpyLinearKind(ToyLinearKind):
    """The toy linear model, keeping a copy of the records each evaluate and train call received."""

    kind = "toy_spy"
    version_prefix = "toy-spy"

    def __init__(self) -> None:
        self.seen: list[list[dict[str, Any]]] = []
        self.fail_with: Exception | None = None

    def evaluate(self, params, records, label_field):  # noqa: ANN001, ANN201 - same signature as the parent
        self.seen.append([dict(record) for record in records])
        if self.fail_with is not None:
            raise self.fail_with
        return super().evaluate(params, records, label_field)

    def train(self, params, records, label_field):  # noqa: ANN001, ANN201 - same signature as the parent
        self.seen.append([dict(record) for record in records])
        return super().train(params, records, label_field)


class SpyExtension:
    extension_id = "dagents-spy-extension"
    version = "1.0.0"

    def __init__(self, kind: SpyLinearKind) -> None:
        self._kind = kind

    def model_kinds(self) -> list[Any]:
        return [self._kind]


class CountingSource(StaticSiteSource):
    """A static source that counts how often its records are read."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        super().__init__(records, location={"name": "counting"})
        self.reads = 0

    def records(self) -> list[dict[str, Any]]:
        self.reads += 1
        return super().records()


def site_records(count: int = 30, labelled_share: float = 1.0) -> list[dict[str, Any]]:
    """The toy records with an id field that is not in the feature contract."""
    return [{**record, "record_id": f"r-{index}"} for index, record in enumerate(toy_records(SITE, count, labelled_share))]


def manifest(round_id: str, phase: str = "evaluation", *, kind: Any = None, params: dict | None = None, **overrides: Any) -> RoundManifest:
    kind = kind or SpyLinearKind()
    params = dict(INITIAL if params is None else params)
    values: dict[str, Any] = {
        "round_id": round_id,
        "study_id": "toy-study",
        "condition_id": TOY_CONDITION_ID,
        "phase": phase,
        "model_version": "toy-spy-init",
        "model_artifact_digest": kind.parameter_digest(params),
        "training_code_digest": kind.code_digest(),
        "feature_contract_version": TOY_CONTRACT.version,
        "privacy_profile": "toy-profile",
        "aggregation": AggregationMethod(kind="fedavg"),
        "minimum_participants": 1,
        "minimum_cohort_per_site": 5,
        "invited_sites": [SITE],
    }
    values.update(overrides)
    return RoundManifest(**values)


def planner_digest(value: RoundManifest) -> str:
    return str(round_digest(value.model_dump(mode="json"))["round_digest"])


def job_model(value: RoundManifest, *, model_kind: Any = None, params: dict | None = None, **overrides: Any) -> dict[str, Any]:
    kind = model_kind or SpyLinearKind()
    params = dict(INITIAL if params is None else params)
    fields: dict[str, Any] = {
        "version": value.model_version,
        "kind": kind.kind,
        "params": params,
        "digest": kind.parameter_digest(params),
    }
    fields.update(overrides)
    return JobModel(**fields).model_dump(mode="json")


def job(value: RoundManifest, **overrides: Any) -> FederatedJob:
    fields: dict[str, Any] = {
        "round_id": value.round_id,
        "site_id": SITE,
        "manifest": value,
        "manifest_digest": planner_digest(value),
        "task": TASKS[value.phase],
        "parameters": {"model": job_model(value)},
    }
    fields.update(overrides)
    return FederatedJob(**fields)


class JobHarness(LmaHarness):
    """An LMA whose registry also holds the spy model kind."""

    def __init__(self, records: list[dict[str, Any]] | None = None, **kwargs: Any) -> None:
        self.spy = SpyLinearKind()
        registry = ExtensionRegistry()
        registry.register(ToyExtension())
        registry.register(SpyExtension(self.spy))
        kwargs.setdefault("source", CountingSource(site_records() if records is None else records))
        super().__init__(registry=registry, **kwargs)

    def offer(self, value: FederatedJob) -> Any:
        return self.client.post(
            "/api/v1/federation/jobs:offer", headers=COORDINATOR_HEADERS, json=value.model_dump(mode="json")
        )

    def execute(self, value: FederatedJob) -> Any:
        return self.client.post(
            "/api/v1/federation/jobs:execute", headers=COORDINATOR_HEADERS, json=value.model_dump(mode="json")
        )

    def audit(self) -> list[dict[str, Any]]:
        return self.get("/api/v1/governance/audit", params={"limit": 100}).json()["records"]

    def jobs(self) -> list[dict[str, Any]]:
        return self.get("/api/v1/federation/jobs", params={"limit": 100}).json()


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class OfferTests(unittest.TestCase):
    def harness(self, **kwargs: Any) -> JobHarness:
        harness = JobHarness(**kwargs)
        self.addCleanup(harness.close)
        return harness

    def test_a_verified_job_is_accepted_for_each_task(self) -> None:
        lma = self.harness()
        for phase in TASKS:
            with self.subTest(phase=phase):
                response = lma.offer(job(manifest(f"toy-study-{phase}-1", phase)))
                self.assertEqual(200, response.status_code, response.text)
                acceptance = response.json()
                self.assertTrue(acceptance["accepted"], acceptance["reason"])
                self.assertTrue(acceptance["verified_digest"])
                self.assertEqual("toy-v1", acceptance["feature_contract_version"])
        self.assertEqual(["before_train"] * 3, [record["boundary"] for record in lma.audit()])
        # An accepted offer is not a contribution, so the job log stays empty.
        self.assertEqual([], lma.jobs())

    def test_a_job_is_refused_before_any_data_is_read(self) -> None:
        lma = self.harness()
        base = manifest("base")
        wrong_digest = "sha256:" + "f" * 64
        cases: list[tuple[str, FederatedJob, str]] = []

        def case(label: str, round_id: str, build: Any, expected: str) -> None:
            cases.append((label, build(round_id), expected))

        case("wrong site", "r-site", lambda r: job(manifest(r), site_id="site-b"), "site-b")
        case(
            "round id differs from the manifest's",
            "r-round",
            lambda r: job(manifest(r)).model_copy(update={"round_id": "another-round"}),
            "round",
        )
        case("wrong condition", "r-condition", lambda r: job(manifest(r, condition_id="other_condition")), "other_condition")
        case("task not the phase's", "r-task", lambda r: job(manifest(r, "evaluation"), task="train"), "task")
        case("no model", "r-no-model", lambda r: job(manifest(r), parameters={}), "model")
        case(
            "malformed model",
            "r-bad-model",
            lambda r: job(manifest(r), parameters={"model": {"version": "toy-spy-init"}}),
            "model",
        )
        case(
            "model version not the manifest's",
            "r-version",
            lambda r: job(manifest(r), parameters={"model": job_model(manifest(r), version="toy-spy-other")}),
            "toy-spy-other",
        )
        case(
            "model digest not the manifest's",
            "r-artifact",
            lambda r: job(manifest(r, params=TRAINED)),
            "digest",
        )
        case(
            "parameters changed after the digest",
            "r-tampered",
            lambda r: job(
                manifest(r),
                parameters={"model": job_model(manifest(r), params=TRAINED, digest=base.model_artifact_digest)},
            ),
            "digest",
        )
        case(
            "unknown model kind",
            "r-kind",
            lambda r: job(manifest(r), parameters={"model": job_model(manifest(r), kind="no_such_kind", digest=base.model_artifact_digest)}),
            "no_such_kind",
        )
        case(
            "code digest not the kind's",
            "r-code",
            lambda r: job(manifest(r, training_code_digest="sha256:" + "0" * 64)),
            "code",
        )
        case(
            "training a kind that cannot be trained",
            "r-rule",
            lambda r: job(
                manifest(r, "training", kind=RULE, params={"threshold": 0.0}, model_version="toy-rule"),
                parameters={
                    "model": job_model(
                        manifest(r, "training", kind=RULE, params={"threshold": 0.0}, model_version="toy-rule"),
                        model_kind=RULE,
                        params={"threshold": 0.0},
                    )
                },
            ),
            "cannot be trained",
        )
        case("tampered manifest digest", "r-digest", lambda r: job(manifest(r), manifest_digest=wrong_digest), "digest")
        case("contract version", "r-contract", lambda r: job(manifest(r, feature_contract_version="toy-v2")), "toy-v2")
        case("unapproved fields", "r-fields", lambda r: job(manifest(r), feature_fields=["secret_field"]), "approved")

        for label, refused, expected in cases:
            with self.subTest(case=label):
                response = lma.offer(refused)
                self.assertEqual(200, response.status_code, response.text)
                acceptance = response.json()
                self.assertFalse(acceptance["accepted"], label)
                self.assertIn(expected, acceptance["reason"])
        self.assertEqual(0, lma.source.reads, "a refused job must not read the site's records")
        self.assertEqual([], lma.audit(), "a refused job must not reach the Guard")
        self.assertEqual([], lma.spy.seen)
        logged = lma.jobs()
        self.assertEqual(len(cases), len(logged))
        self.assertTrue(all(entry["participation"] == "rejected" and entry["reason"] for entry in logged))
        self.assertTrue(all(entry["update_digest"] is None and entry["metrics"] == {} for entry in logged))

    def test_a_cohort_below_the_minimum_is_refused_by_the_before_train_guard(self) -> None:
        lma = self.harness(records=site_records(count=3))
        acceptance = lma.offer(job(manifest("toy-study-evaluation-small"))).json()
        self.assertFalse(acceptance["accepted"])
        self.assertEqual(["before_train"], [record["boundary"] for record in lma.audit()])
        self.assertFalse(lma.audit()[0]["permitted"])

    def test_a_planner_failure_refuses_the_job(self) -> None:
        lma = self.harness()
        value = job(manifest("toy-study-evaluation-planner"))
        with patch.dict(os.environ, {"DAGENTSC_BIN": str(REPO_ROOT / "no-such-dagentsc")}):
            acceptance = lma.offer(value).json()
        self.assertFalse(acceptance["accepted"])
        self.assertIn("planner", acceptance["reason"])
        self.assertNotIn("no-such-dagentsc", acceptance["reason"])
        self.assertEqual([], lma.audit())

    def test_a_second_offer_for_the_same_round_answers_409(self) -> None:
        lma = self.harness()
        value = job(manifest("toy-study-evaluation-twice"))
        self.assertTrue(lma.offer(value).json()["accepted"])
        again = lma.offer(value)
        self.assertEqual(409, again.status_code, again.text)
        self.assertEqual("conflict", again.json()["code"])

    def test_a_malformed_job_answers_422(self) -> None:
        lma = self.harness()
        response = lma.client.post("/api/v1/federation/jobs:offer", headers=COORDINATOR_HEADERS, json={"round_id": "x"})
        self.assertEqual(422, response.status_code)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ExecuteTests(unittest.TestCase):
    def harness(self, **kwargs: Any) -> JobHarness:
        harness = JobHarness(**kwargs)
        self.addCleanup(harness.close)
        return harness

    def accepted(self, lma: JobHarness, value: FederatedJob) -> FederatedJob:
        acceptance = lma.offer(value).json()
        self.assertTrue(acceptance["accepted"], acceptance["reason"])
        return value

    def test_execute_without_an_accepted_offer_answers_409(self) -> None:
        lma = self.harness()
        response = lma.execute(job(manifest("toy-study-evaluation-unoffered")))
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual("conflict", response.json()["code"])
        self.assertEqual([], lma.spy.seen)

    def test_a_refused_offer_cannot_be_executed(self) -> None:
        lma = self.harness()
        refused = job(manifest("toy-study-evaluation-refused"), manifest_digest="sha256:" + "f" * 64)
        self.assertFalse(lma.offer(refused).json()["accepted"])
        self.assertEqual(409, lma.execute(refused).status_code)

    def test_a_job_runs_once(self) -> None:
        lma = self.harness()
        value = self.accepted(lma, job(manifest("toy-study-evaluation-once")))
        first = lma.execute(value)
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual("completed", first.json()["participation"])
        second = lma.execute(value)
        self.assertEqual(409, second.status_code, second.text)

    def test_the_executed_job_must_be_the_accepted_one(self) -> None:
        lma = self.harness()
        value = self.accepted(lma, job(manifest("toy-study-evaluation-changed")))
        changed = value.model_copy(update={"feature_fields": ["x1"]})
        response = lma.execute(changed)
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual([], lma.spy.seen)

    def test_an_evaluation_runs_on_the_guarded_payload(self) -> None:
        lma = self.harness()
        value = self.accepted(lma, job(manifest("toy-study-evaluation-1")))
        result = lma.execute(value).json()
        self.assertEqual("completed", result["participation"])
        [seen] = lma.spy.seen
        contract = set(TOY_CONTRACT.field_names())
        self.assertTrue(all(set(record) <= contract for record in seen), "the runner saw a field outside the contract")
        self.assertTrue(all("record_id" not in record for record in seen))
        self.assertEqual(30, len(seen))
        self.assertEqual(30, result["contributed_examples"])
        self.assertEqual(ToyLinearKind().evaluate(INITIAL, seen, TOY_LABEL_FIELD), result["metrics"])
        self.assertEqual(planner_digest(value.manifest), result["job_digest"])
        self.assertNotIn("refusal_reason", result)
        self.assertEqual(
            ["before_send", "before_read", "before_train"], [record["boundary"] for record in lma.audit()]
        )

    def test_a_training_update_goes_to_the_gma_and_never_into_the_result(self) -> None:
        lma = self.harness()
        value = self.accepted(lma, job(manifest("toy-study-training-1", "training")))
        response = lma.execute(value)
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual("completed", result["participation"], result.get("refusal_reason"))
        [(round_id, site_id, upload)] = lma.gma.uploads
        self.assertEqual(("toy-study-training-1", SITE), (round_id, site_id))
        self.assertEqual(value.manifest_digest, upload.job_digest)
        self.assertEqual(lma.spy.update_digest(upload.update), result["update_digest"])
        self.assertAlmostEqual(math.sqrt(sum(v * v for v in upload.update.values())), result["update_norm"], places=9)
        self.assertNotIn("update", result)
        for name in upload.update:
            self.assertNotIn(name, response.text)
        [entry] = lma.jobs()
        self.assertEqual(result["update_digest"], entry["update_digest"])
        self.assertEqual("training", entry["phase"])

    def test_a_different_digest_from_the_gma_refuses_the_job(self) -> None:
        lma = self.harness()
        lma.gma.receipt_digest = "sha256:" + "0" * 64
        value = self.accepted(lma, job(manifest("toy-study-training-digest", "training")))
        result = lma.execute(value).json()
        self.assertEqual("rejected", result["participation"])
        self.assertIsNone(result["update_digest"])
        self.assertIn("different", result["refusal_reason"])
        [entry] = lma.jobs()
        self.assertEqual(result["refusal_reason"], entry["reason"])

    def test_an_upload_the_gma_refuses_refuses_the_job_without_naming_an_address(self) -> None:
        lma = self.harness()
        lma.gma.refuse_uploads = True
        value = self.accepted(lma, job(manifest("toy-study-training-refused", "training")))
        result = lma.execute(value).json()
        self.assertEqual("rejected", result["participation"])
        self.assertIn("update", result["refusal_reason"])
        for text in ("http://", "https://", ".internal", "Bearer"):
            self.assertNotIn(text, result["refusal_reason"])

    def test_an_unreachable_gma_refuses_the_job_without_naming_its_address(self) -> None:
        # A port in this repository's test range that nothing listens on.
        port = None
        for candidate in range(9899, 9799, -1):
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", candidate))
                except OSError:
                    continue
                port = candidate
                break
        self.assertIsNotNone(port, "no free port in 9800-9899")
        network = AgentNetwork.over_http({GMA_AGENT: f"http://127.0.0.1:{port}"}, timeout_s=5)
        self.addCleanup(network.close)
        lma = self.harness(with_network=False, network=network)
        value = self.accepted(lma, job(manifest("toy-study-training-down", "training")))
        result = lma.execute(value).json()
        self.assertEqual("rejected", result["participation"])
        self.assertNotIn("127.0.0.1", result["refusal_reason"])
        self.assertNotIn(str(port), result["refusal_reason"])

    def test_a_profile_job_reports_counts_and_a_permitted_rate(self) -> None:
        records = site_records()
        positives = sum(1 for record in records if record[TOY_LABEL_FIELD])
        lma = self.harness(records=records)
        value = self.accepted(lma, job(manifest("toy-study-analytics-1", "analytics")))
        result = lma.execute(value).json()
        self.assertEqual("completed", result["participation"], result.get("refusal_reason"))
        self.assertEqual(
            {"record_count": 30.0, "completeness": 1.0, "label_coverage": 1.0, "label_rate": positives / 30},
            result["metrics"],
        )
        self.assertEqual([], lma.spy.seen, "a profile job does not run the model")

    def test_a_profile_job_leaves_out_a_rate_the_planner_does_not_permit(self) -> None:
        lma = self.harness(records=site_records(labelled_share=0.1))
        value = self.accepted(lma, job(manifest("toy-study-analytics-small", "analytics")))
        metrics = lma.execute(value).json()["metrics"]
        self.assertNotIn("label_rate", metrics)
        self.assertAlmostEqual(0.1, metrics["label_coverage"])

    def test_a_profile_job_reads_records_without_the_label(self) -> None:
        lma = self.harness()
        source = SourceDataSource(lma.app.state.lma.site)
        profile = job(manifest("p", "analytics"))
        evaluation = job(manifest("e", "evaluation"))
        self.assertTrue(all(TOY_LABEL_FIELD not in record for record in source.records(profile)))
        self.assertTrue(all(TOY_LABEL_FIELD in record for record in source.records(evaluation)))
        self.assertEqual(30, source.cohort_size(profile))

    def test_a_failing_local_run_is_a_failed_result_in_the_log(self) -> None:
        lma = self.harness()
        lma.spy.fail_with = RuntimeError("the local run broke with a secret value 42")
        value = self.accepted(lma, job(manifest("toy-study-evaluation-broken")))
        response = lma.execute(value)
        self.assertEqual(200, response.status_code, response.text)
        result = response.json()
        self.assertEqual("failed", result["participation"])
        self.assertNotIn("secret value", result["refusal_reason"])
        self.assertEqual("failed", lma.jobs()[0]["participation"])

    def test_the_job_log_lists_results_and_refusals_newest_first(self) -> None:
        lma = self.harness()
        lma.offer(job(manifest("toy-study-evaluation-a"), manifest_digest="sha256:" + "f" * 64))
        value = self.accepted(lma, job(manifest("toy-study-evaluation-b")))
        lma.execute(value)
        entries = lma.jobs()
        self.assertEqual(["toy-study-evaluation-b", "toy-study-evaluation-a"], [entry["round_id"] for entry in entries])
        self.assertEqual(
            {
                "round_id",
                "phase",
                "participation",
                "contributed_examples",
                "update_norm",
                "update_digest",
                "job_digest",
                "metrics",
                "reason",
                "submitted_at",
            },
            set(entries[0]),
        )
        self.assertEqual("completed", entries[0]["participation"])
        self.assertIsNone(entries[0]["reason"])
        self.assertEqual("rejected", entries[1]["participation"])
        self.assertIn("digest", entries[1]["reason"])
        self.assertEqual(1, len(lma.get("/federation/jobs", params={"limit": 1}).json()))

    def test_routes_answer_the_same_in_both_forms(self) -> None:
        lma = self.harness()
        short = job(manifest("toy-study-evaluation-short"))
        acceptance = lma.client.post(
            "/federation/jobs:offer", headers=COORDINATOR_HEADERS, json=short.model_dump(mode="json")
        ).json()
        self.assertTrue(acceptance["accepted"])
        result = lma.client.post(
            "/federation/jobs:execute", headers=COORDINATOR_HEADERS, json=short.model_dump(mode="json")
        ).json()
        self.assertEqual("completed", result["participation"])


if __name__ == "__main__":
    unittest.main()
