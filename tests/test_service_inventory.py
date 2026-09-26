"""Tests for the service inventory.

The inventory is the framework's published HTTP contract: Watchdog, Datalytics
and the demo apps are supposed to call Dagents rather than rebuild it, so
"which endpoints exist" is not documentation, it is interface. A list of that
interface maintained by hand would be wrong within a week, so these tests
regenerate it from the code and fail on drift.

They also promote three conventions from prose into assertions, because a
convention enforced nowhere decays:

- the framework services are uniformly ``/api/v1/...``;
- an LMA/GMA versioned alias really does delegate to its legacy handler, rather
  than being a second implementation that can drift;
- the Spring services mirror the Python control-plane and core surfaces, so a
  consumer integrating through the JVM reaches the same paths.

None of this needs ``dagentsc``, Docker, or a database: the routing tables come
from importing the apps and the Spring paths from parsing their controllers.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from agents.gma.main import app as gma_app
from agents.lma.main import app as lma_app


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import service_inventory  # noqa: E402  (needs the sys.path entry above)


#: Services whose endpoints must all be versioned. The agents are excluded on
#: purpose: their legacy short paths are a deliberate, documented duplication.
UNIFORMLY_VERSIONED = frozenset({"core-service", "pipeline-service", "model-service"})

#: Path parameters are named per language (`{source_id}` vs `{sourceId}`), so
#: cross-language comparison normalizes them away.
_PATH_PARAM = re.compile(r"\{[^}]*\}")


def normalize(path: str) -> str:
    """Reduce a route path to a language-neutral shape for comparison."""
    return _PATH_PARAM.sub("{}", path)


#: Building the inventory imports seven services in seven subprocesses, so it is
#: built once for the whole module rather than once per test class.
_INVENTORY: dict[str, object] | None = None


def inventory() -> dict[str, object]:
    """Return the freshly extracted inventory, building it at most once."""
    global _INVENTORY
    if _INVENTORY is None:
        _INVENTORY = service_inventory.build_inventory()
    return _INVENTORY


class ServiceInventoryFreshnessTests(unittest.TestCase):
    """The committed inventory must match what the code actually serves."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = inventory()

    def test_committed_json_matches_the_code(self) -> None:
        """A route added without regenerating the inventory fails here."""
        expected = json.dumps(self.inventory, indent=2, sort_keys=False) + "\n"
        actual = service_inventory.JSON_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            expected,
            actual,
            "docs/reference/service-inventory.json is stale. Regenerate it with: "
            ".venv/bin/python scripts/service_inventory.py --write",
        )

    def test_committed_markdown_matches_the_code(self) -> None:
        """The human-readable table is generated, so it cannot lag the JSON."""
        expected = service_inventory.render_markdown(self.inventory)
        actual = service_inventory.MARKDOWN_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            expected,
            actual,
            "docs/reference/service-inventory.md is stale. Regenerate it with: "
            ".venv/bin/python scripts/service_inventory.py --write",
        )

    def test_every_service_reports_endpoints(self) -> None:
        """A silently empty service would make the whole inventory a false green."""
        for service in self.inventory["services"]:
            with self.subTest(service=service["name"]):
                self.assertGreater(len(service["endpoints"]), 0)

    def test_both_languages_are_covered(self) -> None:
        """The inventory is only complete if it spans the Python and Java surfaces."""
        languages = {service["language"] for service in self.inventory["services"]}
        self.assertEqual({"python", "java"}, languages)

    def test_every_endpoint_names_its_handler(self) -> None:
        """An unnamed handler means the extractor lost track of the source."""
        for service in self.inventory["services"]:
            for endpoint in service["endpoints"]:
                with self.subTest(service=service["name"], path=endpoint["path"]):
                    self.assertTrue(endpoint["handler"])


class ApiConventionTests(unittest.TestCase):
    """Conventions the contributor guide states, asserted rather than hoped for."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = inventory()
        cls.by_name = {service["name"]: service for service in cls.inventory["services"]}

    def test_framework_services_are_uniformly_versioned(self) -> None:
        """`core`, `pipeline` and `model` expose only `/api/v1/...`."""
        for name in sorted(UNIFORMLY_VERSIONED):
            for endpoint in self.by_name[name]["endpoints"]:
                with self.subTest(service=name, path=endpoint["path"]):
                    self.assertTrue(
                        endpoint["path"].startswith("/api/v1/"),
                        f"{name} exposes an unversioned path: {endpoint['path']}",
                    )

    def test_agents_keep_their_legacy_and_versioned_pairs(self) -> None:
        """The agents' deliberate duplication is still there, and still paired."""
        for name in ("lma", "gma"):
            aliases = [
                endpoint
                for endpoint in self.by_name[name]["endpoints"]
                if endpoint["alias_of"] is not None
            ]
            with self.subTest(agent=name):
                self.assertGreater(
                    len(aliases),
                    0,
                    f"{name} reported no versioned aliases; the pairing convention is gone",
                )
            for alias in aliases:
                with self.subTest(agent=name, alias=alias["path"]):
                    self.assertTrue(alias["path"].startswith("/api/v1/"))
                    self.assertFalse(alias["alias_of"]["path"].startswith("/api/v1/"))

    def test_spring_control_mirrors_the_gma_control_plane(self) -> None:
        """A consumer integrating through the JVM reaches the same paths."""
        self.assert_mirrors("spring-control-service", "gma")

    def test_spring_core_mirrors_the_core_service(self) -> None:
        """Same for the core façade."""
        self.assert_mirrors("spring-core-service", "core-service")

    def assert_mirrors(self, java_service: str, python_service: str) -> None:
        """Assert every Java endpoint has a Python counterpart.

        Params:
        - `java_service`: the Spring service whose surface must be mirrored.
        - `python_service`: the FastAPI service that must carry the same paths.

        What it does:
        - Compares `(method, normalized path)` pairs, so `{sourceId}` and
          `{source_id}` are treated as the same path parameter.

        Returns:
        - `None`; fails naming the endpoints that exist in Java only.
        """
        python_endpoints = {
            (endpoint["method"], normalize(endpoint["path"]))
            for endpoint in self.by_name[python_service]["endpoints"]
        }
        java_endpoints = {
            (endpoint["method"], normalize(endpoint["path"]))
            for endpoint in self.by_name[java_service]["endpoints"]
        }
        missing = sorted(java_endpoints - python_endpoints)
        self.assertEqual(
            [],
            missing,
            f"{java_service} exposes endpoints {python_service} does not: {missing}. "
            "Either add them to the Python service or stop claiming the surfaces mirror.",
        )


class AliasDelegationTests(unittest.TestCase):
    """A versioned alias must delegate, not reimplement.

    The alias exists so newer clients get a versioned path. The moment an alias
    computes its own answer, the framework has two implementations of one
    endpoint and only one of them is under test — the failure mode this repo has
    already been bitten by with the manifest renderers.

    These drive both paths through a real client and compare the responses. Only
    the side-effect-free `GET` pairs are exercised, which is what can be checked
    without inventing request fixtures for every endpoint.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.inventory = inventory()
        cls.clients = {"lma": TestClient(lma_app), "gma": TestClient(gma_app)}

    def test_versioned_get_aliases_answer_exactly_as_their_legacy_paths(self) -> None:
        checked = 0
        for agent, client in self.clients.items():
            service = next(s for s in self.inventory["services"] if s["name"] == agent)
            for endpoint in service["endpoints"]:
                alias_of = endpoint["alias_of"]
                if alias_of is None or endpoint["method"] != "GET":
                    continue
                if "{" in endpoint["path"] or "{" in alias_of["path"]:
                    continue  # needs a real resource id; out of scope here
                with self.subTest(agent=agent, alias=endpoint["path"]):
                    versioned = client.get(endpoint["path"])
                    legacy = client.get(alias_of["path"])
                    self.assertEqual(legacy.status_code, versioned.status_code)
                    self.assertEqual(
                        legacy.json(),
                        versioned.json(),
                        f"{agent} {endpoint['path']} and {alias_of['path']} disagree; "
                        "the versioned alias must call the legacy handler",
                    )
                    checked += 1
        self.assertGreater(
            checked,
            0,
            "No GET alias pairs were exercised, so this test proved nothing",
        )


if __name__ == "__main__":
    unittest.main()
