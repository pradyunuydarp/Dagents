#!/usr/bin/env python3
"""Fail when code changed and none of the docs that describe it changed.

The framework site publishes the docs in this repository from `main`: the API
reference, the guide to building an application, the learning guide and the
Resources page. A code change that leaves those docs untouched leaves the site
describing the old behaviour. The `docs-sync` skill
(`.claude/skills/docs-sync/SKILL.md`) says which docs each part of the code
feeds and how to regenerate them. This script checks the part of that rule a
machine can check.

For each area in `AREAS`, if the change touches a code file of the area, it
must also touch at least one of the area's docs. A change whose docs need no
update says so in a commit message, on a line of its own, naming the areas the
line covers:

    Docs-Impact: none [agents] (the reason no page changes)

The line exempts only the areas in its brackets, so a line written for one
change cannot hide another area's change later in the same range. The reason
must have at least three words.

What counts as code: the files matched by an area's `code` patterns, except
Markdown files, tests, caches and lockfiles (see `NOT_CODE`). Markdown in a
code folder, such as a service's README, is documentation.

What this cannot check: whether the right page changed, or whether the new
text is correct. A typo fix in any of an area's docs satisfies it. The tests
that compare generated docs with the code (`tests/test_api_reference.py`,
`tests/test_api_examples.py`, `tests/test_service_inventory.py`,
`tests/test_getting_started_example.py`, `tests/test_site_resources.py`) and a
reviewer reading the diff do that.

Usage::

    python scripts/check_docs_impact.py --base origin/main             # a branch
    python scripts/check_docs_impact.py --base BASE_SHA --head HEAD_SHA

`--base` is the commit the change starts from. The changed files are
`git diff BASE...HEAD`, and the commit messages read are `git log BASE..HEAD`.
When the base is empty, all zeros (a new branch) or not in the clone (after a
force push), the script checks the head commit alone and says so.

Exit codes: 0 when every changed area has a docs change or is named by a
`Docs-Impact: none [...] (...)` line, 1 when an area has neither, 2 for wrong
usage or a git error.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from functools import lru_cache
import os
from pathlib import Path
import re
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = ".claude/skills/docs-sync/SKILL.md"


@dataclass(frozen=True)
class Area:
    """A part of the code and the docs that describe it."""

    #: Short name, used in messages and in the docs-sync skill's table.
    name: str
    #: Glob patterns for the area's code. `**` matches any number of folders.
    code: tuple[str, ...]
    #: Glob patterns for the docs. A change to any one of them counts.
    docs: tuple[str, ...]
    #: Commands that regenerate the area's generated docs, for the message.
    regenerate: tuple[str, ...] = field(default=())


#: Docs most areas feed: the repository's own guides for people and agents.
REPO_GUIDES = ("README.md", "CLAUDE.md", "AGENTS.md")

#: The site's own pages that state facts about the code: the home page's steps,
#: parts and "Current state" list, and the demo cards.
SITE_FACTS = ("site/src/pages/Home.tsx", "site/src/config.ts")

#: The generated part of the API reference and the endpoint list.
API_FACTS = (
    "docs/reference/api-reference.json",
    "docs/reference/service-inventory.json",
    "docs/reference/service-inventory.md",
)

REGENERATE_API = (
    ".venv/bin/python scripts/service_inventory.py --write",
    ".venv/bin/python scripts/api_reference.py --write",
    "DAGENTSC_BIN=... .venv/bin/python scripts/capture_api_examples.py --service <name>",
)

AREAS: tuple[Area, ...] = (
    Area(
        name="planners",
        code=("bindings/ocaml/**",),
        docs=(
            "bindings/ocaml/**/*.md",
            "docs/reference/api/dagentsc.md",
            "docs/reference/api/examples/dagentsc.*",
            "docs/reference/api-reference.json",
            "docs/guide/**",
            "docs/learn/**",
            "docs/architecture/**",
            "docs/demo/**",
            *SITE_FACTS,
            *REPO_GUIDES,
        ),
        regenerate=(
            ".venv/bin/python scripts/api_reference.py --write",
            "DAGENTSC_BIN=... .venv/bin/python scripts/capture_api_examples.py --service dagentsc",
            "DAGENTSC_BIN=... PYTHONPATH=. .venv/bin/python examples/getting-started/step4_federated_round.py",
        ),
    ),
    Area(
        name="agents",
        code=("agents/**",),
        docs=(
            "agents/**/*.md",
            "docs/reference/api/agents.md",
            "docs/reference/api/examples/lma.*",
            "docs/reference/api/examples/gma.*",
            *API_FACTS,
            "docs/guide/**",
            "docs/learn/**",
            "docs/agents/**",
            *SITE_FACTS,
            *REPO_GUIDES,
        ),
        regenerate=REGENERATE_API,
    ),
    Area(
        name="framework-services",
        code=(
            "services/core-service/**",
            "services/pipeline-service/**",
            "services/model-service/**",
        ),
        docs=(
            "services/core-service/**/*.md",
            "services/pipeline-service/**/*.md",
            "services/model-service/**/*.md",
            "docs/reference/api/framework-services.md",
            "docs/reference/api/examples/core-service.*",
            "docs/reference/api/examples/pipeline-service.*",
            "docs/reference/api/examples/model-service.*",
            *API_FACTS,
            "docs/guide/**",
            "docs/learn/**",
            *REPO_GUIDES,
        ),
        regenerate=REGENERATE_API,
    ),
    Area(
        name="spring-services",
        code=("services/spring-services/**",),
        docs=(
            "docs/reference/api/spring-services.md",
            "docs/reference/api/examples/spring-*",
            *API_FACTS,
            "docs/learn/**",
            *REPO_GUIDES,
        ),
        regenerate=REGENERATE_API,
    ),
    Area(
        name="nl2sql-demo",
        code=("services/nl2sql-demo/**",),
        docs=(
            "services/nl2sql-demo/**/*.md",
            "docs/reference/api/demo-apps.md",
            "docs/reference/api/examples/nl2sql-demo-backend.*",
            *API_FACTS,
            "docs/guide/**",
            "docs/learn/**",
            "docs/demo/**",
            *SITE_FACTS,
            *REPO_GUIDES,
        ),
        regenerate=REGENERATE_API,
    ),
    Area(
        name="healthcare-demo",
        code=("apps/healthcare-demo/**",),
        docs=(
            "apps/healthcare-demo/**/*.md",
            "docs/reference/api/demo-apps.md",
            "docs/reference/api/examples/healthcare-demo-backend.*",
            *API_FACTS,
            "docs/guide/**",
            "docs/learn/**",
            *SITE_FACTS,
            *REPO_GUIDES,
        ),
        regenerate=REGENERATE_API,
    ),
    Area(
        name="contracts",
        code=("contracts/**",),
        docs=(
            "docs/agents/**",
            "docs/learn/**",
            *REPO_GUIDES,
        ),
    ),
    Area(
        name="scripts",
        code=("scripts/**",),
        docs=(
            ".claude/skills/**",
            "docs/reference/**",
            "docs/guide/09-test-your-integration.md",
            "docs/learn/05-hands-on.md",
            "docs/demo/**",
            "docs/presentation/resources.json",
            "docs/presentation/exports/**",
            *REPO_GUIDES,
        ),
    ),
    Area(
        name="example",
        code=("examples/**",),
        docs=(
            "examples/**/*.md",
            "docs/guide/**",
        ),
        regenerate=(
            "DAGENTSC_BIN=... .venv/bin/python -m unittest tests.test_getting_started_example -v",
        ),
    ),
    Area(
        name="config",
        code=("env/**", "docker-compose*.yml", ".github/workflows/**"),
        docs=(
            ".claude/skills/ci/SKILL.md",
            "docs/guide/01-setup.md",
            "docs/guide/08-deploy.md",
            "docs/guide/09-test-your-integration.md",
            "docs/learn/05-hands-on.md",
            "apps/healthcare-demo/docs/deployment.md",
            *REPO_GUIDES,
        ),
    ),
    Area(
        name="decks",
        code=("docs/presentation/**/*.pptx",),
        docs=(
            "docs/presentation/resources.json",
            "docs/presentation/exports/**",
        ),
        regenerate=(".venv/bin/python scripts/export_presentations.py --write",),
    ),
)

#: Files inside a code area that are not code: documentation, tests, caches and
#: lockfiles. Changing only these never needs a docs change.
NOT_CODE = (
    "**/*.md",
    "**/tests/**",
    "**/test/**",
    "**/__pycache__/**",
    "**/node_modules/**",
    "**/package-lock.json",
)

#: `Docs-Impact: none [area, area] (reason)` on a line of its own. The key is
#: matched without regard to case, as git matches trailer keys.
TRAILER = re.compile(r"^\s*docs-impact\s*:\s*(?P<value>.*?)\s*$", re.IGNORECASE | re.MULTILINE)
NONE_WITH_REASON = re.compile(r"^none\s*\[(?P<areas>[^\]]*)\]\s*\((?P<reason>.*)\)$", re.IGNORECASE)
#: A reason needs this many words, so "x", "ok" and "n/a" say nothing.
MIN_REASON_WORDS = 3
#: Reasons that say nothing, however many words they have.
PLACEHOLDER_REASONS = frozenset(
    {"reason", "the reason", "say why no page changes", "say in a few words why no page changes", "no reason given"}
)

ZERO_SHA = re.compile(r"^0+$")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _pattern(glob: str) -> re.Pattern[str]:
    """Compile a path glob: `**` matches any number of folders, `*` and `?`
    stay within one folder."""
    out = []
    index = 0
    while index < len(glob):
        if glob.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, globs: tuple[str, ...]) -> bool:
    return any(_pattern(glob).match(path) for glob in globs)


def is_code(path: str, area: Area) -> bool:
    return matches(path, area.code) and not matches(path, NOT_CODE)


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass
class AreaResult:
    area: Area
    code_changed: list[str]
    docs_changed: list[str]

    @property
    def satisfied(self) -> bool:
        return bool(self.docs_changed)


@dataclass
class Report:
    areas: list[AreaResult]
    #: Area name to the reason a valid `Docs-Impact: none [...] (...)` line gives.
    exemptions: dict[str, str]
    #: `Docs-Impact:` lines that do not have the form `none [areas] (reason)`.
    malformed: list[str]

    @property
    def missing(self) -> list[AreaResult]:
        """Areas whose code changed with no docs change."""
        return [result for result in self.areas if not result.satisfied]

    @property
    def unexcused(self) -> list[AreaResult]:
        """Missing areas that no Docs-Impact line names."""
        return [result for result in self.missing if result.area.name not in self.exemptions]

    @property
    def passed(self) -> bool:
        return not self.unexcused


def _reason_says_something(reason: str) -> bool:
    words = [word for word in reason.split() if re.search(r"[A-Za-z]", word)]
    return len(words) >= MIN_REASON_WORDS and reason.strip().lower() not in PLACEHOLDER_REASONS


def read_trailers(messages: list[str]) -> tuple[dict[str, str], list[str]]:
    """Return the exempted areas with their reasons, and every malformed line.

    A line is malformed when it lacks the form `none [areas] (reason)`, names an
    area that does not exist, or gives a reason of fewer than three words.
    """
    known = {area.name for area in AREAS}
    exemptions: dict[str, str] = {}
    malformed: list[str] = []
    for message in messages:
        for found in TRAILER.finditer(message):
            parsed = NONE_WITH_REASON.match(found.group("value"))
            names = [name.strip() for name in parsed.group("areas").split(",") if name.strip()] if parsed else []
            reason = parsed.group("reason").strip() if parsed else ""
            if parsed and names and set(names) <= known and _reason_says_something(reason):
                for name in names:
                    exemptions.setdefault(name, reason)
            else:
                malformed.append(found.group(0).strip())
    return exemptions, malformed


def evaluate(changed: list[str], messages: list[str]) -> Report:
    """Decide whether a change with these files and commit messages passes."""
    results = []
    for area in AREAS:
        code = sorted(path for path in changed if is_code(path, area))
        if not code:
            continue
        docs = sorted(path for path in changed if matches(path, area.docs))
        results.append(AreaResult(area=area, code_changed=code, docs_changed=docs))
    exemptions, malformed = read_trailers(messages)
    return Report(areas=results, exemptions=exemptions, malformed=malformed)


def _sample(paths: list[str], limit: int = 5) -> str:
    shown = ", ".join(paths[:limit])
    return shown + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


def format_report(report: Report) -> str:
    lines: list[str] = []
    if not report.areas:
        lines.append("No documented code area changed. Nothing to check.")
        return "\n".join(lines)

    for result in report.areas:
        if result.satisfied:
            state = "docs changed"
        elif result.area.name in report.exemptions:
            state = "no docs changed, exempted by a Docs-Impact line"
        else:
            state = "NO docs changed"
        lines.append(f"{result.area.name}: {len(result.code_changed)} code file(s), {state}")
        if result.satisfied:
            lines.append(f"  docs: {_sample(result.docs_changed)}")

    excused = [result for result in report.missing if result.area.name in report.exemptions]
    if excused:
        lines.append("")
        for result in excused:
            lines.append(f"Docs-Impact: none [{result.area.name}] ({report.exemptions[result.area.name]})")
        lines.append("A commit message says no page needs to change. A reviewer should check each reason.")

    if not report.unexcused:
        return "\n".join(lines)

    lines.append("")
    lines.append("The docs did not change with the code.")
    lines.append("")
    for result in report.unexcused:
        lines.append(f"{result.area.name} changed: {_sample(result.code_changed)}")
        lines.append("  None of the docs it feeds changed. Update the ones that describe what you changed:")
        for glob in result.area.docs:
            lines.append(f"    {glob}")
        if result.area.regenerate:
            lines.append("  Regenerate:")
            for command in result.area.regenerate:
                lines.append(f"    {command}")
        lines.append("")
    if report.malformed:
        lines.append(
            "These Docs-Impact lines were not read. A line needs the form `none [areas] (reason)`, "
            f"names only areas listed above, and gives a reason of at least {MIN_REASON_WORDS} words:"
        )
        for line in report.malformed:
            lines.append(f"    {line}")
        lines.append("")
    names = ", ".join(result.area.name for result in report.unexcused)
    lines.append(f"Fix it in one of two ways. {SKILL} has the full list:")
    lines.append("  1. Update the docs in this change, and regenerate the generated ones.")
    lines.append("  2. If no page describes what changed, add this line to a commit message:")
    lines.append(f"       Docs-Impact: none [{names}] (say in a few words why no page changes)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git
# ---------------------------------------------------------------------------


class GitError(RuntimeError):
    pass


def git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise GitError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout


def is_commit(revision: str, cwd: Path) -> bool:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    return completed.returncode == 0


def change_range(base: str, head: str, cwd: Path) -> tuple[list[str], list[str], str]:
    """Return the changed files, the commit messages and a note on the range."""
    if not is_commit(head, cwd):
        raise GitError(f"the head {head!r} is not a commit in this clone")
    note = f"checking {base}...{head}"
    if not base or ZERO_SHA.match(base) or not is_commit(base, cwd):
        why = "no base was given" if not base or ZERO_SHA.match(base) else f"the base {base} is not in this clone"
        if not is_commit(f"{head}^", cwd):
            return [], [], f"{why}, and {head} has no parent; nothing to compare"
        base = f"{head}^"
        note = f"{why}, so only the head commit is checked"
    output = git("diff", "--name-only", "-z", "--no-renames", f"{base}...{head}", cwd=cwd)
    files = [path for path in output.split("\0") if path]
    log = git("log", "--format=%B%x00", f"{base}..{head}", cwd=cwd)
    messages = [message for message in log.split("\0") if message.strip()]
    return files, messages, note


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fail when code changed and none of the docs that describe it changed."
    )
    parser.add_argument("--base", required=True, help="the commit the change starts from")
    parser.add_argument("--head", default="HEAD", help="the last commit of the change (default: HEAD)")
    return parser


def main(argv: list[str] | None = None, cwd: Path = REPO_ROOT) -> int:
    try:
        arguments = _parser().parse_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 2
    try:
        files, messages, note = change_range(arguments.base, arguments.head, cwd)
    except GitError as error:
        print(f"check_docs_impact: {error}", file=sys.stderr)
        return 2
    report = evaluate(files, messages)
    print(note)
    print(format_report(report))
    if report.passed:
        return 0
    if os.environ.get("GITHUB_ACTIONS") == "true":
        names = ", ".join(result.area.name for result in report.unexcused)
        print(f"::error title=Docs did not change with the code::{names} changed without a docs change. "
              f"See {SKILL}, or add 'Docs-Impact: none [{names}] (reason)' to a commit message.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
