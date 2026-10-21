"""Tests that CI and the Pages build check the site as Pages serves it.

GitHub Pages serves the framework site under `/Dagents/` and each demo in a
folder below it. A build that works at the root can still fail there: a demo
card that lost its base path links to `/healthcare-demo/`, and a demo built
for `/` asks for `/assets/...`. Both answer 404 on Pages, and the root check
(`npm run check`) passes them.

`site/check.mjs --tree DIR --base PATH` serves an assembled tree under its base
path the way Pages does (a missing file is a 404), runs every check of the site
there, and opens each demo the home page links. It needs Chromium and three
builds, so it runs in the `frontends` job of ci.yml and in the Pages build.
These tests read the two workflows and check that:

- the Pages build runs the check on `_site` after it assembles the tree and
  before it uploads it, under the base path it resolved, so a failure stops the
  deploy, with the browser build that the locked playwright-core installs;
- the `frontends` job builds the site once, for the published base path, as
  pages.yml builds it, builds the demos into the folders pages.yml uses, and
  runs the tree check in place of the root check, after the step that finds a
  browser, so a missing browser fails the job instead of skipping;
- no `run:` of these steps pastes a `${{ }}` value into the shell, none of
  them may fail without failing the job, and a time limit stops a check that
  hangs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import unittest

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PAGES_WORKFLOW = WORKFLOWS / "pages.yml"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
CHECK_SCRIPT = REPO_ROOT / "site" / "check.mjs"
SITE_CONFIG = REPO_ROOT / "site" / "src" / "config.ts"

#: The command both workflows run, from `site/`, once `BASE_PATH` holds the
#: base path without its last slash (`/Dagents`, or nothing on a custom domain).
TREE_CHECK = ["node", "check.mjs", "--tree", "../_site", "--base", "${BASE_PATH}/"]

#: Each frontend, the variable its Vite config reads its base path from, and
#: the key of its published address in env/.env.published.
FRONTENDS = {
    "site": ("SITE_BASE_PATH", "DAGENTS_PUBLIC_SITE_URL"),
    "apps/healthcare-demo/frontend": ("DEMO_BASE_PATH", "HEALTHCARE_DEMO_PUBLIC_SITE_URL"),
    "services/nl2sql-demo/frontend": ("DEMO_BASE_PATH", "NL2SQL_DEMO_PUBLIC_SITE_URL"),
}

#: The line of pages.yml's "Assemble the published tree" that puts the site at
#: the root of the tree. The frontends job runs the same line.
SITE_INTO_TREE = "cp -r site/dist/. _site/"

#: The browser install the Pages build may run. `npx --no` (or `--no-install`)
#: runs the playwright-core command that `npm ci` installed from the lock file
#: and downloads nothing; `npx playwright` fetches the newest playwright.
LOCKED_INSTALL = re.compile(r"npx --no(?:-install)? playwright-core install --with-deps chromium")

#: The tree check took up to about 3 minutes on a machine with a load of 8 to 11
#: on 4 CPUs. A step limit well above that stops a hung browser from holding
#: the job, and in pages.yml the deploys queued behind it, for hours.
CHECK_MINUTES = range(10, 31)


def load_steps(workflow: Path, job: str) -> list[dict]:
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][job]["steps"]


def step_index(test: unittest.TestCase, steps: list[dict], name: str) -> int:
    for index, step in enumerate(steps):
        if step.get("name") == name:
            return index
    test.fail(f"no step named {name!r}")


def tree_check_steps(steps: list[dict]) -> list[int]:
    return [index for index, step in enumerate(steps) if "check.mjs" in step.get("run", "") and "--tree" in step["run"]]


def browser_installs(steps: list[dict]) -> list[int]:
    return [index for index, step in enumerate(steps) if re.search(r"playwright(-core)? install", step.get("run", ""))]


def locked_playwright_core(folder: Path) -> str:
    """The playwright-core version a frontend's package-lock.json pins."""
    lock = json.loads((folder / "package-lock.json").read_text(encoding="utf-8"))
    return lock["packages"]["node_modules/playwright-core"]["version"]


def published_folders() -> dict[str, str]:
    """The folder of `_site` that pages.yml copies each frontend's `dist/` into ("" for the root)."""
    steps = load_steps(PAGES_WORKFLOW, "build")
    assemble = next(step for step in steps if step.get("name") == "Assemble the published tree")
    folders = {}
    for source, target in re.findall(r"cp -r (\S+)/dist/\. (_site/\S*)", assemble["run"]):
        folders[source] = target.removeprefix("_site").strip("/")
    return folders


class TreeCheckStepAsserts:
    """What every step that runs the tree check must hold, in either workflow. Mixed into the test cases below."""

    def the_tree_check(self: unittest.TestCase) -> int:
        """The index of the one step that runs the tree check."""
        checks = tree_check_steps(self.steps)
        self.assertEqual(1, len(checks), "the job runs `node check.mjs --tree` exactly once")
        return checks[0]

    def assert_tree_check(self: unittest.TestCase, step: dict) -> None:
        self.assertEqual("site", step.get("working-directory"), "playwright-core is installed in site/")
        self.assertEqual(TREE_CHECK, shlex.split(step["run"]))
        self.assertNotIn("${{", step["run"], "values reach the script through env:, never pasted into run:")
        # The link preview tags of the tree's home page name the published address.
        self.assertEqual("${{ env.DAGENTS_PUBLIC_SITE_URL }}", step.get("env", {}).get("PUBLIC_SITE_URL"))
        # Exit 1 (a failed check) and exit 2 (no browser) must both fail the job.
        self.assertNotIn("if", step)
        self.assertNotIn("continue-on-error", step)
        for tolerant in ("|| true", "|| exit 0", "; exit 0"):
            self.assertNotIn(tolerant, step["run"])
        self.assertIn(step.get("timeout-minutes"), CHECK_MINUTES, "a hung browser must not hold the job for hours")

    def assert_browser_found_before(self: unittest.TestCase, steps: list[dict], check: int) -> None:
        """A browser is installed and CHROMIUM_PATH points at it, or the job stops: check.mjs would SKIP."""
        install = browser_installs(steps)
        self.assertTrue(install and install[0] < check, "no browser is installed before the tree check")
        locate = [
            index for index, step in enumerate(steps)
            if "CHROMIUM_PATH=" in step.get("run", "") and "GITHUB_ENV" in step.get("run", "")
        ]
        self.assertTrue(locate and install[0] < locate[0] < check, "CHROMIUM_PATH is not set before the tree check")
        self.assertIn("exit 1", steps[locate[0]]["run"], "a missing browser must stop the job, not skip the check")
        for index in (install[0], locate[0]):
            self.assertNotIn("continue-on-error", steps[index])


class PagesBuildTreeCheckTests(TreeCheckStepAsserts, unittest.TestCase):
    """The Pages build checks the tree it is about to upload, under the base path it resolved."""

    def setUp(self) -> None:
        self.steps = load_steps(PAGES_WORKFLOW, "build")

    def test_the_tree_is_checked_after_it_is_assembled_and_before_the_upload(self) -> None:
        check = self.the_tree_check()
        assemble = step_index(self, self.steps, "Assemble the published tree")
        upload = next(index for index, step in enumerate(self.steps) if "upload-pages-artifact" in step.get("uses", ""))
        self.assertLess(assemble, check)
        self.assertLess(check, upload)
        self.assert_tree_check(self.steps[check])
        # The folder it checks is the one the upload sends.
        checked = os.path.normpath(os.path.join(self.steps[check]["working-directory"], TREE_CHECK[3]))
        self.assertEqual(self.steps[upload]["with"]["path"], checked)

    def test_a_failed_check_stops_the_deploy(self) -> None:
        workflow = yaml.safe_load(PAGES_WORKFLOW.read_text(encoding="utf-8"))
        self.assertIn("build", workflow["jobs"]["deploy"]["needs"])

    def test_the_base_path_is_the_one_pages_resolved(self) -> None:
        """BASE_PATH comes from configure-pages, so a custom domain served from the root is checked at the root."""
        resolve = step_index(self, self.steps, "Resolve the base path")
        self.assertIn('echo "BASE_PATH=${resolved}" >> "$GITHUB_ENV"', self.steps[resolve]["run"])
        self.assertLess(resolve, self.the_tree_check())

    def test_a_browser_is_found_before_the_check(self) -> None:
        self.assert_browser_found_before(self.steps, self.the_tree_check())

    def test_the_browser_is_the_build_the_locked_driver_expects(self) -> None:
        """A deploy must not depend on the newest playwright on the registry.

        `npx playwright install` in a folder that has only playwright-core
        downloads the newest playwright and installs that version's browser
        build. check.mjs drives the browser with the playwright-core from
        site/package-lock.json, whose `executablePath()` then names a build
        that was never installed. So every install in this job runs the locked
        playwright-core, in a folder that pins the same version as site/.
        """
        installs = browser_installs(self.steps)
        self.assertTrue(installs, "the build job installs no browser")
        expected = locked_playwright_core(REPO_ROOT / "site")
        for index in installs:
            step = self.steps[index]
            with self.subTest(step=step.get("name")):
                self.assertRegex(step["run"].strip(), f"^{LOCKED_INSTALL.pattern}$")
                folder = REPO_ROOT / step.get("working-directory", ".")
                self.assertEqual(expected, locked_playwright_core(folder))

    def test_the_site_dependencies_are_installed_before_the_check(self) -> None:
        installs = [
            index for index, step in enumerate(self.steps)
            if step.get("working-directory") == "site" and "npm ci" in step.get("run", "")
        ]
        self.assertTrue(installs and installs[0] < self.the_tree_check())


class FrontendsJobTreeCheckTests(TreeCheckStepAsserts, unittest.TestCase):
    """The frontends job builds the tree as pages.yml does and checks it under the published base path."""

    def setUp(self) -> None:
        self.steps = load_steps(CI_WORKFLOW, "frontends")

    def site_builds(self) -> list[int]:
        """The steps that build the framework site."""
        return [
            index for index, step in enumerate(self.steps)
            if step.get("working-directory") == "site" and re.search(r"npm run build|vite build", step.get("run", ""))
        ]

    def demo_tree_builds(self) -> dict[str, int]:
        """Each demo's build into `_site`, by its folder."""
        builds = {}
        for index, step in enumerate(self.steps):
            if "--outDir" in step.get("run", "") and "_site" in step["run"]:
                builds[step.get("working-directory", "")] = index
        return builds

    def site_into_tree(self) -> int:
        steps = [index for index, step in enumerate(self.steps) if SITE_INTO_TREE in step.get("run", "")]
        self.assertEqual(1, len(steps), f"one step runs `{SITE_INTO_TREE}`")
        return steps[0]

    def test_the_site_is_built_once_as_pages_builds_it(self) -> None:
        """One build, under the published base path. `npm run build` also typechecks (`tsc -b`)."""
        builds = self.site_builds()
        self.assertEqual(1, len(builds), "the job builds the framework site once")
        pages = load_steps(PAGES_WORKFLOW, "build")
        published = pages[step_index(self, pages, "Build the framework site")]
        step = self.steps[builds[0]]
        self.assertEqual(published["env"], step.get("env"))
        self.assertEqual(published["run"].split(), step["run"].split())
        self.assertEqual("${{ env.BASE_PATH }}/", step["env"]["SITE_BASE_PATH"])

    def test_the_site_goes_to_the_root_of_the_tree_as_pages_copies_it(self) -> None:
        copy = self.site_into_tree()
        pages = load_steps(PAGES_WORKFLOW, "build")
        self.assertIn(SITE_INTO_TREE, pages[step_index(self, pages, "Assemble the published tree")]["run"])
        lines = [line.strip() for line in self.steps[copy]["run"].splitlines()]
        self.assertIn("rm -rf _site", lines, "a tree left from an earlier step must not reach the check")
        self.assertLess(lines.index("rm -rf _site"), lines.index(SITE_INTO_TREE))
        self.assertLess(self.site_builds()[0], copy)
        # The copy empties the tree, so the demos are built into it afterwards.
        self.assertLess(copy, min(self.demo_tree_builds().values()))

    def test_the_demos_are_built_into_the_folders_pages_uses(self) -> None:
        builds = self.demo_tree_builds()
        folders = published_folders()
        self.assertEqual(set(FRONTENDS), set(folders), "pages.yml assembles the same three frontends")
        self.assertEqual(set(FRONTENDS) - {"site"}, set(builds), "each demo is built into the tree once")
        for folder, index in builds.items():
            step = self.steps[index]
            with self.subTest(frontend=folder):
                words = shlex.split(step["run"])
                self.assertEqual(["npx", "vite", "build"], words[:3])
                self.assertIn("--emptyOutDir", words)
                out = os.path.normpath(os.path.join(folder, words[words.index("--outDir") + 1]))
                self.assertEqual(os.path.normpath(os.path.join("_site", folders[folder])), out)
                self.assertNotIn("${{", step["run"])
                variable, address = FRONTENDS[folder]
                self.assertEqual(f"${{{{ env.BASE_PATH }}}}/{folders[folder]}/", step["env"].get(variable))
                self.assertEqual(f"${{{{ env.{address} }}}}", step["env"].get("PUBLIC_SITE_URL"))

    def test_the_demo_folders_are_the_ones_the_home_page_links(self) -> None:
        slugs = set(re.findall(r'slug: "([^"]+)"', SITE_CONFIG.read_text(encoding="utf-8")))
        demos = {folder for folder in published_folders().values() if folder}
        self.assertEqual(slugs, demos)

    def test_the_base_path_and_addresses_are_read_from_the_published_addresses(self) -> None:
        steps = [
            index for index, step in enumerate(self.steps)
            if "check_link_previews.py addresses --github-env" in step.get("run", "")
        ]
        self.assertEqual(1, len(steps))
        step = self.steps[steps[0]]
        self.assertIn('BASE_PATH=${base_path}" >> "$GITHUB_ENV"', step["run"])
        self.assertIn("DAGENTS_PUBLIC_SITE_URL", step["run"])
        self.assertNotIn("${{", step["run"])
        self.assertLess(steps[0], min([*self.site_builds(), *self.demo_tree_builds().values()]))

    def test_the_tree_is_checked_after_the_builds_and_the_browser(self) -> None:
        check = self.the_tree_check()
        self.assert_tree_check(self.steps[check])
        self.assertLess(max([self.site_into_tree(), *self.demo_tree_builds().values()]), check)
        self.assert_browser_found_before(self.steps, check)

    def test_the_site_is_checked_once_in_the_tree(self) -> None:
        """The tree check replaces the root check.

        It runs every check of `npm run check`, under Pages rules, and then
        opens the demos. Running both built and checked the site twice, which
        added about four minutes on a loaded machine for three new checks. The
        root check stays for local runs, and on a custom domain pages.yml runs
        the tree check under `/` before the deploy.
        """
        checks = [
            index for index, step in enumerate(self.steps)
            if re.search(r"check\.mjs|npm run check", step.get("run", ""))
        ]
        self.assertEqual([self.the_tree_check()], checks)


class ContractsJobTests(unittest.TestCase):
    """The contracts job runs this file and fails if the tests that need Node skipped."""

    def test_the_option_tests_must_run(self) -> None:
        steps = load_steps(CI_WORKFLOW, "contracts")
        guards = [step for step in steps if "ci_no_skipped_tests.py" in step.get("run", "")]
        self.assertEqual(1, len(guards))
        self.assertIn("--require-ran tests.test_site_check_wiring.CheckScriptOptionTests", guards[0]["run"])


class CheckScriptTests(unittest.TestCase):
    """The options the workflows pass, and the exit codes they rely on."""

    def test_the_header_documents_both_options(self) -> None:
        header = CHECK_SCRIPT.read_text(encoding="utf-8").split("*/", 1)[0]
        for option in ("--tree DIR", "--base PATH", "node check.mjs --tree DIR [--base /Dagents/]"):
            self.assertIn(option, header)

    def test_only_a_missing_browser_or_driver_exits_2(self) -> None:
        """Exit 2 means SKIP. A wrong option must fail with 1, never look like a skip."""
        source = CHECK_SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(2, source.count("process.exit(2)"))
        for line in [line for line in source.splitlines() if "process.exit(2)" in line]:
            self.assertEqual("process.exit(2);", line.strip())


class CheckScriptOptionTests(unittest.TestCase):
    """check.mjs refuses a wrong option with exit 1 before it needs a browser.

    These tests run check.mjs with Node. The contracts job sets up Node and
    requires this class to run (`--require-ran`), so a missing Node fails the
    job instead of skipping these tests.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("node") is None:
            raise unittest.SkipTest("node is not installed; check.mjs cannot run")

    def test_a_wrong_option_fails_before_the_browser_is_needed(self) -> None:
        for arguments in (["--tree"], ["--base", "/a?b"], ["--base", "../up"], ["--tree", "x", "dist"], ["--unknown"]):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    ["node", str(CHECK_SCRIPT), *arguments],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    cwd=REPO_ROOT / "site",
                )
                self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
                self.assertTrue(completed.stdout.startswith("FAIL:"), completed.stdout)


if __name__ == "__main__":
    unittest.main()
