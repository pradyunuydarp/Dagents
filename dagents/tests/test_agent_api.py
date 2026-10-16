"""API integration tests for the LMA and GMA FastAPI surfaces.

The first two classes drive the routes both agents had before sites existed.
The others check conventions that span both agents:

- an agent built with no settings keeps the behaviour it had before sites,
  tokens and the HTTP engine existed;
- every versioned route has a short twin, and a study run through the short
  forms equals the same study run through the versioned forms;
- every route of both agents lets in exactly the token scopes it documents.
"""

from __future__ import annotations

import time
import unittest
from typing import Any
from uuid import uuid4

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Match

from dagents.gma.app_factory import create_app as create_gma
from dagents.gma.config import Settings as GmaSettings
from dagents.gma.main import app as gma_app
from dagents.lma.app_factory import create_app as create_lma
from dagents.lma.config import Settings as LmaSettings
from dagents.lma.main import app as lma_app
from dagents.tests.fixtures.gma_fixtures import ADMIN_TOKEN, COORDINATOR_TOKENS, SITE_TOKENS
from dagents.tests.test_agent_network_flow import STAFF_TOKENS, build_in_process, comparable, run_study
from dagents.tests.test_governance import dagentsc_available


def inline_source_payload(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "kind": "inline",
        "selection": {
            "records": [
                {"value": 1.2, "score": 0.1, "label": 0},
                {"value": 2.4, "score": 0.2, "label": 1},
                {"value": 2.8, "score": 0.3, "label": 1},
                {"value": 1.0, "score": 0.0, "label": 0},
            ]
        },
        "format": "rows",
        "schema_hint": {"value": "float", "score": "float", "label": "integer"},
        "batching": {"batch_size": 2, "max_records": 4},
        "checkpoint": {},
        "options": {},
    }


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lma = TestClient(lma_app)
        self.gma = TestClient(gma_app)
        self.agent_id = f"lma-{uuid4().hex[:8]}"
        self.workspace_id = f"workspace-{uuid4().hex[:8]}"
        self.source_id = f"inline-source-{uuid4().hex[:8]}"
        self.registration = {
            "agent": {
                "agent_id": self.agent_id,
                "workspace_id": self.workspace_id,
                "name": "local-monitor",
                "agent_type": "LMA",
            },
            "scope": {"tenant": self.workspace_id},
            "version": "0.1.0",
            "capabilities": ["monitoring", "profiling", "classification"],
        }
        self.bundle = {
            "agent": self.registration["agent"],
            "bundle_id": f"bundle-{uuid4().hex[:6]}",
            "bundle_version": "1.0.0",
            "bundle_uri": "s3://dagents/test/bundle",
        }
        self.source = inline_source_payload(self.source_id)

    def test_lma_bundle_source_profile_and_model_endpoints(self) -> None:
        health = self.lma.get("/api/v1/health")
        self.assertEqual(health.status_code, 200)

        deploy = self.lma.post("/bundles/deploy", json=self.bundle)
        self.assertEqual(deploy.status_code, 200)
        self.assertTrue(deploy.json()["accepted"])

        run = self.lma.post(
            "/run",
            json={
                "correlation_id": f"corr-{uuid4().hex[:6]}",
                "bundle_id": self.bundle["bundle_id"],
                "bundle_version": self.bundle["bundle_version"],
                "scope": {"tenant": self.workspace_id},
                "metadata": {"workspace_id": self.workspace_id},
            },
        )
        self.assertEqual(run.status_code, 200)
        run_payload = run.json()
        self.assertEqual(run_payload["run"]["bundle_id"], self.bundle["bundle_id"])
        self.assertGreaterEqual(run_payload["run"]["metrics_emitted"], 1)

        register_source = self.lma.post("/api/v1/sources", json=self.source)
        self.assertEqual(register_source.status_code, 200)
        self.assertEqual(register_source.json()["source_id"], self.source_id)

        validate_source = self.lma.post(f"/api/v1/sources/{self.source_id}:validate")
        self.assertEqual(validate_source.status_code, 200)
        self.assertTrue(validate_source.json()["valid"])

        profile = self.lma.post(
            "/api/v1/datasets:profile",
            json={
                "scope_id": self.source_id,
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["value", "score"],
                "label_field": "label",
                "batch_size": 2,
            },
        )
        self.assertEqual(profile.status_code, 200)
        profile_payload = profile.json()
        self.assertEqual(profile_payload["record_count"], 4)
        self.assertEqual(profile_payload["partition_count"], 2)

        model = self.lma.post(
            "/api/v1/model-jobs",
            json={
                "scope_id": self.source_id,
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["value", "score"],
                "label_field": "label",
                "task_type": "classification",
                "model_family": "random_forest",
                "hyperparameters": {"batch_size": 2},
                "artifact_prefix": "artifacts/tests",
                "model_version": "api-test",
            },
        )
        self.assertEqual(model.status_code, 202)
        model_payload = model.json()
        self.assertEqual(model_payload["run"]["scope_kind"], "source")
        self.assertEqual(model_payload["run"]["record_count"], 4)

        listed = self.lma.get("/api/v1/model-jobs")
        self.assertEqual(listed.status_code, 200)
        runs = listed.json()
        self.assertTrue(any(item["scope_id"] == self.source_id for item in runs))

    def test_gma_control_plane_and_assimilated_model_endpoints(self) -> None:
        health = self.gma.get("/api/v1/health")
        self.assertEqual(health.status_code, 200)

        registration = self.gma.put(f"/api/v1/agents/{self.agent_id}/registration", json=self.registration)
        self.assertEqual(registration.status_code, 200)
        self.assertTrue(registration.json()["accepted"])

        desired = self.gma.put(
            f"/api/v1/agents/{self.agent_id}/desired-deployment",
            json={
                "agent_id": self.agent_id,
                "bundle_id": self.bundle["bundle_id"],
                "bundle_version": "1.2.0",
                "bundle_uri": self.bundle["bundle_uri"],
                "config": {"tenant": self.workspace_id},
            },
        )
        self.assertEqual(desired.status_code, 200)
        desired_payload = desired.json()
        self.assertEqual(desired_payload["bundle_version"], "1.2.0")

        heartbeat = self.gma.post(
            f"/api/v1/agents/{self.agent_id}/heartbeats",
            json={
                "agent": self.registration["agent"],
                "status": "ACTIVE",
                "timestamp": int(time.time()),
            },
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(heartbeat.json()["desired_state"], "SYNC_REQUIRED")

        sync = self.gma.post(
            f"/api/v1/agents/{self.agent_id}/deployment-sync",
            json={
                "agent": self.registration["agent"],
                "bundle_id": self.bundle["bundle_id"],
                "bundle_version": self.bundle["bundle_version"],
            },
        )
        self.assertEqual(sync.status_code, 200)
        sync_payload = sync.json()
        self.assertFalse(sync_payload["up_to_date"])
        self.assertEqual(sync_payload["desired_bundle_version"], "1.2.0")

        telemetry = self.gma.post(
            f"/api/v1/agents/{self.agent_id}/telemetry",
            json={
                "agent": self.registration["agent"],
                "metrics": [
                    {"key": "local_checks_total", "value": 2, "observed_at": int(time.time())},
                    {"key": "records_scanned_total", "value": 4, "observed_at": int(time.time())},
                ],
                "pointers": {"tenant": self.workspace_id},
            },
        )
        self.assertEqual(telemetry.status_code, 200)
        self.assertTrue(telemetry.json()["ack"])

        agents = self.gma.get("/api/v1/agents")
        self.assertEqual(agents.status_code, 200)
        self.assertTrue(any(item["agent"]["agent_id"] == self.agent_id for item in agents.json()))

        agent = self.gma.get(f"/api/v1/agents/{self.agent_id}")
        self.assertEqual(agent.status_code, 200)
        self.assertEqual(agent.json()["agent"]["workspace_id"], self.workspace_id)

        register_source = self.gma.post("/api/v1/sources", json=self.source)
        self.assertEqual(register_source.status_code, 200)

        profile = self.gma.post(
            "/api/v1/datasets:profile",
            json={
                "scope_id": f"assim-{self.source_id}",
                "scope_kind": "assimilated",
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["value", "score"],
                "label_field": "label",
                "batch_size": 2,
            },
        )
        self.assertEqual(profile.status_code, 200)
        profile_payload = profile.json()
        self.assertEqual(profile_payload["scope_kind"], "assimilated")
        self.assertEqual(profile_payload["record_count"], 4)

        model = self.gma.post(
            "/api/v1/model-jobs",
            json={
                "scope_id": f"assim-{self.source_id}",
                "scope_kind": "assimilated",
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["value", "score"],
                "label_field": "label",
                "task_type": "classification",
                "model_family": "xgboost",
                "hyperparameters": {"batch_size": 2},
                "artifact_prefix": "artifacts/tests",
                "model_version": "api-test",
            },
        )
        self.assertEqual(model.status_code, 202)
        model_payload = model.json()
        self.assertEqual(model_payload["run"]["scope_kind"], "assimilated")
        self.assertEqual(model_payload["run"]["record_count"], 4)

        overview = self.gma.get("/overview")
        self.assertEqual(overview.status_code, 200)
        overview_payload = overview.json()
        self.assertGreaterEqual(overview_payload["total_agents"], 1)
        self.assertGreaterEqual(overview_payload["telemetry_events"], 1)


# ---------------------------------------------------------------------------
# Agents built with no settings
# ---------------------------------------------------------------------------


class UnconfiguredAgentTests(unittest.TestCase):
    """An LMA and a GMA built from empty settings behave as they did before sites and tokens existed."""

    def test_an_lma_with_no_site_runs_open_and_its_site_routes_answer_404(self) -> None:
        client = TestClient(create_lma(LmaSettings.from_env({})))
        self.assertEqual(client.get("/api/v1/health").status_code, 200)
        source = inline_source_payload(f"inline-{uuid4().hex[:8]}")
        self.assertEqual(client.post("/api/v1/sources", json=source).status_code, 200)
        self.assertEqual(client.get("/api/v1/governance/classifications").status_code, 200)
        for path in ("/site", "/site/summary", "/federation/registration", "/federation/jobs"):
            for form in (path, f"/api/v1{path}"):
                with self.subTest(path=form):
                    response = client.get(form)
                    self.assertEqual(response.status_code, 404, response.text)
                    self.assertEqual(response.json()["code"], "not_found")

    def test_a_gma_with_no_settings_runs_open_with_the_simulator(self) -> None:
        client = TestClient(create_gma(GmaSettings.from_env({})))
        self.assertEqual(client.get("/api/v1/health").json()["engine"], "in-process-simulator")
        source = inline_source_payload(f"inline-{uuid4().hex[:8]}")
        self.assertEqual(client.post("/api/v1/sources", json=source).status_code, 200)
        listing = client.get("/api/v1/model-versions")
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json(), {"model_in_use": None, "versions": []})
        # No LMA is configured, so there is no site to refresh or summarise.
        self.assertEqual(client.post("/api/v1/federation/sites:refresh").json(), [])
        self.assertEqual(client.get("/api/v1/federation/sites/summaries").json(), [])


# ---------------------------------------------------------------------------
# Short and versioned route pairs
# ---------------------------------------------------------------------------

#: Routes the agents had before the pairing rule, which have one form only.
#: The list may shrink; a new route must come with both forms.
UNPAIRED_ROUTES = {
    "lma": {
        "list_bundles",
        "deploy_bundle",
        "list_runs",
        "register_source",
        "list_sources",
        "get_source",
        "validate_source",
        "list_telemetry",
        "run_monitoring",
    },
    "gma": {
        "list_deployments",
        "dispatch_run",
        "list_dispatched_runs",
        "get_agent_v1",
        "register_source",
        "list_sources",
        "get_source",
        "validate_source",
        "recent_telemetry",
        "telemetry_summary",
        "overview",
    },
}

#: The route pairs the site and federation work added. Their short path is the
#: versioned path without ``/api/v1``.
NEW_PAIRS = {
    "lma": {
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
        "run_job",
        "list_jobs",
    },
    "gma": {
        "refresh_sites",
        "site_summaries",
        "upload_update",
        "discard_round",
        "governance_audit_summary",
        "list_model_versions",
        "released_model_version",
        "get_model_version",
        "register_model_version",
        "approve_model_version",
    },
}

#: Pairs whose behaviour the study below must use through both forms: the new
#: pairs and the round routes they changed. The GMA's HTTP engine sends each
#: site one ``jobs:run`` call, so the study does not reach ``jobs:offer`` and
#: ``jobs:execute``; ``test_lma_federation_jobs`` compares their two forms.
STUDY_PAIRS = {
    "lma": NEW_PAIRS["lma"] - {"offer_job", "execute_job"},
    "gma": NEW_PAIRS["gma"]
    | {
        "dispatch_round",
        "list_rounds",
        "get_round",
        "plan_round",
        "submit_result",
        "round_readiness",
        "aggregate_round",
        "evaluate_release",
    },
}


#: Statuses that differ between the two forms of a route since before the
#: pairing rule, by ``(method, versioned path)``: the short form of a round
#: dispatch answers 200 and the versioned form 202. Clients rely on both.
LEGACY_STATUS = {("POST", "/api/v1/federation/rounds"): 202}


def api_routes(app: Any) -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute)]


def route_pairs(app: Any) -> list[tuple[APIRoute, APIRoute]]:
    """``(versioned, short)`` for each route named ``<handler>_v1`` whose short twin ``<handler>`` exists."""
    by_name = {route.name: route for route in api_routes(app)}
    return [
        (route, by_name[route.name[: -len("_v1")]])
        for route in api_routes(app)
        if route.name.endswith("_v1") and route.name[: -len("_v1")] in by_name
    ]


class ShortForms:
    """An ASGI wrapper that sends each versioned call to its short twin, and records the twins it used."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._pairs = route_pairs(app)
        self.used: set[str] = set()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            for versioned, short in self._pairs:
                match, child = versioned.matches(scope)
                if match is Match.FULL:
                    path = str(short.url_path_for(short.name, **child["path_params"]))
                    scope = dict(scope, path=path, raw_path=path.encode("ascii"))
                    self.used.add(short.name)
                    break
        await self._app(scope, receive, send)


class RoutePairConventionTests(unittest.TestCase):
    """CLAUDE.md: an LMA or GMA endpoint has a short path and an /api/v1 path that call the same handler."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.network, cls.gma_app, _, lmas = build_in_process(wrap=lambda app: app)
        cls.addClassCleanup(cls.network.close)
        cls.apps = {"lma": next(iter(lmas.values())), "gma": cls.gma_app}

    def test_only_the_older_routes_have_a_single_form(self) -> None:
        for agent, app in self.apps.items():
            names = {route.name for route in api_routes(app)}
            paired = {name for versioned, short in route_pairs(app) for name in (versioned.name, short.name)}
            with self.subTest(agent=agent):
                self.assertEqual(names - paired, UNPAIRED_ROUTES[agent])

    def test_the_new_pairs_share_their_method_and_path(self) -> None:
        for agent, app in self.apps.items():
            pairs = {short.name: (versioned, short) for versioned, short in route_pairs(app)}
            self.assertTrue(NEW_PAIRS[agent] <= set(pairs), NEW_PAIRS[agent] - set(pairs))
            for name, (versioned, short) in pairs.items():
                with self.subTest(agent=agent, route=name):
                    self.assertTrue(versioned.path.startswith("/api/v1/"))
                    self.assertFalse(short.path.startswith("/api/v1/"))
                    if name in NEW_PAIRS[agent]:
                        self.assertEqual(versioned.methods, short.methods)
                        self.assertEqual(versioned.path, f"/api/v1{short.path}")


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RoutePairStudyTests(unittest.TestCase):
    """The study of ``test_agent_network_flow`` gives the same answers through either form of every route.

    One run sends every call, including the GMA's calls to the LMAs and the
    LMAs' uploads to the GMA, to the short forms; the other to the versioned
    forms. Both use the same round ids, so every number must be equal.
    """

    @classmethod
    def setUpClass(cls) -> None:
        network, _, cls.gma_short, cls.lma_short = build_in_process(wrap=ShortForms)
        try:
            cls.short = run_study(network, "pairs")
        finally:
            network.close()
        network, _, _, _ = build_in_process(wrap=lambda app: app)
        try:
            cls.versioned = run_study(network, "pairs")
        finally:
            network.close()

    def test_the_study_used_every_new_pair_through_its_short_form(self) -> None:
        used = {"gma": self.gma_short.used, "lma": set().union(*(lma.used for lma in self.lma_short.values()))}
        for agent in ("lma", "gma"):
            with self.subTest(agent=agent):
                self.assertEqual(STUDY_PAIRS[agent] - used[agent], set())

    def test_both_forms_give_the_same_study(self) -> None:
        short, versioned = comparable(self.short), comparable(self.versioned)
        self.assertEqual(sorted(short), sorted(versioned))
        for key in short:
            with self.subTest(part=key):
                if key == "calls":
                    # The one status the two forms have always answered differently.
                    short[key] = [
                        call[:4] + (LEGACY_STATUS.get(call[2:4], call[4]),) for call in short[key]
                    ]
                self.assertEqual(short[key], versioned[key])


# ---------------------------------------------------------------------------
# Token scopes on every route
# ---------------------------------------------------------------------------

#: The 403 details the token check answers with. A 403 with another detail
#: comes from the route itself, after the token was let in.
TOKEN_REFUSALS = {"this token may not call this route", "this token may act only for its own site"}

#: What each caller holds: tokens for the LMA of site-a and for the GMA.
CALLERS = {
    "nobody": None,
    "lma staff": STAFF_TOKENS["site-a"],
    "lma coordinator": COORDINATOR_TOKENS["site-a"],
    "gma admin": ADMIN_TOKEN,
    "gma site-a": SITE_TOKENS["site-a"],
}

#: The callers each route lets in, by its short handler name. Every other LMA
#: route lets in the LMA's staff; every other GMA route, the GMA's admin.
LMA_SCOPES = {
    "health": set(CALLERS),
    "site_info": {"lma staff", "lma coordinator"},
    "site_summary": {"lma staff", "lma coordinator"},
    "governance_audit_summary": {"lma staff", "lma coordinator"},
    "federation_registration": {"lma coordinator"},
    "offer_job": {"lma coordinator"},
    "execute_job": {"lma coordinator"},
    "run_job": {"lma coordinator"},
}
GMA_SCOPES = {
    "health": set(CALLERS),
    "register": {"gma admin", "gma site-a"},
    "heartbeat": {"gma admin", "gma site-a"},
    "telemetry": {"gma admin", "gma site-a"},
    "sync_deployment": {"gma admin", "gma site-a"},
    "released_model_version": {"gma admin", "gma site-a"},
    "upload_update": {"gma site-a"},
}

PATH_VALUES = {
    "site_id": "site-a",
    "round_id": "round-1",
    "agent_id": "agent-1",
    "source_id": "source-1",
    "classification_id": "classification-1",
    "version": "version-1",
}


def expected_callers(agent: str, name: str) -> set[str]:
    base = name[: -len("_v1")] if name.endswith("_v1") else name
    if agent == "lma":
        return LMA_SCOPES.get(base, {"lma staff"})
    return GMA_SCOPES.get(base, {"gma admin"})


class TokenScopeTests(unittest.TestCase):
    """Every route of both agents lets in exactly the callers its scopes name.

    A token of the other agent is unknown here (401); a known token of the
    wrong scope is refused (403). The bodies are empty, so a route that lets a
    caller in answers 422 before it changes anything, or reads only.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.network, cls.gma_app, _, lmas = build_in_process(wrap=lambda app: app)
        cls.addClassCleanup(cls.network.close)
        cls.apps = {"lma": lmas["site-a"], "gma": cls.gma_app}

    def outcome(self, client: TestClient, route: APIRoute, token: str | None, **values: str) -> str:
        method = sorted(route.methods)[0]
        path = route.path.format(**{**PATH_VALUES, **values}) if "{" in route.path else route.path
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        kwargs: dict[str, Any] = {"headers": headers}
        if method in ("POST", "PUT"):
            kwargs["json"] = {}
        response = client.request(method, path, **kwargs)
        if response.status_code == 401:
            self.assertEqual(response.json()["code"], "unauthorized")
            return "unauthorized"
        if response.status_code == 403 and response.json().get("detail") in TOKEN_REFUSALS:
            self.assertEqual(response.json()["code"], "forbidden")
            return "forbidden"
        return "admitted"

    def test_each_route_admits_only_its_callers(self) -> None:
        checked = 0
        for agent, app in self.apps.items():
            # An empty body makes some older routes fail inside the handler (500);
            # that still means the token was let in.
            client = TestClient(app, raise_server_exceptions=False)
            for route in api_routes(app):
                allowed = expected_callers(agent, route.name)
                for caller, token in CALLERS.items():
                    with self.subTest(agent=agent, route=route.name, caller=caller):
                        result = self.outcome(client, route, token)
                        if caller in allowed:
                            self.assertEqual(result, "admitted")
                        elif token is None or not caller.startswith(agent):
                            # No token, or a token the agent does not know.
                            self.assertEqual(result, "unauthorized")
                        else:
                            self.assertEqual(result, "forbidden")
                        checked += 1
        self.assertGreater(checked, 5 * 120)

    def test_both_forms_of_a_route_admit_the_same_callers(self) -> None:
        for agent, app in self.apps.items():
            client = TestClient(app, raise_server_exceptions=False)
            for versioned, short in route_pairs(app):
                with self.subTest(agent=agent, route=short.name):
                    self.assertEqual(
                        {caller: self.outcome(client, versioned, token) for caller, token in CALLERS.items()},
                        {caller: self.outcome(client, short, token) for caller, token in CALLERS.items()},
                    )

    def test_a_site_token_uploads_only_for_its_own_site(self) -> None:
        client = TestClient(self.apps["gma"])
        uploads = [route for route in api_routes(self.apps["gma"]) if route.name in ("upload_update", "upload_update_v1")]
        self.assertEqual(len(uploads), 2)
        for route in uploads:
            with self.subTest(route=route.path):
                self.assertEqual(self.outcome(client, route, SITE_TOKENS["site-a"], site_id="site-a"), "admitted")
                self.assertEqual(self.outcome(client, route, SITE_TOKENS["site-a"], site_id="site-b"), "forbidden")
                self.assertEqual(self.outcome(client, route, SITE_TOKENS["site-b"], site_id="site-a"), "forbidden")


if __name__ == "__main__":
    unittest.main()
