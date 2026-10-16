"""The app reaches the framework's site agent and coordinator only through their HTTP APIs.

Each hospital is served by a Dagents LMA and the study by a Dagents GMA. The
app calls them with the framework's agent client and builds no Guard, round
controller, federated worker, running profile or planner call of its own. This
test reads the app's source and fails on an import that would let it do so.

What the app may import from the framework:

- domain types (``agents.common.domain``) and the extension interface
  (``agents.common.extensions``), anywhere;
- the settings loader (``agents.common.env``) and the chart renderer
  (``agents.common.infrastructure.charts``), a library call that holds no data
  and makes no decision, anywhere;
- the Postgres lease, role scoping and audit sink, in the hospital store only
  (``services/site_store.py``);
- the agent client and the LMA and GMA app factories and settings, in the
  wiring only (``services/agents.py``);
- the Compose appliers (``agents.common.infrastructure.compose_apply``), in
  the deployer only (``services/deployment.py``). They start the one service
  in a file the planner rendered, for a hospital that joins while the demo
  runs. They make no decision and read no data.

The store keeps each hospital's audit chain, which the LMA writes to, so it
imports the audit sink's interface and its in-memory twin from
``ethical_guard``. Those two names are allowed there, and nothing else from
that module: not the Guard itself.

The frontend must stay extractable too, so it imports nothing from outside the
app's folder. None of this needs the planner or a database.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import os
from pathlib import Path
import re
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1])
BACKEND = APP_ROOT / "backend" / "app"
SCRIPTS = APP_ROOT / "scripts"
FRONTEND = APP_ROOT / "frontend" / "src"

#: Framework modules the app must never import: the parts the LMA and GMA run
#: for it. Matching a module also matches every module below it.
FORBIDDEN = (
    "agents.common.application.governance_service",
    "agents.common.application.ethical_guard",
    "agents.common.application.federation",
    "agents.common.application.federation_worker",
    "agents.common.application.running_profile",
    "agents.common.infrastructure.dagents_runner",
)

#: Framework modules any app file may import.
ALLOWED_EVERYWHERE = (
    "agents.common.domain",
    "agents.common.extensions",
    "agents.common.env",
    "agents.common.infrastructure.charts",
)

#: Framework modules one app file may import, by its path under ``backend/app``.
ALLOWED_IN = {
    "services/site_store.py": (
        "agents.common.infrastructure.postgres_lease",
        "agents.common.infrastructure.postgres_roles",
        "agents.common.infrastructure.postgres_audit",
    ),
    "services/deployment.py": ("agents.common.infrastructure.compose_apply",),
    "services/agents.py": (
        "agents.common.infrastructure.agent_client",
        "agents.lma.app_factory",
        "agents.lma.config",
        "agents.lma.infrastructure.job_log",
        "agents.gma.app_factory",
        "agents.gma.config",
    ),
}

#: Names one file may import from a forbidden module, and why: the hospital
#: store holds the audit chain the hospital's LMA writes to.
ALLOWED_NAMES = {
    ("services/site_store.py", "agents.common.application.ethical_guard"): {"AuditSink", "InMemoryAuditLog"},
}


@dataclass(frozen=True)
class FrameworkImport:
    path: str
    line: int
    module: str
    names: tuple[str, ...]

    def __str__(self) -> str:
        names = f" ({', '.join(self.names)})" if self.names else ""
        return f"{self.path}:{self.line} imports {self.module}{names}"


def _under(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == prefix or module.startswith(prefix + ".") for prefix in prefixes)


def framework_imports(root: Path, base: Path | None = None) -> list[FrameworkImport]:
    """Every import of an ``agents`` module in the Python files under ``root``, at any depth in the code.

    ``from agents.common.infrastructure import charts`` counts as an import of
    ``agents.common.infrastructure.charts``; ``import agents.x.y`` as one of
    ``agents.x.y``. Imports inside functions count too.
    """
    base = base or root
    found: list[FrameworkImport] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(base).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "agents" or alias.name.startswith("agents."):
                        found.append(FrameworkImport(relative, node.lineno, alias.name, ()))
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if not (node.module == "agents" or node.module.startswith("agents.")):
                    continue
                for alias in node.names:
                    submodule = f"{node.module}.{alias.name}"
                    # ``from package import module`` imports the module.
                    module = submodule if _is_module(submodule) else node.module
                    found.append(FrameworkImport(relative, node.lineno, module, (alias.name,)))
    return found


def _is_module(dotted: str) -> bool:
    """Whether ``dotted`` names a module or package in the framework checkout."""
    path = FRAMEWORK_ROOT.joinpath(*dotted.split("."))
    return path.with_suffix(".py").is_file() or (path / "__init__.py").is_file()


def boundary_violations(imports: list[FrameworkImport]) -> list[str]:
    """The imports the boundary forbids, as readable lines."""
    problems = []
    for item in imports:
        names_allowed = ALLOWED_NAMES.get((item.path, item.module))
        if names_allowed is not None and item.names and set(item.names) <= names_allowed:
            continue
        if _under(item.module, FORBIDDEN):
            problems.append(f"{item}: the app reaches this through the LMA and GMA APIs")
            continue
        if _under(item.module, ALLOWED_EVERYWHERE) or _under(item.module, ALLOWED_IN.get(item.path, ())):
            continue
        problems.append(f"{item}: not a framework module this file may import")
    return problems


class BackendBoundaryTests(unittest.TestCase):
    def test_the_scan_finds_the_apps_framework_imports(self) -> None:
        """A scan that finds nothing would pass on any code."""
        imports = framework_imports(BACKEND)
        modules = {item.module for item in imports}
        self.assertIn("agents.common.infrastructure.agent_client", modules)
        self.assertIn("agents.common.infrastructure.charts", modules)
        self.assertIn("agents.lma.app_factory", modules)
        self.assertIn("agents.common.infrastructure.compose_apply", modules)
        self.assertGreater(len(imports), 20)

    def test_the_scan_catches_a_forbidden_import(self) -> None:
        samples = [
            FrameworkImport("services/pilot.py", 1, "agents.common.application.federation", ("FederatedRoundController",)),
            FrameworkImport("services/hospital_views.py", 1, "agents.common.application.governance_service", ()),
            FrameworkImport("main.py", 1, "agents.common.infrastructure.dagents_runner", ("plan_restrictions",)),
            FrameworkImport("services/site_store.py", 1, "agents.common.application.ethical_guard", ("EthicalGuard",)),
            FrameworkImport("services/overview.py", 1, "agents.common.infrastructure.agent_client", ("AgentNetwork",)),
            FrameworkImport("services/pilot.py", 1, "agents.gma.application.model_registry", ("ModelRegistry",)),
            FrameworkImport("main.py", 1, "agents.common.infrastructure.compose_apply", ("LocalProcessApplier",)),
            FrameworkImport("services/deployment.py", 1, "agents.common.infrastructure.agent_client", ("AgentNetwork",)),
        ]
        self.assertEqual(len(samples), len(boundary_violations(samples)))

    def test_the_app_imports_only_what_the_boundary_allows(self) -> None:
        self.assertEqual([], boundary_violations(framework_imports(BACKEND)))

    def test_the_scan_finds_the_framework_in_an_extracted_copy(self) -> None:
        """``from agents.common.infrastructure import charts`` is read as the module, wherever the framework is."""
        self.assertTrue(_is_module("agents.common.infrastructure.charts"))
        self.assertFalse(_is_module("agents.common.infrastructure.charts.ChartsUnavailableError"))

    def test_the_scripts_import_no_framework_internals(self) -> None:
        """The scripts use the app's services, so they reach the agents the same way.

        A script is not the wiring or the store, so it may import only what
        every app file may import.
        """
        self.assertEqual([], boundary_violations(framework_imports(SCRIPTS, SCRIPTS)))

    def test_no_app_module_imports_a_module_by_name(self) -> None:
        """``importlib.import_module`` or ``__import__`` would pass the scan above unseen."""
        found = []
        for path in sorted([*BACKEND.rglob("*.py"), *SCRIPTS.glob("*.py")]):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
                if isinstance(node, ast.Call):
                    func = node.func
                    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                    if name in ("import_module", "__import__"):
                        found.append(f"{path.relative_to(APP_ROOT).as_posix()}:{node.lineno} calls {name}")
        self.assertEqual([], found)

    def test_the_planner_is_never_run_by_the_app(self) -> None:
        """No subprocess, no planner path: the agents run the planner, and the Guard fails closed there."""
        for path in sorted(BACKEND.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(BACKEND).as_posix()):
                self.assertNotIn("dagentsc", text.replace("DAGENTSC_BIN", ""))
                self.assertNotRegex(text, r"\bimport subprocess\b|\bfrom subprocess\b")


#: A relative import or a dynamic import in TypeScript.
_TS_IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(\s*|\bimport\s+)['"](\.[^'"]+)['"]""")


class FrontendBoundaryTests(unittest.TestCase):
    def test_the_frontend_imports_nothing_outside_the_app(self) -> None:
        """``scripts/extract_repo.sh`` moves the app into its own repository; a path outside it would break."""
        checked = 0
        for path in sorted(FRONTEND.rglob("*.ts*")):
            for target in _TS_IMPORT.findall(path.read_text(encoding="utf-8")):
                resolved = (path.parent / target).resolve()
                checked += 1
                with self.subTest(path=path.relative_to(FRONTEND).as_posix(), target=target):
                    self.assertTrue(
                        resolved.is_relative_to(APP_ROOT), f"{target} resolves outside apps/healthcare-demo"
                    )
        self.assertGreater(checked, 0, "the scan found no import")


if __name__ == "__main__":
    unittest.main()
