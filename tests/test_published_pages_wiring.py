"""The browser checks of the published pages, and the steps that run them.

`scripts/check_published_pages.mjs` opens the published pages in Chrome after
each Pages deploy, and `healthcare-api-check.yml` runs it every day against
the live stroke demo. Both run only on GitHub, against the public internet, so
these tests check what can be checked here:

- the script's rules, run with Node: a published page may send GET, HEAD and
  OPTIONS only, to any origin; requests and console errors are sorted into the
  published files' side and the API's side; a banner's commit is read and
  compared; a wrong option exits 1, never 2, which means "skipped";
- `pages.yml`: the build job reports how it built the stroke demo
  (`healthcare_mode`, from the same value the build reads), and the deploy job
  runs the browser check after the deploy guard and the link preview check,
  with no `if:` and no `continue-on-error`, and with the mode through `env:`;
- `healthcare-api-check.yml`: it runs on a schedule, the scheduled run sets
  the read-only switch of `test_deployed_api.py`, the run's first request is
  timed against the page's cold-start budget and fails the run when it is
  slower, the browser job runs after it and fails on an API problem, and no
  `${{ }}` appears inside a `run:` script;
- the read-only switch itself: `test_deployed_api.py`, run against a local
  stand-in for the API, sends no probe and no pilot with the switch on, and
  sends both without it;
- the browser check itself, in Chromium, against a stand-in Pages tree and a
  stand-in API on free ports: a working page passes, a page the API keeps
  waiting with 503 answers passes, and a page that sends a POST or misses a
  script file fails, while the stand-in API receives no POST.

The contracts job sets up Node and requires `RequestRuleTests` and
`ReadOnlySwitchTests` to run. The frontends job has Chromium, runs
`StandInBrowserTests` and requires it to run.
"""

from __future__ import annotations

import functools
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_published_pages.mjs"
PAGES_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"
API_CHECK_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "healthcare-api-check.yml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PUBLISHED = REPO_ROOT / "env" / ".env.published"
HEALTHCARE_APP = REPO_ROOT / "apps" / "healthcare-demo"
TRANSPORT = HEALTHCARE_APP / "frontend" / "src" / "api.ts"

COMMAND = "node scripts/check_published_pages.mjs"
DEPLOY_GUARD = "One deploy attempt succeeded"
PREVIEW_CHECK = "Each published page's link preview image loads"
BROWSER_CHECK = "The published pages work in a browser"
MODE_STEP = "Record how the healthcare demo is built"
FIRST_REQUEST = "Is it answering at all"
READ_ONLY_EXPRESSION = "${{ github.event_name == 'schedule' && '1' || '' }}"
STAND_IN_CLASS = "tests.test_published_pages_wiring.StandInBrowserTests"


def page_budget_seconds() -> int:
    """The stroke demo's cold-start budget, from its transport, in seconds."""
    match = re.search(r"COLD_START_BUDGET_MS\s*=\s*([\d_]+)", TRANSPORT.read_text(encoding="utf-8"))
    assert match, "api.ts no longer defines COLD_START_BUDGET_MS"
    return int(match.group(1).replace("_", "")) // 1000


def run_node(script: str, payload: object) -> object:
    """Run an ES module script with Node, give it `payload` as JSON on stdin, and return its JSON output."""
    completed = subprocess.run(
        [shutil.which("node") or "node", "--input-type=module", "-e", script],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    if completed.returncode != 0:
        raise AssertionError(f"node failed: {completed.stderr}")
    return json.loads(completed.stdout)


def load_workflow(path: Path) -> dict:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    # YAML 1.1 reads the key `on` as True.
    workflow["on"] = workflow.get("on", workflow.pop(True, None))
    return workflow


def step_index(steps: list[dict], name: str) -> int:
    for index, step in enumerate(steps):
        if step.get("name") == name:
            return index
    raise AssertionError(f"no step named {name!r}")


#: Stands in for curl in the probe's first step: it writes a health answer to
#: the -o and -D files and prints the status for -w. With status 000 it writes
#: an empty headers file and fails, as curl does when nothing answers.
STUB_CURL = r"""#!/bin/bash
body=""; headers=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) body="$2"; shift 2 ;;
    -D) headers="$2"; shift 2 ;;
    -w|-H|--max-time) shift 2 ;;
    *) shift ;;
  esac
done
if [ "${STUB_STATUS}" = "000" ]; then
  : > "${headers}"
  echo "curl: (28) Operation timed out" >&2
  printf '000'
  exit 28
fi
printf '{"status":"ok"}' > "${body}"
printf 'HTTP/2 %s\r\ncontent-type: application/json\r\n\r\n' "${STUB_STATUS}" > "${headers}"
printf '%s' "${STUB_STATUS}"
"""

#: Stands in for `date +%s`: 1000 on the first call, 1000 + STUB_WAKE after.
STUB_DATE = r"""#!/bin/bash
calls="${STUB_FOLDER}/date-calls"
count=$(cat "${calls}" 2>/dev/null || echo 0)
echo $((count + 1)) > "${calls}"
if [ "${count}" = 0 ]; then echo 1000; else echo $((1000 + STUB_WAKE)); fi
"""


def run_step(script: str, env: dict[str, str]) -> tuple[subprocess.CompletedProcess, str]:
    """Run a workflow step's script with bash -e, as the runner does, and return what it wrote to GITHUB_OUTPUT."""
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "output"
        output.touch()
        completed = subprocess.run(
            ["bash", "-e", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output), **env},
        )
        return completed, output.read_text(encoding="utf-8")


class RequestRuleTests(unittest.TestCase):
    """The script's rules, run with Node. The CI contracts job sets up Node and requires this class to run."""

    PAGE = "https://example.github.io"
    API = "https://api.example.org"

    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("node") is None:
            raise unittest.SkipTest("node is not installed; the browser check's request rule cannot run")

    def call(self, function: str, cases: list[list]) -> list:
        script = f"""
import {{ readFileSync }} from "node:fs";
import {{ pathToFileURL }} from "node:url";
const module = await import(pathToFileURL({json.dumps(str(SCRIPT))}).href);
const origins = new Set([{json.dumps(self.PAGE)}]);
const cases = JSON.parse(readFileSync(0, "utf8"));
const results = cases.map((args) => module[{json.dumps(function)}](...args.map((arg) => (arg === "@origins" ? origins : arg))));
process.stdout.write(JSON.stringify(results));
"""
        return run_node(script, cases)

    def test_only_get_head_and_options_are_allowed_to_any_origin(self) -> None:
        targets = {
            "page": f"{self.PAGE}/Dagents/healthcare-demo/recording.json",
            "external": f"{self.API}/api/v1/pilot:run",
            "font": "https://fonts.googleapis.com/css2?family=Archivo",
        }
        methods = {"GET": True, "HEAD": True, "OPTIONS": True, "get": True,
                   "POST": False, "PUT": False, "PATCH": False, "DELETE": False, "post": False}
        cases = [[method, url, "@origins"] for method in methods for url in targets.values()]
        results = self.call("requestVerdict", cases)
        for (method, url, _), result in zip(cases, results):
            with self.subTest(method=method, url=url):
                self.assertEqual(methods[method], result["allowed"])
                side = next(name for name, target in targets.items() if target == url)
                self.assertEqual(side, result["side"])

    def test_each_request_is_sorted_by_where_it_goes(self) -> None:
        cases = {
            f"{self.PAGE}/Dagents/": "page",
            f"{self.PAGE}/Dagents/nl2sql-demo/assets/index.js?published-check=x": "page",
            "https://fonts.gstatic.com/s/archivo.woff2": "font",
            "data:image/svg+xml;base64,AAAA": "local",
            "blob:https://example.github.io/1234": "local",
            f"{self.API}/api/v1/overview": "external",
            "https://example.github.io:8443/Dagents/": "external",
            "not an address": "external",
        }
        results = self.call("sideOf", [[url, "@origins"] for url in cases])
        self.assertEqual(list(cases.values()), results)

    def test_console_errors_are_sorted_by_whose_problem_they_are(self) -> None:
        cases = [
            # A failed API request: Chrome names the resource as the location.
            (["Failed to load resource: net::ERR_CONNECTION_REFUSED", f"{self.API}/api/v1/overview", "@origins"], "external"),
            # A CORS refusal names the API in its text and the page script as its location.
            ([f"Access to fetch at '{self.API}/api/v1/overview' from origin '{self.PAGE}' has been blocked by CORS policy",
              f"{self.PAGE}/Dagents/healthcare-demo/assets/index.js", "@origins"], "external"),
            (["Failed to load resource: the server responded with a status of 404 ()",
              f"{self.PAGE}/Dagents/assets/index.js", "@origins"], "page"),
            (["Failed to load resource: net::ERR_FAILED", "https://fonts.googleapis.com/css2", "@origins"], "font"),
            (["Uncaught TypeError: x is undefined", "", "@origins"], "page"),
        ]
        results = self.call("consoleSide", [args for args, _ in cases])
        self.assertEqual([side for _, side in cases], results)

    def test_a_banner_commit_is_read_and_compared(self) -> None:
        full = "66b3691eb1ddcf2ea860c34d8db2fd8fa51aab44"
        shown = self.call("bannerCommit", [
            ["Recorded run. This page replays answers recorded from the real backend at commit 66b3691eb1 on 2026-10-21."],
            ["Recorded run. This page has no backend."],
        ])
        self.assertEqual(["66b3691eb1", None], shown)
        same = self.call("sameCommit", [
            ["66b3691eb1", full],
            ["66B3691EB1", full],
            ["0123456789", full],
            ["66b36", full],
            [None, full],
            ["66b3691eb1", ""],
        ])
        self.assertEqual([True, True, False, False, False, False], same)

    def test_the_published_file_is_read_as_the_shell_reads_it(self) -> None:
        text = "# comment=1\nA=one\nB=two=three\nA=last\n\nnot a pair\n"
        self.assertEqual([{"A": "last", "B": "two=three"}], self.call("readPublished", [[text]]))

    def test_every_page_key_is_in_the_published_file(self) -> None:
        script = f"""
import {{ readFileSync }} from "node:fs";
import {{ pathToFileURL }} from "node:url";
const module = await import(pathToFileURL({json.dumps(str(SCRIPT))}).href);
readFileSync(0, "utf8");
process.stdout.write(JSON.stringify({{ keys: module.PAGE_KEYS, pages: module.PAGES }}));
"""
        found = run_node(script, None)
        published = PUBLISHED.read_text(encoding="utf-8")
        self.assertEqual(["nl2sql", "site", "healthcare"], found["pages"])
        for page, key in found["keys"].items():
            with self.subTest(page=page):
                self.assertRegex(published, rf"(?m)^{key}=https://")

    def test_a_wrong_option_fails_instead_of_skipping(self) -> None:
        """Exit 2 means "no browser" and fails a CI step too, but it would be read as a skip elsewhere."""
        env = {key: value for key, value in os.environ.items() if key not in ("HEALTHCARE_MODE", "GITHUB_SHA")}
        cases = {
            "no build mode for the stroke demo": [],
            "an unknown build mode": ["--healthcare-mode", "maybe"],
            "an unknown page": ["--only", "nowhere"],
            "an unknown API rule": ["--healthcare-mode", "live", "--api-problems", "ignore"],
            "a short commit": ["--only", "nl2sql", "--commit", "66b3691"],
            "a warm-up with a replay": ["--only", "healthcare", "--healthcare-mode", "replay", "--warm-up"],
            "a warm-up without the stroke demo": ["--only", "nl2sql", "--warm-up"],
        }
        for name, arguments in cases.items():
            with self.subTest(case=name):
                completed = subprocess.run(
                    [shutil.which("node") or "node", str(SCRIPT), *arguments],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                    cwd=REPO_ROOT,
                )
                self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
                self.assertIn("FAIL:", completed.stdout)

    def test_the_api_address_is_replaced_in_every_message(self) -> None:
        message = f"Access to fetch at '{self.API}/api/v1/overview' from origin '{self.PAGE}' has been blocked"
        [redacted] = self.call("redactText", [[message, [self.API]]])
        self.assertNotIn("api.example.org", redacted)
        self.assertIn("<the API>/api/v1/overview", redacted)
        self.assertIn(self.PAGE, redacted, "the published origin is public and stays")
        [bare] = self.call("redactText", [["could not resolve api.example.org", [self.API, "api.example.org"]]])
        self.assertEqual("could not resolve <the API>", bare)

    def test_every_line_goes_through_the_redaction(self) -> None:
        """The script writes with say() only, which redacts the published API address and the one the page calls.

        The warm-up asks the published address, so it is hidden too.
        `StandInBrowserTests` checks a real run's output for the stand-in
        API's address.
        """
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(1, source.count("console.log("))
        self.assertIn("console.log(redact(line))", source)
        self.assertNotRegex(source, r"process\.(stdout|stderr)\.write")
        self.assertIn("hideOrigin(published.HEALTHCARE_DEMO_PUBLIC_API_URL)", source)
        self.assertIn("warmUp(published.HEALTHCARE_DEMO_PUBLIC_API_URL", source)
        self.assertNotIn("HEALTHCARE_API_BASE", source, "the warm-up address comes from the published file only")


class PagesWorkflowWiringTests(unittest.TestCase):
    """`pages.yml` runs the browser check after the deploy, with the build's healthcare mode."""

    def setUp(self) -> None:
        self.workflow = load_workflow(PAGES_WORKFLOW)
        self.build = self.workflow["jobs"]["build"]
        self.deploy = self.workflow["jobs"]["deploy"]

    def test_the_build_job_reports_how_it_built_the_healthcare_demo(self) -> None:
        steps = self.build["steps"]
        self.assertEqual("${{ steps.healthcare_mode.outputs.mode }}", self.build["outputs"]["healthcare_mode"])
        mode = step_index(steps, MODE_STEP)
        self.assertEqual("healthcare_mode", steps[mode].get("id"))
        probe = step_index(steps, "The live API is up and reading Postgres")
        build = step_index(steps, "Build the healthcare demo")
        self.assertTrue(probe < mode < build, "the mode is read after the probe may clear the address, before the build")
        # The mode comes from the value the build itself reads.
        self.assertEqual("${{ env.HEALTHCARE_API_BASE }}", steps[mode]["env"]["API"])
        self.assertEqual("${{ env.HEALTHCARE_API_BASE }}", steps[build]["env"]["VITE_HEALTHCARE_API_BASE"])
        self.assertNotIn("${{", steps[mode]["run"])
        self.assertNotIn("if", steps[mode])
        self.assertNotIn("continue-on-error", steps[mode])

    def test_the_mode_step_says_live_only_when_the_build_calls_the_api(self) -> None:
        script = self.build["steps"][step_index(self.build["steps"], MODE_STEP)]["run"]
        for api, expected in (("https://api.example.org", "live"), ("", "replay")):
            with self.subTest(api=api or "empty"):
                completed, output = run_step(script, {"API": api})
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertEqual(f"mode={expected}\n", output)
                self.assertNotIn("api.example.org", completed.stdout, "the step prints the mode, not the address")

    def test_the_browser_check_runs_after_a_successful_deploy(self) -> None:
        steps = self.deploy["steps"]
        guard = step_index(steps, DEPLOY_GUARD)
        previews = step_index(steps, PREVIEW_CHECK)
        check = step_index(steps, BROWSER_CHECK)
        self.assertLess(guard, check)
        self.assertLess(previews, check, "the curl checks run first, so a missing page fails fast")
        step = steps[check]
        # No `if:` and no continue-on-error: the step runs only after the guard
        # passed, and its failure fails the job.
        self.assertNotIn("if", step)
        self.assertNotIn("continue-on-error", step)
        self.assertEqual(COMMAND, step["run"].strip())
        self.assertEqual("${{ needs.build.outputs.healthcare_mode }}", step["env"]["HEALTHCARE_MODE"])
        self.assertEqual("build", self.deploy["needs"])

    def test_the_deploy_job_installs_the_driver_before_the_check(self) -> None:
        steps = self.deploy["steps"]
        guard = step_index(steps, DEPLOY_GUARD)
        check = step_index(steps, BROWSER_CHECK)
        checkout = next(index for index, step in enumerate(steps) if "actions/checkout" in step.get("uses", ""))
        node = next(index for index, step in enumerate(steps) if "actions/setup-node" in step.get("uses", ""))
        install = next(
            index for index, step in enumerate(steps)
            if step.get("working-directory") == "site" and step.get("run", "").strip() == "npm ci"
        )
        self.assertTrue(guard < checkout < check)
        self.assertTrue(guard < node < install < check)
        self.assertEqual("22", str(steps[node]["with"]["node-version"]))
        for index in (node, install):
            with self.subTest(step=steps[index].get("name") or steps[index].get("uses")):
                self.assertNotIn("if", steps[index])
                self.assertNotIn("continue-on-error", steps[index])

    def test_no_new_step_pastes_an_expression_into_its_script(self) -> None:
        steps = self.deploy["steps"]
        for index in (step_index(steps, BROWSER_CHECK), step_index(steps, "Install the browser driver")):
            with self.subTest(step=steps[index]["name"]):
                self.assertNotIn("${{", steps[index]["run"])


class ApiCheckWorkflowWiringTests(unittest.TestCase):
    """`healthcare-api-check.yml` runs every day, read-only, and fails red on its own."""

    def setUp(self) -> None:
        self.workflow = load_workflow(API_CHECK_WORKFLOW)
        self.jobs = self.workflow["jobs"]

    def test_it_runs_on_a_schedule_and_by_hand(self) -> None:
        triggers = self.workflow["on"]
        crons = [entry["cron"] for entry in triggers["schedule"]]
        self.assertEqual(1, len(crons))
        fields = crons[0].split()
        self.assertEqual(5, len(fields), crons[0])
        self.assertNotEqual("0", fields[0], "minute 0 is when GitHub delays scheduled runs the most")
        self.assertIn("workflow_dispatch", triggers, "a person can still run the full check")
        self.assertNotIn("push", triggers)
        self.assertNotIn("pull_request", triggers)

    def test_the_scheduled_run_sets_the_read_only_switch(self) -> None:
        steps = self.jobs["probe"]["steps"]
        tests = steps[step_index(steps, "Exercise the deployed services")]
        self.assertEqual(READ_ONLY_EXPRESSION, tests.get("env", {}).get("HEALTHCARE_DEPLOYED_API_READ_ONLY"))
        self.assertIn("test_deployed_api.py", tests["run"])

    def test_the_browser_job_fails_on_an_api_problem(self) -> None:
        page = self.jobs["page"]
        # After the probe, so the probe's first request is the run's first
        # request and the one that times the wake (see the test below).
        self.assertEqual("probe", page["needs"])
        self.assertEqual("${{ !cancelled() }}", page["if"])
        step = page["steps"][step_index(page["steps"], "The published stroke demo reads the API in a browser")]
        arguments = step["run"].split()
        self.assertEqual(COMMAND.split(), arguments[:2])
        self.assertIn("--api-problems", arguments)
        self.assertEqual("fail", arguments[arguments.index("--api-problems") + 1])
        self.assertEqual("healthcare", arguments[arguments.index("--only") + 1])
        self.assertIn("--warm-up", arguments)
        self.assertEqual("live", step["env"]["HEALTHCARE_MODE"])
        self.assertNotIn("continue-on-error", step)

    def test_the_browser_job_warms_up_the_api_the_published_page_calls(self) -> None:
        """A person can point the probe at another API; the browser job still opens the published page.

        So its warm-up asks the address in env/.env.published, which the
        script reads itself, and nothing in the job comes from the dispatch
        input or the probe job.
        """
        page = self.jobs["page"]
        text = json.dumps(page)
        self.assertNotIn("inputs.", text)
        self.assertNotIn("needs.probe.outputs", text)
        self.assertNotIn("HEALTHCARE_API_BASE", text)
        self.assertNotIn("--published", text)
        self.assertNotIn("outputs", self.jobs["probe"], "nothing reads an output of the probe job")
        description = self.workflow["on"]["workflow_dispatch"]["inputs"]["api"]["description"]
        self.assertIn("published page", description, "the input says the browser job ignores it")

    def probe_first_request(self, wake: int, status: str = "200") -> tuple[subprocess.CompletedProcess, str]:
        """Run "Is it answering at all" with stand-in curl and date commands that fake a wake of `wake` seconds.

        The step writes to fixed files under /tmp on the runner; here they go
        to a temporary folder instead.
        """
        steps = self.jobs["probe"]["steps"]
        script = steps[step_index(steps, FIRST_REQUEST)]["run"]
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            stubs = folder / "bin"
            stubs.mkdir()
            (stubs / "curl").write_text(STUB_CURL, encoding="utf-8")
            (stubs / "date").write_text(STUB_DATE, encoding="utf-8")
            for stub in stubs.iterdir():
                stub.chmod(0o755)
            github_env = folder / "github_env"
            github_env.touch()
            completed = subprocess.run(
                ["bash", "-e", "-c", script.replace("/tmp/", f"{folder}/")],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=REPO_ROOT,
                env={
                    "PATH": f"{stubs}:/usr/bin:/bin",
                    "API": "https://api.example.org",
                    "GITHUB_ENV": str(github_env),
                    "STUB_FOLDER": str(folder),
                    "STUB_WAKE": str(wake),
                    "STUB_STATUS": status,
                },
            )
            return completed, github_env.read_text(encoding="utf-8")

    def test_the_first_request_of_the_run_is_timed_against_the_page_budget(self) -> None:
        """The probe's first request wakes a sleeping service, so only it can time the wake.

        The browser job runs after the probe job and finds the service awake,
        so a wake longer than the page waits must fail the run here. The step
        still records the status, so the diagnosis steps after it run.
        """
        budget = page_budget_seconds()
        for wake, expected in ((2, 0), (budget - 30, 0), (budget, 0), (budget + 1, 1), (budget + 10, 1)):
            with self.subTest(wake=wake):
                completed, github_env = self.probe_first_request(wake)
                output = completed.stdout + completed.stderr
                self.assertEqual(expected, completed.returncode, output)
                self.assertIn("HEALTH_CODE=200", github_env, "the steps after this one need the status")
                self.assertIn(f"after {wake}s", completed.stdout)
                if expected:
                    self.assertIn("::error::", completed.stdout)
                    self.assertIn(f"{budget}s", completed.stdout, "the error names the page's budget")
                else:
                    self.assertNotIn("::error::", completed.stdout)

    def test_a_service_that_does_not_answer_fails_the_first_request(self) -> None:
        completed, github_env = self.probe_first_request(120, status="000")
        self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("HEALTH_CODE=000", github_env)
        self.assertIn("is not serving", completed.stdout)

    def test_the_diagnosis_still_runs_after_a_slow_wake(self) -> None:
        """A slow wake fails the first step, but the service is up, so every later step of the probe runs."""
        steps = self.jobs["probe"]["steps"]
        later = steps[step_index(steps, FIRST_REQUEST) + 1:]
        self.assertTrue(later)
        for step in later:
            with self.subTest(step=step.get("name") or step.get("uses")):
                self.assertIn("!cancelled()", step.get("if", ""))
                self.assertIn("env.HEALTH_CODE == '200'", step.get("if", ""))

    def test_no_script_holds_an_expression(self) -> None:
        """Values reach every script through env:, never through ${{ }} inside run: (the ci skill)."""
        for job_name, job in self.jobs.items():
            for step in job["steps"]:
                if "run" in step:
                    with self.subTest(job=job_name, step=step.get("name")):
                        self.assertNotIn("${{", step["run"])


def test_outcomes(log: str) -> dict[str, str]:
    """Each test's outcome line in a verbose unittest log. A test with a docstring prints its outcome on the next line."""
    outcomes: dict[str, str] = {}
    lines = log.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(test_\w+) \(", line)
        if match:
            outcome = line if " ... " in line else (lines[index + 1] if index + 1 < len(lines) else "")
            outcomes[match.group(1)] = outcome.rsplit(" ... ", 1)[-1]
    return outcomes


class ReadOnlySwitchTests(unittest.TestCase):
    """`test_deployed_api.py` against a local stand-in for the API, with and without the read-only switch."""

    def serve(self) -> tuple[str, list[tuple[str, str]]]:
        """A stand-in that answers the health check and the overview, refuses every POST, and records each request."""
        seen: list[tuple[str, str]] = []
        overview = {"hospitals": [{"site_id": "uk-134"}], "release_gates": [], "minimum_cohort": 20, "framework_calls": []}

        class Handler(BaseHTTPRequestHandler):
            def answer(self, status: int, body: object) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Dagents-App", "healthcare-demo")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
                seen.append(("GET", self.path))
                if self.path in ("/health", "/api/v1/health"):
                    self.answer(200, {"status": "ok"})
                elif self.path == "/api/v1/overview":
                    self.answer(200, overview)
                else:
                    self.answer(404, {"detail": "Not Found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                seen.append(("POST", self.path))
                self.answer(403, {"detail": "refused by the stand-in"})

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", seen

    def run_suite(self, read_only: str) -> tuple[str, list[tuple[str, str]]]:
        try:
            import httpx  # noqa: F401 - the suite imports it
        except ImportError:
            self.skipTest("httpx is not installed; the deployed-service tests cannot run")
        api, seen = self.serve()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/tmp"),
            "HEALTHCARE_DEPLOYED_API": api,
            "HEALTHCARE_DEPLOYED_API_READ_ONLY": read_only,
        }
        # The command the workflow runs, from the app's folder.
        completed = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_deployed_api.py", "-v"],
            cwd=HEALTHCARE_APP,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        return completed.stdout + completed.stderr, list(seen)

    def test_the_read_only_run_sends_no_probe_and_no_pilot(self) -> None:
        log, seen = self.run_suite("1")
        self.assertIn(("GET", "/api/v1/health"), seen, log)
        posts = {path for method, path in seen if method != "GET"}
        self.assertEqual({"/api/v1/deployments"}, posts, "a read-only run may only send the POST the API refuses")
        outcomes = test_outcomes(log)
        skipped = [name for name, outcome in outcomes.items() if "skipped 'HEALTHCARE_DEPLOYED_API_READ_ONLY is set" in outcome]
        self.assertEqual(
            sorted(skipped),
            [
                "test_a_lever_outside_its_set_is_refused_at_the_boundary",
                "test_a_pilot_reports_a_decision_that_matches_its_gates",
                "test_a_probe_lists_the_lma_call_behind_it",
                "test_the_cohort_floor_applies_where_the_granularity_makes_it_matter",
                "test_the_guard_levers_change_the_strategy",
            ],
            log,
        )
        # The tests that only read still run, here against a stand-in that answers little.
        self.assertEqual(16, len(outcomes), log)
        for name in ("test_the_overview_lists_the_agent_calls_behind_it", "test_the_hosted_api_never_offers_to_add_a_hospital"):
            with self.subTest(test=name):
                self.assertNotIn("skipped", outcomes[name])

    def test_a_manual_run_sends_the_probes_and_the_pilot(self) -> None:
        log, seen = self.run_suite("")
        posts = {path for method, path in seen if method == "POST"}
        self.assertIn("/api/v1/governance:probe", posts, log)
        self.assertIn("/api/v1/pilot:run", posts, log)
        self.assertNotIn("HEALTHCARE_DEPLOYED_API_READ_ONLY is set", log)


def guard_step_arguments(job: dict, log: str) -> list[str]:
    """The words of the `ci_no_skipped_tests.py` call in `job` that checks `log`."""
    for step in job["steps"]:
        command = re.sub(r"\\\n\s*", " ", step.get("run", ""))
        if "ci_no_skipped_tests.py" in command and log in command:
            return command.split("ci_no_skipped_tests.py", 1)[1].split()
    raise AssertionError(f"no step checks {log} with ci_no_skipped_tests.py")


class CiWorkflowWiringTests(unittest.TestCase):
    """`ci.yml` runs the classes of this file where their dependencies are, and fails if they skip."""

    def setUp(self) -> None:
        self.jobs = load_workflow(CI_WORKFLOW)["jobs"]

    def test_the_contracts_job_requires_the_node_and_httpx_classes(self) -> None:
        """RequestRuleTests skips without Node and ReadOnlySwitchTests without httpx, so both must have run."""
        arguments = guard_step_arguments(self.jobs["contracts"], "contracts-suite.log")
        for name in ("RequestRuleTests", "ReadOnlySwitchTests"):
            with self.subTest(test_class=name):
                self.assertIn(f"tests.test_published_pages_wiring.{name}", arguments)

    def test_the_frontends_job_runs_the_stand_in_browser_check(self) -> None:
        steps = self.jobs["frontends"]["steps"]
        runner = next(
            index for index, step in enumerate(steps) if f"python -m unittest {STAND_IN_CLASS}" in step.get("run", "")
        )
        command = steps[runner]["run"]
        self.assertIn("set -o pipefail", command)
        self.assertIn("-v", command.split())
        log = re.search(r"tee (\S+)", command).group(1)
        arguments = guard_step_arguments(self.jobs["frontends"], log)
        self.assertIn(STAND_IN_CLASS, arguments, "the class skips without a browser, so it must have run")
        guard = next(index for index, step in enumerate(steps) if log in step.get("run", "") and "ci_no_skipped_tests.py" in step.get("run", ""))
        site_install = next(
            index for index, step in enumerate(steps)
            if step.get("working-directory") == "site" and "npm ci" in step.get("run", "")
        )
        browser = step_index(steps, "Locate the browser")
        self.assertTrue(site_install < runner < guard, "playwright-core comes from site/node_modules")
        self.assertLess(browser, runner, "the step needs CHROMIUM_PATH")
        for index in (runner, guard):
            with self.subTest(step=steps[index].get("name")):
                self.assertNotIn("continue-on-error", steps[index])
                self.assertNotIn("if", steps[index])


def browser_unavailable() -> str | None:
    """Why the browser check cannot run here, as the script itself decides, or None when it can."""
    node = shutil.which("node")
    if node is None:
        return "node is not installed"
    script = f"""
import {{ pathToFileURL }} from "node:url";
const module = await import(pathToFileURL({json.dumps(str(SCRIPT))}).href);
const reasons = [];
if (!module.loadPlaywright()) reasons.push("playwright-core is not installed (run npm ci in site/)");
if (!module.findBrowser()) reasons.push("no Chromium was found (set CHROMIUM_PATH)");
process.stdout.write(reasons.join("; "));
"""
    completed = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60, cwd=REPO_ROOT
    )
    # A script that does not load is broken, which is a failure, not a skip.
    if completed.returncode != 0:
        raise AssertionError(f"node could not load the script: {completed.stderr.strip()[:500]}")
    return completed.stdout.strip() or None


#: The stand-in stroke demo. It does what the real page does in the parts the
#: check reads: it reads the API with GET, retries 502, 503 and 504 answers,
#: lists the hospitals, says where the records are and how the agents run, and
#: draws a hospital's chart from the API. `window.STAND_IN.post` makes it send
#: a POST to the API when the page loads or when a hospital is opened.
STAND_IN_APP = r"""
const { api, post } = window.STAND_IN;
const main = document.querySelector("main");
const send = () => fetch(new URL("/api/v1/pilot:run", api), { method: "POST", body: "{}" }).catch(() => {});
async function getJson(path) {
  for (;;) {
    const response = await fetch(new URL(path, api));
    if (![502, 503, 504].includes(response.status)) return response.json();
    await new Promise((done) => setTimeout(done, 300));
  }
}
function openHospital(siteId) {
  if (post === "open") send();
  main.innerHTML = `<h1>${siteId}</h1><section data-panel="hospital-charts"><figure data-available="yes"><img class="chart-image" alt="chart"></figure></section>`;
  main.querySelector("img.chart-image").src = new URL(`/api/v1/charts/${siteId}.svg`, api).href;
}
if (post === "load") send();
getJson("/api/v1/overview").then((overview) => {
  const rows = main.querySelector('[data-panel="consortium"] tbody');
  for (const hospital of overview.hospitals) {
    const row = document.createElement("tr");
    row.dataset.site = hospital.site_id;
    row.innerHTML = `<td><a href="#/hospitals/${hospital.site_id}">${hospital.site_id}</a></td>`;
    row.querySelector("a").addEventListener("click", () => openHospital(hospital.site_id));
    rows.append(row);
  }
  document.querySelector("[data-agents-mode]").dataset.agentsMode = overview.agents_mode;
  document.querySelector(".foot").textContent = `Records: ${overview.records}`;
});
"""

STAND_IN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><link rel="icon" href="data:,"><title>Stand-in stroke demo</title></head>
<body>
<header><span class="wordmark">Stroke triage demo</span></header>
<div class="banners"></div>
<main><h1>Consortium</h1><section data-panel="consortium"><table><tbody></tbody></table></section></main>
<section data-panel="who-does-what"><span data-agents-mode="unknown"></span></section>
<footer class="foot"></footer>
<script src="config.js"></script>
<script src="app.js"></script>
</body></html>
"""

CHART = b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20"><rect width="40" height="20"/></svg>'


class StandInBrowserTests(unittest.TestCase):
    """The browser check in Chromium, against a stand-in Pages tree and a stand-in API on free ports.

    The check runs on the published origin only after a deploy, so a break in
    it would show there as a green run. These runs show that it passes a
    working page, stops and fails on a POST, and fails on a missing script
    file even when problems of the API only warn, as in the Pages deploy job.
    The CI frontends job has Chromium and requires this class to run.
    """

    @classmethod
    def setUpClass(cls) -> None:
        reason = browser_unavailable()
        if reason:
            raise unittest.SkipTest(f"the stand-in browser check cannot run: {reason}")

    def serve_api(self, waking: int) -> tuple[int, list[tuple[str, str]]]:
        """A stand-in API that records each request. The overview answers 503 `waking` times first, as a platform does while the API starts."""
        seen: list[tuple[str, str]] = []
        left = {"waking": waking}
        overview = {
            "hospitals": [{"site_id": site} for site in ("uk-134", "it-002", "ch-245")],
            "agents_mode": "in-process",
            "records": "Stored in Postgres, one schema per hospital.",
        }

        class Handler(BaseHTTPRequestHandler):
            def answer(self, status: int, body: bytes, content_type: str, app: bool = True) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                if app:
                    self.send_header("X-Dagents-App", "healthcare-demo")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
                seen.append(("GET", self.path))
                path = self.path.split("?", 1)[0]
                if path == "/api/v1/health":
                    self.answer(200, b'{"status": "ok"}', "application/json")
                elif path == "/api/v1/overview" and left["waking"] > 0:
                    left["waking"] -= 1
                    self.answer(503, b"waking up", "text/plain", app=False)
                elif path == "/api/v1/overview":
                    self.answer(200, json.dumps(overview).encode(), "application/json")
                elif path.startswith("/api/v1/charts/"):
                    self.answer(200, CHART, "image/svg+xml")
                else:
                    self.answer(404, b'{"detail": "Not Found"}', "application/json")

            def do_OPTIONS(self) -> None:  # noqa: N802
                seen.append(("OPTIONS", self.path))
                self.answer(204, b"", "text/plain")

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append((self.command, self.path))
                self.answer(200, b'{"written": true}', "application/json")

            do_PUT = do_PATCH = do_DELETE = do_POST

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1], seen

    def serve_pages(self, tree: Path) -> int:
        """A stand-in Pages server for `tree`, which ignores query strings as Pages does."""

        class Handler(SimpleHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(tree)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def check(self, *arguments: str, post: str = "", missing: str = "", waking: int = 0) -> tuple[int, str, list, int]:
        """Build the tree, start both stand-ins, run the check, and return its exit code, output, the API's requests and the API's port."""
        api_port, seen = self.serve_api(waking)
        api = f"http://127.0.0.1:{api_port}"
        directory = Path(tempfile.mkdtemp(prefix="published-check-"))
        self.addCleanup(shutil.rmtree, directory, True)
        demo = directory / "tree" / "Dagents" / "healthcare-demo"
        demo.mkdir(parents=True)
        for folder in (directory / "tree" / "Dagents", directory / "tree" / "Dagents" / "nl2sql-demo"):
            folder.mkdir(exist_ok=True)
            (folder / "index.html").write_text("<!doctype html><title>Stand-in</title>", encoding="utf-8")
        (demo / "index.html").write_text(STAND_IN_PAGE, encoding="utf-8")
        (demo / "config.js").write_text(f"window.STAND_IN = {json.dumps({'api': api, 'post': post})};\n", encoding="utf-8")
        (demo / "app.js").write_text(STAND_IN_APP, encoding="utf-8")
        if missing:
            (demo / missing).unlink()
        pages = f"http://127.0.0.1:{self.serve_pages(directory / 'tree')}/Dagents/"
        published = directory / "published.env"
        published.write_text(
            f"DAGENTS_PUBLIC_SITE_URL={pages}\n"
            f"NL2SQL_DEMO_PUBLIC_SITE_URL={pages}nl2sql-demo/\n"
            f"HEALTHCARE_DEMO_PUBLIC_SITE_URL={pages}healthcare-demo/\n"
            f"HEALTHCARE_DEMO_PUBLIC_API_URL={api}\n",
            encoding="utf-8",
        )
        env = {
            key: value for key, value in os.environ.items()
            if key not in ("HEALTHCARE_MODE", "GITHUB_SHA", "GITHUB_STEP_SUMMARY")
        }
        env["NO_PROXY"] = "127.0.0.1,localhost"
        completed = subprocess.run(
            [shutil.which("node") or "node", str(SCRIPT), "--published", str(published), "--only", "healthcare",
             "--healthcare-mode", "live", "--wait", "0", *arguments],
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
            cwd=REPO_ROOT,
        )
        output = completed.stdout + completed.stderr
        return completed.returncode, output, list(seen), api_port

    def assert_never_written(self, seen: list[tuple[str, str]], output: str) -> None:
        written = [(method, path) for method, path in seen if method not in ("GET", "HEAD", "OPTIONS")]
        self.assertEqual([], written, f"the stand-in API received a request that could write\n{output}")

    def test_a_working_live_page_passes_with_api_problems_failing(self) -> None:
        """The daily run's options: a page that reads the API with GET and draws everything passes."""
        code, output, seen, api_port = self.check("--api-problems", "fail", "--warm-up")
        self.assertEqual(0, code, output)
        self.assertIn("every check passed", output)
        self.assertIn(("GET", "/api/v1/health"), seen, "the warm-up asks the published API")
        self.assertIn(("GET", "/api/v1/overview"), seen)
        self.assert_never_written(seen, output)
        self.assertNotIn(f"127.0.0.1:{api_port}", output, "the API's address is printed")

    def test_a_cold_start_the_page_waits_out_passes(self) -> None:
        """A platform's 503 without X-Dagents-App while the API starts is retried by the page, not a fault."""
        code, output, seen, _ = self.check("--api-problems", "fail", waking=2)
        self.assertEqual(0, code, output)
        self.assertEqual(3, seen.count(("GET", "/api/v1/overview")), output)

    def test_a_post_when_the_page_loads_is_stopped_and_fails_the_run(self) -> None:
        """The Pages deploy job's options: a POST fails the run even when problems of the API only warn."""
        code, output, seen, api_port = self.check(post="load")
        self.assertEqual(1, code, output)
        self.assertIn("stopped a POST to <the API>/api/v1/pilot:run", output)
        self.assert_never_written(seen, output)
        self.assertNotIn(f"127.0.0.1:{api_port}", output)

    def test_a_post_while_the_api_part_runs_is_stopped_and_fails_the_run(self) -> None:
        """The page part passed before the POST, so the check must look at the page's requests again at the end."""
        code, output, seen, _ = self.check(post="open")
        self.assertEqual(1, code, output)
        self.assertIn("stopped a POST to <the API>/api/v1/pilot:run", output)
        self.assertIn(("GET", "/api/v1/charts/uk-134.svg"), seen, "the hospital page opened")
        self.assert_never_written(seen, output)

    def test_a_missing_script_file_fails_even_when_api_problems_only_warn(self) -> None:
        """The Pages deploy job's options: a fault of the published files fails the run."""
        code, output, seen, _ = self.check(missing="app.js")
        self.assertEqual(1, code, output)
        self.assertRegex(output, r"FAIL .*app\.js answered 404")
        self.assert_never_written(seen, output)


if __name__ == "__main__":
    unittest.main()
