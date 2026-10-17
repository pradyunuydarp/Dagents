"""The GMA's HTTP engine, update store routes and site directory, against fixture LMAs.

One GMA (``GMA_ENGINE=http``, tokens on) and three fixture LMAs run on one
in-process agent network (``httpx.ASGITransport``), so every call goes through
the real routes, request validation and tokens. The fixture LMAs train and
evaluate through the toy extension's model kinds, and every Guard decision,
plan, readiness check and lifecycle event comes from the real ``dagentsc``.

What is checked:

- refresh enrols each configured LMA from its own registration;
- dispatch offers and runs the jobs at up to ``GMA_DISPATCH_CONCURRENCY``
  sites at once, and stores sorted results, acceptances and dispatch rows;
- readiness passes the selected sites and does not write;
- aggregate combines the stored updates with the planner's weights through the
  model kind, registers the candidate, supersedes the parent and deletes the
  updates;
- an LMA that is missing, crashes or refuses gives a ``failed`` or ``rejected``
  result with its reason, and no host;
- the update store refuses each kind of bad upload, and checks tokens;
- 409 for a reused round id and late results, 403 for an unselected site;
- discard deletes the updates; concurrent result posts keep every result;
- no GMA response holds a parameter vector, and no body an LMA sends the GMA
  holds the label field.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from fastapi.testclient import TestClient

from dagents.common.domain.errors import AggregationRefused
from dagents.common.domain.federation import (
    ModelVersionRegistration,
    SiteRegistration,
    SiteResult,
    UpdateUpload,
)
from dagents.common.infrastructure.agent_client import GMA_AGENT, AgentError, AgentNetwork, lma_agent
from dagents.gma.app_factory import GmaDeps, create_app
from dagents.gma.config import Settings
from dagents.tests.fastapi_routes import flat_routes
from dagents.tests.fixtures.gma_fixtures import (
    ADMIN_TOKEN,
    CAPABILITIES,
    COORDINATOR_TOKENS,
    INITIAL_PARAMS,
    INITIAL_VERSION,
    SITE_TOKENS,
    SITES,
    THRESHOLD_VERSION,
    build_fixture_lma,
    build_study,
    json_keys,
    register_models,
    toy_manifest,
    toy_registry,
)
from dagents.tests.fixtures.toy_extension import (
    TOY_CONDITION_ID,
    TOY_CONTRACT,
    TOY_LABEL_FIELD,
    ToyExtension,
    ToyLinearKind,
    ToyThresholdKind,
)
from dagents.tests.test_governance import dagentsc_available

PARAMETER_NAMES = set(ToyLinearKind.NAMES)


class SpyLinearKind(ToyLinearKind):
    """The toy linear kind, recording what the GMA asks it to combine."""

    def __init__(self) -> None:
        self.combined: list[tuple[dict[str, float], list[tuple[float, dict[str, float]]]]] = []

    def combine(self, params, weighted_updates):  # noqa: ANN001, ANN201 - same signature as the parent
        pairs = [(float(weight), dict(update)) for weight, update in weighted_updates]
        self.combined.append((dict(params), pairs))
        return super().combine(params, pairs)


class SpyExtension(ToyExtension):
    """The toy extension with a spying linear kind."""

    def __init__(self) -> None:
        self.linear = SpyLinearKind()

    def model_kinds(self):  # noqa: ANN201 - same signature as the parent
        return [self.linear, ToyThresholdKind()]


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class HttpEngineTests(unittest.TestCase):
    """One GMA with the HTTP engine and three fixture LMAs."""

    def setUp(self) -> None:
        self.extension = SpyExtension()
        self.study = build_study(registry=toy_registry(self.extension))
        self.addCleanup(self.study.close)
        self.gma = self.study.gma()
        self.digests = register_models(self.gma)
        self.gma.refresh_sites()

    def training_manifest(self, round_id: str = "round-train-1", **overrides):
        return toy_manifest(round_id, "training", INITIAL_VERSION, self.digests[INITIAL_VERSION], **overrides)

    def evaluation_manifest(self, round_id: str = "round-eval-1", **overrides):
        return toy_manifest(round_id, "evaluation", THRESHOLD_VERSION, self.digests[THRESHOLD_VERSION], **overrides)

    # --- Health and refresh ----------------------------------------------------

    def test_health_names_the_http_engine(self) -> None:
        health = self.gma.health()
        self.assertEqual(health["engine"], "http")
        self.assertEqual(health["agent_type"], "GMA")

    def test_refresh_enrols_each_configured_lma_from_its_registration(self) -> None:
        refreshed = self.gma.refresh_sites()
        self.assertEqual([item.site_id for item in refreshed], list(SITES))
        sites = self.gma.list_sites()
        self.assertEqual([site.site_id for site in sites], list(SITES))
        for site in sites:
            self.assertTrue(site.enrolled)
            self.assertEqual(site.cohort_size, 30)
            self.assertEqual(site.capabilities, CAPABILITIES)
            self.assertEqual(site.feature_contract_version, TOY_CONTRACT.version)

    def test_refresh_skips_an_lma_that_does_not_answer(self) -> None:
        self.study.lmas["site-b"].app.router.routes = [
            route for route in self.study.lmas["site-b"].app.router.routes if "registration" not in getattr(route, "path", "")
        ]
        refreshed = self.gma.refresh_sites()
        self.assertEqual([item.site_id for item in refreshed], ["site-a", "site-c"])

    def test_site_summaries_report_each_lma_and_its_audit_state_when_asked(self) -> None:
        entries = self.gma.site_summaries(include_audit=True)
        self.assertEqual([entry.site_id for entry in entries], list(SITES))
        for entry in entries:
            self.assertTrue(entry.reachable)
            self.assertEqual(entry.source_kind, "memory")
            self.assertEqual(entry.summary.record_count, 30)
            self.assertIsNotNone(entry.audit)
            self.assertTrue(entry.audit.chain_intact)
        without = self.gma.site_summaries()
        self.assertTrue(all(entry.audit is None for entry in without))

    def test_a_slow_lma_is_reported_unreachable_after_the_summary_timeout(self) -> None:
        study = build_study(registry=toy_registry(), summary_timeout_seconds=0.5)
        self.addCleanup(study.close)
        study.lmas["site-b"].summary_delay = 3.0
        started = time.monotonic()
        entries = study.gma().site_summaries()
        self.assertLess(time.monotonic() - started, 2.5, "the GMA waited for the slow LMA")
        self.assertEqual([entry.site_id for entry in entries], list(SITES))
        slow = next(entry for entry in entries if entry.site_id == "site-b")
        self.assertFalse(slow.reachable)
        self.assertIsNone(slow.summary)
        self.assertTrue(all(entry.reachable for entry in entries if entry.site_id != "site-b"))

    def test_site_summaries_keep_an_lma_that_refused_the_call_as_reachable(self) -> None:
        study = build_study(registry=toy_registry(), sites=("site-a",), lma_tokens={"site-a": "x" * 20})
        self.addCleanup(study.close)
        # site-a answers 401 to a wrong coordinator token: it answered, with no summary.
        entries = study.gma().site_summaries()
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0].reachable)
        self.assertIsNone(entries[0].summary)

    # --- Dispatch ----------------------------------------------------------------

    def test_training_round_runs_every_site_and_stores_sorted_evidence(self) -> None:
        for lma in self.study.lmas.values():
            lma.execute_delay = 0.3
        record = self.gma.dispatch_round(self.training_manifest())

        self.assertEqual(record.status, "collecting")
        self.assertEqual(record.plan.selected_sites, list(SITES))
        self.assertEqual([result.site_id for result in record.results], list(SITES))
        self.assertEqual([acceptance.site_id for acceptance in record.acceptances], list(SITES))
        self.assertTrue(all(acceptance.accepted for acceptance in record.acceptances))
        for result in record.results:
            self.assertEqual(result.participation, "completed", result.refusal_reason)
            self.assertEqual(result.job_digest, record.plan.round_digest)
            self.assertTrue(result.update_digest)
            self.assertGreater(result.update_norm, 0.0)
        # Each site was offered the job and ran it in one jobs:run call.
        self.assertEqual(len(record.dispatch), 3)
        for site_id in SITES:
            calls = [call for call in record.dispatch if call.site_id == site_id]
            self.assertEqual([call.step for call in calls], ["run"])
            self.assertEqual([call.outcome for call in calls], ["completed"])
            self.assertEqual([call.status_code for call in calls], [200])
            # The site checked the job before it ran it, as with a separate offer.
            self.assertEqual(len(self.study.lmas[site_id].offers), 1)
        # The sites ran at the same time, not one after another.
        self.assertGreaterEqual(self.study.meter.highest, 2)
        # Every site stored its clipped update with the GMA.
        self.assertEqual(sorted(self.study.state.updates.updates(record.manifest.round_id)), list(SITES))

    def test_each_job_carries_the_registered_model_with_its_digest(self) -> None:
        record = self.gma.dispatch_round(self.training_manifest())
        for site_id, lma in self.study.lmas.items():
            self.assertEqual(len(lma.offers), 1)
            job = lma.offers[0]
            model = job.parameters["model"]
            self.assertEqual(model["version"], INITIAL_VERSION)
            self.assertEqual(model["kind"], "toy_linear")
            self.assertEqual(model["params"], INITIAL_PARAMS)
            self.assertEqual(model["digest"], self.digests[INITIAL_VERSION])
            self.assertEqual((job.site_id, job.task), (site_id, "train"))
            self.assertEqual(job.manifest_digest, record.plan.round_digest)

    def test_a_training_round_reads_its_starting_model_from_the_store_once(self) -> None:
        # Each read is a database round trip when the registry is in Postgres.
        # The dispatch loads the model once; the sites' update uploads use the
        # kind kept from that load instead of reading the version again.
        store = self.study.state.models.store
        real_get = store.get
        reads: list[str] = []

        def counting_get(version: str):  # noqa: ANN202 - same signature as the store's
            reads.append(version)
            return real_get(version)

        with mock.patch.object(store, "get", side_effect=counting_get):
            record = self.gma.dispatch_round(self.training_manifest())
        self.assertEqual([result.participation for result in record.results], ["completed"] * 3)
        self.assertLessEqual(reads.count(INITIAL_VERSION), 1, reads)

    def test_a_round_whose_model_does_not_match_its_digest_is_refused(self) -> None:
        manifest = self.training_manifest(model_artifact_digest="sha256:not-the-registered-model")
        with self.assertRaises(AgentError) as caught:
            self.gma.dispatch_round(manifest)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, "conflict")
        record = self.gma.get_round(manifest.round_id)
        self.assertEqual(record.status, "closed")
        self.assertIsNotNone(record.abandoned_reason)
        self.assertTrue(all(not lma.offers for lma in self.study.lmas.values()))

    def test_a_missing_lma_gives_a_failed_result_with_its_reason(self) -> None:
        # site-d is enrolled and invited, but no LMA serves it.
        self.gma.register_site(
            SiteRegistration(
                site_id="site-d",
                capabilities=list(CAPABILITIES),
                feature_contract_version=TOY_CONTRACT.version,
                approved_conditions=[TOY_CONDITION_ID],
                cohort_size=30,
                enrolled=True,
            )
        )
        record = self.gma.dispatch_round(self.training_manifest(invited=(*SITES, "site-d")))
        result = next(result for result in record.results if result.site_id == "site-d")
        self.assertEqual(result.participation, "failed")
        self.assertIn("unavailable", result.refusal_reason)
        acceptance = next(item for item in record.acceptances if item.site_id == "site-d")
        self.assertFalse(acceptance.accepted)
        call = next(call for call in record.dispatch if call.site_id == "site-d")
        self.assertEqual((call.step, call.status_code, call.outcome), ("run", None, "failed"))
        # The other sites still completed.
        self.assertEqual(
            [result.participation for result in record.results if result.site_id != "site-d"], ["completed"] * 3
        )

    def test_an_lma_that_crashes_gives_a_failed_result_without_its_error_text(self) -> None:
        self.study.lmas["site-b"].crash_on_execute = True
        record = self.gma.dispatch_round(self.training_manifest())
        result = next(result for result in record.results if result.site_id == "site-b")
        self.assertEqual(result.participation, "failed")
        self.assertIn("500", result.refusal_reason)
        self.assertNotIn("postgresql", result.refusal_reason)
        self.assertNotIn("example.internal", record.model_dump_json())
        [call] = [call for call in record.dispatch if call.site_id == "site-b"]
        self.assertEqual((call.step, call.status_code, call.outcome), ("run", 500, "failed"))

    def test_an_lma_error_detail_never_reaches_the_round(self) -> None:
        # A site store that cannot connect answers 503 with a detail that names a
        # host, a password and a vendor. The round keeps the status and code only.
        leaks = ("hunter2", "postgresql", "example.internal", "ExampleCloud", "ExampleHost", "://admin")
        # The store can fail at the offer or the execution inside a jobs:run
        # call; the GMA sees one failed call either way.
        prefix = "the job run failed"
        for step in ("offer", "execute", "run"):
            with self.subTest(step=step):
                self.study.lmas["site-b"].store_error_on = step
                round_id = f"round-store-error-{step}"
                with self.assertLogs("dagents.gma.adapters.http_federation_engine", "WARNING") as logs:
                    record = self.gma.dispatch_round(self.training_manifest(round_id))
                result = next(result for result in record.results if result.site_id == "site-b")
                self.assertEqual(result.participation, "failed")
                self.assertEqual(result.refusal_reason, f"{prefix}: the LMA of site site-b answered 503 (store_unavailable)")
                acceptance = next(item for item in record.acceptances if item.site_id == "site-b")
                self.assertFalse(acceptance.accepted)
                self.assertEqual(acceptance.reason, result.refusal_reason)
                stored = self.gma.request("GET", "/api/v1/federation/rounds/{round_id}", path_params={"round_id": round_id})
                for text in (record.model_dump_json(), json.dumps(stored.body)):
                    for leak in leaks:
                        self.assertNotIn(leak, text)
                # The operator's log keeps the detail, with the address removed.
                logged = "\n".join(logs.output)
                self.assertIn("site-b", logged)
                self.assertIn("[address]", logged)
                self.assertNotIn("hunter2", logged)
                self.gma.discard_round(round_id, "the test is done with it")
        self.study.lmas["site-b"].store_error_on = None

    def test_a_refused_offer_gives_a_rejected_result_with_the_site_reason(self) -> None:
        self.study.lmas["site-c"].refuse_with = "the site is closed for maintenance"
        record = self.gma.dispatch_round(self.training_manifest())
        result = next(result for result in record.results if result.site_id == "site-c")
        self.assertEqual(result.participation, "rejected")
        self.assertEqual(result.refusal_reason, "the site is closed for maintenance")
        calls = [call for call in record.dispatch if call.site_id == "site-c"]
        self.assertEqual([(call.step, call.outcome) for call in calls], [("run", "refused")])

    def test_a_reused_round_id_is_refused_with_409(self) -> None:
        manifest = self.evaluation_manifest()
        self.gma.dispatch_round(manifest)
        with self.assertRaises(AgentError) as caught:
            self.gma.dispatch_round(manifest)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))

    # --- Readiness and aggregation --------------------------------------------------

    def test_readiness_checks_the_selected_sites_and_writes_nothing(self) -> None:
        record = self.gma.dispatch_round(self.training_manifest())
        before = self.gma.get_round(record.manifest.round_id)
        readiness = self.gma.readiness(record.manifest.round_id)
        self.assertTrue(readiness.aggregation_permitted, readiness)
        self.assertEqual(readiness.accepted_sites, list(SITES))
        # The read changed nothing on the stored round.
        self.assertEqual(before, self.gma.get_round(record.manifest.round_id))

    def test_a_dispatched_round_carries_its_readiness(self) -> None:
        # The HTTP engine collects every result while it dispatches, so the GMA
        # asks the planner for the readiness then: the caller needs no second call.
        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        self.assertIsNotNone(record.readiness)
        self.assertTrue(record.readiness.aggregation_permitted, record.readiness)
        self.assertEqual(record.readiness, self.gma.readiness(round_id))
        self.assertEqual(record.readiness, self.gma.get_round(round_id).readiness)
        # A round without quorum is closed at dispatch and has no readiness.
        closed = self.gma.dispatch_round(self.training_manifest("round-no-quorum", minimum_participants=9))
        self.assertEqual(closed.status, "closed")
        self.assertIsNone(closed.readiness)

    def test_a_readiness_failure_at_dispatch_discards_the_round(self) -> None:
        rounds = self.study.state.rounds

        def failing(round_id, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
            raise RuntimeError("the planner stopped answering")

        with mock.patch.object(rounds, "evaluate_readiness", side_effect=failing):
            with self.assertRaises(AgentError) as caught, self.assertLogs(level="ERROR"):
                self.gma.dispatch_round(self.training_manifest("round-readiness-fails"))
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(self.study.state.updates.pending_rounds(), [])
        closed = self.gma.get_round("round-readiness-fails")
        self.assertEqual(closed.status, "closed")
        self.assertEqual(closed.abandoned_reason, "the planner could not check the round's readiness after its dispatch")

    def test_aggregate_combines_the_stored_updates_and_registers_a_candidate(self) -> None:
        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        readiness = self.gma.readiness(round_id)
        candidate = self.gma.aggregate(round_id)

        self.assertEqual(candidate.candidate_version, f"toy-linear+{round_id}")
        self.assertEqual(candidate.parent_version, INITIAL_VERSION)
        self.assertEqual(candidate.contributing_sites, list(SITES))
        self.assertEqual(candidate.site_weights, readiness.site_weights)
        self.assertEqual(candidate.artifact_uri, f"registry://models/{candidate.candidate_version}")

        # The kind combined the base parameters with each site's update, at the planner's weight.
        self.assertEqual(len(self.extension.linear.combined), 1)
        base, pairs = self.extension.linear.combined[0]
        self.assertEqual(base, INITIAL_PARAMS)
        reported = {result.site_id: result.update_digest for result in record.results}
        self.assertEqual(sorted(weight for weight, _ in pairs), sorted(readiness.site_weights.values()))
        kind = ToyLinearKind()
        self.assertEqual(sorted(kind.update_digest(update) for _, update in pairs), sorted(reported.values()))
        expected = kind.combine(INITIAL_PARAMS, pairs)
        self.assertEqual(candidate.artifact_digest, kind.parameter_digest(expected))

        # Registered as a candidate; the parent is superseded; the updates are gone.
        stored = self.gma.get_model_version(candidate.candidate_version)
        self.assertEqual((stored.status, stored.parent_version), ("candidate", INITIAL_VERSION))
        self.assertEqual(stored.digest, candidate.artifact_digest)
        self.assertIsNone(stored.params)
        self.assertEqual(self.gma.get_model_version(INITIAL_VERSION).status, "superseded")
        self.assertEqual(self.study.state.updates.pending_rounds(), [])
        saved = self.gma.get_round(round_id)
        self.assertEqual(saved.status, "aggregated")
        self.assertEqual(saved.candidate.candidate_version, candidate.candidate_version)

    def test_the_next_round_trains_from_the_candidate(self) -> None:
        first = self.gma.dispatch_round(self.training_manifest("round-train-1"))
        candidate = self.gma.aggregate(first.manifest.round_id)
        second = self.gma.dispatch_round(
            toy_manifest(
                "round-train-2", "training", candidate.candidate_version, candidate.artifact_digest
            )
        )
        self.assertEqual([result.participation for result in second.results], ["completed"] * 3)
        later = self.gma.aggregate(second.manifest.round_id)
        self.assertEqual(later.parent_version, candidate.candidate_version)
        self.assertEqual(self.gma.get_model_version(candidate.candidate_version).status, "superseded")

    def test_an_evaluation_round_aggregates_metrics_and_registers_nothing(self) -> None:
        record = self.gma.dispatch_round(self.evaluation_manifest())
        self.assertEqual([result.participation for result in record.results], ["completed"] * 3)
        candidate = self.gma.aggregate(record.manifest.round_id)
        self.assertIn("accuracy", candidate.metrics)
        self.assertEqual(candidate.artifact_digest, "")
        versions = {item.version for item in self.gma.list_model_versions(limit=20).versions}
        self.assertEqual(versions, {THRESHOLD_VERSION, INITIAL_VERSION})

    def test_aggregation_is_refused_when_an_update_is_missing(self) -> None:
        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        # The update store loses site-b's update, as a restart would.
        self.study.state.updates._updates.pop((round_id, "site-b"))  # noqa: SLF001 - simulating a lost update
        with self.assertRaises(AgentError) as caught:
            self.gma.aggregate(round_id)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "aggregation_refused"))
        self.assertIn("site-b", caught.exception.detail)
        self.assertEqual(self.study.state.updates.pending_rounds(), [])
        self.assertIsNone(self.gma.get_round(round_id).candidate)

    def test_aggregation_is_refused_when_a_stored_update_changed_after_it_was_received(self) -> None:
        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        stored = self.study.state.updates._updates[(round_id, "site-a")]  # noqa: SLF001 - simulating a changed update
        stored.update["bias"] += 1.0
        with self.assertRaises(AgentError) as caught:
            self.gma.aggregate(round_id)
        self.assertEqual(caught.exception.code, "aggregation_refused")
        self.assertIn("changed", caught.exception.detail)
        self.assertEqual(self.study.state.updates.pending_rounds(), [])

    def test_aggregation_is_refused_when_a_stored_update_differs_from_the_reported_digest(self) -> None:
        self.study.lmas["site-a"].report_update_digest = "sha256:changed"
        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        self.assertEqual(record.results[0].update_digest, "sha256:changed")
        with self.assertRaises(AgentError) as caught:
            self.gma.aggregate(round_id)
        self.assertEqual(caught.exception.code, "aggregation_refused")
        self.assertIn("digest", caught.exception.detail)

    def test_aggregation_is_refused_when_training_starts_from_a_kind_that_cannot_be_trained(self) -> None:
        # The LMA refuses such a job, so the engine is called directly: its own
        # check must hold even when a site would accept the job.
        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        readiness = self.gma.readiness(round_id)
        from_rule = record.manifest.model_copy(
            update={"model_version": THRESHOLD_VERSION, "model_artifact_digest": self.digests[THRESHOLD_VERSION]}
        )
        with self.assertRaises(AggregationRefused) as caught:
            self.study.state.engine.aggregate(from_rule, readiness, record.results)
        self.assertIn("cannot be trained", str(caught.exception))
        self.assertEqual(self.study.state.updates.pending_rounds(), [])
        versions = {item.version for item in self.gma.list_model_versions(limit=20).versions}
        self.assertEqual(versions, {THRESHOLD_VERSION, INITIAL_VERSION})

    def test_aggregation_is_refused_when_the_planner_gave_an_accepted_site_no_weight(self) -> None:
        import dagents.common.application.federation as federation

        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        real = federation.evaluate_aggregation_readiness

        def without_site_b_weight(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202 - same signature as the runner
            payload = real(*args, **kwargs)
            weights = payload.get("site_weights", payload.get("siteWeights"))
            weights.pop("site-b")
            return payload

        with mock.patch.object(federation, "evaluate_aggregation_readiness", without_site_b_weight):
            with self.assertRaises(AgentError) as caught:
                self.gma.aggregate(round_id)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "aggregation_refused"))
        self.assertIn("no weight for site-b", caught.exception.detail)
        self.assertEqual(self.study.state.updates.pending_rounds(), [])
        self.assertIsNone(self.gma.get_round(round_id).candidate)
        versions = {item.version for item in self.gma.list_model_versions(limit=20).versions}
        self.assertEqual(versions, {THRESHOLD_VERSION, INITIAL_VERSION})

    # --- Results ---------------------------------------------------------------------

    def test_late_results_and_unselected_sites_are_refused(self) -> None:
        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        stranger = SiteResult(round_id=round_id, site_id="site-x", participation="completed")
        with self.assertRaises(AgentError) as caught:
            self.gma.submit_result(round_id, stranger)
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "forbidden"))
        self.gma.aggregate(round_id)
        with self.assertRaises(AgentError) as caught:
            self.gma.submit_result(round_id, record.results[0])
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))

    def test_results_cannot_be_posted_while_the_engine_collects_them(self) -> None:
        """A posted result would change the planner's weights and the release Guard's cohort."""
        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        weights = self.gma.readiness(round_id).site_weights
        for result in record.results:
            inflated = result.model_copy(update={"contributed_examples": 100000})
            with self.subTest(site=result.site_id), self.assertRaises(AgentError) as caught:
                self.gma.submit_result(round_id, inflated)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))
        self.assertEqual(self.gma.get_round(round_id).results, record.results)
        self.assertEqual(self.gma.readiness(round_id).site_weights, weights)
        candidate = self.gma.aggregate(round_id)
        self.assertEqual(candidate.site_weights, weights)
        self.assertEqual(candidate.contributed_examples, sum(result.contributed_examples for result in record.results))

    def test_a_round_is_aggregated_once(self) -> None:
        record = self.gma.dispatch_round(self.evaluation_manifest())
        round_id = record.manifest.round_id
        first = self.gma.aggregate(round_id)
        with self.assertRaises(AgentError) as caught:
            self.gma.aggregate(round_id)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))
        self.assertEqual(self.gma.get_round(round_id).candidate, first)

    # --- Update store ---------------------------------------------------------------

    def upload(self, round_id: str, site_id: str, token: str | None, *, job_digest: str, update=None):
        return self.study.gma(token).upload_update(
            round_id,
            site_id,
            UpdateUpload(job_digest=job_digest, update=update or {"bias": 0.1, "w.x1": 0.2, "w.x2": 0.3}),
        )

    def assert_refused(self, call, status: int, code: str) -> AgentError:
        with self.assertRaises(AgentError) as caught:
            call()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code), caught.exception.detail)
        return caught.exception

    def test_update_uploads_are_refused_for_each_reason(self) -> None:
        for lma in self.study.lmas.values():
            lma.refuse_with = "uploads are tested by hand"
        training = self.gma.dispatch_round(self.training_manifest())
        evaluation = self.gma.dispatch_round(self.evaluation_manifest())
        digest = training.plan.round_digest
        round_id = training.manifest.round_id

        self.assert_refused(lambda: self.upload("round-unknown", "site-a", SITE_TOKENS["site-a"], job_digest=digest), 404, "not_found")
        self.assert_refused(lambda: self.upload(round_id, "site-x", SITE_TOKENS["site-x"], job_digest=digest), 403, "forbidden")
        self.assert_refused(lambda: self.upload(round_id, "site-a", SITE_TOKENS["site-a"], job_digest="fnv1a:wrong"), 409, "conflict")
        self.assert_refused(
            lambda: self.upload(
                evaluation.manifest.round_id, "site-a", SITE_TOKENS["site-a"], job_digest=evaluation.plan.round_digest
            ),
            409,
            "conflict",
        )
        self.assert_refused(
            lambda: self.upload(round_id, "site-a", SITE_TOKENS["site-a"], job_digest=digest, update={"bias": 1.0}),
            409,
            "conflict",
        )
        # A token for another site, the admin token and no token.
        self.assert_refused(lambda: self.upload(round_id, "site-b", SITE_TOKENS["site-a"], job_digest=digest), 403, "forbidden")
        self.assert_refused(lambda: self.upload(round_id, "site-a", ADMIN_TOKEN, job_digest=digest), 403, "forbidden")
        self.assert_refused(lambda: self.upload(round_id, "site-a", None, job_digest=digest), 401, "unauthorized")

        receipt = self.upload(round_id, "site-a", SITE_TOKENS["site-a"], job_digest=digest)
        self.assertEqual(receipt.digest, ToyLinearKind().update_digest({"bias": 0.1, "w.x1": 0.2, "w.x2": 0.3}))
        error = self.assert_refused(lambda: self.upload(round_id, "site-a", SITE_TOKENS["site-a"], job_digest=digest), 409, "conflict")
        self.assertIn("already", error.detail)

    def test_an_unselected_site_is_refused_before_the_rounds_model_is_looked_up(self) -> None:
        manifest = toy_manifest(
            "round-unregistered", "training", "toy-linear-not-registered", self.digests[INITIAL_VERSION]
        )
        self.assert_refused(lambda: self.gma.dispatch_round(manifest), 409, "conflict")
        record = self.gma.get_round(manifest.round_id)
        digest = record.plan.round_digest
        self.assert_refused(lambda: self.upload(manifest.round_id, "site-x", SITE_TOKENS["site-x"], job_digest=digest), 403, "forbidden")
        error = self.assert_refused(
            lambda: self.upload(manifest.round_id, "site-a", SITE_TOKENS["site-a"], job_digest=digest), 409, "conflict"
        )
        self.assertIn("not registered", error.detail)

    def test_both_upload_paths_bind_the_token_to_its_site(self) -> None:
        for lma in self.study.lmas.values():
            lma.refuse_with = "uploads are tested by hand"
        record = self.gma.dispatch_round(self.training_manifest())
        client = TestClient(self.study.gma_app)
        body = {"job_digest": record.plan.round_digest, "update": {"bias": 0.1, "w.x1": 0.2, "w.x2": 0.3}}
        for prefix in ("", "/api/v1"):
            path = f"{prefix}/federation/rounds/{record.manifest.round_id}/updates/site-b"
            with self.subTest(path=path):
                for token, status in ((SITE_TOKENS["site-a"], 403), (ADMIN_TOKEN, 403), (None, 401)):
                    headers = {"Authorization": f"Bearer {token}"} if token else {}
                    self.assertEqual(client.put(path, json=body, headers=headers).status_code, status)
        accepted = client.put(
            f"/federation/rounds/{record.manifest.round_id}/updates/site-b",
            json=body,
            headers={"Authorization": f"Bearer {SITE_TOKENS['site-b']}"},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)

    def test_discard_deletes_the_updates_and_closes_the_round(self) -> None:
        record = self.gma.dispatch_round(self.training_manifest())
        round_id = record.manifest.round_id
        self.assertEqual(self.study.state.updates.pending_rounds(), [round_id])
        self.assertIsNone(self.gma.discard_round(round_id, "the study stopped"))
        self.assertEqual(self.study.state.updates.pending_rounds(), [])
        stored = self.gma.get_round(round_id)
        self.assertEqual((stored.status, stored.abandoned_reason), ("closed", "the study stopped"))
        self.assert_refused(lambda: self.gma.aggregate(round_id), 409, "conflict")
        self.assert_refused(lambda: self.gma.discard_round("round-unknown", "why"), 404, "not_found")
        self.assert_refused(lambda: self.gma.discard_round(round_id, ""), 422, "invalid_request")
        self.assert_refused(lambda: self.gma.discard_round(round_id, "   "), 422, "invalid_request")

    # --- Scans -----------------------------------------------------------------------

    def test_no_gma_answer_holds_a_parameter_vector(self) -> None:
        manifest = self.training_manifest()
        round_id = manifest.round_id
        answers = [
            self.gma.request("POST", "/api/v1/federation/rounds", json=manifest.model_dump(mode="json")).body,
            self.gma.request("GET", "/api/v1/federation/rounds/{round_id}/readiness", path_params={"round_id": round_id}).body,
            self.gma.request("POST", "/api/v1/federation/rounds/{round_id}:aggregate", path_params={"round_id": round_id}).body,
            self.gma.request("GET", "/api/v1/federation/rounds/{round_id}", path_params={"round_id": round_id}).body,
            self.gma.request("GET", "/api/v1/federation/rounds").body,
            self.gma.request("GET", "/api/v1/model-versions").body,
            self.gma.request(
                "GET", "/api/v1/model-versions/{version}", path_params={"version": f"toy-linear+{round_id}"}
            ).body,
        ]
        for answer in answers:
            keys = json_keys(answer)
            self.assertFalse(keys & PARAMETER_NAMES, f"a GMA answer holds parameters: {sorted(keys & PARAMETER_NAMES)}")
            self.assertNotIn("update", keys)

    def test_no_body_an_lma_sends_the_gma_holds_the_label_field(self) -> None:
        record = self.gma.dispatch_round(self.training_manifest())
        self.gma.aggregate(record.manifest.round_id)
        self.gma.dispatch_round(self.evaluation_manifest())
        self.gma.site_summaries(include_audit=True)
        uploads = [body for method, path, body in self.study.recorder.bodies if "/updates/" in path]
        self.assertEqual(len(uploads), 3)
        for lma in self.study.lmas.values():
            # The runner did see labels: they stay at the site.
            self.assertTrue(any(TOY_LABEL_FIELD in record for record in lma.runner_payloads[0]))
            for body in [*lma.answers, *lma.uploads]:
                self.assertNotIn(TOY_LABEL_FIELD, json_keys(body))
        for body in uploads:
            self.assertNotIn(TOY_LABEL_FIELD, json_keys(body))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SimulatorResultPostTests(unittest.TestCase):
    """On a GMA with the simulator, results are posted, and posts at the same time keep every result."""

    def test_concurrent_result_posts_keep_every_result(self) -> None:
        network = AgentNetwork.in_process({GMA_AGENT: create_app(Settings())}, thread_tokens=16)
        self.addCleanup(network.close)
        gma = network.gma()
        for site_id in SITES:
            gma.register_site(
                SiteRegistration(
                    site_id=site_id,
                    capabilities=list(CAPABILITIES),
                    feature_contract_version=TOY_CONTRACT.version,
                    approved_conditions=[TOY_CONDITION_ID],
                    cohort_size=30,
                    enrolled=True,
                )
            )
        record = gma.dispatch_round(toy_manifest("round-posted-1", "evaluation", THRESHOLD_VERSION, "sha256:none"))
        round_id = record.manifest.round_id
        # The simulator has no worker for any site, so each gets a failed result.
        self.assertEqual([result.participation for result in record.results], ["failed"] * 3)
        barrier = threading.Barrier(len(SITES))
        errors: list[BaseException] = []

        def post(site_id: str) -> None:
            try:
                barrier.wait(timeout=10)
                gma.submit_result(
                    round_id,
                    SiteResult(
                        round_id=round_id,
                        site_id=site_id,
                        job_digest=record.plan.round_digest,
                        participation="completed",
                        code_verified=True,
                        privacy_checks_passed=True,
                        contributed_examples=30,
                        metrics={"accuracy": 0.7},
                    ),
                )
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=post, args=(site,)) for site in SITES]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        stored = gma.get_round(round_id)
        self.assertEqual([result.participation for result in stored.results], ["completed"] * 3)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class UploadThreadTests(unittest.TestCase):
    """An update upload never waits for the thread tokens the dispatch handlers hold.

    The GMA and the LMAs run on two in-process networks, as separate processes
    would: each has its own thread limiter. The GMA's limiter has fewer tokens
    than the training dispatches sent at once. A dispatch handler holds its
    token while the LMAs run their jobs, and each LMA uploads its update to the
    GMA during that time.
    """

    ROUNDS = 8

    def test_more_training_dispatches_than_thread_tokens_all_complete(self) -> None:
        registry = toy_registry()
        gma_network = AgentNetwork.in_process(thread_tokens=4, timeout_s=5.0, dispatch_timeout_s=60.0)
        self.addCleanup(gma_network.close)
        lma_network = AgentNetwork.in_process(thread_tokens=64, timeout_s=30.0, dispatch_timeout_s=60.0)
        self.addCleanup(lma_network.close)
        settings = Settings(
            engine="http",
            admin_token=ADMIN_TOKEN,
            site_tokens=dict(SITE_TOKENS),
            lma_tokens=dict(COORDINATOR_TOKENS),
            dispatch_concurrency=3,
            summary_ttl_seconds=0.0,
        )
        gma_network.add_app(GMA_AGENT, create_app(settings, GmaDeps(registry=registry, network=lma_network)))
        for site_id in SITES:
            lma = build_fixture_lma(site_id, registry=registry, network=gma_network)
            lma_network.add_app(lma_agent(site_id), lma.app)
        gma = gma_network.gma(ADMIN_TOKEN)
        digests = register_models(gma)
        gma.refresh_sites()

        def dispatch(number: int):
            return gma.dispatch_round(
                toy_manifest(f"burst-{number}", "training", INITIAL_VERSION, digests[INITIAL_VERSION])
            )

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=self.ROUNDS) as pool:
            records = list(pool.map(dispatch, range(self.ROUNDS)))
        elapsed = time.monotonic() - started
        for record in records:
            with self.subTest(round_id=record.manifest.round_id):
                self.assertEqual(
                    [result.participation for result in record.results],
                    ["completed"] * len(SITES),
                    [result.refusal_reason for result in record.results],
                )
        # Before the fix every upload waited for the LMA's 5 s timeout.
        self.assertLess(elapsed, 5.0)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ModelVersionRouteTests(unittest.TestCase):
    """The model version routes, over the in-process network with tokens."""

    def setUp(self) -> None:
        self.study = build_study()
        self.addCleanup(self.study.close)
        self.gma = self.study.gma()

    def test_registration_answers_201_then_200_for_the_same_content_and_409_for_other_content(self) -> None:
        registration = ModelVersionRegistration(kind="toy_linear", params=dict(INITIAL_PARAMS))
        first = self.gma.request(
            "PUT", "/api/v1/model-versions/{version}", path_params={"version": INITIAL_VERSION}, json=registration.model_dump(mode="json")
        )
        self.assertEqual(first.status, 201)
        again = self.gma.request(
            "PUT", "/api/v1/model-versions/{version}", path_params={"version": INITIAL_VERSION}, json=registration.model_dump(mode="json")
        )
        self.assertEqual(again.status, 200)
        self.assertEqual(again.body, first.body)
        other = registration.model_copy(update={"params": {"bias": 1.0, "w.x1": 0.0, "w.x2": 0.0}})
        with self.assertRaises(AgentError) as caught:
            self.gma.register_model_version(INITIAL_VERSION, other)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))

    def test_registration_refuses_unknown_kinds_and_bad_parameters(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.gma.register_model_version("v-unknown", ModelVersionRegistration(kind="no_such_kind", params={}))
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "not_found"))
        with self.assertRaises(AgentError) as caught:
            self.gma.register_model_version("v-bad", ModelVersionRegistration(kind="toy_linear", params={"bias": 1.0}))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))
        with self.assertRaises(AgentError) as caught:
            self.gma.request(
                "PUT",
                "/api/v1/model-versions/{version}",
                path_params={"version": "v-extra"},
                json={"kind": "toy_linear", "params": dict(INITIAL_PARAMS), "owner": "someone"},
            )
        self.assertEqual(caught.exception.status, 422)

    def test_a_second_release_request_is_refused_by_the_lifecycle_rule(self) -> None:
        register_models(self.gma)
        client = TestClient(self.study.gma_app)
        response = client.put(
            "/api/v1/model-versions/toy-threshold-v2",
            json={"kind": "toy_threshold", "params": {"threshold": 0.5}, "requested_status": "released"},
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "conflict")
        self.assertEqual(response.json()["refusal"], "already_released")
        with self.assertRaises(AgentError) as caught:
            self.gma.get_model_version("toy-threshold-v2")
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.gma.list_model_versions().model_in_use.version, THRESHOLD_VERSION)

    def test_listing_and_reading_versions_never_returns_parameters(self) -> None:
        register_models(self.gma)
        listing = self.gma.list_model_versions(limit=20)
        self.assertEqual(listing.model_in_use.version, THRESHOLD_VERSION)
        self.assertEqual({item.version for item in listing.versions}, {THRESHOLD_VERSION, INITIAL_VERSION})
        self.assertTrue(all(item.params is None for item in [listing.model_in_use, *listing.versions]))
        self.assertIsNone(self.gma.get_model_version(INITIAL_VERSION).params)
        with self.assertRaises(AgentError) as caught:
            self.gma.get_model_version("no-such-version")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "not_found"))
        with self.assertRaises(AgentError) as caught:
            self.gma.list_model_versions(limit=0)
        self.assertEqual(caught.exception.status, 422)

    def test_the_released_model_has_its_parameters_and_an_etag(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.gma.released_model()
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "not_found"))
        digests = register_models(self.gma)
        released = self.gma.released_model()
        self.assertEqual(released.version, THRESHOLD_VERSION)
        self.assertEqual(released.params, {"threshold": 0.0})
        raw = self.gma.request("GET", "/api/v1/model-versions:released")
        self.assertEqual(raw.headers["etag"], f'"{digests[THRESHOLD_VERSION]}"')
        self.assertIsNone(self.gma.released_model(known_digest=digests[THRESHOLD_VERSION]))
        self.assertEqual(self.gma.released_model(known_digest="sha256:old").version, THRESHOLD_VERSION)
        # A site token may read it too; the model-version list is for the admin only.
        self.assertEqual(self.study.gma(SITE_TOKENS["site-a"]).released_model().version, THRESHOLD_VERSION)
        with self.assertRaises(AgentError) as caught:
            self.study.gma(SITE_TOKENS["site-a"]).list_model_versions()
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "forbidden"))


class RouteTokenTests(unittest.TestCase):
    """With tokens configured, every route but the health check needs one, on both paths."""

    #: The routes a site's token may call, besides the update upload.
    SITE_ROUTES = {
        ("GET", "/model-versions:released"),
        ("POST", "/register"),
        ("PUT", "/agents/{agent_id}/registration"),
        ("POST", "/heartbeat"),
        ("POST", "/agents/{agent_id}/heartbeats"),
        ("POST", "/telemetry"),
        ("POST", "/agents/{agent_id}/telemetry"),
        ("POST", "/deployments/sync"),
        ("POST", "/agents/{agent_id}/deployment-sync"),
    }

    def setUp(self) -> None:
        self.app = create_app(Settings(admin_token=ADMIN_TOKEN, site_tokens={"site-a": SITE_TOKENS["site-a"]}))
        self.client = TestClient(self.app)

    def routes(self):
        for route in flat_routes(self.app):
            methods = getattr(route, "methods", None)
            path = getattr(route, "path", "")
            if not methods or path in ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"):
                continue
            for method in sorted(methods - {"HEAD", "OPTIONS"}):
                concrete = (
                    path.replace("{site_id}", "site-a")
                    .replace("{round_id}", "round-1")
                    .replace("{agent_id}", "agent-1")
                    .replace("{source_id}", "source-1")
                    .replace("{classification_id}", "class-1")
                    .replace("{version}", "v1")
                )
                yield method, path, concrete

    def test_every_route_but_health_refuses_a_call_without_a_token(self) -> None:
        checked = 0
        for method, path, concrete in self.routes():
            with self.subTest(method=method, path=path):
                response = self.client.request(method, concrete, json={})
                if path in ("/health", "/api/v1/health"):
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(response.json()["code"], "unauthorized")
                checked += 1
        self.assertGreater(checked, 80)

    def test_a_site_token_reaches_only_its_own_routes(self) -> None:
        headers = {"Authorization": f"Bearer {SITE_TOKENS['site-a']}"}
        for method, path, concrete in self.routes():
            short = path.removeprefix("/api/v1")
            if path in ("/health", "/api/v1/health") or "/updates/" in path or (method, short) in self.SITE_ROUTES:
                continue
            with self.subTest(method=method, path=path):
                response = self.client.request(method, concrete, json={}, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)

    def test_a_site_token_reports_only_for_an_agent_of_its_own_site(self) -> None:
        """One LMA must not rewrite another LMA's registration, status, telemetry or bundle."""
        app = create_app(Settings(admin_token=ADMIN_TOKEN, site_tokens=dict(SITE_TOKENS)))
        client = TestClient(app)
        site_a = {"Authorization": f"Bearer {SITE_TOKENS['site-a']}"}
        site_b = {"Authorization": f"Bearer {SITE_TOKENS['site-b']}"}
        admin = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

        def identity(agent_id: str) -> dict:
            return {"agent_id": agent_id, "workspace_id": "toy", "name": agent_id}

        def registration(agent_id: str, site: str | None) -> dict:
            return {"agent": identity(agent_id), "scope": {"site": site} if site else {}, "version": "1.0"}

        ok = client.put("/api/v1/agents/lma-b/registration", json=registration("lma-b", "site-b"), headers=site_b)
        self.assertEqual(ok.status_code, 200, ok.text)
        before = client.get("/api/v1/agents/lma-b", headers=admin).json()

        reports = {
            "registration": ("PUT", "/api/v1/agents/lma-b/registration", registration("lma-b", "site-a")),
            "registration under its own scope": ("PUT", "/api/v1/agents/lma-b/registration", registration("lma-b", "site-b")),
            "short registration": ("POST", "/register", registration("lma-b", "site-a")),
            "heartbeat": ("POST", "/api/v1/agents/lma-b/heartbeats", {"agent": identity("lma-b"), "status": "DEGRADED", "timestamp": 1}),
            "short heartbeat": ("POST", "/heartbeat", {"agent": identity("lma-b"), "status": "DEGRADED", "timestamp": 1}),
            "telemetry": ("POST", "/api/v1/agents/lma-b/telemetry", {"agent": identity("lma-b"), "metrics": []}),
            "short telemetry": ("POST", "/telemetry", {"agent": identity("lma-b"), "metrics": []}),
            "deployment sync": (
                "POST",
                "/api/v1/agents/lma-b/deployment-sync",
                {"agent": identity("lma-b"), "bundle_id": "b", "bundle_version": "9"},
            ),
            "short deployment sync": ("POST", "/deployments/sync", {"agent": identity("lma-b"), "bundle_id": "b", "bundle_version": "9"}),
            "an agent never registered": ("POST", "/api/v1/agents/lma-new/heartbeats", {"agent": identity("lma-new"), "status": "ACTIVE", "timestamp": 1}),
            "a registration with no site": ("PUT", "/api/v1/agents/lma-x/registration", registration("lma-x", None)),
        }
        for name, (method, path, body) in reports.items():
            with self.subTest(report=name):
                response = client.request(method, path, json=body, headers=site_a)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["code"], "forbidden")
        self.assertEqual(client.get("/api/v1/agents/lma-b", headers=admin).json(), before)
        # The GMA answers an unknown agent with null.
        self.assertIsNone(client.get("/api/v1/agents/lma-new", headers=admin).json())
        self.assertIsNone(client.get("/api/v1/agents/lma-x", headers=admin).json())

        # Its own agent, and the admin for any agent, may report.
        mine = client.put("/api/v1/agents/lma-a/registration", json=registration("lma-a", "site-a"), headers=site_a)
        self.assertEqual(mine.status_code, 200, mine.text)
        for method, path, body in (
            ("POST", "/api/v1/agents/lma-a/heartbeats", {"agent": identity("lma-a"), "status": "ACTIVE", "timestamp": 2}),
            ("POST", "/api/v1/agents/lma-a/telemetry", {"agent": identity("lma-a"), "metrics": []}),
            ("POST", "/api/v1/agents/lma-a/deployment-sync", {"agent": identity("lma-a"), "bundle_id": "b", "bundle_version": "1"}),
        ):
            with self.subTest(own=path):
                self.assertEqual(client.request(method, path, json=body, headers=site_a).status_code, 200)
        heartbeat = {"agent": identity("lma-b"), "status": "ACTIVE", "timestamp": 3}
        self.assertEqual(client.post("/api/v1/agents/lma-b/heartbeats", json=heartbeat, headers=admin).status_code, 200)


def closed_loopback_url() -> str:
    """The address of a loopback port in 9800-9899 that nothing listens on."""
    for port in range(9899, 9799, -1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return f"http://127.0.0.1:{port}"
    raise unittest.SkipTest("no free loopback port in 9800-9899")


class GmaSettingsTests(unittest.TestCase):
    """Settings from the environment, and a GMA that builds its own network from them."""

    def test_settings_read_the_documented_variables(self) -> None:
        settings = Settings.from_env(
            {
                "GMA_ENGINE": "http",
                "GMA_SITE_ENDPOINTS": "site-a=http://127.0.0.1:9101, site-b=http://127.0.0.1:9102",
                "GMA_LMA_TOKENS": "site-a=lma-site-a-coordinator-token,site-b=lma-site-b-coordinator-token",
                "GMA_ADMIN_TOKEN": ADMIN_TOKEN,
                "GMA_SITE_TOKENS": f"site-a={SITE_TOKENS['site-a']}",
                "GMA_DISPATCH_CONCURRENCY": "5",
                "GMA_SUMMARY_TTL_SECONDS": "0.5",
                "GMA_SUMMARY_TIMEOUT_SECONDS": "4",
                "GMA_EXTENSIONS": "dagents.tests.fixtures.toy_extension:ToyExtension",
                "GMA_ALLOW_SOURCE_READS": "0",
                "GMA_REQUIRE_TOKENS": "1",
            }
        )
        self.assertEqual(settings.engine, "http")
        self.assertEqual(dict(settings.site_endpoints), {"site-a": "http://127.0.0.1:9101", "site-b": "http://127.0.0.1:9102"})
        self.assertEqual(settings.dispatch_concurrency, 5)
        self.assertEqual(settings.summary_ttl_seconds, 0.5)
        self.assertEqual(settings.summary_timeout_seconds, 4.0)
        self.assertFalse(settings.allow_source_reads)
        self.assertTrue(settings.require_tokens)
        # No token and no connection string is shown by repr.
        self.assertNotIn(ADMIN_TOKEN, repr(settings))
        self.assertNotIn("coordinator-token", repr(settings))

    def test_defaults_keep_todays_behaviour(self) -> None:
        settings = Settings.from_env({})
        self.assertEqual(settings.engine, "in-process")
        self.assertTrue(settings.allow_source_reads)
        self.assertFalse(settings.require_tokens)
        self.assertEqual(settings.dispatch_concurrency, 3)
        self.assertEqual(settings.summary_ttl_seconds, 2.0)
        # The agent network's own timeout for a call, as before the setting existed.
        self.assertEqual(settings.summary_timeout_seconds, 30.0)

    def test_settings_refuse_values_they_cannot_use(self) -> None:
        for variables in (
            {"GMA_ENGINE": "flower"},
            {"GMA_DISPATCH_CONCURRENCY": "0"},
            {"GMA_SUMMARY_TIMEOUT_SECONDS": "0"},
            {"GMA_ALLOW_SOURCE_READS": "maybe"},
            {"GMA_SITE_ENDPOINTS": "site-a"},
        ):
            with self.subTest(variables=variables), self.assertRaises(ValueError):
                Settings.from_env(variables)

    def test_the_http_engine_needs_sites_to_call(self) -> None:
        with self.assertRaises(ValueError):
            create_app(Settings(engine="http"))

    def test_tokens_can_be_required(self) -> None:
        with self.assertRaises(ValueError):
            create_app(Settings(require_tokens=True))
        app = create_app(Settings(require_tokens=True, admin_token=ADMIN_TOKEN))
        self.assertEqual(TestClient(app).get("/api/v1/federation/sites").status_code, 401)

    def test_a_database_needs_a_role(self) -> None:
        with self.assertRaises(ValueError):
            create_app(Settings(database_url="postgresql://localhost/none", model_table="coordinator.models"))
        with self.assertRaises(ValueError):
            create_app(Settings(model_table="coordinator.models"))

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_an_lma_that_cannot_be_reached_over_http_is_reported_not_enrolled(self) -> None:
        app = create_app(
            Settings(
                engine="http",
                site_endpoints={"site-a": closed_loopback_url()},
                lma_tokens={"site-a": COORDINATOR_TOKENS["site-a"]},
                extensions="dagents.tests.fixtures.toy_extension:ToyExtension",
                summary_ttl_seconds=0.0,
            )
        )
        self.addCleanup(app.state.gma.close)
        client = TestClient(app)
        self.assertEqual(client.post("/api/v1/federation/sites:refresh").json(), [])
        entries = client.get("/api/v1/federation/sites/summaries").json()
        self.assertEqual(entries, [{"site_id": "site-a", "reachable": False, "source_kind": None, "summary": None, "audit": None}])
        self.assertEqual(client.get("/api/v1/health").json()["engine"], "http")
        self.assertIn("toy_linear", client.get("/api/v1/extensions").json()["model_kinds"])


if __name__ == "__main__":
    unittest.main()
