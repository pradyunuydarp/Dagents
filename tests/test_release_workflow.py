"""The release workflow and the CI packaging job.

``.github/workflows/release.yml`` can publish to PyPI, so its rules are
checked here instead of trusted to review: least permissions, actions pinned
to a commit, no GitHub expression pasted into a shell script, TestPyPI before
PyPI, publishing only from a run on a tag, and no stored credential.

A release starts in one of two ways: a push of the tag ``v<version>``, or a
manual run on ``main`` with the ``release_tag`` input ticked. That manual run
only creates the tag and starts the workflow again on the tag, so the run that
publishes is always on ``refs/tags/v...``. The job conditions are evaluated
here for every combination of event, ref and input (:func:`running_jobs`), and
the shell steps that decide what a run does are run with stand-in commands.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE = REPO_ROOT / ".github" / "workflows" / "release.yml"
CI = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PLATFORMS = {"manylinux_2_17_x86_64", "manylinux_2_17_aarch64", "macosx_11_0_arm64"}

#: The pins for the build backend, which every build in both workflows must use.
CONSTRAINTS = "${{ github.workspace }}/.github/build-constraints.txt"

#: The job that creates the release tag and starts the workflow on it.
TAG_JOB = "release-tag"

#: The jobs that upload files, check what an index serves, or create the GitHub Release.
PUBLISHING = {"publish-testpypi", "verify-testpypi", "publish-pypi", "verify-pypi", "github-release"}

#: The jobs a dry run runs: everything up to and including the checks.
DRY_RUN = {"version", "planner", "sdist", "wheels", "check"}

TAG_REF = "refs/tags/v0.1.0a1"
REFS = ("refs/heads/main", "refs/heads/feature", TAG_REF, "refs/tags/nightly")


def contexts() -> list[dict]:
    """Every combination of event, ref and input the conditions must handle."""
    found = []
    for ref in REFS:
        # A push run has no inputs; the expression `inputs.release_tag` is then null.
        found.append({"github": {"event_name": "push", "ref": ref}, "inputs": {}})
        for ticked in (True, False):
            found.append({"github": {"event_name": "workflow_dispatch", "ref": ref}, "inputs": {"release_tag": ticked}})
    return found


def describe(context: dict) -> str:
    return f"{context['github']['event_name']} on {context['github']['ref']} with inputs {context['inputs']}"


# --- A small evaluator for the GitHub expressions the job conditions use ------
#
# It covers literals, context paths, `!`, `==`, `!=`, `&&`, `||`, parentheses,
# startsWith and the status functions, with GitHub's rules: strings compare
# without case, operands of different types are compared as numbers, and a
# condition without a status function also needs every earlier job to succeed.

_TOKEN = re.compile(
    r"\s*(?:(?P<op>&&|\|\||==|!=|!|\(|\)|,)|(?P<str>'(?:[^']|'')*')|(?P<num>-?\d+(?:\.\d+)?)|(?P<name>[A-Za-z_][\w.-]*))"
)
_STATUS_FUNCTIONS = {"success", "always", "failure", "cancelled"}


def _truthy(value: object) -> bool:
    if isinstance(value, float) and math.isnan(value):
        return False
    return value not in (None, False, 0, "")


def _number(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if text == "":
        return 0.0
    return float(text) if re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", text) else math.nan


def _equal(left: object, right: object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if type(left) is type(right) and not isinstance(left, str):
        return left == right
    return _number(left) == _number(right)


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class _Expression:
    def __init__(self, text: str, context: dict, needs_succeeded: bool) -> None:
        text = text.strip()
        if text.startswith("${{") and text.endswith("}}"):
            text = text[3:-2].strip()
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(text):
            match = _TOKEN.match(text, position)
            if match is None or match.end() == position:
                raise ValueError(f"cannot read the expression from {text[position:]!r}")
            position = match.end()
            self.tokens.append((match.lastgroup, match.group(match.lastgroup)))
        self.index = 0
        self.context = context
        self.needs_succeeded = needs_succeeded
        self.uses_status_function = False

    def evaluate(self) -> object:
        value = self._or()
        if self.index != len(self.tokens):
            raise ValueError(f"unexpected {self.tokens[self.index][1]!r}")
        return value

    def _peek(self) -> str | None:
        return self.tokens[self.index][1] if self.index < len(self.tokens) else None

    def _take(self, expected: str | None = None) -> tuple[str, str]:
        if self.index >= len(self.tokens):
            raise ValueError("the expression ends too early")
        kind, token = self.tokens[self.index]
        if expected is not None and token != expected:
            raise ValueError(f"expected {expected!r}, found {token!r}")
        self.index += 1
        return kind, token

    def _or(self) -> object:
        value = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            value = value if _truthy(value) else right
        return value

    def _and(self) -> object:
        value = self._comparison()
        while self._peek() == "&&":
            self._take()
            right = self._comparison()
            value = right if _truthy(value) else value
        return value

    def _comparison(self) -> object:
        value = self._unary()
        if self._peek() in ("==", "!="):
            _, operator = self._take()
            equal = _equal(value, self._unary())
            return equal if operator == "==" else not equal
        return value

    def _unary(self) -> object:
        if self._peek() == "!":
            self._take()
            return not _truthy(self._unary())
        return self._primary()

    def _primary(self) -> object:
        kind, token = self._take()
        if token == "(":
            value = self._or()
            self._take(")")
            return value
        if kind == "str":
            return token[1:-1].replace("''", "'")
        if kind == "num":
            return float(token)
        if kind != "name":
            raise ValueError(f"unexpected {token!r}")
        if token in ("true", "false"):
            return token == "true"
        if token == "null":
            return None
        if self._peek() == "(":
            self._take("(")
            arguments = []
            if self._peek() != ")":
                arguments.append(self._or())
                while self._peek() == ",":
                    self._take()
                    arguments.append(self._or())
            self._take(")")
            return self._call(token, arguments)
        value: object = self.context
        for part in token.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        return value

    def _call(self, name: str, arguments: list[object]) -> object:
        if name in _STATUS_FUNCTIONS:
            self.uses_status_function = True
            return {"success": self.needs_succeeded, "always": True, "failure": False, "cancelled": False}[name]
        if name == "startsWith":
            return _text(arguments[0]).casefold().startswith(_text(arguments[1]).casefold())
        raise ValueError(f"the test evaluator does not know the function {name}()")


def job_starts(condition: object, context: dict, needs_succeeded: bool) -> bool:
    """Whether a job with this ``if:`` starts, when every job it needs succeeded or not."""
    if condition is None:
        return needs_succeeded
    if isinstance(condition, bool):
        return needs_succeeded and condition
    expression = _Expression(str(condition), context, needs_succeeded)
    value = _truthy(expression.evaluate())
    return value if expression.uses_status_function else needs_succeeded and value


def running_jobs(jobs: dict, context: dict) -> set[str]:
    """The jobs that start in a run with this context, when every job that starts succeeds."""
    decided: dict[str, bool] = {}

    def starts(name: str) -> bool:
        if name not in decided:
            succeeded = all(starts(other) for other in needs(jobs[name]))
            decided[name] = job_starts(jobs[name].get("if"), context, succeeded)
        return decided[name]

    return {name for name in jobs if starts(name)}


def run_step(script: str, environment: dict[str, str], stand_ins: dict[str, str]) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    """Run a step's shell script the way a runner does (``bash -e``), with stand-in commands.

    Each stand-in is a command name (``python``, ``gh``) that records its
    arguments and then runs the given shell snippet. Returns the finished
    process and the recorded calls, one argument list per call.
    """
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        calls = root / "calls.jsonl"
        for name, body in stand_ins.items():
            command = bin_dir / name
            command.write_text(
                "#!/usr/bin/env bash\n"
                f"python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' \"$@\" >> \"{calls}\"\n"
                f"{body}\n",
                encoding="utf-8",
            )
            command.chmod(0o755)
        for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
            (root / name).touch()
        env = {
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(root / "GITHUB_OUTPUT"),
            "GITHUB_STEP_SUMMARY": str(root / "GITHUB_STEP_SUMMARY"),
            **environment,
        }
        completed = subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True, text=True)
        completed.summary = (root / "GITHUB_STEP_SUMMARY").read_text(encoding="utf-8")
        recorded = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] if calls.exists() else []
        return completed, recorded


def step_named(job: dict, words: str) -> dict:
    found = [step for step in steps(job) if words in step.get("name", "")]
    if len(found) != 1:
        raise AssertionError(f"expected one step whose name contains {words!r}, found {len(found)}")
    return found[0]


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

    def test_a_tag_push_or_a_manual_run_starts_the_workflow(self) -> None:
        self.assertEqual({"push", "workflow_dispatch"}, set(self.workflow["on"]))
        self.assertEqual({"tags": ["v*"]}, self.workflow["on"]["push"])

    def test_the_manual_run_has_a_release_tag_input_that_is_off_by_default(self) -> None:
        inputs = (self.workflow["on"]["workflow_dispatch"] or {}).get("inputs", {})
        self.assertEqual({"release_tag"}, set(inputs))
        release_tag = inputs["release_tag"]
        self.assertEqual("boolean", release_tag["type"])
        self.assertIs(False, release_tag["default"], "an unticked run must stay a dry run")
        self.assertFalse(release_tag.get("required", False))
        self.assertIn("main", release_tag["description"])

    def test_permissions_are_granted_per_job_only(self) -> None:
        self.assertEqual({}, self.workflow["permissions"])
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertIn("permissions", job, "every job states the permissions it needs")

    def test_only_the_tag_job_can_push_a_tag_and_start_a_run(self) -> None:
        self.assertEqual({"contents": "write", "actions": "write"}, self.jobs[TAG_JOB]["permissions"])
        starting = {name for name, job in self.jobs.items() if job["permissions"].get("actions") == "write"}
        self.assertEqual({TAG_JOB}, starting)
        # github-release needs contents: write to create the GitHub Release; it runs only on a tag.
        writing = {name for name, job in self.jobs.items() if job["permissions"].get("contents") == "write"}
        self.assertEqual({TAG_JOB, "github-release"}, writing)
        for name, job in self.jobs.items():
            if name not in {TAG_JOB, "publish-testpypi", "publish-pypi", "github-release"}:
                with self.subTest(job=name):
                    self.assertEqual({"contents": "read"}, job["permissions"])

    def test_only_the_publish_jobs_can_mint_an_id_token(self) -> None:
        minting = {name for name, job in self.jobs.items() if job["permissions"].get("id-token") == "write"}
        self.assertEqual({"publish-testpypi", "publish-pypi", "github-release"}, minting)
        for context in contexts():
            started = running_jobs(self.jobs, context) & minting
            if started:
                with self.subTest(context=describe(context)):
                    self.assertTrue(context["github"]["ref"].startswith("refs/tags/v"), started)

    def test_publishing_happens_only_in_a_run_on_a_tag(self) -> None:
        """A run on refs/tags/v* publishes, whether a tag push or a manual run started it; no other run does."""
        for context in contexts():
            started = running_jobs(self.jobs, context)
            with self.subTest(context=describe(context)):
                if started & PUBLISHING:
                    self.assertTrue(context["github"]["ref"].startswith("refs/tags/v"), started & PUBLISHING)
        for event, inputs in (("push", {}), ("workflow_dispatch", {"release_tag": False})):
            context = {"github": {"event_name": event, "ref": TAG_REF}, "inputs": inputs}
            with self.subTest(context=describe(context)):
                self.assertEqual(DRY_RUN | PUBLISHING, running_jobs(self.jobs, context))

    def test_a_manual_run_without_the_input_is_a_dry_run(self) -> None:
        for ref in ("refs/heads/main", "refs/heads/feature"):
            context = {"github": {"event_name": "workflow_dispatch", "ref": ref}, "inputs": {"release_tag": False}}
            with self.subTest(context=describe(context)):
                self.assertEqual(DRY_RUN, running_jobs(self.jobs, context))

    def test_a_manual_run_on_main_with_the_input_runs_only_the_tag_job(self) -> None:
        context = {"github": {"event_name": "workflow_dispatch", "ref": "refs/heads/main"}, "inputs": {"release_tag": True}}
        self.assertEqual({TAG_JOB}, running_jobs(self.jobs, context))

    def test_the_tag_job_runs_only_for_a_manual_run_on_main_with_the_input(self) -> None:
        self.assertEqual([], needs(self.jobs[TAG_JOB]), "it must not wait for jobs its own run skips")
        for context in contexts():
            expected = (
                context["github"]["event_name"] == "workflow_dispatch"
                and context["github"]["ref"] == "refs/heads/main"
                and context["inputs"].get("release_tag") is True
            )
            with self.subTest(context=describe(context)):
                self.assertEqual(expected, TAG_JOB in running_jobs(self.jobs, context))

    def test_the_expression_evaluator_follows_github_rules(self) -> None:
        """The tests above depend on the evaluator, so it is checked against known answers."""
        context = {"github": {"event_name": "push", "ref": "refs/tags/v1"}, "inputs": {}}
        for condition, ok, expected in (
            ("github.event_name == 'PUSH'", True, True),  # strings compare without case
            ("startsWith(github.ref, 'refs/tags/v')", True, True),
            ("startsWith(github.ref, 'refs/tags/v')", False, False),  # no status function: needs must succeed
            ("always() && startsWith(github.ref, 'refs/heads/')", False, False),
            ("always()", False, True),
            ("inputs.release_tag == true", True, False),  # a push has no inputs
            ("${{ !(inputs.release_tag == true) }}", True, True),
            ("'true' == true", True, False),  # a string is compared with a boolean as a number
            ("!'' == 'x'", True, False),  # `!` binds before `==`: (true == 'x'), not !('' == 'x')
            ("github.event_name == 'push' || github.ref == 'x' && false", True, True),  # && before ||
        ):
            with self.subTest(condition=condition, needs_succeeded=ok):
                self.assertEqual(expected, job_starts(condition, context, ok))

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

    def run_job_steps(self, job: str, context: dict, stand_ins: dict[str, str], extra: dict[str, str] | None = None):
        """Run each shell step of ``job`` in order, as a runner would, until one fails.

        Each step's ``env:`` must use only the expressions this test knows, so
        the test fails when a step starts reading a value it does not cover.
        """
        github = context["github"]
        ticked = context["inputs"].get("release_tag")
        known = {
            "${{ github.event_name }}": github["event_name"],
            "${{ github.ref }}": github["ref"],
            "${{ github.ref_name }}": github["ref"].split("/", 2)[2],
            "${{ inputs.release_tag }}": "" if ticked is None else str(ticked).lower(),
            "${{ needs.version.outputs.version }}": "0.1.0a1",
            **(extra or {}),
        }
        calls: list[list[str]] = []
        summary = ""
        for step in steps(self.jobs[job]):
            if "run" not in step:
                continue
            environment = {}
            for key, value in step.get("env", {}).items():
                if "${{" in str(value):
                    self.assertIn(value, known, f"the test does not know the expression {value!r}")
                    environment[key] = known[value]
                else:
                    environment[key] = str(value)
            completed, recorded = run_step(step["run"], environment, stand_ins)
            calls.extend(recorded)
            summary += completed.summary
            if completed.returncode != 0:
                return completed, calls, summary
        return completed, calls, summary

    def test_the_version_job_checks_the_tag_for_a_push_and_for_a_manual_run_on_a_tag(self) -> None:
        """Whatever started the run, a run on a tag checks the tag against the version and main."""
        for event, inputs in (("push", {}), ("workflow_dispatch", {"release_tag": False})):
            context = {"github": {"event_name": event, "ref": TAG_REF}, "inputs": inputs}
            with self.subTest(context=describe(context)):
                completed, calls, _ = self.run_job_steps("version", context, {"python": "exit 0"})
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(1, len(calls))
                command = " ".join(calls[0])
                self.assertIn("scripts/release_version.py --tag v0.1.0a1 --require-on-branch origin/main", command)
                self.assertIn("--github-output", command)

    def test_the_version_job_checks_no_tag_in_a_dry_run(self) -> None:
        for ref in ("refs/heads/main", "refs/heads/feature"):
            context = {"github": {"event_name": "workflow_dispatch", "ref": ref}, "inputs": {"release_tag": False}}
            with self.subTest(context=describe(context)):
                completed, calls, _ = self.run_job_steps("version", context, {"python": "exit 0"})
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(1, len(calls))
                self.assertNotIn("--tag", calls[0])
                self.assertIn("--github-output", calls[0])

    def test_the_input_is_refused_on_any_ref_but_main(self) -> None:
        """Ticking the box on a branch or a tag stops the run, instead of quietly running a dry run or a release."""
        for ref in ("refs/heads/feature", TAG_REF):
            context = {"github": {"event_name": "workflow_dispatch", "ref": ref}, "inputs": {"release_tag": True}}
            self.assertIn("version", running_jobs(self.jobs, context))
            with self.subTest(context=describe(context)):
                completed, calls, _ = self.run_job_steps("version", context, {"python": "exit 0"})
                self.assertNotEqual(0, completed.returncode)
                self.assertEqual([], calls)
                self.assertIn("only on main", completed.stdout + completed.stderr)

    def test_the_summary_says_publishing_only_in_a_run_on_a_tag(self) -> None:
        cases = (
            ({"event_name": "push", "ref": TAG_REF}, {}, "publishing to TestPyPI, then PyPI"),
            ({"event_name": "workflow_dispatch", "ref": TAG_REF}, {"release_tag": False}, "publishing to TestPyPI, then PyPI"),
            ({"event_name": "workflow_dispatch", "ref": "refs/heads/main"}, {"release_tag": False}, "Nothing was published"),
        )
        step = step_named(self.jobs["check"], "Say what happens next")
        for github, inputs, expected in cases:
            context = {"github": github, "inputs": inputs}
            with self.subTest(context=describe(context)):
                completed, _ = run_step(
                    step["run"],
                    {key: {"${{ github.ref }}": github["ref"], "${{ needs.version.outputs.version }}": "0.1.0a1"}[value] for key, value in step["env"].items()},
                    {},
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertIn(expected, completed.summary)

    def test_the_tag_job_tags_the_head_of_main_and_starts_the_release_on_the_tag(self) -> None:
        job = self.jobs[TAG_JOB]
        tag_step = [s for s in steps(job) if "--create-tag" in s.get("run", "")]
        self.assertEqual(1, len(tag_step), "one step creates the tag")
        self.assertEqual("tag", tag_step[0].get("id"))
        run = tag_step[0]["run"]
        self.assertIn("scripts/release_version.py --create-tag", run)
        for option in ('--commit "${GITHUB_SHA}"', "--branch main", "--remote origin", '--github-output "${GITHUB_OUTPUT}"'):
            self.assertIn(option, run)
        dispatch = step_named(job, "Start the release")
        self.assertGreater(steps(job).index(dispatch), steps(job).index(tag_step[0]))
        self.assertEqual("${{ github.token }}", dispatch["env"]["GH_TOKEN"])
        extra = {
            "${{ github.token }}": "token",
            "${{ github.repository }}": "owner/repo",
            "${{ steps.tag.outputs.tag }}": "v0.1.0a1",
            "${{ steps.tag.outputs.created }}": "true",
        }
        environment = {key: extra[value] for key, value in dispatch["env"].items()}
        completed, calls = run_step(dispatch["run"], environment, {"gh": "exit 0"})
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            [["api", "--method", "POST", f"repos/owner/repo/actions/workflows/{RELEASE.name}/dispatches", "-f", f"ref={TAG_REF}"]],
            calls,
        )
        self.assertIn("v0.1.0a1", completed.summary)
        self.assertIn("created", completed.summary)
        completed, _ = run_step(dispatch["run"], {**environment, "CREATED": "false"}, {"gh": "exit 0"})
        self.assertIn("already", completed.summary)

    def test_the_dispatch_step_refuses_a_tag_it_did_not_get_and_fails_when_the_dispatch_fails(self) -> None:
        dispatch = step_named(self.jobs[TAG_JOB], "Start the release")
        base = {"GH_TOKEN": "token", "GH_REPO": "owner/repo", "CREATED": "true"}
        for tag in ("", "0.1.0a1", "v0.1.0a1; true", "v0.1.0-a1", "main"):
            with self.subTest(tag=tag):
                completed, calls = run_step(dispatch["run"], {**base, "TAG": tag}, {"gh": "exit 0"})
                self.assertNotEqual(0, completed.returncode)
                self.assertEqual([], calls)
        completed, calls = run_step(dispatch["run"], {**base, "TAG": "v0.1.0a1"}, {"gh": "exit 1"})
        self.assertNotEqual(0, completed.returncode)
        self.assertEqual(1, len(calls))

    def test_only_the_tag_job_keeps_the_checkout_credentials(self) -> None:
        """The tag job pushes the tag with git; every other checkout drops the token."""
        for name, job in self.jobs.items():
            for step in steps(job):
                if "actions/checkout" in step.get("uses", ""):
                    with self.subTest(job=name):
                        self.assertIs(name == TAG_JOB, step["with"]["persist-credentials"])

    def test_every_job_waits_for_the_version_check(self) -> None:
        for name in self.jobs:
            if name not in ("version", TAG_JOB):
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
