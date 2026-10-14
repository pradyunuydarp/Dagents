"""Tests for the API reference: its generated data, its hierarchy and its rules.

The reference on the framework site has two sources. Facts about each endpoint
come from the code, through `scripts/api_reference.py`, into
`docs/reference/api-reference.json`. The structure (which interface owns which
endpoint) and the prose come from the Markdown in `docs/reference/api/`. These
tests keep the two in step with each other and with the code:

- the generated file must equal a fresh run;
- every endpoint the services serve, and every dagentsc command, must be placed
  in exactly one interface, and every placed endpoint must exist;
- every file and class an interface names must exist;
- an interface marked `complete` must document every endpoint, have an example
  for each, and have field descriptions in the code.

`docs/reference/api/README.md` explains the format these tests read.
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import api_reference as ref  # noqa: E402  (needs the sys.path entry above)

INVENTORY_PATH = REPO_ROOT / "docs" / "reference" / "service-inventory.json"
SITE_MODULES = REPO_ROOT / "site" / "src" / "api" / "modules.ts"

#: FastAPI's own error schemas. They have no descriptions, and the overview page
#: describes them once for every service.
FRAMEWORK_SCHEMAS = frozenset({"HTTPValidationError", "ValidationError"})


def committed_reference() -> dict[str, Any]:
    return json.loads(ref.OUTPUT_PATH.read_text(encoding="utf-8"))


def inventory_endpoints() -> set[tuple[str, str, str]]:
    document = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    return {
        (service["name"], endpoint["method"], endpoint["path"])
        for service in document["services"]
        for endpoint in service["endpoints"]
    }


def placement_key(interface: ref.Interface, endpoint: ref.EndpointRef) -> tuple[str, str, str]:
    if endpoint.kind == "command":
        return (ref.PLANNER, "CMD", " ".join(endpoint.words))
    return (interface.service, endpoint.method, endpoint.path)


def defines(path: Path, name: str) -> bool:
    """Whether a source file defines a class, record, function or module named `name`."""
    text = path.read_text(encoding="utf-8")
    escaped = re.escape(name)
    if path.suffix == ".py":
        patterns = [rf"^\s*class\s+{escaped}\b", rf"^{escaped}\s*[:=]", rf"^def\s+{escaped}\b"]
    elif path.suffix == ".java":
        patterns = [rf"\b(class|record|interface|enum)\s+{escaped}\b"]
    elif path.suffix in (".ml", ".mli"):
        if name.lower() == path.stem.lower():
            return True
        patterns = [rf"^\s*(let|module|type)\s+{escaped}\b"]
    else:
        patterns = [rf"\b{escaped}\b"]
    return any(re.search(pattern, text, re.M) for pattern in patterns)


_REFERENCE: dict[str, Any] | None = None


def fresh_reference() -> dict[str, Any]:
    """Build the reference from the code once for the module (it imports every service)."""
    global _REFERENCE
    if _REFERENCE is None:
        _REFERENCE = ref.build_reference()
    return _REFERENCE


class ApiReferenceFreshnessTests(unittest.TestCase):
    """The committed facts must be what the code says now."""

    def test_committed_file_matches_the_code(self) -> None:
        expected = ref.serialize(fresh_reference())
        actual = ref.OUTPUT_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            expected,
            actual,
            "docs/reference/api-reference.json is stale. Regenerate it with: "
            ".venv/bin/python scripts/api_reference.py --write",
        )

    def test_it_describes_every_endpoint_in_the_inventory(self) -> None:
        described = {
            (service["name"], endpoint["method"], endpoint["path"])
            for service in fresh_reference()["services"]
            for endpoint in service["endpoints"]
        }
        self.assertEqual(inventory_endpoints(), described)

    def test_endpoint_ids_are_unique(self) -> None:
        ids = [endpoint["id"] for service in fresh_reference()["services"] for endpoint in service["endpoints"]]
        ids += [command["id"] for command in fresh_reference()["planner"]["commands"]]
        repeated = sorted(item for item, count in Counter(ids).items() if count > 1)
        self.assertEqual([], repeated)

    def test_handlers_point_at_real_files(self) -> None:
        for service in fresh_reference()["services"]:
            for endpoint in service["endpoints"]:
                with self.subTest(endpoint=endpoint["id"]):
                    self.assertTrue((REPO_ROOT / endpoint["handler"]["file"]).exists())

    def test_spring_bodies_resolve_to_records_with_fields(self) -> None:
        # The Spring schemas come from parsing Java records. If the generator stops
        # finding a record, the schema is a bare title and --check still passes, so
        # a renamed record field would go unnoticed. Every record a Spring endpoint
        # names must resolve to a schema with fields.
        for service in fresh_reference()["services"]:
            if service.get("language") != "java":
                continue
            schemas = service["schemas"]
            for endpoint in service["endpoints"]:
                nodes = [response["schema"] for response in endpoint["responses"]]
                if endpoint["request_body"]:
                    nodes.append(endpoint["request_body"]["schema"])
                for node in nodes:
                    while isinstance(node, dict) and node.get("type") == "array":
                        node = node.get("items")
                    name = (node or {}).get("$ref", "").rsplit("/", 1)[-1]
                    if not name:
                        continue
                    with self.subTest(endpoint=endpoint["id"], record=name):
                        self.assertIn(name, schemas, f"{name} is not resolved to a record")
                        self.assertTrue(schemas[name].get("properties"), f"{name} has no fields")


class ApiHierarchyTests(unittest.TestCase):
    """Every endpoint has exactly one place, and every place is real."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.modules = ref.parse_modules()
        cls.reference = committed_reference()
        cls.interfaces = [(module, interface) for module in cls.modules for interface in module.interfaces]
        cls.services = {service["name"]: service for service in cls.reference["services"]}
        cls.commands = {" ".join(command["words"]) for command in cls.reference["planner"]["commands"]}

    def test_there_are_five_module_pages(self) -> None:
        self.assertEqual(
            ["agents", "dagentsc", "demo-apps", "framework-services", "spring-services"],
            [module.slug for module in self.modules],
        )

    def test_module_pages_parse_without_errors(self) -> None:
        for module in self.modules:
            with self.subTest(module=module.slug):
                self.assertEqual([], module.errors)

    def test_every_interface_names_a_real_service(self) -> None:
        for module, interface in self.interfaces:
            with self.subTest(module=module.slug, interface=interface.heading):
                self.assertIn(interface.service, {*self.services, ref.PLANNER})

    def test_every_endpoint_is_placed_exactly_once(self) -> None:
        placed = Counter(
            placement_key(interface, endpoint) for _, interface in self.interfaces for endpoint in interface.endpoints
        )
        expected = inventory_endpoints() | {(ref.PLANNER, "CMD", command) for command in self.commands}
        missing = sorted(expected - set(placed))
        twice = sorted(key for key, count in placed.items() if count > 1)
        self.assertEqual([], missing, "endpoints that no interface lists; add each to one api-interface block")
        self.assertEqual([], twice, "endpoints listed in more than one interface")

    def test_every_listed_endpoint_exists(self) -> None:
        expected = inventory_endpoints() | {(ref.PLANNER, "CMD", command) for command in self.commands}
        for module, interface in self.interfaces:
            for endpoint in interface.endpoints:
                key = placement_key(interface, endpoint)
                with self.subTest(module=module.slug, endpoint=key):
                    self.assertIn(key, expected, "this endpoint does not exist on the service the interface names")

    def test_alias_pairs_share_an_interface(self) -> None:
        where = {
            placement_key(interface, endpoint): (module.slug, interface.anchor)
            for module, interface in self.interfaces
            for endpoint in interface.endpoints
        }
        for name, service in self.services.items():
            for endpoint in service["endpoints"]:
                alias = endpoint["alias_of"]
                if alias is None:
                    continue
                with self.subTest(service=name, endpoint=endpoint["path"]):
                    self.assertEqual(
                        where.get((name, alias["method"], alias["path"])),
                        where.get((name, endpoint["method"], endpoint["path"])),
                        "list both paths of an alias pair in the same interface",
                    )

    def test_endpoint_prose_belongs_to_its_interface_once(self) -> None:
        aliases: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        for name, service in self.services.items():
            for endpoint in service["endpoints"]:
                if endpoint["alias_of"]:
                    pair = (name, endpoint["alias_of"]["method"], endpoint["alias_of"]["path"])
                    aliases[(name, endpoint["method"], endpoint["path"])] = pair
        for module, interface in self.interfaces:
            listed = {placement_key(interface, endpoint) for endpoint in interface.endpoints}
            documented = Counter()
            for doc in interface.docs:
                key = placement_key(interface, doc.ref)
                with self.subTest(module=module.slug, endpoint=key):
                    self.assertIn(key, listed, "an api-endpoint block must name an endpoint of its own interface")
                documented[aliases.get(key, key)] += 1
            twice = sorted(key for key, count in documented.items() if count > 1)
            with self.subTest(module=module.slug, interface=interface.heading):
                self.assertEqual([], twice, "an endpoint (or alias pair) has more than one api-endpoint section")

    def test_flags_in_the_command_prose_exist_in_the_handler(self) -> None:
        # The flag tables in dagentsc.md list flags the usage text leaves out.
        # Each must be read by its command's handler in dagentsc.ml.
        flags = {" ".join(command["words"]): {flag["name"] for flag in command["flags"]} for command in self.reference["planner"]["commands"]}
        for module, interface in self.interfaces:
            for doc in interface.docs:
                if doc.ref.kind != "command":
                    continue
                command = " ".join(doc.ref.words)
                named = set(re.findall(r"`(--[a-z][\w-]*)", doc.prose))
                with self.subTest(command=command):
                    self.assertEqual(set(), named - flags[command], "the page names flags this command does not read")

    def test_code_files_exist(self) -> None:
        for module, interface in self.interfaces:
            for path in interface.code:
                with self.subTest(module=module.slug, file=path):
                    self.assertTrue((REPO_ROOT / path).is_file(), f"{path} does not exist")

    def test_classes_are_defined_in_their_files(self) -> None:
        for module, interface in self.interfaces:
            for name, path in interface.classes:
                with self.subTest(module=module.slug, cls=name):
                    file = REPO_ROOT / path
                    self.assertTrue(file.is_file(), f"{path} does not exist")
                    self.assertTrue(defines(file, name), f"{path} does not define {name}")

    def test_anchors_are_unique_on_each_page(self) -> None:
        for module in self.modules:
            anchors = [anchor for _, _, anchor in module.headings]
            for interface in module.interfaces:
                for endpoint in interface.endpoints:
                    if endpoint.kind == "command":
                        anchors.append(ref.command_id(endpoint.words))
                    else:
                        anchors.append(ref.endpoint_id(interface.service, endpoint.method, endpoint.path))
            repeated = sorted(anchor for anchor, count in Counter(anchors).items() if count > 1)
            with self.subTest(module=module.slug):
                self.assertEqual([], repeated, "two headings or endpoints on one page share an anchor")

    def test_every_module_page_is_listed_on_the_site(self) -> None:
        listed = SITE_MODULES.read_text(encoding="utf-8")
        for module in self.modules:
            with self.subTest(module=module.slug):
                self.assertIn(f'"{module.slug}.md"', listed, f"add {module.slug}.md to MODULES in {SITE_MODULES.name}")

    def test_objects_are_unique_and_described(self) -> None:
        objects = [obj for module in self.modules for obj in module.objects]
        names = Counter(obj.name for obj in objects)
        self.assertEqual([], sorted(name for name, count in names.items() if count > 1))
        described = {(entry["service"], entry["class"]) for entry in self.reference["objects"]}
        for obj in objects:
            with self.subTest(obj=obj.class_path):
                self.assertIn((obj.service, obj.class_path), described)

    def test_object_examples_point_at_captured_examples(self) -> None:
        for module in self.modules:
            for obj in module.objects:
                if not obj.example:
                    continue
                with self.subTest(obj=obj.class_path):
                    parts = obj.example.split()
                    self.assertEqual(3, len(parts), "write the example as '<service> <example id> <JSON pointer>'")
                    service, example_id, pointer = parts
                    stored = ref.EXAMPLES_DIR / f"{service}.json"
                    self.assertTrue(stored.exists(), f"no captured examples for {service}")
                    examples = {e["id"]: e for e in json.loads(stored.read_text(encoding="utf-8"))["examples"]}
                    self.assertIn(example_id, examples)
                    node: Any = examples[example_id]
                    for token in pointer.strip("/").split("/"):
                        token = token.replace("~1", "/").replace("~0", "~")
                        node = node[int(token)] if isinstance(node, list) else node[token]
                    self.assertIsNotNone(node)


def _schema_names(schema: Any, schemas: dict[str, Any], seen: set[str]) -> None:
    """Every component schema reachable from `schema`."""
    if isinstance(schema, dict):
        target = schema.get("$ref")
        if isinstance(target, str):
            name = target.rsplit("/", 1)[-1]
            if name not in seen and name in schemas:
                seen.add(name)
                _schema_names(schemas[name], schemas, seen)
        for value in schema.values():
            _schema_names(value, schemas, seen)
    elif isinstance(schema, list):
        for value in schema:
            _schema_names(value, schemas, seen)


class CompleteInterfaceTests(unittest.TestCase):
    """An interface marked `complete` meets the bar in docs/reference/api/README.md."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.modules = ref.parse_modules()
        cls.reference = committed_reference()
        cls.services = {service["name"]: service for service in cls.reference["services"]}
        cls.complete = [
            (module, interface)
            for module in cls.modules
            for interface in module.interfaces
            if interface.status == "complete"
        ]
        cls.examples: dict[str, set[str]] = {}
        for path in ref.EXAMPLES_DIR.glob("*.json"):
            if path.name.endswith(".requests.json"):
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
            cls.examples[document["service"]] = {example["endpoint"] for example in document["examples"]}

    def endpoint_entry(self, service: str, endpoint: ref.EndpointRef) -> dict[str, Any]:
        return next(
            entry
            for entry in self.services[service]["endpoints"]
            if entry["method"] == endpoint.method and entry["path"] == endpoint.path
        )

    def pair_keys(self, interface: ref.Interface, endpoint: ref.EndpointRef) -> set[str]:
        """The endpoint's key and, for an alias pair, its twin's."""
        if endpoint.kind == "command":
            return {endpoint.key}
        keys = {endpoint.key}
        entry = self.endpoint_entry(interface.service, endpoint)
        if entry["alias_of"]:
            keys.add(f"{entry['alias_of']['method']} {entry['alias_of']['path']}")
        for other in self.services[interface.service]["endpoints"]:
            alias = other["alias_of"]
            if alias and alias["method"] == endpoint.method and alias["path"] == endpoint.path:
                keys.add(f"{other['method']} {other['path']}")
        return keys

    def test_the_core_service_is_complete(self) -> None:
        """The model module for the content writers; the rules below must apply to something."""
        services = {interface.service for _, interface in self.complete}
        self.assertIn("core-service", services)

    def test_every_endpoint_has_prose(self) -> None:
        for module, interface in self.complete:
            documented = set()
            for doc in interface.docs:
                if doc.prose.strip():
                    documented |= self.pair_keys(interface, doc.ref)
            for endpoint in interface.endpoints:
                with self.subTest(module=module.slug, endpoint=endpoint.key):
                    self.assertIn(endpoint.key, documented, "a complete interface needs an api-endpoint section with prose")

    def test_every_endpoint_has_an_example(self) -> None:
        for module, interface in self.complete:
            captured = self.examples.get(interface.service, set())
            for endpoint in interface.endpoints:
                with self.subTest(module=module.slug, endpoint=endpoint.key):
                    self.assertTrue(
                        self.pair_keys(interface, endpoint) & captured,
                        "a complete interface needs a captured example for every endpoint",
                    )

    def test_every_field_and_parameter_has_a_description(self) -> None:
        for module, interface in self.complete:
            if interface.service == ref.PLANNER:
                continue
            schemas = self.services[interface.service]["schemas"]
            for endpoint in interface.endpoints:
                entry = self.endpoint_entry(interface.service, endpoint)
                names: set[str] = set()
                if entry["request_body"]:
                    _schema_names(entry["request_body"]["schema"], schemas, names)
                for response in entry["responses"]:
                    _schema_names(response["schema"], schemas, names)
                for parameter in entry["parameters"]:
                    with self.subTest(module=module.slug, endpoint=endpoint.key, parameter=parameter["name"]):
                        self.assertTrue(
                            parameter.get("description") or parameter["schema"].get("description"),
                            f"{endpoint.key} parameter {parameter['name']} has no description; "
                            "give it one in the code (Path(description=...), Query(description=...))",
                        )
                for name in sorted(names - FRAMEWORK_SCHEMAS):
                    for field, prop in schemas[name].get("properties", {}).items():
                        with self.subTest(module=module.slug, schema=name, field=field):
                            self.assertTrue(
                                isinstance(prop, dict) and prop.get("description"),
                                f"{name}.{field} has no description; add Field(description=...) in the code",
                            )


if __name__ == "__main__":
    unittest.main()
