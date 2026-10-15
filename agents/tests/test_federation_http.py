"""Tests for the HTTP transport between a coordinator and sites in other processes.

``HttpSiteWorker`` is checked against a small FastAPI site app whose routes
wrap a fake ``SiteWorker``, and ``HttpUpdateChannel`` against a FastAPI app
that wraps a fake ``UpdateChannel``. A ``TestClient`` is passed as
``client=``, so the requests go through the real routing and JSON encoding
without opening a port.

Only the test that runs ``LocalFederatedWorker`` needs the planner, because
the worker's Guard calls it. It skips without ``dagentsc``.
"""

from __future__ import annotations

import unittest

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient

from agents.common.application.federation import InProcessFederationEngine
from agents.common.domain.federation import (
    AggregationMethod,
    FederatedJob,
    JobAcceptance,
    RoundManifest,
    RoundPlan,
    SiteResult,
)
from agents.common.infrastructure.federation_http import (
    EXECUTE_PATH,
    OFFER_PATH,
    UPDATE_PATH,
    HttpSiteWorker,
    HttpUpdateChannel,
    UpdateReceipt,
    UpdateSubmission,
)
from agents.tests.test_federation_updates import (
    RecordingChannel,
    build_job,
    build_manifest,
    build_worker,
)
from agents.tests.test_governance import dagentsc_available

SITE_URL = "http://site-a.internal:8082"
COORDINATOR_URL = "http://coordinator.internal:8081"


class FakeSiteWorker:
    """A site that accepts or refuses by a flag and returns a fixed result shape."""

    def __init__(self, site_id: str, *, accept: bool = True) -> None:
        self.site_id = site_id
        self.accept = accept
        self.offered: list[FederatedJob] = []
        self.executed: list[FederatedJob] = []

    def offer(self, job: FederatedJob) -> JobAcceptance:
        self.offered.append(job)
        return JobAcceptance(
            round_id=job.round_id,
            site_id=self.site_id,
            accepted=self.accept,
            reason="" if self.accept else "the site's policy refuses this round",
            verified_digest=self.accept,
            feature_contract_version=job.manifest.feature_contract_version,
        )

    def execute(self, job: FederatedJob) -> SiteResult:
        self.executed.append(job)
        return SiteResult(
            round_id=job.round_id,
            site_id=self.site_id,
            job_digest=job.manifest_digest,
            participation="completed",
            code_verified=True,
            privacy_checks_passed=True,
            contributed_examples=42,
            metrics={"auc": 0.75, "record_count": 42.0},
            update_norm=0.5,
            update_digest="sha256:abc",
            local_evidence_pointer=f"site-local://{self.site_id}/{job.round_id}",
        )


def site_app(worker: FakeSiteWorker) -> FastAPI:
    """The two routes an LMA serves for the coordinator, wrapping ``worker``."""
    app = FastAPI()

    @app.post(OFFER_PATH)
    def offer(job: FederatedJob) -> JobAcceptance:
        return worker.offer(job)

    @app.post(EXECUTE_PATH)
    def execute(job: FederatedJob) -> SiteResult:
        return worker.execute(job)

    return app


def coordinator_app(channel: RecordingChannel, *, open_rounds: set[str]) -> FastAPI:
    """The coordinator's update route, wrapping a fake ``UpdateChannel``."""
    app = FastAPI()

    @app.post(UPDATE_PATH)
    def submit(round_id: str, submission: UpdateSubmission) -> UpdateReceipt:
        if round_id != submission.job.round_id or round_id not in open_rounds:
            raise HTTPException(status_code=409, detail=f"no round {round_id} is running")
        return UpdateReceipt(update_digest=channel.submit(submission.job, submission.update))

    return app


def failing_client(error: Exception) -> httpx.Client:
    """A client whose every request fails with ``error``, as a network failure would."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    return httpx.Client(transport=httpx.MockTransport(handler))


def answering_client(response: httpx.Response) -> httpx.Client:
    """A client whose every request gets ``response``."""
    return httpx.Client(transport=httpx.MockTransport(lambda request: response))


class HttpSiteWorkerRoundTripTests(unittest.TestCase):
    """A site that answers: the models arrive and return unchanged."""

    def setUp(self) -> None:
        self.fake = FakeSiteWorker("site-a")
        self.client = TestClient(site_app(self.fake))
        self.worker = HttpSiteWorker("site-a", SITE_URL + "/", client=self.client)
        self.job = build_job(build_manifest())

    def test_offer_sends_the_job_and_returns_the_sites_acceptance(self) -> None:
        acceptance = self.worker.offer(self.job)
        self.assertEqual(self.fake.offered, [self.job])
        self.assertEqual(acceptance, self.fake.offer(self.job))
        self.assertTrue(acceptance.accepted)
        self.assertTrue(acceptance.verified_digest)

    def test_a_refusal_comes_back_with_the_sites_reason(self) -> None:
        self.fake.accept = False
        acceptance = self.worker.offer(self.job)
        self.assertFalse(acceptance.accepted)
        self.assertEqual(acceptance.reason, "the site's policy refuses this round")

    def test_execute_sends_the_job_and_returns_the_sites_result(self) -> None:
        result = self.worker.execute(self.job)
        self.assertEqual(self.fake.executed, [self.job])
        self.assertEqual(result, self.fake.execute(self.job))
        self.assertEqual(result.metrics, {"auc": 0.75, "record_count": 42.0})
        self.assertEqual(result.update_digest, "sha256:abc")

    def test_the_trailing_slash_of_the_address_is_ignored(self) -> None:
        sent: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(str(request.url))
            return httpx.Response(200, json=self.fake.offer(self.job).model_dump(mode="json"))

        worker = HttpSiteWorker("site-a", SITE_URL + "/", client=httpx.Client(transport=httpx.MockTransport(handler)))
        worker.offer(self.job)
        self.assertEqual(sent, [SITE_URL + OFFER_PATH])


class HttpSiteWorkerUnreachableTests(unittest.TestCase):
    """A site that does not answer is recorded as refusing or failing, never skipped."""

    def setUp(self) -> None:
        self.job = build_job(build_manifest())

    def assert_refused_and_failed(self, worker: HttpSiteWorker, words: str) -> None:
        acceptance = worker.offer(self.job)
        self.assertFalse(acceptance.accepted)
        self.assertEqual((acceptance.round_id, acceptance.site_id), (self.job.round_id, "site-a"))
        self.assertIn(words, acceptance.reason)

        result = worker.execute(self.job)
        self.assertEqual(result.participation, "failed")
        self.assertEqual((result.round_id, result.site_id), (self.job.round_id, "site-a"))
        self.assertEqual(result.job_digest, self.job.manifest_digest)
        self.assertEqual(result.contributed_examples, 0)
        self.assertIsNone(result.update_digest)
        self.assertTrue(result.local_evidence_pointer.startswith("unreachable://site-a#"))
        self.assertIn(words, result.local_evidence_pointer)
        # The reason is shown to the coordinator, so it never names the address.
        for text in (acceptance.reason, result.local_evidence_pointer):
            self.assertNotIn("site-a.internal", text)
            self.assertNotIn("\n", text)

    def test_a_connection_error(self) -> None:
        worker = HttpSiteWorker("site-a", SITE_URL, client=failing_client(httpx.ConnectError("refused")))
        self.assert_refused_and_failed(worker, "connection to the site failed")

    def test_a_closed_port_without_a_test_client(self) -> None:
        worker = HttpSiteWorker("site-a", "http://127.0.0.1:1", timeout=2.0)
        self.assert_refused_and_failed(worker, "connection to the site failed")

    def test_a_timeout(self) -> None:
        worker = HttpSiteWorker(
            "site-a", SITE_URL, timeout=2.5, client=failing_client(httpx.ReadTimeout("slow"))
        )
        self.assert_refused_and_failed(worker, "did not answer within 2.5 seconds")

    def test_a_server_error(self) -> None:
        app = FastAPI()

        @app.post(OFFER_PATH)
        @app.post(EXECUTE_PATH)
        def broken() -> JSONResponse:
            return JSONResponse({"detail": "internal error"}, status_code=500)

        worker = HttpSiteWorker("site-a", SITE_URL, client=TestClient(app))
        self.assert_refused_and_failed(worker, "answered with status 500")

    def test_an_exception_inside_the_site(self) -> None:
        app = FastAPI()

        @app.post(OFFER_PATH)
        @app.post(EXECUTE_PATH)
        def broken() -> None:
            raise RuntimeError("the site crashed")

        worker = HttpSiteWorker("site-a", SITE_URL, client=TestClient(app, raise_server_exceptions=False))
        self.assert_refused_and_failed(worker, "answered with status 500")

    def test_a_body_that_is_not_json(self) -> None:
        app = FastAPI()

        @app.post(OFFER_PATH)
        @app.post(EXECUTE_PATH)
        def html() -> PlainTextResponse:
            return PlainTextResponse("<html>proxy page</html>")

        worker = HttpSiteWorker("site-a", SITE_URL, client=TestClient(app))
        self.assert_refused_and_failed(worker, "not in the expected format")

    def test_json_that_is_not_the_model(self) -> None:
        worker = HttpSiteWorker("site-a", SITE_URL, client=answering_client(httpx.Response(200, json={"ok": True})))
        self.assert_refused_and_failed(worker, "not in the expected format")

    def test_an_answer_for_another_site(self) -> None:
        """A site address that reaches the wrong LMA must not file a result under another site."""
        worker = HttpSiteWorker("site-a", SITE_URL, client=TestClient(site_app(FakeSiteWorker("site-b"))))
        self.assert_refused_and_failed(worker, "answered for a different round or site")


class HttpUpdateChannelTests(unittest.TestCase):
    """The site's end of the update path."""

    def setUp(self) -> None:
        self.recorded = RecordingChannel()
        self.job = build_job(build_manifest())
        self.client = TestClient(coordinator_app(self.recorded, open_rounds={self.job.round_id}))
        self.channel = HttpUpdateChannel(COORDINATOR_URL, client=self.client)

    def test_the_coordinator_stores_the_update_and_its_digest_comes_back(self) -> None:
        update = {"w.reading": 0.6, "w.group": -0.8, "bias": 0.0}
        digest = self.channel.submit(self.job, update)
        self.assertEqual(self.recorded.calls, [(self.job.round_id, "site-a", update)])
        self.assertEqual(digest, RecordingChannel().submit(self.job, update))

    def test_the_request_goes_to_the_rounds_path(self) -> None:
        sent: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(str(request.url))
            return httpx.Response(200, json={"update_digest": "sha256:x"})

        channel = HttpUpdateChannel(COORDINATOR_URL + "/", client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(channel.submit(self.job, {"bias": 0.0}), "sha256:x")
        self.assertEqual(sent, [COORDINATOR_URL + UPDATE_PATH.format(round_id=self.job.round_id)])

    def test_a_refusal_raises_with_the_coordinators_detail(self) -> None:
        job = build_job(build_manifest(round_id="round-9999"))
        with self.assertRaises(RuntimeError) as raised:
            self.channel.submit(job, {"bias": 0.0})
        self.assertIn("409", str(raised.exception))
        self.assertIn("no round round-9999 is running", str(raised.exception))
        self.assertEqual(self.recorded.calls, [])

    def test_a_refusal_whose_body_is_not_an_object_still_names_the_status(self) -> None:
        for body in ([{"msg": "bad"}], "plain words"):
            channel = HttpUpdateChannel(COORDINATOR_URL, client=answering_client(httpx.Response(422, json=body)))
            with self.assertRaises(RuntimeError) as raised:
                channel.submit(self.job, {"bias": 0.0})
            self.assertIn("422", str(raised.exception))

    def test_an_unreachable_coordinator_raises(self) -> None:
        channel = HttpUpdateChannel(COORDINATOR_URL, client=failing_client(httpx.ConnectError("refused")))
        with self.assertRaises(httpx.ConnectError):
            channel.submit(self.job, {"bias": 0.0})


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class LocalWorkerOverHttpChannelTests(unittest.TestCase):
    """``LocalFederatedWorker`` sends its bounded update through the HTTP channel."""

    def setUp(self) -> None:
        self.recorded = RecordingChannel()
        self.manifest = build_manifest()
        self.client = TestClient(coordinator_app(self.recorded, open_rounds={self.manifest.round_id}))

    def test_the_update_reaches_the_coordinator_and_the_job_completes(self) -> None:
        worker = build_worker(update_channel=HttpUpdateChannel(COORDINATOR_URL, client=self.client))
        result = worker.execute(build_job(self.manifest))
        self.assertEqual(result.participation, "completed")
        self.assertEqual(len(self.recorded.calls), 1)
        self.assertEqual(result.update_digest, worker.default_update_digest(self.recorded.calls[0][2]))

    def test_the_job_is_refused_when_the_coordinator_refuses_the_update(self) -> None:
        manifest = build_manifest(round_id="round-closed")
        worker = build_worker(update_channel=HttpUpdateChannel(COORDINATOR_URL, client=self.client))
        result = worker.execute(build_job(manifest))
        self.assertEqual(result.participation, "rejected")
        self.assertIsNone(result.update_digest)
        self.assertIn("409", result.local_evidence_pointer)
        self.assertEqual(self.recorded.calls, [])

    def test_the_job_is_refused_when_the_coordinator_cannot_be_reached(self) -> None:
        channel = HttpUpdateChannel(COORDINATOR_URL, client=failing_client(httpx.ConnectError("refused")))
        result = build_worker(update_channel=channel).execute(build_job(self.manifest))
        self.assertEqual(result.participation, "rejected")
        self.assertIsNone(result.update_digest)
        self.assertIn("update channel did not accept", result.local_evidence_pointer)


class EngineOverHttpTests(unittest.TestCase):
    """The in-process engine dispatches to HTTP workers as to any other worker.

    The plan is written by hand, so the engine runs without the planner.
    """

    def test_a_round_reaches_every_selected_site(self) -> None:
        answering = FakeSiteWorker("site-a")
        refusing = FakeSiteWorker("site-b", accept=False)
        engine = InProcessFederationEngine(
            [
                HttpSiteWorker("site-a", "http://site-a.internal", client=TestClient(site_app(answering))),
                HttpSiteWorker("site-b", "http://site-b.internal", client=TestClient(site_app(refusing))),
                HttpSiteWorker("site-c", "http://site-c.internal", client=failing_client(httpx.ConnectError("down"))),
            ]
        )
        manifest: RoundManifest = build_manifest(minimum_participants=1)
        plan = RoundPlan(
            round_id=manifest.round_id,
            study_id=manifest.study_id,
            phase=manifest.phase,
            selected_sites=["site-a", "site-b", "site-c"],
            quorum_met=True,
            required_participants=1,
            aggregation=AggregationMethod(kind="fedavg"),
            round_digest="fnv1a:0123456789abcdef",
        )

        submission = engine.submit_round(manifest, plan)

        self.assertEqual(submission.dispatched_sites, ["site-a"])
        self.assertEqual([a.accepted for a in submission.acceptances], [True, False, False])
        self.assertIn("connection to the site failed", submission.acceptances[2].reason)
        self.assertEqual(len(answering.executed), 1)
        job = answering.executed[0]
        self.assertEqual((job.site_id, job.manifest_digest, job.task), ("site-a", plan.round_digest, "train"))
        self.assertEqual(job.manifest, manifest)
        self.assertEqual(refusing.executed, [])

        results = {result.site_id: result for result in engine.collect_results(manifest.round_id)}
        self.assertEqual(sorted(results), ["site-a", "site-b", "site-c"])
        self.assertEqual(results["site-a"].participation, "completed")
        self.assertEqual(results["site-a"].job_digest, plan.round_digest)
        self.assertEqual(results["site-b"].participation, "rejected")
        self.assertEqual(results["site-c"].participation, "rejected")


if __name__ == "__main__":
    unittest.main()
