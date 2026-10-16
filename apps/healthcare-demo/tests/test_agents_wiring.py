"""How the app wires the Dagents agents: who holds what, and who may call whom.

Each hospital's LMA gets that hospital's store and audit chain, and no other
hospital's. The GMA gets the coordinator's tables and no hospital's store, and
acts as a role that cannot read a hospital's schema. Every agent requires a
token, each caller gets the token of its own scope, and a hospital view's token
cannot call the routes the GMA uses. Agents in other processes cannot read this
process's in-memory stores, so network mode refuses them. The app's public API
serves none of the agents' routes.

The in-process tests need nothing. The token test that runs a pilot needs the
planner. The Postgres tests need ``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see
``test_site_isolation.py``).
"""

from __future__ import annotations

from collections import deque
import dataclasses
import functools
import types
import unittest
from typing import Any, Iterator
from unittest import mock

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from agents.common.infrastructure.agent_client import AgentNetwork

from app.core.config import settings as app_settings
from app.domain.dataset import SITES, load_site_records
from app.services.agents import AgentError, HealthcareDemo, connect_network_agents
from app.services.site_source import StoreSiteSource
from app.services.site_store import MemorySiteStore, PostgresSiteStore

from tests.support import HIGH_RISK_ENTRY, BuiltAgent, dagentsc_available, recorded_agents

SITE_IDS = [site.site_id for site in SITES]

#: Paths every FastAPI app or every agent serves: the health check and the API docs.
SHARED_PATHS = {"/health", "/api/v1/health", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def reachable(root: Any) -> Iterator[Any]:
    """Every object reachable from ``root`` through the framework's and the app's own objects.

    It follows attributes, containers, bound methods, partials and closures. It
    stops at an :class:`AgentNetwork`: an agent reaches another agent only
    through its HTTP routes, and the in-process network holds every agent app.
    It does not look inside library objects.
    """
    seen: set[int] = set()
    stack: deque[Any] = deque([root])
    while stack:
        obj = stack.pop()
        if id(obj) in seen or isinstance(obj, (str, bytes, int, float, bool, type(None), type)):
            continue
        seen.add(id(obj))
        yield obj
        if isinstance(obj, AgentNetwork):
            continue
        if isinstance(obj, dict):
            stack.extend(obj.keys())
            stack.extend(obj.values())
        elif isinstance(obj, (list, tuple, set, frozenset, deque)):
            stack.extend(obj)
        elif isinstance(obj, types.MethodType):
            stack.append(obj.__self__)
        elif isinstance(obj, functools.partial):
            stack.extend([obj.func, *obj.args, *obj.keywords.values()])
        elif isinstance(obj, types.FunctionType):
            for cell in obj.__closure__ or ():
                try:
                    stack.append(cell.cell_contents)
                except ValueError:  # an empty cell
                    pass
        else:
            module = type(obj).__module__ or ""
            if not (module == "agents" or module.startswith(("agents.", "app."))):
                continue
            stack.extend(getattr(obj, "__dict__", {}).values())
            for cls in type(obj).__mro__:
                for name in getattr(cls, "__slots__", ()):
                    if isinstance(name, str) and hasattr(obj, name):
                        stack.append(getattr(obj, name))


def holds(root: Any, targets: list[Any]) -> list[Any]:
    """Which of ``targets`` (compared by identity) can be reached from ``root``."""
    wanted = {id(target): target for target in targets}
    return [wanted[id(obj)] for obj in reachable(root) if id(obj) in wanted]


class InProcessWiringTests(unittest.TestCase):
    """The default wiring: in-memory stores and four agents in this process."""

    demo: HealthcareDemo
    agents: dict[str, BuiltAgent]

    @classmethod
    def setUpClass(cls) -> None:
        cls.demo = HealthcareDemo()
        try:
            # Build the stores and the agents. Registering the model versions
            # needs the planner and is not needed here, so these tests run
            # without it.
            with recorded_agents() as cls.agents:
                cls.demo.agents
        except BaseException:
            cls.demo.close()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.demo.close()

    def lma(self, site_id: str) -> BuiltAgent:
        return self.agents[f"lma:{site_id}"]

    def test_the_app_built_one_lma_per_hospital_and_one_gma(self) -> None:
        self.assertEqual(sorted(["gma", *(f"lma:{site_id}" for site_id in SITE_IDS)]), sorted(self.agents))
        self.assertEqual("in-process", self.demo.agents.mode)
        self.assertEqual("http", self.agents["gma"].settings.engine)

    def test_each_lma_reads_its_own_hospitals_store_and_keeps_its_own_audit_chain(self) -> None:
        for site_id in SITE_IDS:
            with self.subTest(site=site_id):
                lma = self.lma(site_id)
                store = self.demo.stores[site_id]
                self.assertEqual(site_id, lma.settings.site_id)
                self.assertEqual(site_id, lma.state.site.site_id)
                self.assertIsInstance(lma.deps.source, StoreSiteSource)
                self.assertIs(store, lma.deps.source.store)
                self.assertIs(store.audit_sink(), lma.deps.audit)
        self.assertEqual(len(SITE_IDS), len({id(self.lma(site_id).deps.source) for site_id in SITE_IDS}))
        self.assertEqual(len(SITE_IDS), len({id(self.lma(site_id).deps.audit) for site_id in SITE_IDS}))

    def test_no_lma_holds_another_hospitals_store_or_the_coordinators(self) -> None:
        for site_id in SITE_IDS:
            with self.subTest(site=site_id):
                others = [store for other, store in self.demo.stores.items() if other != site_id]
                foreign = others + [store.audit_sink() for store in others]
                foreign += [self.demo.coordinator_store, self.demo.coordinator_store.audit_sink()]
                self.assertEqual([], holds(self.lma(site_id).state, foreign))
                # The walk does reach the LMA's own store, so it would find a foreign one.
                self.assertEqual([self.demo.stores[site_id]], holds(self.lma(site_id).state, [self.demo.stores[site_id]]))

    def test_the_gma_holds_no_hospital_store(self) -> None:
        gma = self.agents["gma"]
        self.assertIsNone(gma.deps.coordinator_lease, "in memory there is no database connection")
        self.assertIs(self.demo.coordinator_store.audit_sink(), gma.deps.audit)
        sites = list(self.demo.stores.values())
        targets = sites + [store.audit_sink() for store in sites]
        self.assertEqual([], holds(gma.state, targets))
        self.assertEqual([], [obj for obj in reachable(gma.state) if isinstance(obj, (MemorySiteStore, StoreSiteSource))])
        self.assertTrue(holds(gma.state, [self.demo.coordinator_store.audit_sink()]), "the walk reaches the GMA's own sink")

    def test_the_gma_reads_no_source(self) -> None:
        self.assertFalse(self.agents["gma"].settings.allow_source_reads)
        with self.assertRaises(AgentError) as refused:
            self.demo.agents.gma().request("POST", "/api/v1/datasets:profile", json={})
        self.assertEqual(403, refused.exception.status)

    def test_every_agent_requires_a_token_and_every_token_is_its_own(self) -> None:
        gma = self.agents["gma"].settings
        self.assertTrue(gma.require_tokens)
        tokens = [gma.admin_token]
        for site_id in SITE_IDS:
            with self.subTest(site=site_id):
                lma = self.lma(site_id).settings
                self.assertTrue(lma.require_tokens)
                # The GMA calls each LMA with that LMA's coordinator token, and
                # each LMA calls the GMA with a site token bound to its hospital.
                self.assertEqual(lma.coordinator_token, gma.lma_tokens[site_id])
                self.assertEqual(lma.gma_token, gma.site_tokens[site_id])
                tokens += [lma.site_token, lma.coordinator_token, lma.gma_token]
        self.assertTrue(all(isinstance(token, str) and len(token) >= 40 for token in tokens), "a token is missing")
        self.assertEqual(len(tokens), len(set(tokens)), "two callers share a token")
        for text in (repr(self.demo.agents), repr(gma), *(repr(self.lma(site_id).settings) for site_id in SITE_IDS)):
            for token in tokens:
                self.assertNotIn(token, text)

    def test_a_call_without_a_token_is_refused(self) -> None:
        network = self.demo.agents.network
        for site_id in SITE_IDS:
            with self.subTest(site=site_id), self.assertRaises(AgentError) as refused:
                network.lma(site_id).request("GET", "/api/v1/site/summary")
            self.assertEqual(401, refused.exception.status)
        with self.assertRaises(AgentError) as refused:
            network.gma().request("GET", "/api/v1/model-versions")
        self.assertEqual(401, refused.exception.status)

    def test_a_hospital_views_token_cannot_call_the_routes_the_gma_uses(self) -> None:
        view_client = self.demo.agents.lma("uk-134")  # the hospital view's site token
        self.assertEqual("lma", view_client.service)
        for method, path, body in (
            ("GET", "/api/v1/federation/registration", None),
            ("POST", "/api/v1/federation/jobs:offer", {}),
            ("POST", "/api/v1/federation/jobs:execute", {}),
            ("POST", "/api/v1/federation/jobs:run", {}),
        ):
            with self.subTest(path=path), self.assertRaises(AgentError) as refused:
                view_client.request(method, path, json=body)
            self.assertEqual(403, refused.exception.status)
        network = self.demo.agents.network
        site_token = self.lma("uk-134").settings.site_token
        # One hospital's token means nothing at another hospital's LMA or at the GMA.
        with self.assertRaises(AgentError) as refused:
            network.lma("it-002", site_token).request("GET", "/api/v1/site/summary")
        self.assertEqual(401, refused.exception.status)
        with self.assertRaises(AgentError) as refused:
            network.gma(site_token).request("GET", "/api/v1/model-versions")
        self.assertEqual(401, refused.exception.status)
        # The study operator's GMA token means nothing at an LMA.
        with self.assertRaises(AgentError) as refused:
            network.lma("uk-134", self.agents["gma"].settings.admin_token).request("GET", "/api/v1/site/summary")
        self.assertEqual(401, refused.exception.status)

    def test_the_app_serves_no_agent_route(self) -> None:
        import app.main as main

        self.assertEqual([], [route for route in main.app.routes if isinstance(route, Mount)], "an agent is mounted")
        demo_paths = {route.path for route in main.app.routes if isinstance(route, APIRoute)}
        for name, agent in self.agents.items():
            with self.subTest(agent=name):
                agent_paths = {route.path for route in agent.app.routes if isinstance(route, APIRoute)}
                self.assertTrue(agent_paths)
                self.assertEqual(set(), (demo_paths & agent_paths) - SHARED_PATHS)

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_no_token_appears_in_a_response_or_a_log(self) -> None:
        import app.main as main

        tokens = [self.agents["gma"].settings.admin_token]
        for site_id in SITE_IDS:
            settings = self.lma(site_id).settings
            tokens += [settings.site_token, settings.coordinator_token, settings.gma_token]
        texts: list[str] = []
        with mock.patch.object(main, "demo", self.demo), self.assertLogs(level="DEBUG") as logged:
            main.rate_limits.configure({"entries": 0, "probes": 0, "pilots": 0, "approvals": 0})
            main.hourly_caps.configure({"probes": 0, "pilots": 0})
            try:
                client = TestClient(main.app)
                for method, path, body in (
                    ("GET", "/api/v1/framework/status", None),
                    ("GET", "/api/v1/overview", None),
                    ("GET", "/api/v1/hospitals/uk-134", None),
                    ("POST", "/api/v1/hospitals/uk-134/encounters", HIGH_RISK_ENTRY),
                    ("POST", "/api/v1/governance:probe", {"site_id": "it-002"}),
                    ("POST", "/api/v1/pilot:run", {"round_prefix": "api"}),
                    ("POST", "/api/v1/models/none:approve", None),
                    ("GET", "/api/v1/models", None),
                ):
                    response = client.request(method, path, json=body)
                    self.assertLess(response.status_code, 500, response.text[:300])
                    texts.append(response.text)
                    texts.append(str(dict(response.headers)))
            finally:
                main.rate_limits.configure(
                    {
                        "entries": main.settings.rate_limit_entries_per_minute,
                        "probes": main.settings.rate_limit_probes_per_minute,
                        "pilots": main.settings.rate_limit_pilots_per_minute,
                        "approvals": main.settings.rate_limit_approvals_per_minute,
                    }
                )
                main.hourly_caps.configure(
                    {"probes": main.settings.probes_per_site_per_hour, "pilots": main.settings.pilots_per_hour}
                )
        texts += logged.output
        everything = "\n".join(texts)
        self.assertIn("framework_calls", everything)
        for token in tokens:
            self.assertNotIn(token, everything)


class NetworkModeSettingsTests(unittest.TestCase):
    """Network mode reads addresses and tokens from the environment, and refuses what cannot work."""

    def network_settings(self, **changes: Any):
        return dataclasses.replace(app_settings, agents_mode="network", **changes)

    def test_memory_stores_are_refused_in_network_mode(self) -> None:
        settings = self.network_settings(gma_url="http://127.0.0.1:1", gma_token="a-token-for-this-test")
        demo = HealthcareDemo(settings=settings, database_url="")
        self.addCleanup(demo.close)
        with mock.patch("app.services.agents.connect_network_agents") as connect, \
                mock.patch("app.services.agents.build_in_process_agents") as build:
            with self.assertRaises(ValueError) as refused:
                demo.ready()
        self.assertIn("HEALTHCARE_DEMO_DATABASE_URL", str(refused.exception))
        connect.assert_not_called()
        build.assert_not_called()

    def test_a_missing_address_or_token_is_named_and_never_shown(self) -> None:
        secret = "a-secret-that-must-not-be-shown"
        address = "http://10.11.12.13:9823"
        complete = {}
        for site in SITES:
            key = site.site_id.upper().replace("-", "_")
            complete[f"HEALTHCARE_DEMO_LMA_{key}_URL"] = address
            complete[f"HEALTHCARE_DEMO_LMA_{key}_TOKEN"] = secret
        cases = [
            (self.network_settings(gma_url="", gma_token=secret), complete, "HEALTHCARE_DEMO_GMA_URL"),
            (self.network_settings(gma_url=address, gma_token=""), complete, "HEALTHCARE_DEMO_GMA_TOKEN"),
            (
                self.network_settings(gma_url=address, gma_token=secret),
                {k: v for k, v in complete.items() if k != "HEALTHCARE_DEMO_LMA_IT_002_URL"},
                "HEALTHCARE_DEMO_LMA_IT_002_URL",
            ),
            (
                self.network_settings(gma_url=address, gma_token=secret),
                {k: v for k, v in complete.items() if k != "HEALTHCARE_DEMO_LMA_CH_245_TOKEN"},
                "HEALTHCARE_DEMO_LMA_CH_245_TOKEN",
            ),
        ]
        for settings, environ, missing in cases:
            with self.subTest(missing=missing), self.assertRaises(ValueError) as refused:
                connect_network_agents(settings, SITES, environ)
            message = str(refused.exception)
            self.assertIn(missing, message)
            self.assertNotIn(secret, message)
            self.assertNotIn("10.11.12.13", message)

    def test_an_agent_that_does_not_answer_is_reported_without_its_address(self) -> None:
        # Nothing listens on port 1, so every call is refused at once, on any machine.
        unreachable = "http://127.0.0.1:1"
        environ = {}
        for site in SITES:
            key = site.site_id.upper().replace("-", "_")
            environ[f"HEALTHCARE_DEMO_LMA_{key}_URL"] = unreachable
            environ[f"HEALTHCARE_DEMO_LMA_{key}_TOKEN"] = f"token-{key}"
        settings = self.network_settings(gma_url=unreachable, gma_token="gma-token")
        agents = connect_network_agents(settings, SITES, environ)
        self.addCleanup(agents.close)
        self.assertEqual("network", agents.mode)
        checks = agents.health_checks(timeout_s=1.0)
        self.assertEqual(["gma", *(f"lma-{site_id}" for site_id in SITE_IDS)], [check["name"] for check in checks])
        self.assertEqual({"unavailable"}, {check["status"] for check in checks})
        for check in checks:
            self.assertNotIn("127.0.0.1", str(check))
            self.assertNotIn("token", str(check))

    def test_an_unknown_mode_stops_the_app(self) -> None:
        with self.assertRaises(ValueError) as refused:
            dataclasses.replace(app_settings, agents_mode="sideways").check_agent_capacity()
        self.assertIn("HEALTHCARE_DEMO_AGENTS", str(refused.exception))


def _postgres() -> tuple[Any, Any]:
    from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip

    return SupabaseLikeDatabase, admin_dsn_or_skip


class PostgresWiringTests(unittest.TestCase):
    """With a database: each agent runs on its own party's connection, as its own party's role."""

    db: Any
    demo: HealthcareDemo
    agents: dict[str, BuiltAgent]

    @classmethod
    def setUpClass(cls) -> None:
        SupabaseLikeDatabase, admin_dsn_or_skip = _postgres()
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        cls.db.start()
        cls.demo = None  # type: ignore[assignment]
        try:
            for site in SITES:
                store = PostgresSiteStore(site, cls.db.dsn)
                store.seed_dataset(load_site_records(site.site_id))
                store.close()
            cls.demo = HealthcareDemo(database_url=cls.db.dsn, environ={})
            # Build the stores and the agents; registering the model versions is not needed here.
            with recorded_agents() as cls.agents:
                cls.demo.agents
        except BaseException:
            if cls.demo is not None:
                cls.demo.close()
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.demo.close()
        finally:
            cls.db.stop()

    def test_each_lma_runs_on_its_hospitals_connection_as_its_role(self) -> None:
        leases = []
        for site in SITES:
            with self.subTest(site=site.site_id):
                lma = self.agents[f"lma:{site.site_id}"]
                store = self.demo.stores[site.site_id]
                self.assertEqual(site.role, lma.settings.site_role)
                self.assertIs(store, lma.deps.source.store)
                # The job log and the audit chain run on the store's own connection, as the hospital.
                job_log = lma.deps.job_log
                self.assertIs(store.lease, job_log._lease)  # noqa: SLF001
                self.assertEqual(site.role, job_log.role)
                audit = lma.deps.audit
                self.assertIs(store.audit_sink(), audit)
                self.assertEqual((site.role, f"{site.schema}.guard_audit"), (audit.role, audit.table))
                leases.append(store.lease)
                others = [s for other, s in self.demo.stores.items() if other != site.site_id]
                foreign = [*(s.lease for s in others), *others, self.demo.coordinator_store.lease]
                self.assertEqual([], holds(lma.state, foreign))
        self.assertEqual(len(SITES), len({id(lease) for lease in leases}))

    def test_the_gma_runs_on_the_coordinators_connection_as_the_coordinator(self) -> None:
        gma = self.agents["gma"]
        self.assertIs(self.demo.coordinator_store.lease, gma.deps.coordinator_lease)
        self.assertEqual("dagents_coordinator", gma.settings.database_role)
        stores = list(self.demo.stores.values())
        self.assertEqual([], holds(gma.state, [*stores, *(store.lease for store in stores)]))
        self.assertEqual([], [obj for obj in reachable(gma.state) if isinstance(obj, (PostgresSiteStore, StoreSiteSource))])

    def test_the_coordinators_role_cannot_read_a_hospitals_records(self) -> None:
        """On the GMA's own connection, acting as the GMA's role, a hospital's table is refused."""
        from psycopg import errors, sql

        from app.services.site_store import StoreNotSetUpError

        gma = self.agents["gma"]
        lease, role = gma.deps.coordinator_lease, gma.settings.database_role
        for site in SITES:
            with self.subTest(site=site.site_id), self.assertRaises(StoreNotSetUpError) as refused:
                with lease.acting_as(role) as conn:
                    conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(site.schema, "encounters")))
            self.assertIsInstance(refused.exception.__cause__, errors.InsufficientPrivilege)
        # The connection still serves the coordinator's own tables afterwards.
        with lease.acting_as(role) as conn:
            self.assertIsNotNone(conn.execute("SELECT count(*) FROM coordinator.models").fetchone())


if __name__ == "__main__":
    unittest.main()
