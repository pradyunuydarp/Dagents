"""Every image whose service calls the planner must contain the planner.

The Ethical Guard and the federated round controller call `dagentsc` for every
decision, and the Guard denies every request when it cannot run it. The test
suites point `DAGENTSC_BIN` at the dune build, so they pass whether or not an
image contains the planner. The LMA and GMA images once had no planner: every
test passed, and in the compose stack the Guard denied every request.

CI does not build images, so this check reads the Dockerfiles. For each Python
image it starts at the module that uvicorn runs and follows the imports through
the code the image copies. If the walk reaches
`dagents.common.infrastructure.dagents_runner`, the module that starts the
planner, the image must build `dagentsc.exe` in an earlier stage and copy it to
`/usr/local/bin/dagentsc` in its final stage.

The Java images are not checked here. Of those, only `spring-core-service`
calls the planner, and its Dockerfile copies it.
"""

from __future__ import annotations

import ast
from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]

#: The only module that starts `dagentsc`. Services call the planner through it.
PLANNER_RUNNER = "dagents.common.infrastructure.dagents_runner"
#: Where the images put the planner. `dagents_runner` finds it on PATH there.
PLANNER_IN_IMAGE = "/usr/local/bin/dagentsc"
BUILDS_THE_PLANNER = re.compile(r"\bdune build\s+\./bin/dagentsc\.exe\b")
UVICORN_ENTRY = re.compile(r"\buvicorn\s+([A-Za-z_][\w.]*):")
PYTHON_BASE = re.compile(r"(^|/)python:")
#: Directories that hold build output or installed packages, not service code.
SKIPPED_DIRECTORIES = {".git", ".venv", "venv", "node_modules", "_build", "target", "dist"}

#: Images known to call the planner. The walk must find at least these, so the
#: check cannot pass because the walk stopped following imports.
KNOWN_PLANNER_IMAGES = {
    "dagents/gma/Dockerfile",
    "dagents/lma/Dockerfile",
    "apps/healthcare-demo/backend/Dockerfile",
    "services/core-service/Dockerfile",
    "services/nl2sql-demo/backend/Dockerfile",
    "services/pipeline-service/Dockerfile",
}


# ---------------------------------------------------------------------------
# Reading a Dockerfile
# ---------------------------------------------------------------------------


@dataclass
class Instruction:
    keyword: str
    arguments: str


@dataclass
class Stage:
    base: str
    name: str | None
    instructions: list[Instruction] = field(default_factory=list)


def parse_dockerfile(text: str) -> list[Stage]:
    """Split a Dockerfile into its stages, with continuation lines joined."""
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        lines.append(pending + line)
        pending = ""
    if pending:
        lines.append(pending)

    stages: list[Stage] = []
    for line in lines:
        keyword, _, arguments = line.partition(" ")
        keyword, arguments = keyword.upper(), arguments.strip()
        if keyword == "FROM":
            match = re.fullmatch(r"(?:--\S+\s+)*(\S+)(?:\s+as\s+(\S+))?", arguments, re.IGNORECASE)
            if match is None:
                raise ValueError(f"cannot read the FROM line {line!r}")
            stages.append(Stage(base=match.group(1), name=(match.group(2) or "").lower() or None))
        elif stages:
            stages[-1].instructions.append(Instruction(keyword, arguments))
    if not stages:
        raise ValueError("the Dockerfile has no FROM line")
    return stages


def copy_arguments(arguments: str) -> tuple[dict[str, str], list[str]]:
    """Return a COPY instruction's flags and its paths, the target last."""
    if arguments.startswith("["):
        tokens = json.loads(arguments)
    else:
        tokens = shlex.split(arguments)
    flags: dict[str, str] = {}
    paths: list[str] = []
    for token in tokens:
        if token.startswith("--") and not paths:
            name, _, value = token[2:].partition("=")
            flags[name.lower()] = value
        else:
            paths.append(token)
    return flags, paths


def planner_problems(stages: list[Stage]) -> list[str]:
    """Say why the image would not contain the planner. Empty means it does."""
    builders = {
        stage.name
        for stage in stages[:-1]
        if stage.name
        and any(i.keyword == "RUN" and BUILDS_THE_PLANNER.search(i.arguments) for i in stage.instructions)
    }
    if not builders:
        return ["no stage before the final one runs `dune build ./bin/dagentsc.exe`"]
    for instruction in stages[-1].instructions:
        if instruction.keyword != "COPY":
            continue
        flags, paths = copy_arguments(instruction.arguments)
        if (
            flags.get("from", "").lower() in builders
            and len(paths) == 2
            and paths[0].endswith("/_build/default/bin/dagentsc.exe")
            and paths[1] == PLANNER_IN_IMAGE
        ):
            return []
    return [
        f"the final stage has no `COPY --from={sorted(builders)[0]} "
        f".../_build/default/bin/dagentsc.exe {PLANNER_IN_IMAGE}`"
    ]


# ---------------------------------------------------------------------------
# Following a service's imports through the code its image copies
# ---------------------------------------------------------------------------


def entry_module(stages: list[Stage]) -> str | None:
    """The module that the final stage's CMD or ENTRYPOINT runs with uvicorn."""
    for instruction in reversed(stages[-1].instructions):
        if instruction.keyword in {"CMD", "ENTRYPOINT"}:
            command = instruction.arguments
            if command.startswith("["):
                command = " ".join(json.loads(command))
            match = UVICORN_ENTRY.search(command)
            if match:
                return match.group(1)
    return None


@dataclass
class ImageLayout:
    """Where the final stage puts each copied repository directory."""

    workdir: PurePosixPath
    copies: list[tuple[PurePosixPath, Path]]

    @classmethod
    def of(cls, stages: list[Stage]) -> "ImageLayout":
        workdir = PurePosixPath("/")
        copies: list[tuple[PurePosixPath, Path]] = []
        for instruction in stages[-1].instructions:
            if instruction.keyword == "WORKDIR":
                workdir = workdir / instruction.arguments
            elif instruction.keyword == "COPY":
                flags, paths = copy_arguments(instruction.arguments)
                if "from" in flags or len(paths) != 2:
                    continue
                source = REPO_ROOT / paths[0]
                if source.is_dir():
                    copies.append((workdir / paths[1], source))
        # The most specific target first, so `/app/app` wins over `/app`.
        copies.sort(key=lambda copy: len(copy[0].parts), reverse=True)
        return cls(workdir=workdir, copies=copies)

    def module_file(self, module: str) -> Path | None:
        """The repository file the image would import for `module`, if it copies one.

        uvicorn imports from its working directory, which is the final WORKDIR.
        """
        in_image = self.workdir.joinpath(*module.split("."))
        for target, source in self.copies:
            try:
                relative = in_image.relative_to(target)
            except ValueError:
                continue
            base = source.joinpath(*relative.parts)
            for candidate in (base / "__init__.py", base.with_name(base.name + ".py")):
                if candidate.is_file():
                    return candidate
        return None


def imported_modules(path: Path, module: str) -> set[str]:
    """Every module `path` may import, with the packages each one loads first.

    Imports inside functions and `if TYPE_CHECKING` blocks count too. Counting
    too many can only require the planner where it is not needed.
    """
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
            else:
                base = node.module or ""
            found.add(base)
            # `from package import name` may load the submodule `package.name`.
            found.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    with_packages: set[str] = set()
    for name in filter(None, found):
        parts = name.split(".")
        with_packages.update(".".join(parts[:index]) for index in range(1, len(parts) + 1))
    return with_packages


def planner_import_chain(stages: list[Stage]) -> list[str] | None:
    """The imports from the entry module to the planner runner, or None if it is not reached."""
    entry = entry_module(stages)
    if entry is None:
        return None
    layout = ImageLayout.of(stages)
    parents: dict[str, str | None] = {entry: None}
    queue = deque([entry])
    while queue:
        module = queue.popleft()
        path = layout.module_file(module)
        if path is None:
            continue
        if module == PLANNER_RUNNER:
            chain = [module]
            parent = parents[module]
            while parent is not None:
                chain.append(parent)
                parent = parents[parent]
            return list(reversed(chain))
        for imported in sorted(imported_modules(path, module)):
            if imported not in parents:
                parents[imported] = module
                queue.append(imported)
    return None


def dockerfiles() -> dict[str, list[Stage]]:
    """Every Dockerfile in the repository, by its path from the root."""
    found: dict[str, list[Stage]] = {}
    for directory, subdirectories, files in os.walk(REPO_ROOT):
        subdirectories[:] = sorted(name for name in subdirectories if name not in SKIPPED_DIRECTORIES)
        if "Dockerfile" in files:
            path = Path(directory) / "Dockerfile"
            found[path.relative_to(REPO_ROOT).as_posix()] = parse_dockerfile(path.read_text(encoding="utf-8"))
    return found


def python_images() -> dict[str, list[Stage]]:
    return {name: stages for name, stages in dockerfiles().items() if PYTHON_BASE.search(stages[-1].base)}


def planner_images() -> dict[str, list[str]]:
    """The Python images whose service reaches the planner, with the import chain for each."""
    chains = {name: planner_import_chain(stages) for name, stages in python_images().items()}
    return {name: chain for name, chain in chains.items() if chain is not None}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class PlannerInImagesTests(unittest.TestCase):
    def test_every_image_that_calls_the_planner_contains_it(self) -> None:
        all_stages = dockerfiles()
        missing = []
        for name, chain in sorted(planner_images().items()):
            for problem in planner_problems(all_stages[name]):
                missing.append(f"{name}: {problem}. Its service reaches the planner by {' -> '.join(chain)}.")
        self.assertEqual(
            [],
            missing,
            "These images run code that calls dagentsc but do not contain it, so the Ethical Guard "
            "would deny every request. Add the ocaml-builder stage from "
            "services/core-service/Dockerfile and copy the binary to /usr/local/bin/dagentsc.",
        )

    def test_the_walk_finds_the_known_planner_images(self) -> None:
        found = set(planner_images())
        self.assertEqual(
            set(),
            KNOWN_PLANNER_IMAGES - found,
            "The import walk no longer finds these images, so the test above would pass without "
            "checking them.",
        )

    def test_every_python_image_has_an_entry_module_the_walk_can_read(self) -> None:
        unreadable = []
        for name, stages in sorted(python_images().items()):
            entry = entry_module(stages)
            if entry is None:
                unreadable.append(f"{name}: no `uvicorn module:app` in CMD or ENTRYPOINT")
            elif ImageLayout.of(stages).module_file(entry) is None:
                unreadable.append(f"{name}: the image copies no file for its entry module {entry}")
        self.assertEqual(
            [],
            unreadable,
            "The planner check cannot follow these images' imports. Teach it how they start.",
        )

    def test_the_guard_and_the_round_controller_reach_the_planner(self) -> None:
        # A service that uses either one needs the planner, even if it never
        # imports the runner itself.
        for module in ("dagents.common.application.governance_service", "dagents.common.application.federation"):
            with self.subTest(module=module):
                stages = parse_dockerfile("\n".join([
                    "FROM python:3.11-slim",
                    "WORKDIR /app",
                    "COPY dagents ./dagents",
                    f'CMD ["uvicorn", "{module}:app"]',
                ]))
                chain = planner_import_chain(stages)
                self.assertIsNotNone(chain)
                self.assertEqual([module, PLANNER_RUNNER], [chain[0], chain[-1]])


class PlannerProblemTests(unittest.TestCase):
    """The check refuses each way an image can lose the planner."""

    AGENT_IMAGES = ("dagents/lma/Dockerfile", "dagents/gma/Dockerfile")

    def read(self, name: str) -> str:
        return (REPO_ROOT / name).read_text(encoding="utf-8")

    def test_the_agent_images_contain_the_planner(self) -> None:
        for name in self.AGENT_IMAGES:
            with self.subTest(image=name):
                self.assertEqual([], planner_problems(parse_dockerfile(self.read(name))))

    def test_an_image_without_the_copy_line_is_refused(self) -> None:
        for name in self.AGENT_IMAGES:
            with self.subTest(image=name):
                text = self.read(name)
                without_copy = "\n".join(line for line in text.splitlines() if "dagentsc.exe /usr/local" not in line)
                self.assertNotEqual(text, without_copy, "the copy line was not found, so this test checks nothing")
                problems = planner_problems(parse_dockerfile(without_copy))
                self.assertEqual(1, len(problems))
                self.assertIn(PLANNER_IN_IMAGE, problems[0])

    def test_an_image_without_the_build_stage_is_refused(self) -> None:
        for name in self.AGENT_IMAGES:
            with self.subTest(image=name):
                text = self.read(name)
                final_stage = text[text.index("FROM python:"):]
                problems = planner_problems(parse_dockerfile(final_stage))
                self.assertEqual(["no stage before the final one runs `dune build ./bin/dagentsc.exe`"], problems)

    def test_a_copy_into_an_earlier_stage_does_not_count(self) -> None:
        text = "\n".join([
            "FROM ocaml/opam:debian-ocaml-5.0 AS ocaml-builder",
            "RUN opam exec -- dune build ./bin/dagentsc.exe",
            "FROM python:3.11-slim AS middle",
            "COPY --from=ocaml-builder /src/_build/default/bin/dagentsc.exe /usr/local/bin/dagentsc",
            "FROM python:3.11-slim",
            "COPY dagents ./dagents",
        ])
        self.assertEqual(1, len(planner_problems(parse_dockerfile(text))))

    def test_a_copy_to_another_path_does_not_count(self) -> None:
        text = "\n".join([
            "FROM ocaml/opam:debian-ocaml-5.0 AS ocaml-builder",
            "RUN opam exec -- dune build ./bin/dagentsc.exe",
            "FROM python:3.11-slim",
            "COPY --from=ocaml-builder /src/_build/default/bin/dagentsc.exe /opt/dagentsc",
        ])
        self.assertEqual(1, len(planner_problems(parse_dockerfile(text))))

    def test_stage_names_are_case_insensitive(self) -> None:
        text = "\n".join([
            "FROM ocaml/opam:debian-ocaml-5.0 as OCaml-Builder",
            "RUN opam exec -- dune build ./bin/dagentsc.exe",
            "FROM python:3.11-slim",
            "COPY --from=ocaml-builder /src/_build/default/bin/dagentsc.exe /usr/local/bin/dagentsc",
        ])
        self.assertEqual([], planner_problems(parse_dockerfile(text)))


if __name__ == "__main__":
    unittest.main()
