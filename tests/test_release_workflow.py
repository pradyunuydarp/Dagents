"""The release workflow and the CI packaging job.

``.github/workflows/release.yml`` can publish to PyPI, so its rules are
checked here instead of trusted to review: least permissions, actions pinned
to a commit, no GitHub expression pasted into a shell script, TestPyPI before
PyPI, publishing only from a tag, and no stored credential.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PLATFORMS = {"manylinux_2_17_x86_64", "manylinux_2_17_aarch64", "macosx_11_0_arm64"}

#: The pins for the build backend, which every build in both workflows must use.
CONSTRAINTS = "${{ github.workspace }}/.github/build-constraints.txt"


def build_steps(jobs: dict) -> list[tuple[str, dict]]:
    """Every step that runs ``python -m build``, with its job name."""
    return [(name, step) for name, job in jobs.items() for step in steps(job) if "-m build" in step.get("run", "")]


def assert_builds_use_the_pinned_backend(case: unittest.TestCase, jobs: dict) -> None:
    found = build_steps(jobs)
    case.assertTrue(found, "no step builds a distribution")
    for name, step in found:
        with case.subTest(job=name, step=step.get("name")):
            environment = step.get("env", {})
            case.assertEqual(CONSTRAINTS, environment.get("PIP_CONSTRAINT"))
            case.assertEqual(CONSTRAINTS, environment.get("PIP_BUILD_CONSTRAINT"))
    checks = [step["run"] for job in jobs.values() for step in steps(job) if "--require-platform" in step.get("run", "")]
    case.assertTrue(checks)
    for run in checks:
        case.assertIn("--build-constraints .github/build-constraints.txt", run)


def load(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    # YAML 1.1 reads the key `on` as True.
    if True in document:
        document["on"] = document.pop(True)
    return document


def steps(job: dict) -> list[dict]:
    return job.get("steps", [])


def needs(job: dict) -> list[str]:
    value = job.get("needs", [])
    return [value] if isinstance(value, str) else list(value)


def upstream(jobs: dict, name: str) -> set[str]:
    """Every job ``name`` waits for, directly or through another job."""
    found: set[str] = set()
    pending = needs(jobs[name])
    while pending:
        current = pending.pop()
        if current not in found:
            found.add(current)
            pending.extend(needs(jobs[current]))
    return found


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = RELEASE.read_text(encoding="utf-8")
        cls.workflow = load(RELEASE)
        cls.jobs = cls.workflow["jobs"]

    def test_a_tag_starts_a_release_and_a_manual_run_is_a_dry_run(self) -> None:
        self.assertEqual({"push", "workflow_dispatch"}, set(self.workflow["on"]))
        self.assertEqual({"tags": ["v*"]}, self.workflow["on"]["push"])

    def test_permissions_are_granted_per_job_only(self) -> None:
        self.assertEqual({}, self.workflow["permissions"])
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertIn("permissions", job, "every job states the permissions it needs")

    def test_only_the_publish_jobs_can_mint_an_id_token(self) -> None:
        minting = {name for name, job in self.jobs.items() if job["permissions"].get("id-token") == "write"}
        self.assertEqual({"publish-testpypi", "publish-pypi", "github-release"}, minting)
        for name in minting:
            with self.subTest(job=name):
                condition = self.jobs[name].get("if", "")
                self.assertIn("github.event_name == 'push'", condition)
                self.assertIn("startsWith(github.ref, 'refs/tags/v')", condition)

    def test_testpypi_comes_before_pypi_and_each_has_its_environment(self) -> None:
        self.assertEqual("testpypi", self.jobs["publish-testpypi"]["environment"]["name"])
        self.assertEqual("pypi", self.jobs["publish-pypi"]["environment"]["name"])
        self.assertIn("verify-testpypi", upstream(self.jobs, "publish-pypi"))
        self.assertIn("publish-testpypi", upstream(self.jobs, "publish-pypi"))
        self.assertIn("check", upstream(self.jobs, "publish-testpypi"))
        self.assertIn("verify-pypi", upstream(self.jobs, "github-release"))
        testpypi = [s for s in steps(self.jobs["publish-testpypi"]) if "gh-action-pypi-publish" in s.get("uses", "")]
        self.assertEqual("https://test.pypi.org/legacy/", testpypi[0]["with"]["repository-url"])

    def test_a_tag_publishes_only_a_commit_on_main(self) -> None:
        """Only reviewed code reaches the publish jobs: the tagged commit must be on main."""
        checkout = steps(self.jobs["version"])[0]
        self.assertIn("actions/checkout", checkout["uses"])
        self.assertEqual(0, checkout["with"]["fetch-depth"], "the check needs the history of main")
        run = " ".join(s.get("run", "") for s in steps(self.jobs["version"]))
        self.assertIn("--require-on-branch origin/main", run)

    def test_every_build_uses_the_pinned_backend(self) -> None:
        assert_builds_use_the_pinned_backend(self, self.jobs)

    def test_every_job_waits_for_the_version_check(self) -> None:
        for name in self.jobs:
            if name != "version":
                with self.subTest(job=name):
                    self.assertIn("version", upstream(self.jobs, name))
        run = " ".join(s.get("run", "") for s in steps(self.jobs["version"]))
        self.assertIn('scripts/release_version.py --tag "${REF_NAME}"', run)

    def test_publishing_uses_trusted_publishing_with_attestations_and_no_token(self) -> None:
        self.assertNotIn("secrets.", self.text)
        for name in ("publish-testpypi", "publish-pypi"):
            publish = [s for s in steps(self.jobs[name]) if "gh-action-pypi-publish" in s.get("uses", "")]
            with self.subTest(job=name):
                self.assertEqual(1, len(publish))
                options = publish[0].get("with", {})
                self.assertNotIn("password", options)
                self.assertTrue(options.get("attestations"))
        users = {name for name, job in self.jobs.items() if any("gh-action-pypi-publish" in s.get("uses", "") for s in steps(job))}
        self.assertEqual({"publish-testpypi", "publish-pypi"}, users)

    def test_every_action_is_pinned_to_a_commit_with_its_version(self) -> None:
        uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)(.*)$", self.text, re.MULTILINE)
        self.assertTrue(uses)
        for action, rest in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
                self.assertRegex(rest, r"^\s+#\s+v\d+\.\d+\.\d+\s*$")
        # One version per action, so a bump cannot leave an old pin behind.
        pins: dict[str, set[str]] = {}
        for action, _ in uses:
            name, sha = action.split("@")
            pins.setdefault(name, set()).add(sha)
        self.assertEqual({name: 1 for name in pins}, {name: len(shas) for name, shas in pins.items()})

    def test_no_expression_is_pasted_into_a_shell_script(self) -> None:
        for name, job in self.jobs.items():
            for step in steps(job):
                if "run" in step:
                    with self.subTest(job=name, step=step.get("name")):
                        self.assertNotIn("${{", step["run"])

    def test_the_wheels_cover_the_three_platforms_and_are_checked_together(self) -> None:
        checked = {entry["check"] for entry in self.jobs["wheels"]["strategy"]["matrix"]["include"]}
        self.assertEqual(PLATFORMS, checked)
        built = {entry["target"] for entry in self.jobs["planner"]["strategy"]["matrix"]["include"]}
        self.assertEqual(built, {entry["target"] for entry in self.jobs["wheels"]["strategy"]["matrix"]["include"]})
        run = " ".join(s.get("run", "") for s in steps(self.jobs["check"]))
        self.assertEqual(PLATFORMS, set(re.findall(r"--require-platform (\S+)", run)))
        self.assertIn("--expect-planner none", run)

    def test_each_wheel_is_smoke_tested_with_its_bundled_planner(self) -> None:
        run = " ".join(s.get("run", "") for s in steps(self.jobs["wheels"]))
        self.assertIn("smoke_installed_package.py", run)
        self.assertIn("--expect-planner bundled", run)
        self.assertIn("env -u PYTHONPATH -u DAGENTSC_BIN", run)
        self.assertIn('-m build --wheel', run)
        self.assertIn("unpacked/dagents-", run, "the wheel is built from the sdist")

    def test_the_release_is_marked_as_a_pre_release_for_a_b_and_rc(self) -> None:
        run = " ".join(s.get("run", "") for s in steps(self.jobs["github-release"]))
        self.assertIn('if [ "${PRERELEASE}" = "true" ]', run)
        self.assertIn("--prerelease", run)
        self.assertIn("SHA256SUMS", run)
        outputs = self.jobs["version"]["outputs"]
        self.assertEqual({"version", "prerelease"}, set(outputs))


class CiPackagingJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = load(CI)
        cls.jobs = cls.workflow["jobs"]

    def test_the_gate_waits_for_every_job(self) -> None:
        """A job missing from the gate can fail without blocking a merge."""
        others = set(self.jobs) - {"ci"}
        self.assertEqual(others, set(needs(self.jobs["ci"])))
        self.assertIn("packaging", others)

    def test_the_packaging_job_builds_from_the_sdist_and_smoke_tests_both_wheels(self) -> None:
        job = self.jobs["packaging"]
        self.assertEqual(["planners"], needs(job))
        run = " ".join(s.get("run", "") for s in steps(job))
        self.assertIn("--require-platform manylinux_2_17_x86_64", run)
        self.assertIn("--expect-planner bundled", run)
        self.assertIn("--expect-planner none", run)
        self.assertIn("env -u PYTHONPATH -u DAGENTSC_BIN", run)
        downloads = [s for s in steps(job) if "download-artifact" in s.get("uses", "")]
        self.assertEqual("dagentsc-static", downloads[0]["with"]["name"])

    def test_the_wheel_is_smoke_tested_next_to_openai_agents(self) -> None:
        """The package was renamed from ``agents`` so that it installs next to openai-agents, whose module is ``agents``."""
        runs = [s.get("run", "") for s in steps(self.jobs["packaging"])]
        together = [run for run in runs if "--agents-from openai-agents" in run]
        self.assertEqual(1, len(together), "one step runs the smoke test with openai-agents installed")
        run = together[0]
        self.assertRegex(run, r'pip install --quiet "\$\(ls .*/dist/\*\.whl\)\[lma,gma\]" openai-agents==\d+\.\d+\.\d+\n')
        self.assertIn("pip check", run)
        self.assertIn("--expect-planner bundled", run)
        self.assertIn("env -u PYTHONPATH -u DAGENTSC_BIN", run)

    def test_every_build_uses_the_pinned_backend(self) -> None:
        assert_builds_use_the_pinned_backend(self, {"packaging": self.jobs["packaging"]})

    def test_the_agent_suite_also_runs_on_the_newest_versions_the_extras_allow(self) -> None:
        """The agents job pins the lowest versions; users who install dagents[lma,gma] get the newest."""
        runs = [s.get("run", "") for s in steps(self.jobs["packaging"])]
        suite = [run for run in runs if "discover -s dagents/tests" in run]
        self.assertEqual(1, len(suite), "one step runs the agent suite in the packaging job")
        run = suite[0]
        self.assertIn("[all]", run, "every extra, as the agents job installs psycopg and matplotlib too")
        self.assertIn("pip uninstall --yes", run, "the suite imports dagents from the checkout, not the wheel")
        self.assertIn("set -o pipefail", run)
        self.assertIn("--forbid planner", run)
        self.assertIn("--forbid charts", run)
        self.assertIn("--require-ran dagents.tests.test_agent_api.TokenScopeTests", run)

    def test_the_planners_job_uploads_the_static_planner(self) -> None:
        uploads = [s["with"]["name"] for s in steps(self.jobs["planners"]) if "upload-artifact" in s.get("uses", "")]
        self.assertEqual(["dagentsc", "dagentsc-static"], uploads)


if __name__ == "__main__":
    unittest.main()
