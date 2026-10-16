"""Tests for the CI guards.

`scripts/ci_no_skipped_tests.py` fails a CI job when the planner, database or
chart tests skipped. A guard like this can stop matching without anyone
noticing: if a suite rewords its skip message, CI goes back to passing with
those tests skipped. So these tests read the skip messages from the test files
themselves, let unittest print them in its real verbose format, and check that
the guard fails on them. They also run two real suites in the condition that
makes them skip.

The last part checks the workflows: every step that pipes into `tee` sets
`pipefail`, the jobs call the guard with the categories they must check, and
every test class that needs a database is in a `--require-ran` list whose step
runs its module.
"""

from __future__ import annotations

import ast
import contextlib
import io
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Callable
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import ci_no_skipped_planner_tests as planner_guard  # noqa: E402  (needs the sys.path entry)
import ci_no_skipped_tests as guard  # noqa: E402

AGENT_TESTS = REPO_ROOT / "agents" / "tests"
HEALTHCARE_TESTS = REPO_ROOT / "apps" / "healthcare-demo" / "tests"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
DATA_WORKFLOW = WORKFLOWS / "healthcare-data.yml"
#: The logs of the steps that run the database tests on a PostgreSQL service:
#: the healthcare suite, and the framework's database tests from agents/tests.
HEALTHCARE_LOG = "healthcare-suite.log"
FRAMEWORK_DATABASE_LOG = "framework-database.log"
#: Where a module named in `--require-ran` can live: the suites run from the
#: repository root, except the healthcare suite, which runs from its app.
TEST_ROOTS = (REPO_ROOT, REPO_ROOT / "apps" / "healthcare-demo")


# ---------------------------------------------------------------------------
# Skip reasons, read from the test files
# ---------------------------------------------------------------------------


def string_constant(path: Path, name: str) -> str:
    """Return the string assigned to the module-level name `name` in `path`."""
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    raise AssertionError(f"{path.relative_to(REPO_ROOT)} no longer defines {name} as a string")


def decorator_reasons(paths: list[Path], condition: str | None = None) -> set[str]:
    """Return the literal reasons in `skipIf` and `skipUnless` decorators.

    Params:
    - `paths`: the test files to read.
    - `condition`: when given, keep only decorators whose condition calls or
      names this function or variable.
    """
    reasons: set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("skipIf", "skipUnless")
                and len(node.args) == 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                continue
            if condition is not None and condition not in ast.unparse(node.args[0]):
                continue
            reasons.add(node.args[1].value)
    return reasons


def planner_reasons() -> set[str]:
    files = sorted(AGENT_TESTS.glob("test_*.py")) + sorted(HEALTHCARE_TESTS.glob("test_*.py"))
    reasons = decorator_reasons(files, condition="dagentsc_available")
    if not reasons:
        raise AssertionError("no test skips on dagentsc_available() any more; update this test")
    return reasons


def database_reasons() -> set[str]:
    """One reason per database suite, built from its SKIP_PREFIX."""
    prefixes = {
        string_constant(AGENT_TESTS / "test_postgres_roles.py", "SKIP_PREFIX"),
        string_constant(HEALTHCARE_TESTS / "test_site_isolation.py", "SKIP_PREFIX"),
    }
    return {prefix + "set the test connection string to a PostgreSQL 16 server" for prefix in prefixes}


def chart_reasons() -> set[str]:
    return {string_constant(AGENT_TESTS / "test_charts.py", "NO_MATPLOTLIB")}


def allowed_reasons() -> set[str]:
    """Skips CI accepts: no job has the pagila dataset, and the deployed API is
    checked by its own workflow."""
    reasons = decorator_reasons([AGENT_TESTS / "test_postgres_api.py"], condition="PAGILA_AVAILABLE")
    reasons |= decorator_reasons([HEALTHCARE_TESTS / "test_deployed_api.py"], condition="API")
    if len(reasons) != 2:
        raise AssertionError(f"expected the pagila and deployed-API skip reasons, found {sorted(reasons)}")
    return reasons


REASONS: dict[str, Callable[[], set[str]]] = {
    "planner": planner_reasons,
    "database": database_reasons,
    "charts": chart_reasons,
}


# ---------------------------------------------------------------------------
# Logs in unittest's real verbose format
# ---------------------------------------------------------------------------


def _passing(self: unittest.TestCase) -> None:
    pass


def make_case(name: str, namespace: dict[str, Any]) -> type[unittest.TestCase]:
    """Create a test class at run time, so the loader does not collect it here.

    `type()` keeps the class name free of `<locals>`, as in a real test id.
    """
    return type(name, (unittest.TestCase,), dict(namespace))


def skipped_case(reason: str, shape: str) -> type[unittest.TestCase]:
    """A test class that skips with `reason` in one of the shapes the suites use.

    - `method`: a skipped test method without a docstring.
    - `docstring`: the same with a docstring, so the result is on the next line.
    - `class_decorator`: a skip decorator on the class, as `skipUnless` in
      test_charts.
    - `set_up_class`: `setUpClass` raises `SkipTest`, as in the database tests.
    """
    def documented(self: unittest.TestCase) -> None:
        """A test with a docstring."""

    if shape == "method":
        return make_case("MethodSkip", {"test_a": unittest.skip(reason)(_passing)})
    if shape == "docstring":
        return make_case("DocstringSkip", {"test_a": unittest.skip(reason)(documented)})
    if shape == "class_decorator":
        return unittest.skip(reason)(make_case("DecoratedSkip", {"test_a": _passing, "test_b": _passing}))
    if shape == "set_up_class":
        def set_up_class(cls: type) -> None:
            raise unittest.SkipTest(reason)

        return make_case("FixtureSkip", {"setUpClass": classmethod(set_up_class), "test_a": _passing})
    raise ValueError(shape)


SHAPES = ("method", "docstring", "class_decorator", "set_up_class")


def class_name(case: type[unittest.TestCase]) -> str:
    """The class name as unittest prints it in a test id."""
    return f"{case.__module__}.{case.__qualname__}"


def verbose_log(*cases: type[unittest.TestCase], stream: io.StringIO | None = None) -> str:
    """Run the classes with unittest's own verbose runner and return its output."""
    stream = stream or io.StringIO()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in cases)
    unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return stream.getvalue()


def passing_case() -> type[unittest.TestCase]:
    return make_case("Passing", {"test_one": _passing, "test_two": _passing})


def guard_result(log: str, *arguments: str, script: Any = guard) -> tuple[int, str]:
    """Write `log` to a temporary file, run the guard on it, and return its
    exit code and everything it printed."""
    output = io.StringIO()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "suite.log"
        path.write_text(log, encoding="utf-8")
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = script.main([*arguments, str(path)])
    return code, output.getvalue()


def run_guard(log: str, *arguments: str, script: Any = guard) -> int:
    """Return the guard's exit code for `log`."""
    return guard_result(log, *arguments, script=script)[0]


def forbid_all() -> list[str]:
    return [argument for name in guard.CATEGORIES for argument in ("--forbid", name)]


# ---------------------------------------------------------------------------
# The skip categories
# ---------------------------------------------------------------------------


class SkipReasonTests(unittest.TestCase):
    """The reasons the guard matches are the ones the suites use."""

    def test_every_category_has_reasons_from_the_test_files(self) -> None:
        self.assertEqual(set(guard.CATEGORIES), set(REASONS))
        for name, reasons in REASONS.items():
            with self.subTest(category=name):
                self.assertTrue(reasons())

    def test_the_database_suites_share_the_documented_prefix(self) -> None:
        for reason in database_reasons():
            self.assertTrue(reason.startswith("database test skipped"), reason)


class ForbiddenSkipTests(unittest.TestCase):
    def test_each_category_fails_on_its_real_skip_messages_in_every_shape(self) -> None:
        for name, reasons in REASONS.items():
            for reason in sorted(reasons()):
                for shape in SHAPES:
                    with self.subTest(category=name, reason=reason, shape=shape):
                        log = verbose_log(passing_case(), skipped_case(reason, shape))
                        self.assertIn(reason, log)
                        self.assertEqual(1, run_guard(log, "--forbid", name))

    def test_a_category_that_is_not_forbidden_is_allowed(self) -> None:
        """The agents job has no database, so it does not forbid database skips."""
        for name, reasons in REASONS.items():
            others = [argument for other in guard.CATEGORIES if other != name for argument in ("--forbid", other)]
            for reason in sorted(reasons()):
                with self.subTest(category=name, reason=reason):
                    log = verbose_log(passing_case(), skipped_case(reason, "method"))
                    self.assertEqual(0, run_guard(log, *others))

    def test_the_skips_ci_accepts_pass_with_every_category_forbidden(self) -> None:
        for reason in sorted(allowed_reasons()):
            with self.subTest(reason=reason):
                log = verbose_log(passing_case(), skipped_case(reason, "class_decorator"))
                self.assertEqual(0, run_guard(log, *forbid_all()))

    def test_a_passing_log_passes(self) -> None:
        log = verbose_log(passing_case())
        self.assertIn("OK", log)
        self.assertEqual(0, run_guard(log, *forbid_all(), "--require-ran", class_name(passing_case())))

    def test_a_non_verbose_log_is_a_failure(self) -> None:
        """Without -v there is no skip reason to check, so the log cannot be cleared."""
        log = "..sss\n----\nRan 5 tests in 0.01s\n\nOK (skipped=3)\n"
        self.assertEqual(1, run_guard(log, *forbid_all()))

    def test_a_missing_log_is_a_failure(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            code = guard.main(["--forbid", "planner", str(REPO_ROOT / "no-such-file.log")])
        self.assertEqual(1, code)

    def test_wrong_usage_is_reported(self) -> None:
        log = verbose_log(passing_case())
        self.assertEqual(2, run_guard(log))  # nothing to check
        self.assertEqual(2, run_guard(log, "--forbid", "no-such-category"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, guard.main([]))

    def test_the_message_names_the_cause_and_the_skipped_tests(self) -> None:
        [reason] = sorted(chart_reasons())
        case = skipped_case(reason, "method")
        code, output = guard_result(verbose_log(passing_case(), case), "--forbid", "charts")
        self.assertEqual(1, code)
        self.assertIn(guard.CATEGORIES["charts"].reason, output)
        self.assertIn(class_name(case), output)


class RequireRanTests(unittest.TestCase):
    """`--require-ran` catches a skip whose message no pattern matches."""

    REASON = "a reason no pattern matches"

    def test_a_class_whose_tests_ran_passes(self) -> None:
        case = passing_case()
        self.assertEqual(0, run_guard(verbose_log(case), "--require-ran", class_name(case)))

    def test_a_class_that_skipped_fails_in_every_shape(self) -> None:
        for shape in SHAPES:
            with self.subTest(shape=shape):
                case = skipped_case(self.REASON, shape)
                log = verbose_log(passing_case(), case)
                self.assertEqual(1, run_guard(log, "--require-ran", class_name(case)))

    def test_a_class_missing_from_the_log_fails(self) -> None:
        log = verbose_log(passing_case())
        self.assertEqual(1, run_guard(log, "--require-ran", "tests.test_nothing.NoSuchTests"))

    def test_a_failing_test_counts_as_run(self) -> None:
        """The suite step already fails on it; this guard only asks whether it ran."""
        def failing(self: unittest.TestCase) -> None:
            self.fail("expected")

        case = make_case("Failing", {"test_a": failing})
        self.assertEqual(0, run_guard(verbose_log(case), "--require-ran", class_name(case)))

    def test_output_before_the_result_is_read_correctly(self) -> None:
        """A test that writes output pushes its result onto a later line."""
        stream = io.StringIO()

        def noisy(self: unittest.TestCase) -> None:
            stream.write("output from the test\nmore output\n")

        case = make_case("Noisy", {"test_a": noisy})
        log = verbose_log(case, stream=stream)
        self.assertIn("output from the test", log)
        self.assertEqual(0, run_guard(log, "--require-ran", class_name(case)))

    def test_a_test_id_inside_failure_details_does_not_count(self) -> None:
        def failing(self: unittest.TestCase) -> None:
            # The newline puts the fake test line at the start of a line in
            # the traceback, where it looks like a real result.
            self.fail("\ntest_fake (tests.test_other.Ghost.test_fake) ... ok")

        log = verbose_log(make_case("Failing", {"test_a": failing}))
        self.assertIn("\ntest_fake (tests.test_other.Ghost.test_fake) ... ok\n", log)
        self.assertEqual(1, run_guard(log, "--require-ran", "tests.test_other.Ghost"))

    def test_the_test_id_format_before_python_3_11_is_read(self) -> None:
        log = "test_a (tests.test_old.OldTests) ... ok\n\nRan 1 test in 0.001s\n\nOK\n"
        self.assertEqual(0, run_guard(log, "--require-ran", "tests.test_old.OldTests"))


# ---------------------------------------------------------------------------
# Real suites, run in the condition that makes them skip
# ---------------------------------------------------------------------------


def run_suite(code: list[str], environment: dict[str, str]) -> str:
    """Run a unittest command from the repository root and return its output."""
    completed = subprocess.run(
        [sys.executable, *code],
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=300,
        check=False,
    )
    return completed.stdout


def clean_environment(*remove: str) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if key not in remove}
    environment["PYTHONPATH"] = str(REPO_ROOT)
    return environment


class RealSuiteTests(unittest.TestCase):
    def test_the_role_tests_without_a_database_fail_the_guard(self) -> None:
        log = run_suite(
            ["-m", "unittest", "agents.tests.test_postgres_roles", "-v"],
            clean_environment("DAGENTS_TEST_DATABASE_URL"),
        )
        self.assertIn("database test skipped", log, log[-2000:])
        self.assertEqual(1, run_guard(log, "--forbid", "database"))
        self.assertEqual(
            1, run_guard(log, "--require-ran", "agents.tests.test_postgres_roles.ActingAsTests")
        )
        # The tests that need no database still ran, so the log is parsed.
        self.assertEqual(
            0, run_guard(log, "--require-ran", "agents.tests.test_postgres_roles.RoleNameTests")
        )

    def test_the_chart_tests_without_matplotlib_fail_the_guard(self) -> None:
        # Setting a module to None in sys.modules makes find_spec return None
        # and the import fail, as if the package were not installed.
        program = (
            "import sys; sys.modules['matplotlib'] = None; import unittest; "
            "unittest.main(module=None, argv=['unittest', '-v', 'agents.tests.test_charts.RenderTests'])"
        )
        log = run_suite(["-c", program], clean_environment())
        self.assertIn("matplotlib is not installed", log, log[-2000:])
        self.assertEqual(1, run_guard(log, "--forbid", "charts"))
        self.assertEqual(1, run_guard(log, "--require-ran", "agents.tests.test_charts.RenderTests"))

    def test_a_real_passing_suite_passes_the_guard(self) -> None:
        log = run_suite(["-m", "unittest", "agents.tests.test_source_connection_args", "-v"], clean_environment())
        self.assertRegex(log, r"\nOK\n", log[-2000:])
        self.assertEqual(
            0,
            run_guard(
                log,
                *forbid_all(),
                "--require-ran",
                "agents.tests.test_source_connection_args.PostgresConnectionArgsTests",
            ),
        )


class PlannerGuardTests(unittest.TestCase):
    """`ci_no_skipped_planner_tests.py LOG` still works for existing instructions."""

    def test_it_fails_on_the_real_skip_message(self) -> None:
        for reason in sorted(planner_reasons()):
            log = verbose_log(passing_case(), skipped_case(reason, "docstring"))
            self.assertEqual(1, run_guard(log, script=planner_guard))

    def test_it_passes_skips_for_other_reasons(self) -> None:
        for reason in sorted(allowed_reasons() | database_reasons() | chart_reasons()):
            log = verbose_log(passing_case(), skipped_case(reason, "method"))
            self.assertEqual(0, run_guard(log, script=planner_guard))

    def test_a_non_verbose_log_is_a_failure(self) -> None:
        self.assertEqual(1, run_guard("..s\nOK (skipped=1)\n", script=planner_guard))

    def test_wrong_usage_is_reported(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2, planner_guard.main([]))
            self.assertEqual(2, planner_guard.main(["a.log", "b.log"]))


# ---------------------------------------------------------------------------
# The workflows
# ---------------------------------------------------------------------------


def run_blocks(workflow: str) -> list[str]:
    """Return the body of every `run:` step in a workflow file.

    A small hand parser, because PyYAML is not a dependency of this suite. It
    handles the two forms the workflows use: `run: command` on one line, and
    `run: |` followed by an indented block.
    """
    lines = workflow.splitlines()
    blocks: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        # A step can start with `run:` itself, as in `- run: make test`.
        if stripped.startswith("- "):
            stripped = stripped[2:].lstrip()
        if not stripped.startswith("run:"):
            continue
        rest = stripped[len("run:"):].strip()
        if rest not in ("|", ">"):
            blocks.append(rest)
            continue
        indent = len(line) - len(line.lstrip()) + 2
        body = []
        for following in lines[index + 1:]:
            if following.strip() and not following.startswith(" " * indent):
                break
            body.append(following)
        blocks.append("\n".join(body))
    return blocks


def jobs(workflow: str) -> dict[str, str]:
    """Split a workflow into its jobs: job id -> the job's text."""
    found: dict[str, list[str]] = {}
    current: list[str] | None = None
    in_jobs = False
    for line in workflow.splitlines():
        if re.match(r"^\S", line):
            in_jobs = line.rstrip() == "jobs:"
            current = None
            continue
        heading = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if in_jobs and heading:
            current = found.setdefault(heading.group(1), [])
            continue
        if current is not None:
            current.append(line)
    return {name: "\n".join(lines) for name, lines in found.items()}


def guard_calls(text: str) -> list[Any]:
    """Parse every `ci_no_skipped_tests.py` call in `text` with the guard's parser."""
    calls = []
    for block in run_blocks(text):
        joined = re.sub(r"\\\n\s*", " ", block)
        for line in joined.splitlines():
            if "ci_no_skipped_tests.py" not in line:
                continue
            words = shlex.split(line.split("ci_no_skipped_tests.py", 1)[1].split("||")[0])
            calls.append(guard._parser().parse_args(words))
    return calls


#: Calls that open a test database, or skip with the database message when none
#: is configured.
DATABASE_ENTRY_POINTS = frozenset({"admin_dsn_or_skip", "SupabaseLikeDatabase", "ScratchDatabase"})


def _called_names(node: ast.AST) -> set[str]:
    """Names of the plain functions and classes called anywhere inside `node`."""
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


def database_test_classes(test_dir: Path, module_prefix: str) -> set[str]:
    """Every test class in `test_dir` that needs a database, as `module.Class`.

    A class needs a database when one of its methods calls an entry point in
    `DATABASE_ENTRY_POINTS`, or a module-level function that does, or when it
    inherits from a class in the same module that does. Only classes with at
    least one test method count, so a shared base or mixin is not listed.
    """
    found: set[str] = set()
    for path in sorted(test_dir.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

        opens: set[str] = set(DATABASE_ENTRY_POINTS)
        changed = True
        while changed:  # helpers that call helpers that open a database
            changed = False
            for name, function in functions.items():
                if name not in opens and _called_names(function) & opens:
                    opens.add(name)
                    changed = True

        def bases(node: ast.ClassDef) -> list[ast.ClassDef]:
            return [classes[base.id] for base in node.bases if isinstance(base, ast.Name) and base.id in classes]

        def needs_database(node: ast.ClassDef) -> bool:
            return bool(_called_names(node) & opens) or any(needs_database(base) for base in bases(node))

        def has_tests(node: ast.ClassDef) -> bool:
            own = any(
                isinstance(item, ast.FunctionDef) and item.name.startswith("test")
                for item in node.body
            )
            return own or any(has_tests(base) for base in bases(node))

        for name, node in classes.items():
            if needs_database(node) and has_tests(node):
                found.add(f"{module_prefix}{path.stem}.{name}")
    return found


def requirement(path: Path, package: str) -> str:
    """Return the requirement line for `package`, such as `numpy>=2,<3`."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(rf"^{re.escape(package)}\s*[<>=!~]", line.strip()):
            return line.strip()
    raise AssertionError(f"{path.relative_to(REPO_ROOT)} has no requirement for {package}")


class WorkflowGuardTests(unittest.TestCase):
    """The CI jobs call the guard for the tests they are set up to run."""

    CI = CI_WORKFLOW.read_text(encoding="utf-8")

    def forbidden_in(self, job: str) -> set[str]:
        return {name for call in guard_calls(jobs(self.CI)[job]) for name in call.forbid}

    def test_the_agents_job_forbids_planner_and_chart_skips(self) -> None:
        self.assertLessEqual({"planner", "charts"}, self.forbidden_in("agents"))

    def test_the_demo_apps_job_forbids_planner_and_database_skips(self) -> None:
        self.assertLessEqual({"planner", "database"}, self.forbidden_in("demo-apps"))

    def test_the_agents_job_installs_the_app_chart_and_compose_requirements(self) -> None:
        """The same ranges as the healthcare app, which draws the charts and
        reads the rendered Compose file in production."""
        app_requirements = REPO_ROOT / "apps" / "healthcare-demo" / "requirements.txt"
        install = "\n".join(
            block for block in run_blocks(jobs(self.CI)["agents"]) if "pip install" in block
        )
        for package in ("matplotlib", "numpy", "pyyaml"):
            with self.subTest(package=package):
                self.assertIn(f'"{requirement(app_requirements, package)}"', install)

    def test_every_required_class_exists(self) -> None:
        """A renamed test class would fail CI; this finds it before a push."""
        names = [
            name
            for path in sorted(WORKFLOWS.glob("*.yml"))
            for call in guard_calls(path.read_text(encoding="utf-8"))
            for name in call.require_ran
        ]
        self.assertTrue(names, "no workflow passes --require-ran")
        for name in names:
            module, _, class_ = name.rpartition(".")
            with self.subTest(test_class=name):
                files = [root / (module.replace(".", "/") + ".py") for root in TEST_ROOTS]
                existing = [path for path in files if path.is_file()]
                self.assertTrue(existing, f"no file for module {module}")
                self.assertTrue(
                    any(re.search(rf"^class {class_}\(", path.read_text(encoding="utf-8"), re.MULTILINE)
                        for path in existing),
                    f"{module} defines no class {class_}",
                )

    def test_the_data_workflow_requires_what_ci_requires_before_migrating(self) -> None:
        """healthcare-data.yml tests on a scratch database before it touches the project.

        It requires every class the demo-apps job requires: the healthcare
        database classes and the framework's, because the deployed demo runs
        both on the project's database.
        """
        data = DATA_WORKFLOW.read_text(encoding="utf-8")
        data_calls = [call for call in guard_calls(jobs(data)["migrate"]) if call.require_ran]
        ci_calls = [call for call in guard_calls(jobs(self.CI)["demo-apps"]) if call.require_ran]
        self.assertTrue(data_calls, "healthcare-data.yml requires no test class to run")
        for call in data_calls:
            with self.subTest(log=call.log):
                self.assertLessEqual({"planner", "database"}, set(call.forbid))
        self.assertEqual(
            sorted(name for call in ci_calls for name in call.require_ran),
            sorted(name for call in data_calls for name in call.require_ran),
            "healthcare-data.yml and the demo-apps job in ci.yml must require the same classes",
        )
        # Every check runs before the first step that changes the project.
        self.assertLess(data.rindex("ci_no_skipped_tests.py"), data.index("apply_migrations.py"))
        self.assertLess(data.index("SHOW server_version"), data.index("apply_migrations.py"))

    # `--forbid database` catches a skip with the database message. A database
    # class that is renamed, removed or skipped for another reason is caught
    # only by `--require-ran`, so every database class must be listed.

    def demo_apps_call(self, log: str) -> Any:
        calls = [
            call for call in guard_calls(jobs(self.CI)["demo-apps"])
            if Path(call.log).name == log and call.require_ran
        ]
        self.assertEqual(1, len(calls), f"the demo-apps job should check {log} with --require-ran exactly once")
        return calls[0]

    def test_every_healthcare_database_class_is_required_to_run(self) -> None:
        needed = database_test_classes(HEALTHCARE_TESTS, "tests.")
        self.assertIn("tests.test_site_isolation.SiteIsolationTests", needed, "the class finder found nothing")
        missing = sorted(needed - set(self.demo_apps_call(HEALTHCARE_LOG).require_ran))
        self.assertEqual(
            [],
            missing,
            "these healthcare classes need a database and are not in the demo-apps --require-ran list "
            "in ci.yml; add them there and in healthcare-data.yml",
        )

    def test_every_framework_database_class_is_required_to_run(self) -> None:
        needed = database_test_classes(AGENT_TESTS, "agents.tests.")
        self.assertIn("agents.tests.test_postgres_roles.ActingAsTests", needed, "the class finder found nothing")
        missing = sorted(needed - set(self.demo_apps_call(FRAMEWORK_DATABASE_LOG).require_ran))
        self.assertEqual(
            [],
            missing,
            f"these agent classes need a database and are not in the demo-apps --require-ran list for "
            f"{FRAMEWORK_DATABASE_LOG}; the agents job has no database, so they run only there. "
            "Add them there and in healthcare-data.yml",
        )

    def test_every_database_log_is_also_checked_for_planner_skips(self) -> None:
        """The database steps have the planner, and some database classes need it.

        The model registry, the GMA's coordinator wiring and the pilot skip
        without `dagentsc`. `--require-ran` sees such a skip only when every
        test of the class skipped, so each log checked for database skips is
        also checked for planner skips.
        """
        workflows = {"ci.yml": jobs(self.CI)["demo-apps"], "healthcare-data.yml": jobs(DATA_WORKFLOW.read_text())["migrate"]}
        for workflow, text in workflows.items():
            forbidden: dict[str, set[str]] = {}
            for call in guard_calls(text):
                forbidden.setdefault(Path(call.log).name, set()).update(call.forbid)
            database_logs = sorted(log for log, names in forbidden.items() if "database" in names)
            self.assertTrue(database_logs, f"{workflow} checks no log for database skips")
            for log in database_logs:
                with self.subTest(workflow=workflow, log=log):
                    self.assertIn("planner", forbidden[log], f"{workflow} does not check {log} for planner skips")

    def test_the_finder_skips_mixins_and_follows_helpers(self) -> None:
        source = (
            "class Contract:\n"
            "    def test_shared(self): pass\n"
            "def _seeded():\n"
            "    return SupabaseLikeDatabase(admin_dsn_or_skip())\n"
            "def _twice():\n"
            "    return _seeded()\n"
            "class Base(unittest.TestCase):\n"
            "    @classmethod\n"
            "    def setUpClass(cls): cls.db = _twice()\n"
            "class PostgresTests(Contract, Base):\n"
            "    pass\n"
            "class MemoryTests(Contract, unittest.TestCase):\n"
            "    db: SupabaseLikeDatabase\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "test_sample.py").write_text(source, encoding="utf-8")
            self.assertEqual({"tests.test_sample.PostgresTests"}, database_test_classes(Path(directory), "tests."))

    def test_each_required_module_runs_in_the_step_that_writes_the_log(self) -> None:
        """A class can only run if the command that writes the log runs its module."""
        workflows = {"ci.yml": self.CI, "healthcare-data.yml": DATA_WORKFLOW.read_text(encoding="utf-8")}
        for workflow, text in workflows.items():
            blocks = run_blocks(text)
            for call in guard_calls(text):
                log = Path(call.log).name
                writers = [block for block in blocks if f"tee {log}" in block]
                if not call.require_ran or not writers:
                    continue
                [writer] = writers
                if "discover" in writer:
                    continue
                for name in call.require_ran:
                    module = name.rpartition(".")[0]
                    with self.subTest(workflow=workflow, log=log, module=module):
                        self.assertIn(module, writer, f"{workflow} requires {name} but does not run {module}")

    def test_the_helpers_find_jobs_and_calls(self) -> None:
        sample = (
            "on: push\n"
            "jobs:\n"
            "  first:\n"
            "    steps:\n"
            "      - run: |\n"
            "          python scripts/ci_no_skipped_tests.py --forbid planner \\\n"
            "            --require-ran a.B one.log || status=1\n"
            "  second:\n"
            "    steps:\n"
            "      - run: echo two\n"
        )
        found = jobs(sample)
        self.assertEqual(["first", "second"], list(found))
        [call] = guard_calls(found["first"])
        self.assertEqual((["planner"], ["a.B"], "one.log"), (call.forbid, call.require_ran, call.log))


class PipefailTests(unittest.TestCase):
    """A step that pipes a test run into `tee` must also set `pipefail`.

    GitHub runs `run:` steps with `bash -e`, without `pipefail`. In
    `unittest ... | tee log`, the step's exit code is `tee`'s, which is 0, so
    a failing suite passes. This hid two real test failures in the healthcare
    demo for several commits.
    """

    WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.yml"))

    def test_every_tee_pipeline_sets_pipefail(self) -> None:
        self.assertTrue(self.WORKFLOW_FILES, "no workflows found")
        for path in self.WORKFLOW_FILES:
            for block in run_blocks(path.read_text(encoding="utf-8")):
                if "| tee" not in block:
                    continue
                with self.subTest(workflow=path.name, step=block.strip().splitlines()[-1][:60]):
                    self.assertIn(
                        "set -o pipefail",
                        block,
                        f"{path.name}: a step pipes into tee without `set -o pipefail`, "
                        "so a failing command would pass",
                    )

    def test_the_parser_finds_both_step_forms(self) -> None:
        sample = (
            "    steps:\n"
            "      - run: echo one\n"
            "      - run: |\n"
            "          set -o pipefail\n"
            "          make test | tee out.log\n"
            "      - name: after\n"
        )
        self.assertEqual(
            ["echo one", "          set -o pipefail\n          make test | tee out.log"],
            run_blocks(sample),
        )


if __name__ == "__main__":
    unittest.main()
