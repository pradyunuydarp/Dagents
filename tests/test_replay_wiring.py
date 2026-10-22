"""The stroke demo's replay build: what the page posts, what the capture records, and where CI opens it.

When the Pages workflow cannot use the deployed healthcare API, it publishes
the demo as a replay of ``recording.json``. The page finds a POST in the
recording by its exact body (``keyFor`` in ``frontend/src/api.ts``). So the
body the guard panel sends and the body the capture records are kept equal,
and a key added on one side only turns every Guard answer into "Not recorded".
Before this test, only a person kept them equal.

These checks read the source and the workflows. They need no browser:

- the keys and constant values of the probe and pilot bodies in
  ``GuardProbe.tsx`` and ``PilotPanel.tsx`` equal the bodies
  ``scripts/capture_demo_recordings.py`` records;
- each granularity the guard panel offers is recorded, and the panel starts on
  a granularity and a cohort size the recording holds;
- the ``frontends`` job in ``ci.yml`` and the build job in ``pages.yml`` build
  a replay copy and drive it with ``replay-smoke.mjs`` in a browser, in a step
  that fails when the drive exits 2 (no browser) and gets every value through
  ``env:``;
- the ``pages.yml`` build job installs its browser with the playwright-core
  that a lockfile pins, because that job can deploy to Pages.

``apps/healthcare-demo/frontend/replay-smoke.mjs`` is the browser check itself.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import capture_demo_recordings as capture  # noqa: E402  (needs the sys.path entry)

FRONTEND = "apps/healthcare-demo/frontend"
GUARD_PROBE = REPO_ROOT / FRONTEND / "src" / "components" / "GuardProbe.tsx"
PILOT_PANEL = REPO_ROOT / FRONTEND / "src" / "components" / "PilotPanel.tsx"
REPLAY_SMOKE = REPO_ROOT / FRONTEND / "replay-smoke.mjs"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"

#: The command every replay drive step runs, and nothing else, so its exit
#: code is the step's: 1 for a failure and 2 when no browser was found.
DRIVE_COMMAND = 'node replay-smoke.mjs "$REPLAY_DIST" --base "$REPLAY_BASE"'
BUILD_COMMAND = 'npx vite build --outDir "$REPLAY_DIST" --emptyOutDir'
#: How the Pages build job installs a browser. `npx --no` runs the
#: playwright-core command that `npm ci` installed from the lockfile, and
#: downloads nothing. `npx playwright` names a package no lockfile holds, so
#: npx would fetch the newest playwright from the registry and run it in a job
#: that can deploy to Pages.
PAGES_BROWSER_INSTALL = "npx --no playwright-core install --with-deps chromium"


# --- reading the page's source ----------------------------------------------


def literal(text: str) -> object:
    """The value of a JavaScript literal (a string, a number, true, false or null), or raise ValueError."""
    text = text.strip()
    if re.fullmatch(r'"[^"\\]*"|-?\d+(\.\d+)?|true|false|null', text):
        return json.loads(text)
    raise ValueError(f"not a literal: {text}")


def posted_body(source: str, path: str) -> dict[str, str]:
    """The object literal a component posts to `path`: each key with the source text of its value.

    A shorthand property (``verified``) maps to its own name, a variable.
    Raises AssertionError when the source has no single flat ``postJson`` call
    to that path, so a rewrite of the call fails here instead of testing nothing.
    """
    calls = re.findall(r'postJson<\w+>\(\s*"' + re.escape(path) + r'"\s*,\s*\{(?P<body>[^{}]*)\}\s*\)', source)
    if len(calls) != 1:
        raise AssertionError(f"expected one flat postJson call to {path}, found {len(calls)}")
    body: dict[str, str] = {}
    for part in calls[0].split(","):
        part = part.strip()
        if not part:
            continue
        key, colon, value = part.partition(":")
        body[key.strip()] = value.strip() if colon else key.strip()
    return body


def initial_state(source: str, name: str) -> str:
    """The source text of the value a `useState` hook named `name` starts with."""
    match = re.search(r"const \[" + re.escape(name) + r", set\w+\] = useState(?:<[^>]*>)?\(([^()]*)\);", source)
    if not match:
        raise AssertionError(f"no useState for {name}")
    return match.group(1)


class ParserTests(unittest.TestCase):
    """The readers above, on inputs whose answer is known."""

    def test_a_body_with_shorthand_and_literal_values(self) -> None:
        source = 'await postJson<X>("/api/v1/a", {\n  site_id: siteId,\n  verified,\n  boundary: "before_read"\n})'
        self.assertEqual(
            {"site_id": "siteId", "verified": "verified", "boundary": '"before_read"'}, posted_body(source, "/api/v1/a")
        )

    def test_a_missing_or_doubled_call_is_an_error(self) -> None:
        with self.assertRaises(AssertionError):
            posted_body('postJson<X>("/api/v1/b", {})', "/api/v1/a")
        twice = 'postJson<X>("/api/v1/a", { a: 1 }); postJson<X>("/api/v1/a", { b: 2 })'
        with self.assertRaises(AssertionError):
            posted_body(twice, "/api/v1/a")

    def test_literals(self) -> None:
        self.assertEqual("ui", literal('"ui"'))
        self.assertEqual(25, literal("25"))
        self.assertIs(True, literal("true"))
        with self.assertRaises(ValueError):
            literal("cohort")


# --- the page and the capture ------------------------------------------------


class ProbeBodyTests(unittest.TestCase):
    """The guard panel's body equals the body the capture records."""

    def setUp(self) -> None:
        self.source = GUARD_PROBE.read_text(encoding="utf-8")
        self.page = posted_body(self.source, capture.PROBE_PATH)
        self.recorded = capture.probe_body("uk-134", True, "row", 25)

    def test_the_page_and_the_capture_send_the_same_keys(self) -> None:
        self.assertEqual(
            sorted(self.recorded),
            sorted(self.page),
            "GuardProbe.tsx and capture_demo_recordings.probe_body send different keys, so the replay "
            "would answer every probe 'Not recorded'. Change both, then record again.",
        )

    def test_each_constant_value_is_the_one_recorded(self) -> None:
        constants = {}
        for key, text in self.page.items():
            try:
                constants[key] = literal(text)
            except ValueError:
                continue
        self.assertIn("boundary", constants, "the page no longer sends a fixed boundary")
        for key, value in constants.items():
            with self.subTest(key=key):
                self.assertEqual(value, self.recorded.get(key))

    def test_every_granularity_the_page_offers_is_recorded(self) -> None:
        block = re.search(r"const GRANULARITIES[^=]*=\s*\[(.*?)\];", self.source, re.DOTALL)
        self.assertIsNotNone(block, "GuardProbe.tsx has no GRANULARITIES list")
        offered = re.findall(r'value:\s*"([a-z_]+)"', block.group(1))
        self.assertTrue(offered)
        self.assertEqual(sorted(capture.GRANULARITIES), sorted(offered))

    def test_the_controls_start_on_recorded_values(self) -> None:
        """A visitor who selects "Ask the guard" at once must get an answer, not "Not recorded"."""
        self.assertIn(literal(initial_state(self.source, "granularity")), capture.GRANULARITIES)
        self.assertIn(literal(initial_state(self.source, "cohort")), capture.COHORT_SIZES)


class PilotBodyTests(unittest.TestCase):
    def test_the_pilot_panel_sends_the_body_the_capture_records(self) -> None:
        page = posted_body(PILOT_PANEL.read_text(encoding="utf-8"), capture.PILOT_PATH)
        self.assertEqual(dict(capture.PILOT_BODY), {key: literal(text) for key, text in page.items()})


class CaptureUsesTheBodiesTests(unittest.TestCase):
    """The capture posts through the functions the tests above compare, not a copy of them."""

    def test_the_capture_posts_the_shared_bodies(self) -> None:
        source = (REPO_ROOT / "scripts" / "capture_demo_recordings.py").read_text(encoding="utf-8")
        for call in ("recorder.post(PROBE_PATH, probe_body(", "recorder.post(PILOT_PATH, dict(PILOT_BODY))"):
            with self.subTest(call=call):
                self.assertEqual(1, source.count(call), f"the capture should post once with {call}...")
        for path in (capture.PROBE_PATH, capture.PILOT_PATH):
            with self.subTest(path=path):
                self.assertEqual(1, source.count(f'"{path}"'), f"{path} should be written once, in its constant")


# --- the workflows -------------------------------------------------------------


def steps_of(workflow: Path, job: str) -> list[dict]:
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"][job]["steps"]


def locked_version(folder: str, package: str) -> str | None:
    """The version of `package` that the lockfile in `folder` pins, or None."""
    lock = json.loads((REPO_ROOT / folder / "package-lock.json").read_text(encoding="utf-8"))
    return lock.get("packages", {}).get(f"node_modules/{package}", {}).get("version")


def index_of(steps: list[dict], name: str, where: str) -> int:
    for index, step in enumerate(steps):
        if step.get("name") == name:
            return index
    raise AssertionError(f"{where} has no step named {name!r}")


class DriveStepRules:
    """What every replay drive step must look like, whichever workflow runs it."""

    def assert_drive_step(self, step: dict, build: dict) -> None:
        self.assertEqual(DRIVE_COMMAND, step["run"].strip(), "the step must run the drive alone, so exit 2 fails it")
        self.assertEqual(FRONTEND, step.get("working-directory"))
        self.assertNotIn("continue-on-error", step)
        self.assertNotIn("if", step)
        self.assertEqual(build["env"]["REPLAY_DIST"], step["env"]["REPLAY_DIST"], "the drive opens another folder")
        self.assertEqual(build["env"]["DEMO_BASE_PATH"], step["env"]["REPLAY_BASE"], "the drive serves another base path")

    def assert_replay_build(self, step: dict) -> None:
        self.assertEqual(BUILD_COMMAND, step["run"].strip())
        self.assertEqual(FRONTEND, step.get("working-directory"))
        self.assertEqual("1", step["env"].get("VITE_DAGENTS_STATIC"))
        # Empty, not left out: a set address would make the page call that API.
        self.assertEqual("", step["env"].get("VITE_HEALTHCARE_API_BASE"))
        self.assertTrue(step["env"]["DEMO_BASE_PATH"].endswith("/healthcare-demo/"))
        self.assertTrue(
            step["env"]["REPLAY_DIST"].startswith("${{ runner.temp }}/"),
            "the replay copy goes to a scratch folder, never into the published dist",
        )

    def assert_no_expression_in_run(self, steps: list[dict]) -> None:
        for step in steps:
            with self.subTest(step=step.get("name")):
                self.assertNotIn("${{", step.get("run", ""), "pass values through env:, not into run:")

    def assert_browser_located_before(self, steps: list[dict], drive: int) -> None:
        locate = index_of(steps, "Locate the browser", "the job")
        self.assertLess(locate, drive)
        self.assertIn('echo "CHROMIUM_PATH=${browser}" >> "$GITHUB_ENV"', steps[locate]["run"])
        self.assertIn("exit 1", steps[locate]["run"], "a missing browser must fail the job, not reach the drive")


class CiWorkflowTests(DriveStepRules, unittest.TestCase):
    CAPTURE = "Capture a recording for the healthcare replay"
    CHECK = "Check the recording for the healthcare replay"
    BUILD = "Build the healthcare replay"
    DRIVE = "Drive the healthcare replay in a browser"

    def setUp(self) -> None:
        self.steps = steps_of(CI_WORKFLOW, "frontends")
        self.at = {name: index_of(self.steps, name, "the frontends job") for name in (self.CAPTURE, self.CHECK, self.BUILD, self.DRIVE)}

    def test_capture_check_build_and_drive_run_in_order(self) -> None:
        order = [self.at[name] for name in (self.CAPTURE, self.CHECK, self.BUILD, self.DRIVE)]
        self.assertEqual(sorted(order), order)

    def test_the_capture_uses_the_planner_and_a_free_port(self) -> None:
        step = self.steps[self.at[self.CAPTURE]]
        self.assertIn("scripts/capture_demo_recordings.py --demo healthcare --port", step["run"])
        self.assertIn("bind((\"127.0.0.1\", 0))", step["run"], "the port comes from the system, not a fixed number")
        self.assertEqual("${{ env.PLANNER_DIR }}/dagentsc.exe", step["env"]["DAGENTSC_BIN"])

    def test_the_recording_is_checked_before_the_build(self) -> None:
        self.assertEqual("python scripts/check_demo_recordings.py healthcare", self.steps[self.at[self.CHECK]]["run"].strip())

    def test_the_replay_copy_is_built_and_driven(self) -> None:
        build = self.steps[self.at[self.BUILD]]
        self.assert_replay_build(build)
        self.assertEqual("/Dagents/healthcare-demo/", build["env"]["DEMO_BASE_PATH"])
        self.assert_drive_step(self.steps[self.at[self.DRIVE]], build)
        self.assert_browser_located_before(self.steps, self.at[self.DRIVE])

    def test_no_value_is_pasted_into_a_run_script(self) -> None:
        self.assert_no_expression_in_run([self.steps[index] for index in self.at.values()])

    def test_the_frontends_job_is_in_the_gate(self) -> None:
        jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
        self.assertIn("frontends", jobs["ci"]["needs"])


class PagesWorkflowTests(DriveStepRules, unittest.TestCase):
    MAIN_BUILD = "Build the healthcare demo"
    BUILD = "Build a replay copy of the healthcare demo"
    DRIVE = "Drive the healthcare replay in a browser"

    def setUp(self) -> None:
        self.steps = steps_of(PAGES_WORKFLOW, "build")
        self.at = {
            name: index_of(self.steps, name, "the Pages build job") for name in (self.MAIN_BUILD, self.BUILD, self.DRIVE)
        }
        self.upload = next(i for i, step in enumerate(self.steps) if "upload-pages-artifact" in step.get("uses", ""))
        # Found by its command, not its name, so one install step can serve
        # every browser check in this job.
        self.installs = [i for i, step in enumerate(self.steps) if "install --with-deps chromium" in step.get("run", "")]

    def test_the_replay_copy_is_built_after_the_published_build(self) -> None:
        """tests/test_social_previews.py reads the first healthcare build step, which must stay the published one."""
        first = next(
            i
            for i, step in enumerate(self.steps)
            if step.get("working-directory") == FRONTEND and "npm run build" in step.get("run", "")
        )
        self.assertEqual(self.at[self.MAIN_BUILD], first)
        self.assertLess(self.at[self.MAIN_BUILD], self.at[self.BUILD])
        build = self.steps[self.at[self.BUILD]]
        self.assert_replay_build(build)
        self.assertEqual("${{ env.BASE_PATH }}/healthcare-demo/", build["env"]["DEMO_BASE_PATH"])

    def test_the_replay_is_driven_before_the_upload_whatever_mode_was_published(self) -> None:
        capture_check = index_of(self.steps, "Check the captures are usable", "the Pages build job")
        drive = self.at[self.DRIVE]
        self.assertLess(capture_check, self.at[self.BUILD])
        self.assertLess(self.at[self.BUILD], drive)
        self.assertLess(drive, self.upload)
        self.assertNotIn("if", self.steps[self.at[self.BUILD]], "the replay copy is built in both modes")
        self.assert_drive_step(self.steps[drive], self.steps[self.at[self.BUILD]])
        self.assert_browser_located_before(self.steps, drive)

    def test_the_browser_comes_from_the_locked_playwright_core(self) -> None:
        """This job can deploy to Pages, so it runs no package that a lockfile does not pin."""
        self.assertTrue(self.installs, "the Pages build job installs no browser")
        for index in self.installs:
            with self.subTest(step=self.steps[index].get("name")):
                self.assertEqual(PAGES_BROWSER_INSTALL, self.steps[index]["run"].strip())
        install = self.installs[0]
        self.assertLess(install, index_of(self.steps, "Locate the browser", "the Pages build job"))
        self.assertLess(install, self.at[self.DRIVE])
        folder = self.steps[install].get("working-directory")
        self.assertIsNotNone(folder, "the install must run where npm ci installed playwright-core")
        self.assertTrue(
            any(
                step.get("working-directory") == folder and "npm ci" in step.get("run", "")
                for step in self.steps[:install]
            ),
            f"no step runs npm ci in {folder} before the install",
        )
        # The command's version decides which browser build it installs, so it
        # must be the version the drive runs.
        self.assertIsNotNone(locked_version(folder, "playwright-core"), f"{folder} does not lock playwright-core")
        self.assertEqual(locked_version(FRONTEND, "playwright-core"), locked_version(folder, "playwright-core"))

    def test_the_replay_copy_is_not_published(self) -> None:
        assemble = self.steps[index_of(self.steps, "Assemble the published tree", "the Pages build job")]
        self.assertNotIn("REPLAY_DIST", assemble["run"])
        self.assertNotIn("healthcare-replay", assemble["run"])

    def test_no_value_is_pasted_into_a_run_script(self) -> None:
        replay_steps = [index for index in self.at.values() if index != self.at[self.MAIN_BUILD]] + self.installs
        self.assert_no_expression_in_run([self.steps[index] for index in replay_steps])


class ReplaySmokeExitCodeTests(unittest.TestCase):
    """The drive reports a missing browser with exit 2, which fails a step that runs it alone."""

    def test_every_skip_exits_2(self) -> None:
        lines = REPLAY_SMOKE.read_text(encoding="utf-8").splitlines()
        skips = [i for i, line in enumerate(lines) if 'console.log("SKIP' in line]
        self.assertEqual(2, len(skips), "one skip for playwright-core and one for the browser")
        for index in skips:
            self.assertEqual("process.exit(2);", lines[index + 1].strip())


if __name__ == "__main__":
    unittest.main()
