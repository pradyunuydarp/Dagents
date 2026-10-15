"""The LMA started for one site: settings, the site routes, tokens and the site-mode lock-down.

The site is the toy application in ``agents/tests/fixtures/toy_extension.py``:
30 records with two readings, a group and a yes/no ``label``, served by a
``StaticSiteSource``. The GMA is a small fake (:class:`FakeGma`) that serves
the two routes an LMA calls, the released model and the update upload, so
these tests do not depend on the GMA's own code. The LMA and the fake GMA run
on one in-process :class:`AgentNetwork`, as they do inside the demo server.

The Guard and the label-rate decision use the real ``dagentsc``; those tests
skip with ``dagentsc binary is not available`` without it.

``test_lma_federation_jobs.py`` imports the helpers defined here.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from agents.common.domain.federation import ModelVersion, UpdateUpload
from agents.common.domain.governance import KyuAttribute, Requester
from agents.common.extensions import ExtensionRegistry
from agents.common.infrastructure.agent_auth import (
    AccessConfigurationError,
    AgentAccess,
    TokenGrant,
    install_agent_access,
    require_scope,
)
from agents.common.infrastructure.agent_client import GMA_AGENT, AgentNetwork, lma_agent
from agents.common.infrastructure.site_sources import StaticSiteSource
from agents.lma.app_factory import LmaDeps, create_app
from agents.lma.config import Settings
from agents.lma.domain.site_config import SiteConfig
from agents.tests.fixtures.toy_extension import (
    TOY_CLASSIFICATION,
    TOY_CONDITION_ID,
    TOY_LABEL_FIELD,
    ToyExtension,
    ToyLinearKind,
    toy_records,
)
from agents.tests.test_governance import dagentsc_available

REPO_ROOT = Path(__file__).resolve().parents[2]

SITE = "site-a"
SITE_TOKEN = "lma-site-token-aaaaaaaaaaaaaaaa"
COORDINATOR_TOKEN = "lma-coordinator-token-bbbbbbbbbbbb"
GMA_SITE_TOKEN = "gma-site-token-for-site-a-cccccccc"
TOY_EXTENSION_SPEC = "agents.tests.fixtures.toy_extension:ToyExtension"

LINEAR = ToyLinearKind()
INITIAL = LINEAR.initial_params()
TRAINED = {"bias": 0.1, "w.x1": 1.5, "w.x2": 0.4}
CAPABILITIES = ("local_profiling", "local_evaluation", "local_training")

SITE_HEADERS = {"Authorization": f"Bearer {SITE_TOKEN}"}
COORDINATOR_HEADERS = {"Authorization": f"Bearer {COORDINATOR_TOKEN}"}

#: Text that must never appear in an LMA response.
CONNECTION_SECRETS = ("s3cret-pw", "db.internal.example", "postgresql://", "alice")


def toy_registry() -> ExtensionRegistry:
    registry = ExtensionRegistry()
    registry.register(ToyExtension())
    return registry


def site_config(**overrides: Any) -> SiteConfig:
    values: dict[str, Any] = {
        "site_id": SITE,
        "condition_id": TOY_CONDITION_ID,
        "study_id": "toy-study",
        "capabilities": CAPABILITIES,
        "policy_version": "toy-policy-v1",
    }
    values.update(overrides)
    return SiteConfig(**values)


def lma_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "site_token": SITE_TOKEN,
        "coordinator_token": COORDINATOR_TOKEN,
        "gma_token": GMA_SITE_TOKEN,
        "source_max_staleness_ms": 60_000,
    }
    values.update(overrides)
    return Settings(**values)


def linear_version(version: str = "toy-linear-v2", params: dict[str, float] | None = None, **overrides: Any) -> ModelVersion:
    params = dict(TRAINED if params is None else params)
    values: dict[str, Any] = {
        "version": version,
        "kind": LINEAR.kind,
        "digest": LINEAR.parameter_digest(params),
        "status": "released",
        "params": params,
    }
    values.update(overrides)
    return ModelVersion(**values)


def coordinator_requester() -> Requester:
    return Requester(
        requester_id="toy-study",
        requester_kind="coordinator",
        affiliation="toy-study",
        stated_purpose=TOY_CONDITION_ID,
        compliance_history=1.0,
        attributes=[
            KyuAttribute(attribute_id=name, verified=True)
            for name in ("site_registration", "feature_contract_version", "privacy_profile")
        ],
    )


class FakeGma:
    """A GMA that serves only what an LMA calls: the released model and the update upload.

    It checks the GMA site token bound to the site, as the real GMA does.
    ``uploads`` keeps every stored update. ``receipt_digest`` replaces the
    digest it answers with, and ``refuse_uploads`` makes it answer 409.
    """

    def __init__(self, released: ModelVersion | None = None, site_tokens: dict[str, str] | None = None) -> None:
        self.released = released
        self.uploads: list[tuple[str, str, UpdateUpload]] = []
        self.released_requests = 0
        self.not_modified = 0
        self.receipt_digest: str | None = None
        self.refuse_uploads = False
        tokens = site_tokens if site_tokens is not None else {SITE: GMA_SITE_TOKEN}
        self.app = FastAPI(title="fake GMA")
        install_agent_access(
            self.app,
            AgentAccess([TokenGrant("site", token, site_id=site) for site, token in tokens.items()]),
            agent_name="fake GMA",
        )

        @self.app.get("/api/v1/model-versions:released", dependencies=[Depends(require_scope("site", "admin"))])
        def released_model(request: Request) -> Any:
            self.released_requests += 1
            if self.released is None:
                return JSONResponse({"detail": "no model is released", "code": "not_found"}, status_code=404)
            etag = f'"{self.released.digest}"'
            if request.headers.get("if-none-match") == etag:
                self.not_modified += 1
                return Response(status_code=304, headers={"ETag": etag})
            return JSONResponse(self.released.model_dump(mode="json"), headers={"ETag": etag})

        @self.app.put(
            "/api/v1/federation/rounds/{round_id}/updates/{site_id}",
            dependencies=[Depends(require_scope("site", bind_site="site_id"))],
        )
        def upload_update(round_id: str, site_id: str, upload: UpdateUpload) -> Any:
            if self.refuse_uploads:
                return JSONResponse(
                    {"detail": f"an update from {site_id} is already stored for {round_id}", "code": "conflict"},
                    status_code=409,
                )
            self.uploads.append((round_id, site_id, upload))
            return {"digest": self.receipt_digest or LINEAR.update_digest(upload.update)}


class LmaHarness:
    """One LMA for ``SITE`` and a fake GMA on one in-process network.

    ``client`` calls the LMA through FastAPI's ``TestClient``. Close the
    harness with :meth:`close`.
    """

    def __init__(
        self,
        *,
        records: list[dict[str, Any]] | None = None,
        settings: Settings | None = None,
        site: SiteConfig | None = None,
        gma: FakeGma | None = None,
        with_network: bool = True,
        **deps: Any,
    ) -> None:
        self.source = deps.pop("source", None) or StaticSiteSource(
            toy_records(SITE) if records is None else records, location={"name": "fixture"}
        )
        self.gma = gma if gma is not None else FakeGma(released=linear_version())
        given_network = deps.pop("network", None)
        self._owns_network = given_network is None and with_network
        if given_network is not None:
            self.network = given_network
        else:
            self.network = AgentNetwork.in_process({GMA_AGENT: self.gma.app}) if with_network else None
        self.registry = deps.pop("registry", None) or toy_registry()
        self.app = create_app(
            settings if settings is not None else lma_settings(),
            LmaDeps(
                registry=self.registry,
                site=site if site is not None else site_config(),
                source=self.source,
                network=self.network,
                **deps,
            ),
        )
        if self._owns_network:
            self.network.add_app(lma_agent(SITE), self.app)
        self.client = TestClient(self.app)

    def close(self) -> None:
        if self._owns_network:
            self.network.close()

    def get(self, path: str, headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
        return self.client.get(path, headers=SITE_HEADERS if headers is None else headers, **kwargs)

    def post(self, path: str, headers: dict[str, str] | None = None, **kwargs: Any) -> Any:
        return self.client.post(path, headers=SITE_HEADERS if headers is None else headers, **kwargs)


def probe_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "boundary": "before_read",
        "granularity": "row",
        "requested_fields": ["x1", "x2", "group", TOY_LABEL_FIELD],
        "record_limit": 5,
        "requester": coordinator_requester().model_dump(mode="json"),
        "declared_purpose": TOY_CONDITION_ID,
    }
    body.update(overrides)
    return body


# --------------------------------------------------------------------------- #
# Settings and wiring: no planner needed
# --------------------------------------------------------------------------- #


class SettingsTests(unittest.TestCase):
    def test_from_env_reads_every_site_setting(self) -> None:
        settings = Settings.from_env(
            {
                "LMA_APP_NAME": "lma-site-a",
                "LMA_API_HOST": "0.0.0.0",
                "LMA_API_PORT": "9810",
                "LMA_GMA_ENDPOINT": "http://gma.example:9811",
                "LMA_GMA_TOKEN": GMA_SITE_TOKEN,
                "LMA_SITE_ID": SITE,
                "LMA_CONDITION_ID": TOY_CONDITION_ID,
                "LMA_STUDY_ID": "toy-study",
                "LMA_REPORTING_PURPOSE": "toy_research",
                "LMA_PROFILE_FIELDS": "x1, x2 ,label",
                "LMA_CAPABILITIES": "local_profiling,local_training",
                "LMA_POLICY_VERSION": "toy-policy-v2",
                "LMA_EXTENSIONS": TOY_EXTENSION_SPEC,
                "LMA_SITE_SOURCE": "toy_static",
                "LMA_DATABASE_URL": "postgresql://alice:s3cret-pw@db.internal.example:5432/sites",
                "LMA_SITE_ROLE": "site_a",
                "LMA_AUDIT_TABLE": "site_a.guard_audit",
                "LMA_AUDIT_ANCHOR_TABLE": "site_a.guard_audit_anchor",
                "LMA_JOB_LOG_TABLE": "coordinator.site_contributions",
                "LMA_SITE_TOKEN": SITE_TOKEN,
                "LMA_COORDINATOR_TOKEN": COORDINATOR_TOKEN,
                "LMA_SOURCE_MAX_STALENESS_MS": "5000",
                "LMA_STATEMENT_TIMEOUT_MS": "7000",
                "LMA_REQUIRE_TOKENS": "1",
            }
        )
        self.assertEqual("lma-site-a", settings.app_name)
        self.assertEqual(9810, settings.api_port)
        self.assertEqual(SITE, settings.site_id)
        self.assertEqual(TOY_CONDITION_ID, settings.condition_id)
        self.assertEqual("toy-study", settings.study_id)
        self.assertEqual("toy_research", settings.reporting_purpose)
        self.assertEqual(("x1", "x2", "label"), settings.profile_fields)
        self.assertEqual(("local_profiling", "local_training"), settings.capabilities)
        self.assertEqual("toy-policy-v2", settings.policy_version)
        self.assertEqual("toy_static", settings.site_source)
        self.assertEqual("site_a", settings.site_role)
        self.assertEqual("coordinator.site_contributions", settings.job_log_table)
        self.assertEqual(5000, settings.source_max_staleness_ms)
        self.assertEqual(7000, settings.statement_timeout_ms)
        self.assertTrue(settings.require_tokens)
        self.assertTrue(settings.site_configured)

    def test_an_empty_environment_gives_an_lma_without_a_site(self) -> None:
        settings = Settings.from_env({})
        self.assertEqual("dagents-lma", settings.app_name)
        self.assertIsNone(settings.site_id)
        self.assertFalse(settings.site_configured)
        self.assertFalse(settings.require_tokens)
        self.assertEqual(30_000, settings.source_max_staleness_ms)

    def test_repr_holds_no_token_and_no_connection_string(self) -> None:
        settings = Settings.from_env(
            {
                "LMA_DATABASE_URL": "postgresql://alice:s3cret-pw@db.internal.example:5432/sites",
                "LMA_SITE_TOKEN": SITE_TOKEN,
                "LMA_COORDINATOR_TOKEN": COORDINATOR_TOKEN,
                "LMA_GMA_TOKEN": GMA_SITE_TOKEN,
            }
        )
        text = repr(settings)
        for secret in (SITE_TOKEN, COORDINATOR_TOKEN, GMA_SITE_TOKEN, *CONNECTION_SECRETS):
            self.assertNotIn(secret, text)

    def test_a_number_that_is_not_a_number_is_refused_by_name(self) -> None:
        with self.assertRaises(ValueError) as raised:
            Settings.from_env({"LMA_SOURCE_MAX_STALENESS_MS": "soon"})
        self.assertIn("LMA_SOURCE_MAX_STALENESS_MS", str(raised.exception))

    def test_a_site_needs_a_condition(self) -> None:
        with self.assertRaises(ValueError) as raised:
            create_app(Settings(site_id=SITE))
        self.assertIn("LMA_CONDITION_ID", str(raised.exception))

    def test_importing_the_modules_reads_no_environment_variable(self) -> None:
        """``config`` reads nothing at import, and ``main`` starts with no LMA variable set."""
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO_ROOT)}
        code = (
            "import os\n"
            "before = dict(os.environ)\n"
            "import agents.lma.config\n"
            "assert dict(os.environ) == before, 'importing config changed the environment'\n"
            "import agents.lma.main\n"
            "print(agents.lma.main.app.title)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code], cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("dagents-lma", completed.stdout.strip())


class NoSiteTests(unittest.TestCase):
    """An LMA started without a site keeps today's behaviour, and its site routes answer 404."""

    def setUp(self) -> None:
        self.client = TestClient(create_app(Settings()))

    def test_site_routes_answer_404_with_a_code(self) -> None:
        for method, path in (
            ("GET", "/api/v1/site"),
            ("GET", "/site/summary"),
            ("POST", "/api/v1/site/source:refresh"),
            ("GET", "/api/v1/federation/registration"),
            ("GET", "/api/v1/federation/jobs"),
            ("GET", "/api/v1/governance/audit:summary"),
        ):
            with self.subTest(path=path):
                response = self.client.request(method, path)
                self.assertEqual(404, response.status_code, response.text)
                self.assertEqual("not_found", response.json()["code"])

    def test_existing_routes_need_no_token(self) -> None:
        self.assertEqual(200, self.client.get("/api/v1/health").status_code)
        self.assertEqual(200, self.client.get("/api/v1/sources").status_code)
        self.assertEqual(200, self.client.get("/api/v1/extensions").status_code)

    def test_source_definitions_come_back_without_credentials(self) -> None:
        source = {
            "source_id": "orders-db",
            "kind": "postgres",
            "connection_ref": {
                "connection_id": "orders",
                "options": {"role": "orders_reader", "password": "s3cret-pw", "host": "db.internal.example"},
            },
            "selection": {"table": "orders"},
        }
        echoed = self.client.post("/api/v1/sources", json=source)
        self.assertEqual(200, echoed.status_code, echoed.text)
        for response in (echoed, self.client.get("/api/v1/sources/orders-db"), self.client.get("/api/v1/sources")):
            text = response.text
            self.assertNotIn("s3cret-pw", text)
            self.assertNotIn("db.internal.example", text)
            self.assertIn("orders_reader", text)

    def test_required_tokens_refuse_to_start_without_tokens(self) -> None:
        with self.assertRaises(AccessConfigurationError):
            create_app(Settings(require_tokens=True))

    def test_an_lma_without_tokens_warns_once(self) -> None:
        with self.assertLogs(level=logging.WARNING) as logs:
            create_app(Settings())
        self.assertTrue(any("without tokens" in line for line in logs.output))

    def test_health_keeps_its_shape(self) -> None:
        body = self.client.get("/health").json()
        self.assertEqual(
            {"status", "agent_type", "deployed_bundles", "runs", "model_runs", "active_bundle"}, set(body)
        )
        self.assertEqual("LMA", body["agent_type"])


class StartupTests(unittest.TestCase):
    def test_an_unknown_site_source_fails_at_startup(self) -> None:
        settings = Settings(
            site_id=SITE, condition_id=TOY_CONDITION_ID, extensions=TOY_EXTENSION_SPEC, site_source="no_such_source"
        )
        with self.assertRaises(Exception) as raised:
            create_app(settings)
        self.assertIn("no_such_source", str(raised.exception))

    def test_a_site_without_a_source_fails_at_startup(self) -> None:
        settings = Settings(site_id=SITE, condition_id=TOY_CONDITION_ID, extensions=TOY_EXTENSION_SPEC)
        with self.assertRaises(ValueError) as raised:
            create_app(settings)
        self.assertIn("LMA_SITE_SOURCE", str(raised.exception))

    def test_an_unknown_condition_fails_at_startup(self) -> None:
        with self.assertRaises(Exception) as raised:
            create_app(Settings(), LmaDeps(registry=toy_registry(), site=site_config(condition_id="other"),
                                           source=StaticSiteSource([])))
        self.assertIn("other", str(raised.exception))


# --------------------------------------------------------------------------- #
# The site routes, with the real planner
# --------------------------------------------------------------------------- #


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SiteRoutesTests(unittest.TestCase):
    def harness(self, **kwargs: Any) -> LmaHarness:
        harness = LmaHarness(**kwargs)
        self.addCleanup(harness.close)
        return harness

    def test_site_info_names_the_site_and_no_connection_details(self) -> None:
        lma = self.harness()
        response = lma.get("/api/v1/site")
        self.assertEqual(200, response.status_code, response.text)
        info = response.json()
        self.assertEqual(SITE, info["site_id"])
        self.assertEqual(TOY_CONDITION_ID, info["condition_id"])
        self.assertEqual(TOY_CLASSIFICATION.classification_id, info["classification_id"])
        self.assertEqual("toy-v1", info["feature_contract_version"])
        self.assertEqual(TOY_LABEL_FIELD, info["label_field"])
        self.assertEqual({"kind": "memory", "location": {"name": "fixture"}, "role": None}, info["source"])
        self.assertEqual(30, info["record_count"])
        self.assertEqual(lma.source.version(), info["source_version"])
        self.assertEqual(list(CAPABILITIES), info["capabilities"])
        self.assertEqual("toy-policy-v1", info["policy_version"])
        self.assertEqual(5, info["minimum_cohort"])

    def test_an_lma_started_from_settings_shows_no_connection_string(self) -> None:
        settings = Settings(
            site_id=SITE,
            condition_id=TOY_CONDITION_ID,
            extensions=TOY_EXTENSION_SPEC,
            site_source="toy_static",
            database_url="postgresql://alice:s3cret-pw@db.internal.example:5432/sites",
            site_role="site_a",
            site_token=SITE_TOKEN,
        )
        app = create_app(settings)
        client = TestClient(app)
        for path in ("/api/v1/site", "/api/v1/site/summary", "/api/v1/federation/jobs"):
            with self.subTest(path=path):
                response = client.get(path, headers=SITE_HEADERS)
                self.assertEqual(200, response.status_code, response.text)
                for secret in CONNECTION_SECRETS:
                    self.assertNotIn(secret, response.text)
        info = client.get("/api/v1/site", headers=SITE_HEADERS).json()
        self.assertEqual({"memory": "toy", "site": SITE}, info["source"]["location"])
        app.state.lma.close()

    def test_a_source_location_that_names_a_host_is_not_shown(self) -> None:
        source = StaticSiteSource(
            toy_records(SITE),
            location={"schema": "site_a", "dsn": "postgresql://alice:s3cret-pw@db.internal.example/x", "url": "http://x"},
        )
        lma = self.harness(source=source)
        info = lma.get("/api/v1/site").json()
        self.assertEqual({"schema": "site_a"}, info["source"]["location"])

    def test_summary_releases_the_label_rate_when_the_planner_permits_it(self) -> None:
        records = toy_records(SITE)
        positives = sum(1 for record in records if record[TOY_LABEL_FIELD])
        lma = self.harness(records=records)
        response = lma.get("/api/v1/site/summary", headers=COORDINATOR_HEADERS)
        self.assertEqual(200, response.status_code, response.text)
        summary = response.json()
        self.assertEqual(
            {
                "site_id": SITE,
                "record_count": 30,
                "labelled_count": 30,
                "label_rate": positives / 30,
                "label_rate_status": "released",
                "minimum_cohort": 5,
                "planner_reason": None,
            },
            summary,
        )

    def test_summary_withholds_a_rate_below_the_minimum_cohort(self) -> None:
        lma = self.harness(records=toy_records(SITE, labelled_share=0.1))
        summary = lma.get("/api/v1/site/summary").json()
        self.assertEqual(3, summary["labelled_count"])
        self.assertEqual("below_minimum_cohort", summary["label_rate_status"])
        self.assertIsNone(summary["label_rate"])
        self.assertIn("minimum_cohort", summary["planner_reason"])

    def test_summary_without_labels(self) -> None:
        lma = self.harness(records=toy_records(SITE, labelled_share=0.0))
        summary = lma.get("/api/v1/site/summary").json()
        self.assertEqual(0, summary["labelled_count"])
        self.assertEqual("no_labels", summary["label_rate_status"])
        self.assertIsNone(summary["label_rate"])

    def test_summary_is_denied_for_a_purpose_the_pack_does_not_approve(self) -> None:
        lma = self.harness(site=site_config(reporting_purpose="marketing"))
        summary = lma.get("/api/v1/site/summary").json()
        self.assertEqual("denied", summary["label_rate_status"])
        self.assertIsNone(summary["label_rate"])
        self.assertTrue(summary["planner_reason"])

    def test_summary_withholds_the_rate_when_the_planner_cannot_be_run(self) -> None:
        lma = self.harness()
        with patch.dict(os.environ, {"DAGENTSC_BIN": str(REPO_ROOT / "no-such-dagentsc")}):
            summary = lma.get("/api/v1/site/summary").json()
        self.assertEqual("withheld", summary["label_rate_status"])
        self.assertIsNone(summary["label_rate"])
        # A withheld status is not kept: the next call asks the planner again.
        self.assertEqual("released", lma.get("/api/v1/site/summary").json()["label_rate_status"])

    def test_summary_and_info_write_no_audit_record(self) -> None:
        lma = self.harness()
        lma.get("/api/v1/site/summary")
        lma.get("/api/v1/site")
        self.assertEqual(0, lma.get("/api/v1/governance/audit:summary").json()["record_count"])

    def test_profile_tracks_the_contract_fields_by_default(self) -> None:
        lma = self.harness()
        profile = lma.get("/api/v1/site/profile").json()
        self.assertEqual(30, profile["record_count"])
        self.assertEqual(["group", "label", "x1", "x2"], profile["tracked_fields"])
        self.assertEqual(30, profile["fields"][TOY_LABEL_FIELD]["present"])

    def test_profile_fields_come_from_the_site_configuration(self) -> None:
        lma = self.harness(site=site_config(profile_fields=("x1", TOY_LABEL_FIELD)))
        self.assertEqual(["label", "x1"], lma.get("/api/v1/site/profile").json()["tracked_fields"])

    def test_the_profile_is_rebuilt_when_the_source_version_changes(self) -> None:
        lma = self.harness()
        before = lma.get("/api/v1/site").json()["source_version"]
        self.assertEqual(30, lma.get("/api/v1/site/profile").json()["record_count"])
        lma.source.append({"x1": 0.5, "x2": 0.5, "group": "a", TOY_LABEL_FIELD: True})
        # Within the staleness window the LMA keeps what it read.
        self.assertEqual(30, lma.get("/api/v1/site/profile").json()["record_count"])
        refreshed = lma.post("/api/v1/site/source:refresh")
        self.assertEqual(200, refreshed.status_code, refreshed.text)
        state = refreshed.json()
        self.assertEqual(31, state["record_count"])
        self.assertNotEqual(before, state["source_version"])
        self.assertEqual(31, lma.get("/api/v1/site/profile").json()["record_count"])
        self.assertEqual(31, lma.get("/api/v1/site/summary").json()["labelled_count"])

    def test_with_no_staleness_window_a_change_is_seen_at_once(self) -> None:
        lma = self.harness(settings=lma_settings(source_max_staleness_ms=0))
        lma.get("/api/v1/site/profile")
        lma.source.append({"x1": 0.5, "x2": 0.5, "group": "a"})
        self.assertEqual(31, lma.get("/api/v1/site/profile").json()["record_count"])

    def test_probe_reports_what_the_guard_releases_and_never_rows(self) -> None:
        lma = self.harness()
        response = lma.post("/api/v1/governance/restrictions:probe", json=probe_body())
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual(
            {
                "request_id",
                "boundary",
                "permitted",
                "plan",
                "released_count",
                "released_fields",
                "withheld_fields",
                "transformed_fields",
                "audit_id",
                "message",
            },
            set(body),
        )
        self.assertTrue(body["permitted"])
        self.assertEqual(5, body["released_count"])
        self.assertTrue(set(body["released_fields"]) <= {"x1", "x2", "group", TOY_LABEL_FIELD})
        for record in lma.source.records()[:5]:
            self.assertNotIn(str(record["x1"]), response.text)
        audit = lma.get("/api/v1/governance/audit:summary", headers=COORDINATOR_HEADERS).json()
        self.assertEqual({"chain_intact": True, "record_count": 1}, audit)
        self.assertTrue(body["request_id"].startswith(f"probe:{SITE}:"))

    def test_probe_uses_the_site_classification_and_purposes(self) -> None:
        lma = self.harness()
        body = lma.post(
            "/api/v1/governance/restrictions:probe", json=probe_body(declared_purpose="marketing")
        ).json()
        self.assertFalse(body["permitted"])
        self.assertEqual(0, body["released_count"])

    def test_probe_refuses_a_body_that_names_its_own_policy_or_too_many_records(self) -> None:
        lma = self.harness()
        for body in (
            probe_body(classification=TOY_CLASSIFICATION.model_dump(mode="json")),
            probe_body(approved_purposes=["marketing"]),
            probe_body(record_limit=21),
            probe_body(record_limit=-1),
        ):
            with self.subTest(body=sorted(body)):
                response = lma.post("/api/v1/governance/restrictions:probe", json=body)
                self.assertEqual(422, response.status_code, response.text)
        self.assertEqual(0, lma.get("/api/v1/governance/audit:summary").json()["record_count"])

    def test_probe_without_a_cohort_uses_the_records_it_read(self) -> None:
        lma = self.harness()
        small = lma.post(
            "/api/v1/governance/restrictions:probe", json=probe_body(granularity="table", record_limit=3)
        ).json()
        self.assertFalse(small["permitted"])
        self.assertEqual(0, small["released_count"])

    def test_model_in_use_comes_from_the_gma_and_is_checked(self) -> None:
        lma = self.harness()
        first = lma.get("/api/v1/site/model")
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(
            {
                "version": "toy-linear-v2",
                "kind": "toy_linear",
                "digest": LINEAR.parameter_digest(TRAINED),
                "status": "released",
                "params": TRAINED,
            },
            first.json(),
        )
        second = lma.get("/api/v1/site/model")
        self.assertEqual(first.json(), second.json())
        self.assertEqual(2, lma.gma.released_requests)
        self.assertEqual(1, lma.gma.not_modified, "the second read must send the known digest")

    def test_a_released_model_whose_parameters_do_not_match_is_refused(self) -> None:
        tampered = linear_version(digest=LINEAR.parameter_digest(INITIAL))
        lma = self.harness(gma=FakeGma(released=tampered))
        for path in ("/api/v1/site/model", "/api/v1/site/model:evaluate"):
            with self.subTest(path=path):
                response = lma.client.request(
                    "GET" if path.endswith("model") else "POST", path, headers=SITE_HEADERS
                )
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual("conflict", response.json()["code"])

    def test_no_released_model_answers_404(self) -> None:
        lma = self.harness(gma=FakeGma(released=None))
        response = lma.get("/api/v1/site/model")
        self.assertEqual(404, response.status_code, response.text)
        self.assertEqual("not_found", response.json()["code"])

    def test_an_lma_without_a_gma_answers_503(self) -> None:
        lma = self.harness(with_network=False)
        response = lma.get("/api/v1/site/model")
        self.assertEqual(503, response.status_code, response.text)
        self.assertEqual("agent_unavailable", response.json()["code"])

    def test_evaluate_measures_the_released_model_on_the_site_records(self) -> None:
        records = toy_records(SITE, labelled_share=0.5)
        lma = self.harness(records=records)
        response = lma.post("/api/v1/site/model:evaluate")
        self.assertEqual(200, response.status_code, response.text)
        expected = LINEAR.evaluate(
            TRAINED, [record for record in records if isinstance(record.get(TOY_LABEL_FIELD), bool)], TOY_LABEL_FIELD
        )
        self.assertEqual(
            {"version": "toy-linear-v2", "labelled_count": 15, "metrics": expected}, response.json()
        )

    def test_registration_carries_the_current_cohort_size(self) -> None:
        lma = self.harness()
        response = lma.get("/api/v1/federation/registration", headers=COORDINATOR_HEADERS)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            {
                "site_id": SITE,
                "capabilities": list(CAPABILITIES),
                "feature_contract_version": "toy-v1",
                "approved_conditions": [TOY_CONDITION_ID],
                "policy_version": "toy-policy-v1",
                "cohort_size": 30,
                "enrolled": True,
            },
            response.json(),
        )
        lma.source.append({"x1": 0.1, "x2": 0.1, "group": "a"})
        # Registration reads the source's version now, not after the staleness window.
        self.assertEqual(31, lma.get("/api/v1/federation/registration", headers=COORDINATOR_HEADERS).json()["cohort_size"])

    def test_two_lmas_in_one_process_share_nothing(self) -> None:
        """One process can serve one LMA per site: each keeps its own records, Guard and audit chain."""
        first = self.harness()
        second_records = toy_records("site-b", count=12)
        second = self.harness(
            records=second_records,
            site=site_config(site_id="site-b"),
            settings=lma_settings(site_token="lma-site-token-for-site-b-dddddddd", coordinator_token="lma-coordinator-token-b-eeeeeeeeee"),
        )
        first.post("/api/v1/governance/restrictions:probe", json=probe_body())
        self.assertEqual(SITE, first.get("/api/v1/site").json()["site_id"])
        b_headers = {"Authorization": "Bearer lma-site-token-for-site-b-dddddddd"}
        self.assertEqual("site-b", second.get("/api/v1/site", headers=b_headers).json()["site_id"])
        self.assertEqual(12, second.get("/api/v1/site", headers=b_headers).json()["record_count"])
        self.assertEqual(1, first.get("/api/v1/governance/audit:summary").json()["record_count"])
        self.assertEqual(0, second.get("/api/v1/governance/audit:summary", headers=b_headers).json()["record_count"])
        # Each LMA accepts only its own tokens.
        self.assertEqual(401, second.get("/api/v1/site").status_code)

    def test_job_log_starts_empty_and_checks_its_limit(self) -> None:
        lma = self.harness()
        self.assertEqual([], lma.get("/api/v1/federation/jobs").json())
        for limit in (0, 201):
            with self.subTest(limit=limit):
                self.assertEqual(422, lma.get("/api/v1/federation/jobs", params={"limit": limit}).status_code)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class TokenScopeTests(unittest.TestCase):
    """Each route lets in only the scopes in plan section 6.8."""

    #: (method, versioned path, scopes allowed, whether a short form exists). The
    #: short form is the same path without /api/v1.
    ROUTES = (
        ("GET", "/api/v1/site", {"site", "coordinator"}, True),
        ("GET", "/api/v1/site/summary", {"site", "coordinator"}, True),
        ("GET", "/api/v1/site/profile", {"site"}, True),
        ("POST", "/api/v1/site/source:refresh", {"site"}, True),
        ("GET", "/api/v1/site/model", {"site"}, True),
        ("POST", "/api/v1/site/model:evaluate", {"site"}, True),
        ("GET", "/api/v1/governance/audit:summary", {"site", "coordinator"}, True),
        ("GET", "/api/v1/federation/registration", {"coordinator"}, True),
        ("GET", "/api/v1/federation/jobs", {"site"}, True),
        ("GET", "/api/v1/sources", {"site"}, False),
        ("GET", "/api/v1/governance/audit", {"site"}, True),
        ("GET", "/api/v1/governance/classifications", {"site"}, True),
        ("GET", "/api/v1/extensions", {"site"}, True),
        ("GET", "/api/v1/model-jobs", {"site"}, False),
    )

    def setUp(self) -> None:
        self.lma = LmaHarness()
        self.addCleanup(self.lma.close)

    def test_each_route_lets_in_only_its_scopes(self) -> None:
        tokens = {"site": SITE_HEADERS, "coordinator": COORDINATOR_HEADERS}
        for method, path, allowed, has_short in self.ROUTES:
            for short in (False, True) if has_short else (False,):
                route = path.replace("/api/v1", "", 1) if short else path
                for scope, headers in tokens.items():
                    with self.subTest(route=route, scope=scope):
                        response = self.lma.client.request(method, route, headers=headers)
                        if scope in allowed:
                            self.assertEqual(200, response.status_code, response.text)
                        else:
                            self.assertEqual(403, response.status_code, response.text)
                            self.assertEqual("forbidden", response.json()["code"])

    def test_a_missing_or_unknown_token_answers_401(self) -> None:
        for headers in ({}, {"Authorization": "Bearer not-a-token-of-this-lma"}, {"Authorization": "Basic abc"}):
            with self.subTest(headers=headers):
                response = self.lma.client.get("/api/v1/site", headers=headers)
                self.assertEqual(401, response.status_code, response.text)
                self.assertEqual("unauthorized", response.json()["code"])
                self.assertEqual("Bearer", response.headers.get("www-authenticate"))

    def test_job_routes_take_only_the_coordinator_token(self) -> None:
        for path in ("/api/v1/federation/jobs:offer", "/api/v1/federation/jobs:execute", "/federation/jobs:offer"):
            with self.subTest(path=path):
                response = self.lma.client.post(path, headers=SITE_HEADERS, json={})
                self.assertEqual(403, response.status_code, response.text)

    def test_probe_takes_only_the_site_token(self) -> None:
        response = self.lma.client.post(
            "/api/v1/governance/restrictions:probe", headers=COORDINATOR_HEADERS, json=probe_body()
        )
        self.assertEqual(403, response.status_code, response.text)

    def test_health_needs_no_token(self) -> None:
        self.assertEqual(200, self.lma.client.get("/api/v1/health").status_code)
        self.assertEqual(200, self.lma.client.get("/health").status_code)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RoutePairTests(unittest.TestCase):
    """Every new route has a short path and an /api/v1 path that answer the same."""

    def test_get_pairs_answer_the_same(self) -> None:
        lma = LmaHarness()
        self.addCleanup(lma.close)
        for path in (
            "/api/v1/site",
            "/api/v1/site/summary",
            "/api/v1/site/profile",
            "/api/v1/site/model",
            "/api/v1/governance/audit:summary",
            "/api/v1/federation/jobs",
        ):
            with self.subTest(path=path):
                versioned = lma.get(path)
                short = lma.get(path.replace("/api/v1", "", 1))
                self.assertEqual(200, versioned.status_code, versioned.text)
                self.assertEqual(versioned.status_code, short.status_code)
                self.assertEqual(versioned.json(), short.json())
        registration = [
            lma.get(path, headers=COORDINATOR_HEADERS).json()
            for path in ("/api/v1/federation/registration", "/federation/registration")
        ]
        self.assertEqual(registration[0], registration[1])

    def test_post_pairs_answer_the_same(self) -> None:
        lma = LmaHarness()
        self.addCleanup(lma.close)
        for path in ("/api/v1/site/source:refresh", "/api/v1/site/model:evaluate"):
            with self.subTest(path=path):
                versioned = lma.post(path)
                short = lma.post(path.replace("/api/v1", "", 1))
                self.assertEqual(200, versioned.status_code, versioned.text)
                self.assertEqual(versioned.json(), short.json())
        probes = [
            lma.post(path, json=probe_body()).json()
            for path in ("/api/v1/governance/restrictions:probe", "/governance/restrictions:probe")
        ]
        for probe in probes:
            probe.pop("request_id")
            probe.pop("audit_id")
            probe["plan"].pop("request_id")
            # The plan's obligations name the request id too.
            probe["plan"].pop("obligations")
        self.assertEqual(probes[0], probes[1])

    def test_inventory_pairs_each_new_route_with_its_short_form(self) -> None:
        app = create_app(Settings())
        handlers = {getattr(route, "name", None): route.path for route in app.routes}
        for name in (
            "site_info",
            "site_summary",
            "site_profile",
            "refresh_site_source",
            "site_model",
            "evaluate_site_model",
            "probe_restriction",
            "governance_audit_summary",
            "federation_registration",
            "offer_job",
            "execute_job",
            "list_jobs",
        ):
            with self.subTest(handler=name):
                self.assertIn(name, handlers)
                self.assertIn(f"{name}_v1", handlers)
                self.assertEqual("/api/v1" + handlers[name], handlers[f"{name}_v1"])


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SiteModeLockDownTests(unittest.TestCase):
    """A site's records come from its configuration, and its Guard plans against its own policy."""

    def setUp(self) -> None:
        self.lma = LmaHarness()
        self.addCleanup(self.lma.close)

    def assert_refused(self, response: Any, status: int = 403, code: str = "forbidden") -> None:
        self.assertEqual(status, response.status_code, response.text)
        self.assertEqual(code, response.json()["code"])

    def test_a_request_cannot_register_a_database_source(self) -> None:
        response = self.lma.post(
            "/api/v1/sources",
            json={
                "source_id": "other-db",
                "kind": "postgres",
                "connection_ref": {"connection_id": "x", "options": {"role": "site_b"}},
                "selection": {"sql": "select * from site_b.records"},
            },
        )
        self.assert_refused(response)
        self.assertEqual([], self.lma.get("/api/v1/sources").json())

    def test_a_request_cannot_set_a_role_on_an_inline_source(self) -> None:
        response = self.lma.post(
            "/api/v1/sources",
            json={
                "source_id": "inline-with-role",
                "kind": "inline",
                "connection_ref": {"connection_id": "x", "options": {"role": "site_b", "password": "s3cret-pw"}},
                "selection": {"records": [{"x1": 1.0}]},
            },
        )
        self.assert_refused(response)
        self.assertNotIn("s3cret-pw", response.text)

    def test_an_inline_source_registers_and_its_options_come_back_redacted(self) -> None:
        response = self.lma.post(
            "/api/v1/sources",
            json={
                "source_id": "inline-ok",
                "kind": "inline",
                "connection_ref": {"connection_id": "x", "options": {"rows": [{"x1": 1.0}]}},
                "selection": {"records": [{"x1": 1.0}]},
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        stored = self.lma.get("/api/v1/sources/inline-ok").json()
        self.assertNotEqual([{"x1": 1.0}], stored["connection_ref"]["options"]["rows"])

    def test_dataset_requests_cannot_embed_a_source(self) -> None:
        embedded = {
            "source_id": "other-db",
            "kind": "postgres",
            "connection_ref": {"connection_id": "x"},
            "selection": {"table": "records"},
        }
        for path, body in (
            ("/api/v1/datasets:profile", {"scope_id": SITE, "dataset": {"source": embedded}}),
            ("/datasets/profile", {"scope_id": SITE, "dataset": {"source": embedded}}),
            (
                "/api/v1/model-jobs",
                {"scope_id": SITE, "model_family": "random_forest", "dataset": {"source": embedded}},
            ),
            ("/models/run", {"scope_id": SITE, "model_family": "random_forest", "dataset": {"source": embedded}}),
        ):
            with self.subTest(path=path):
                self.assert_refused(self.lma.post(path, json=body))

    def test_inline_records_can_still_be_profiled(self) -> None:
        response = self.lma.post(
            "/api/v1/datasets:profile", json={"scope_id": SITE, "records": [{"x1": 1.0}, {"x1": 2.0}]}
        )
        self.assertEqual(200, response.status_code, response.text)

    def test_the_guard_refuses_a_policy_that_is_not_the_site_policy(self) -> None:
        weak = TOY_CLASSIFICATION.model_copy(
            update={"field_sensitivity": {name: "low" for name in TOY_CLASSIFICATION.field_sensitivity}}
        )
        unknown = TOY_CLASSIFICATION.model_copy(update={"classification_id": "caller-chosen"})
        request = {
            "request_id": "strict-1",
            "boundary": "before_read",
            "requester": coordinator_requester().model_dump(mode="json"),
            "requested_fields": ["x1"],
            "granularity": "row",
            "declared_purpose": TOY_CONDITION_ID,
            "approved_purposes": [TOY_CONDITION_ID],
        }
        for classification in (weak, unknown):
            with self.subTest(classification=classification.classification_id):
                body = {**request, "classification": classification.model_dump(mode="json")}
                self.assert_refused(
                    self.lma.post("/api/v1/governance/restrictions:plan", json=body), 409, "conflict"
                )
                self.assert_refused(
                    self.lma.post("/api/v1/governance/restrictions:enforce", json={"request": body}), 409, "conflict"
                )
        allowed = {**request, "classification": TOY_CLASSIFICATION.model_dump(mode="json")}
        self.assertEqual(200, self.lma.post("/api/v1/governance/restrictions:plan", json=allowed).status_code)

    def test_a_runtime_classification_cannot_replace_the_extension_policy(self) -> None:
        weak = TOY_CLASSIFICATION.model_copy(update={"minimum_cohort": 1})
        response = self.lma.client.put(
            f"/api/v1/governance/classifications/{weak.classification_id}",
            headers=SITE_HEADERS,
            json=weak.model_dump(mode="json"),
        )
        self.assert_refused(response, 409, "conflict")


if __name__ == "__main__":
    unittest.main()
