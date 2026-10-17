"""Tests for `scripts/check_docs_impact.py`, the docs-impact check.

The check fails a change that touches a documented code area without touching
any of the docs that area feeds, unless a commit message carries
`Docs-Impact: none (reason)`. These tests cover the decision, the trailer, the
git range (including a force push and a new branch), the area map against the
real tree, and the CI job that runs it.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_docs_impact as impact  # noqa: E402  (needs the sys.path entry)
from tests.test_ci_guards import jobs, run_blocks  # noqa: E402

SKILL = REPO_ROOT / ".claude" / "skills" / "docs-sync" / "SKILL.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def area_names(report: impact.Report) -> list[str]:
    return [result.area.name for result in report.areas]


def missing_names(report: impact.Report) -> list[str]:
    return [result.area.name for result in report.missing]


class MatchingTests(unittest.TestCase):
    def test_double_star_crosses_folders_and_single_star_does_not(self) -> None:
        self.assertTrue(impact.matches("dagents/lma/main.py", ("dagents/**",)))
        self.assertTrue(impact.matches("docs/guide/01-setup.md", ("docs/guide/**",)))
        self.assertTrue(impact.matches("README.md", ("**/*.md",)))
        self.assertTrue(impact.matches("a/b/c/README.md", ("**/*.md",)))
        self.assertTrue(impact.matches("docs/reference/api/examples/lma.json", ("docs/reference/api/examples/lma.*",)))
        self.assertFalse(impact.matches("docs/reference/api/examples/lma/x.json", ("docs/reference/api/examples/lma.*",)))
        self.assertFalse(impact.matches("dagentsx/main.py", ("dagents/**",)))
        self.assertFalse(impact.matches("docs/presentation/x.pptx.bak", ("docs/presentation/**/*.pptx",)))

    def test_markdown_tests_and_lockfiles_in_a_code_area_are_not_code(self) -> None:
        [agents] = [area for area in impact.AREAS if area.name == "agents"]
        [healthcare] = [area for area in impact.AREAS if area.name == "healthcare-demo"]
        self.assertTrue(impact.is_code("dagents/lma/main.py", agents))
        self.assertFalse(impact.is_code("dagents/tests/test_governance.py", agents))
        self.assertFalse(impact.is_code("dagents/README.md", agents))
        self.assertFalse(impact.is_code("apps/healthcare-demo/frontend/package-lock.json", healthcare))
        self.assertFalse(impact.is_code("apps/healthcare-demo/tests/test_api.py", healthcare))
        self.assertTrue(impact.is_code("apps/healthcare-demo/frontend/src/App.tsx", healthcare))


class DecisionTests(unittest.TestCase):
    def test_code_without_docs_fails_and_names_the_area(self) -> None:
        report = impact.evaluate(["dagents/lma/main.py"], ["Change the LMA"])
        self.assertFalse(report.passed)
        self.assertEqual(["agents"], missing_names(report))
        text = impact.format_report(report)
        self.assertIn("agents changed: dagents/lma/main.py", text)
        self.assertIn("docs/reference/api/agents.md", text)
        self.assertIn("scripts/api_reference.py --write", text)
        self.assertIn("Docs-Impact: none [agents] (say in a few words why no page changes)", text)
        self.assertIn(".claude/skills/docs-sync/SKILL.md", text)

    def test_code_with_one_of_its_docs_passes(self) -> None:
        report = impact.evaluate(["dagents/lma/main.py", "docs/reference/api/agents.md"], ["x"])
        self.assertTrue(report.passed)
        self.assertEqual(["agents"], area_names(report))

    def test_a_generated_doc_counts(self) -> None:
        report = impact.evaluate(
            ["services/core-service/app/models.py", "docs/reference/api-reference.json"], ["x"]
        )
        self.assertTrue(report.passed)

    def test_a_doc_of_another_area_does_not_count(self) -> None:
        report = impact.evaluate(["dagents/lma/main.py", "docs/presentation/resources.json"], ["x"])
        self.assertFalse(report.passed)

    def test_each_area_is_checked_on_its_own(self) -> None:
        report = impact.evaluate(
            [
                "services/spring-services/spring-core-service/src/main/java/A.java",
                "contracts/grpc/dagents/agents/v1/lma_gma.proto",
                "docs/reference/api/spring-services.md",
            ],
            ["x"],
        )
        self.assertEqual(["spring-services", "contracts"], area_names(report))
        self.assertEqual(["contracts"], missing_names(report))

    def test_tests_and_docs_alone_need_nothing(self) -> None:
        for changed in (
            ["dagents/tests/test_governance.py"],
            ["services/core-service/README.md"],
            ["docs/learn/02-privacy-and-governance.md"],
            ["site/src/App.tsx"],
            ["bindings/ocaml/test/test_compilers.ml"],
        ):
            with self.subTest(changed=changed):
                report = impact.evaluate(changed, ["x"])
                self.assertEqual([], report.areas)
                self.assertTrue(report.passed)

    def test_a_readme_in_a_code_folder_is_that_area_s_doc(self) -> None:
        report = impact.evaluate(
            ["services/pipeline-service/app/api/routes.py", "services/pipeline-service/README.md"], ["x"]
        )
        self.assertTrue(report.passed)

    def test_a_deleted_code_file_counts_as_a_change(self) -> None:
        """git lists a deleted file by its old path, so removing code needs docs too."""
        report = impact.evaluate(["examples/getting-started/shop/rule.py"], ["Remove the rule"])
        self.assertEqual(["example"], missing_names(report))

    def test_a_deck_needs_its_exports(self) -> None:
        deck = "docs/presentation/healthcare-case-study/stroke.pptx"
        self.assertEqual(["decks"], missing_names(impact.evaluate([deck], ["x"])))
        self.assertTrue(impact.evaluate([deck, "docs/presentation/resources.json"], ["x"]).passed)


class TrailerTests(unittest.TestCase):
    CHANGE = ["bindings/ocaml/lib/governance_compiler/governance_compiler.ml"]

    def test_a_reason_exempts_the_named_area_and_is_printed(self) -> None:
        message = "Rename a helper\n\nDocs-Impact: none [planners] (internal rename; no output changes)\n"
        report = impact.evaluate(self.CHANGE, ["first commit", message])
        self.assertTrue(report.passed)
        self.assertEqual({"planners": "internal rename; no output changes"}, report.exemptions)
        self.assertIn("Docs-Impact: none [planners] (internal rename; no output changes)", impact.format_report(report))

    def test_the_key_is_matched_without_regard_to_case(self) -> None:
        report = impact.evaluate(self.CHANGE, ["x\n\ndocs-impact: None [planners] (refactor of one helper)"])
        self.assertTrue(report.passed)

    def test_a_line_exempts_only_the_areas_it_names(self) -> None:
        # A line written for a script comment must not hide a planner and an agent
        # change made later in the same pull request.
        changed = [*self.CHANGE, "dagents/gma/main.py", "scripts/export_presentations.py"]
        report = impact.evaluate(changed, ["Fix a comment\n\nDocs-Impact: none [scripts] (comment typo, no page quotes it)", "Change the planner"])
        self.assertFalse(report.passed)
        self.assertEqual(["planners", "agents"], [result.area.name for result in report.unexcused])
        self.assertIn("Docs-Impact: none [planners, agents]", impact.format_report(report))

    def test_one_line_may_name_several_areas(self) -> None:
        changed = [*self.CHANGE, "dagents/gma/main.py"]
        report = impact.evaluate(changed, ["x\n\nDocs-Impact: none [planners, agents] (rename of private helpers only)"])
        self.assertTrue(report.passed)

    def test_a_line_without_a_reason_is_refused_and_reported(self) -> None:
        for line in (
            "Docs-Impact: none",
            "Docs-Impact: none ()",
            "Docs-Impact: none (internal rename; no output changes)",
            "Docs-Impact: none [planners]",
            "Docs-Impact: none [planners] ()",
            "Docs-Impact: none [planners] (reason)",
            "Docs-Impact: none [planners] (n/a)",
            "Docs-Impact: none [planners] (x)",
            "Docs-Impact: none [planners] (ok)",
            "Docs-Impact: none [planners] (no docs)",
            "Docs-Impact: none [] (internal rename only here)",
            "Docs-Impact: none [all] (internal rename only here)",
            "Docs-Impact: none [planner] (internal rename only here)",
            "Docs-Impact: skip [planners] (tests only here)",
        ):
            with self.subTest(line=line):
                report = impact.evaluate(self.CHANGE, [f"Change\n\n{line}\n"])
                self.assertFalse(report.passed)
                self.assertEqual([line], report.malformed)
                self.assertIn(line, impact.format_report(report))

    def test_the_phrase_inside_a_sentence_does_not_count(self) -> None:
        report = impact.evaluate(self.CHANGE, ["Explain why Docs-Impact: none [planners] (a real reason) is allowed"])
        self.assertFalse(report.passed)


def _git(directory: Path, *arguments: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    return subprocess.run(
        ["git", *arguments], cwd=directory, env=environment, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(directory: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(directory, "add", "--all")
    _git(directory, "commit", "--quiet", "--allow-empty", "-m", message)
    return _git(directory, "rev-parse", "HEAD")


class GitRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        _git(self.repo, "init", "--quiet", "--initial-branch=main")
        self.base = _commit(self.repo, {"dagents/lma/main.py": "a = 1\n", "docs/learn/README.md": "# Learn\n"}, "Start")

    def run_check(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = impact.main(list(arguments), cwd=self.repo)
        return code, output.getvalue()

    def test_a_code_only_range_fails_and_a_trailer_in_any_commit_passes(self) -> None:
        _commit(self.repo, {"dagents/lma/main.py": "a = 2\n"}, "Change the LMA")
        head = _commit(self.repo, {"dagents/tests/test_x.py": "pass\n"}, "Add a test")
        code, output = self.run_check("--base", self.base, "--head", head)
        self.assertEqual(1, code, output)
        self.assertIn("agents changed: dagents/lma/main.py", output)

        head = _commit(self.repo, {}, "Note\n\nDocs-Impact: none [agents] (the value is internal)")
        self.assertEqual(0, self.run_check("--base", self.base, "--head", head)[0])

    def test_an_earlier_trailer_for_another_area_does_not_cover_a_later_change(self) -> None:
        _commit(self.repo, {"scripts/tool.py": "# a comment\n"}, "Fix a comment\n\nDocs-Impact: none [scripts] (comment typo, no page quotes it)")
        head = _commit(self.repo, {"dagents/lma/main.py": "a = 2\n"}, "Change the LMA")
        code, output = self.run_check("--base", self.base, "--head", head)
        self.assertEqual(1, code, output)
        self.assertIn("agents changed: dagents/lma/main.py", output)
        self.assertIn("scripts: 1 code file(s), no docs changed, exempted", output)

    def test_docs_in_a_later_commit_of_the_range_pass(self) -> None:
        _commit(self.repo, {"dagents/lma/main.py": "a = 2\n"}, "Change the LMA")
        head = _commit(self.repo, {"docs/learn/README.md": "# Learn\n\nNew.\n"}, "Describe it")
        code, output = self.run_check("--base", self.base, "--head", head)
        self.assertEqual(0, code, output)

    def test_a_file_moved_out_of_an_area_counts_on_both_sides(self) -> None:
        _git(self.repo, "mv", "dagents/lma/main.py", "notes.py")
        head = _commit(self.repo, {}, "Move it")
        code, output = self.run_check("--base", self.base, "--head", head)
        self.assertEqual(1, code, output)

    def test_an_empty_or_zero_base_checks_the_head_commit_alone(self) -> None:
        _commit(self.repo, {"dagents/lma/main.py": "a = 2\n", "docs/learn/README.md": "x\n"}, "Both")
        head = _commit(self.repo, {"dagents/lma/main.py": "a = 3\n"}, "Code only")
        for base in ("", "0" * 40):
            with self.subTest(base=base):
                code, output = self.run_check("--base", base, "--head", head)
                self.assertEqual(1, code, output)
                self.assertIn("only the head commit is checked", output)

    def test_a_base_missing_from_the_clone_checks_the_head_commit_alone(self) -> None:
        head = _commit(self.repo, {"dagents/lma/main.py": "a = 2\n", "docs/learn/README.md": "x\n"}, "Both")
        code, output = self.run_check("--base", "1" * 40, "--head", head)
        self.assertEqual(0, code, output)
        self.assertIn("is not in this clone", output)

    def test_a_root_commit_has_nothing_to_compare(self) -> None:
        code, output = self.run_check("--base", "", "--head", self.base)
        self.assertEqual(0, code, output)
        self.assertIn("nothing to compare", output)

    def test_an_unknown_head_is_a_usage_error(self) -> None:
        code, output = self.run_check("--base", self.base, "--head", "no-such-branch")
        self.assertEqual(2, code)
        self.assertIn("not a commit", output)

    def test_a_missing_base_argument_is_a_usage_error(self) -> None:
        self.assertEqual(2, self.run_check()[0])

    def test_ci_gets_an_error_annotation(self) -> None:
        head = _commit(self.repo, {"dagents/lma/main.py": "a = 2\n"}, "Change the LMA")
        previous = os.environ.get("GITHUB_ACTIONS")
        os.environ["GITHUB_ACTIONS"] = "true"
        try:
            code, output = self.run_check("--base", self.base, "--head", head)
        finally:
            if previous is None:
                del os.environ["GITHUB_ACTIONS"]
            else:
                os.environ["GITHUB_ACTIONS"] = previous
        self.assertEqual(1, code)
        self.assertIn("::error title=Docs did not change with the code::agents", output)


class AreaMapTests(unittest.TestCase):
    """The map names real files, so moving a doc or a folder cannot leave an
    area checking nothing."""

    TRACKED = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()

    def tracked_or_new(self) -> list[str]:
        """Tracked files plus untracked ones that are not ignored, so the test
        also works before new docs are committed."""
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout.splitlines()
        return self.TRACKED + untracked

    def test_every_code_pattern_matches_code(self) -> None:
        files = self.tracked_or_new()
        for area in impact.AREAS:
            for pattern in area.code:
                with self.subTest(area=area.name, pattern=pattern):
                    self.assertTrue(
                        any(impact.matches(path, (pattern,)) and impact.is_code(path, area) for path in files),
                        f"{pattern} matches no code file; update AREAS in scripts/check_docs_impact.py",
                    )

    def test_every_docs_pattern_matches_a_file(self) -> None:
        files = self.tracked_or_new()
        for area in impact.AREAS:
            for pattern in area.docs:
                with self.subTest(area=area.name, pattern=pattern):
                    self.assertTrue(
                        any(impact.matches(path, (pattern,)) for path in files),
                        f"{pattern} matches no file; update AREAS in scripts/check_docs_impact.py",
                    )

    def test_no_doc_is_also_code(self) -> None:
        """A file that counted as both would satisfy its own area."""
        files = self.tracked_or_new()
        for area in impact.AREAS:
            with self.subTest(area=area.name):
                both = [path for path in files if impact.is_code(path, area) and impact.matches(path, area.docs)]
                self.assertEqual([], both)

    def test_area_names_are_unique_and_in_the_skill(self) -> None:
        names = [area.name for area in impact.AREAS]
        self.assertEqual(len(names), len(set(names)))
        skill = SKILL.read_text(encoding="utf-8")
        for name in names:
            with self.subTest(area=name):
                self.assertIn(f"`{name}`", skill, "the docs-sync skill's table must name every area")


class WorkflowTests(unittest.TestCase):
    """CI runs the check on pull requests and pushes to main, with the history it needs."""

    CI = CI_WORKFLOW.read_text(encoding="utf-8")

    def job(self) -> str:
        found = jobs(self.CI)
        self.assertTrue("docs-impact" in found, "ci.yml has no docs-impact job")
        return found["docs-impact"]

    def test_the_job_fetches_the_history_and_runs_the_check(self) -> None:
        job = self.job()
        self.assertIn("fetch-depth: 0", job)
        self.assertTrue(any("scripts/check_docs_impact.py" in block for block in run_blocks(job)))

    def test_no_expression_is_pasted_into_its_scripts(self) -> None:
        for block in run_blocks(self.job()):
            self.assertNotIn("${{", block)

    def test_the_gate_needs_it(self) -> None:
        gate = jobs(self.CI)["ci"]
        needs = gate.split("needs:", 1)[1].split("steps:", 1)[0]
        self.assertIn("- docs-impact", needs)

    def test_it_runs_on_pull_requests_and_pushes_to_main(self) -> None:
        trigger = self.CI.split("\non:", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("pull_request:", trigger)
        self.assertIn("branches: [main]", trigger)


if __name__ == "__main__":
    unittest.main()
