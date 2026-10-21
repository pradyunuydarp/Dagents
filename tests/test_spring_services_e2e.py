"""spring-core-service started from its jar, compiling workloads through the real planner.

Before these tests, the JUnit tests built the service's objects by hand, so
they could not see two problems that only a running service shows:

- The jar did not start. Spring found two `InMemoryWorkloadPlanRepository`
  beans, and no JUnit test loaded the application context.
- The planner's answer was thrown away. The gateway ran `dagentsc manifest
  compile`, the planner printed a valid plan, and then a `flush()` on the
  stream Jackson had already closed threw. The gateway caught the exception
  without logging it, and the Java renderer answered instead.

These tests start the jar twice as a JVM process: once with
`DAGENTS_OCAML_MANIFEST_CLI` naming a wrapper around the real planner, and once
without it. They call the service over HTTP and compare its answers with what
`dagentsc manifest compile` prints for the same JSON. The wrapper writes each
call and its exit status to a file, so the tests can tell that the planner ran
and how it ended.

The tests need the planner, Java and the jar. Build the jar with:

    mvn --batch-mode -f services/spring-services/pom.xml -pl spring-core-service -am package -DskipTests

The `contracts` CI job builds it, sets `DAGENTSC_BIN`, and fails if this class
did not run. `SpringCiWiringTests` checks that wiring, and that the `spring`
job runs `mvn verify` with the planner and fails when the gateway's JUnit test
skipped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import textwrap
import time
from typing import Any
import unittest
import urllib.error
import urllib.request


REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_JAR = (
    REPO_ROOT / "services" / "spring-services" / "spring-core-service" / "target"
    / "spring-core-service-0.1.0-SNAPSHOT.jar"
)
DUNE_PLANNER = REPO_ROOT / "bindings" / "ocaml" / "_build" / "default" / "bin" / "dagentsc.exe"
BUILD_COMMAND = (
    "mvn --batch-mode -f services/spring-services/pom.xml -pl spring-core-service -am package -DskipTests"
)

#: How long a service may take from the start of the JVM to its first health answer.
START_SECONDS = 60
#: Spring Boot logs this line once Tomcat listens. With `SERVER_PORT=0` it is
#: the only place the chosen port appears.
TOMCAT_STARTED = re.compile(r"Tomcat started on port (\d+)")
#: What the Java renderer uses as the plan id when the request has none.
JAVA_PLAN_ID = re.compile(r"^spring-workload-plan-\d+$")
#: The planner's message for a component whose `args` is null.
ARGS_REFUSAL = "Expected list field: args"

#: No proxy: the services listen on the loopback address.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def planner_binary() -> str | None:
    """The planner: `DAGENTSC_BIN`, the dune build, or `dagentsc` on PATH.

    A `DAGENTSC_BIN` that names a missing file is returned as it is, so that
    `setUpClass` fails on a wrong setting instead of skipping.
    """
    configured = os.environ.get("DAGENTSC_BIN")
    if configured:
        return configured
    if DUNE_PLANNER.is_file():
        return str(DUNE_PLANNER)
    return shutil.which("dagentsc")


#: One workload, with every field of `WorkloadComponent` set.
COMPONENT: dict[str, Any] = {
    "name": "router",
    "image": "ghcr.io/example/router:latest",
    "kind": "Deployment",
    "replicas": 2,
    "schedule": None,
    "ports": [{"name": "http", "containerPort": 8080}],
    "env": [{"name": "APP_ENV", "value": "test"}],
    "args": ["--serve"],
    "resources": {"cpuRequest": "250m", "cpuLimit": "1", "memoryRequest": "256Mi", "memoryLimit": "1Gi"},
}


def compile_request(**component_changes: Any) -> dict[str, Any]:
    """A compile body with `planId` and `namespace` left unset.

    Jackson writes every field of the Java records and writes null for a field
    the request left out. So this body, with every field present, is also the
    JSON the gateway sends to the planner.
    """
    return {
        "planId": None,
        "namespace": None,
        "components": [{**COMPONENT, **component_changes}],
        "includeServices": True,
        "includeConfigMaps": True,
    }


def run_planner(binary: str, body: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """What `dagentsc manifest compile` prints for `body`, run here without the service."""
    return subprocess.run(
        [binary, "manifest", "compile", "--input", "-", "--output", "json"],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def call(base: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """Send one request; return the status and the JSON body, or None for an empty body."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(request, timeout=30) as answer:
            status, raw = answer.status, answer.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    return status, (json.loads(raw) if raw.strip() else None)


def manifests_without_service_accounts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """The manifests the Spring response carries. It has no `serviceAccountYaml` field."""
    fields = ("componentName", "kind", "deploymentYaml", "serviceYaml", "configMapYaml")
    return [{field: manifest.get(field) for field in fields} for manifest in plan["manifests"]]


class Service:
    """spring-core-service running from its jar in its own process group."""

    def __init__(self, name: str, directory: Path, planner_command: str | None) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "DAGENTS_OCAML_MANIFEST_CLI"}
        environment.update({
            # Port 0: Tomcat picks a free port and logs it, so nothing can take
            # the port between choosing it and binding it.
            "SERVER_PORT": "0",
            "DAGENTS_CORE_APP_NAME": f"spring-core-e2e-{name}",
            "DAGENTS_CORE_ENVIRONMENT": "test",
            # Nothing listens on port 9; the service lists these addresses and never calls them.
            "DAGENTS_CORE_CONTROL_SERVICE_URL": "http://127.0.0.1:9",
            "DAGENTS_CORE_LMA_URL": "http://127.0.0.1:9",
            "DAGENTS_CORE_GMA_URL": "http://127.0.0.1:9",
            "DAGENTS_CORE_MODEL_SERVICE_URL": "http://127.0.0.1:9",
            "DAGENTS_CORE_PIPELINE_SERVICE_URL": "http://127.0.0.1:9",
        })
        if planner_command is not None:
            environment["DAGENTS_OCAML_MANIFEST_CLI"] = planner_command
        self.name = name
        self.log_path = directory / f"{name}.log"
        # The JVM's temporary files go in the class's directory, which the class
        # cleanup removes. Tomcat's base directory would otherwise stay in /tmp.
        self.temp_directory = directory / f"{name}-tmp"
        self.temp_directory.mkdir()
        self.base = ""
        with open(self.log_path, "wb") as log:
            self.process = subprocess.Popen(
                ["java", f"-Djava.io.tmpdir={self.temp_directory}", "-jar", str(CORE_JAR)],
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def output(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace")

    def tail(self, lines: int = 60) -> str:
        return f"--- last lines of the {self.name} service's output ---\n" + "\n".join(
            self.output().splitlines()[-lines:]
        )

    def wait_until_ready(self, deadline: float) -> None:
        """Read the port from the log, then wait for `/api/v1/health` to answer 200."""
        while not self.base:
            found = TOMCAT_STARTED.search(self.output())
            if found:
                self.base = f"http://127.0.0.1:{found.group(1)}"
                break
            self._check_running(deadline)
            time.sleep(0.2)
        while True:
            try:
                status, _ = call(self.base, "GET", "/api/v1/health")
                if status == 200:
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            self._check_running(deadline)
            time.sleep(0.2)

    def _check_running(self, deadline: float) -> None:
        if self.process.poll() is not None:
            raise AssertionError(
                f"the {self.name} service exited with status {self.process.returncode} before it answered\n"
                + self.tail()
            )
        if time.monotonic() > deadline:
            raise AssertionError(
                f"the {self.name} service did not answer /api/v1/health within {START_SECONDS} s\n" + self.tail()
            )

    def stop(self) -> None:
        """Stop the whole process group, even when the test failed."""
        stop_process_group(self.process)


def stop_process_group(process: subprocess.Popen[bytes]) -> None:
    """Stop every process in the group that `process` leads, also after `process` exited.

    The planner wrapper that the JVM starts stays in the JVM's process group.
    The wrapper can outlive the JVM, and then only a signal to the group
    reaches it. A group takes signals while any of its processes runs, even
    after its leader exited. SIGKILL follows SIGTERM for a process that
    ignored SIGTERM.
    """
    for signal_number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, signal_number)
        except ProcessLookupError:
            break  # no process of the group is left
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
    process.poll()


def is_running(pid: int) -> bool:
    """Whether process `pid` runs. A zombie, which waits for its parent to read its status, does not."""
    if not Path("/proc/self/stat").exists():
        # No /proc, as on macOS: signal 0 checks that the process exists.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rpartition(")")[2].split()
    except (FileNotFoundError, ProcessLookupError):
        return False
    return bool(fields) and fields[0] != "Z"


@unittest.skipUnless(
    planner_binary() is not None,
    "dagentsc binary is not available; build it or set DAGENTSC_BIN to start spring-core-service with the planner",
)
@unittest.skipUnless(shutil.which("java"), "java is not installed; spring-core-service cannot start")
@unittest.skipUnless(CORE_JAR.is_file(), f"the spring-core-service jar is not built; run {BUILD_COMMAND}")
class SpringCoreProcessTests(unittest.TestCase):
    """spring-core-service as a JVM process: health, compile, stored plan and fallback."""

    planner: str
    calls_log: Path
    with_planner: Service
    without_planner: Service

    @classmethod
    def setUpClass(cls) -> None:
        cls.planner = str(planner_binary())
        if not (os.path.isfile(cls.planner) and os.access(cls.planner, os.X_OK)):
            raise AssertionError(f"DAGENTSC_BIN names {cls.planner}, which is not a file this process can run")
        directory = tempfile.TemporaryDirectory(prefix="spring-core-e2e-")
        cls.addClassCleanup(directory.cleanup)
        root = Path(directory.name)

        # ProcessBuilder runs the command directly, so the wrapper is an
        # executable script with a shebang. It passes its standard input on to
        # the planner and records how each call ended.
        cls.calls_log = root / "planner-calls.log"
        cls.calls_log.touch()
        wrapper = root / "dagentsc-wrapper"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"printf 'call %s\\n' \"$*\" >> {shlex.quote(str(cls.calls_log))}\n"
            f"{shlex.quote(cls.planner)} \"$@\"\n"
            "status=$?\n"
            f"printf 'exit %s\\n' \"$status\" >> {shlex.quote(str(cls.calls_log))}\n"
            "exit \"$status\"\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

        # Both JVMs start at once; each takes a few seconds.
        cls.with_planner = Service("with-planner", root, str(wrapper))
        cls.addClassCleanup(cls.with_planner.stop)
        cls.without_planner = Service("without-planner", root, None)
        cls.addClassCleanup(cls.without_planner.stop)
        deadline = time.monotonic() + START_SECONDS
        cls.with_planner.wait_until_ready(deadline)
        cls.without_planner.wait_until_ready(deadline)

    # -- helpers ----------------------------------------------------------------

    def planner_calls(self) -> list[tuple[str, int | None]]:
        """Each call the wrapper recorded: its arguments and its exit status."""
        calls: list[tuple[str, int | None]] = []
        for line in self.calls_log.read_text(encoding="utf-8").splitlines():
            word, _, rest = line.partition(" ")
            if word == "call":
                calls.append((rest, None))
            elif word == "exit" and calls:
                calls[-1] = (calls[-1][0], int(rest))
        return calls

    def compile_on(self, service: Service, body: dict[str, Any]) -> dict[str, Any]:
        status, plan = call(service.base, "POST", "/api/v1/workloads:compile", body)
        self.assertEqual(200, status, f"{plan}\n{service.tail()}")
        self.assertIsInstance(plan, dict, service.tail())
        return plan

    def planner_answer(self, body: dict[str, Any]) -> dict[str, Any]:
        completed = run_planner(self.planner, body)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    # -- tests ------------------------------------------------------------------

    def test_both_services_answer_their_health_check(self) -> None:
        for service in (self.with_planner, self.without_planner):
            with self.subTest(service=service.name):
                status, health = call(service.base, "GET", "/api/v1/health")
                self.assertEqual(200, status, service.tail())
                self.assertEqual(
                    {"status": "ok", "service": f"spring-core-e2e-{service.name}", "environment": "test",
                     "transport": "http"},
                    health,
                )

    def test_a_compile_returns_the_planners_plan_and_stores_it(self) -> None:
        body = compile_request()
        expected = self.planner_answer(body)
        self.assertEqual(("dagents-plan", "dagents"), (expected["planId"], expected["namespace"]))
        calls_before = len(self.planner_calls())

        plan = self.compile_on(self.with_planner, body)

        self.assertEqual(
            [("manifest compile --input - --output json", 0)],
            self.planner_calls()[calls_before:],
            "the service should run the planner once, and the planner should succeed",
        )
        # The Java renderer would answer spring-workload-plan-<ms> and a null namespace.
        self.assertEqual("dagents-plan", plan["planId"], self.with_planner.tail())
        self.assertEqual("dagents", plan["namespace"], self.with_planner.tail())
        self.assertEqual(expected["combinedYaml"], plan["combinedYaml"])
        self.assertEqual(manifests_without_service_accounts(expected), plan["manifests"])

        status, stored = call(self.with_planner.base, "GET", "/api/v1/workload-plans/dagents-plan")
        self.assertEqual(200, status)
        self.assertEqual(plan, stored)

    def test_a_component_without_args_gets_the_java_renderer_and_the_reason_is_logged(self) -> None:
        body = compile_request(args=None)
        refused = run_planner(self.planner, body)
        self.assertNotEqual(0, refused.returncode, "the planner now accepts a null args; update this test")
        self.assertIn(ARGS_REFUSAL, refused.stderr)
        calls_before = len(self.planner_calls())

        plan = self.compile_on(self.with_planner, body)

        [(arguments, status)] = self.planner_calls()[calls_before:]
        self.assertEqual("manifest compile --input - --output json", arguments)
        self.assertEqual(refused.returncode, status)
        self.assertRegex(plan["planId"], JAVA_PLAN_ID)
        self.assertIsNone(plan["namespace"])
        java_plan = self.compile_on(self.without_planner, body)
        self.assertEqual(java_plan["combinedYaml"], plan["combinedYaml"])
        self.assertEqual(java_plan["manifests"], plan["manifests"])

        # The service logs why it used the Java renderer: the planner's message.
        deadline = time.monotonic() + 5
        while ARGS_REFUSAL not in self.with_planner.output() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertIn(ARGS_REFUSAL, self.with_planner.output(), self.with_planner.tail())

        status_code, stored = call(self.with_planner.base, "GET", f"/api/v1/workload-plans/{plan['planId']}")
        self.assertEqual((200, plan), (status_code, stored))

    def test_without_the_planner_setting_the_java_renderer_answers(self) -> None:
        body = compile_request()
        calls_before = len(self.planner_calls())

        plan = self.compile_on(self.without_planner, body)

        self.assertEqual([], self.planner_calls()[calls_before:])
        self.assertRegex(plan["planId"], JAVA_PLAN_ID)
        self.assertIsNone(plan["namespace"])
        self.assertIn("  namespace: null\n", plan["combinedYaml"])
        self.assertNotEqual(self.planner_answer(body)["combinedYaml"], plan["combinedYaml"])
        self.assertIn("DAGENTS_OCAML_MANIFEST_CLI is not set", self.without_planner.output())

    def test_each_service_keeps_its_temporary_files_in_the_test_directory(self) -> None:
        # Tomcat makes its base directory in java.io.tmpdir and asks the JVM to
        # delete it at exit, which fails for a directory with contents. In /tmp
        # it would stay after every run; here the class cleanup removes it.
        for service in (self.with_planner, self.without_planner):
            with self.subTest(service=service.name):
                self.assertTrue(
                    any(service.temp_directory.glob("tomcat.*")),
                    f"Tomcat did not make its base directory in {service.temp_directory}\n{service.tail()}",
                )


def _kill_group(group: int) -> None:
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass


class ProcessGroupStopTests(unittest.TestCase):
    """The teardown stops a planner wrapper that outlived the JVM that started it."""

    def test_a_process_left_in_the_group_after_its_leader_exited_is_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child_pid = Path(directory) / "child.pid"
            # The leader starts a child in its group and exits, as a JVM that
            # dies while a planner wrapper runs would.
            leader = subprocess.Popen(
                ["sh", "-c", f"sleep 60 & echo $! > {shlex.quote(str(child_pid))}"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.addCleanup(_kill_group, leader.pid)
            leader.wait(timeout=20)
            child = int(child_pid.read_text(encoding="utf-8").strip())
            self.assertTrue(is_running(child), f"process {child} should still run after its leader exited")

            stop_process_group(leader)

            deadline = time.monotonic() + 10
            while is_running(child) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(is_running(child), f"process {child} of the group is still running")


# ---------------------------------------------------------------------------
# The CI jobs that run the Spring tests
# ---------------------------------------------------------------------------


def steps(job: str) -> list[str]:
    """Split a job's text into its steps; each starts with `      - ` in the workflow."""
    found: list[list[str]] = []
    for line in job.splitlines():
        if line.startswith("      - "):
            found.append([])
        if found:
            found[-1].append(line)
    return ["\n".join(lines) for lines in found]


#: Where the spring job reads the gateway test's result.
GATEWAY_REPORT = (
    "services/spring-services/spring-core-service/target/surefire-reports/"
    "TEST-io.dagents.spring.core.compiler.ProcessManifestCompilerGatewayTest.xml"
)


def surefire_report(skipped: int, test_names: tuple[str, ...]) -> str:
    """A report in the shape Surefire 3 writes, with one line per test case."""
    cases = "\n".join(
        f'  <testcase name="{name}" classname="io.dagents.spring.core.compiler.ProcessManifestCompilerGatewayTest"'
        f' time="0.01"/>'
        for name in test_names
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<testsuite xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="3.0"'
        ' name="io.dagents.spring.core.compiler.ProcessManifestCompilerGatewayTest" time="0.08"'
        f' tests="{len(test_names)}" errors="0" skipped="{skipped}" failures="0">\n'
        f"{cases}\n</testsuite>\n"
    )


class SpringCiWiringTests(unittest.TestCase):
    """The CI jobs give these tests what they need and fail when a planner test skipped."""

    def setUp(self) -> None:
        # Imported here so that loading this module does not import the guard
        # tests' module, which adds scripts/ to sys.path.
        from tests.test_ci_guards import CI_WORKFLOW, guard_calls, jobs, run_blocks

        self.guard_calls = guard_calls
        self.run_blocks = run_blocks
        self.jobs = jobs(CI_WORKFLOW.read_text(encoding="utf-8"))

    def test_the_contracts_job_builds_the_core_jar_and_requires_the_process_tests(self) -> None:
        contracts = self.jobs["contracts"]
        modules = re.search(r"-pl\s+(\S+)", contracts)
        self.assertIsNotNone(modules, "the contracts job builds no Spring module")
        self.assertIn("spring-core-service", modules.group(1).split(","))
        self.assertTrue(
            any(
                "tests.test_spring_services_e2e.SpringCoreProcessTests" in call.require_ran
                and "planner" in call.forbid
                for call in self.guard_calls(contracts)
            ),
            "the contracts job must forbid planner skips and require SpringCoreProcessTests to run",
        )

    def test_the_spring_job_runs_maven_with_the_planner(self) -> None:
        spring = self.jobs["spring"]
        self.assertRegex(spring, r"(?m)^    needs:.*\bplanners\b")
        self.assertRegex(spring, r"(?m)^          name: dagentsc$")
        maven = [step for step in steps(spring) if re.search(r"\bmvn\b.*\bverify\b", step)]
        self.assertEqual(1, len(maven), "the spring job should run mvn verify in one step")
        self.assertIn("DAGENTSC_BIN: ${{ env.PLANNER_DIR }}/dagentsc.exe", maven[0])

    def test_the_spring_job_fails_unless_the_planner_test_ran(self) -> None:
        checks = [block for block in self.run_blocks(self.jobs["spring"]) if "surefire-reports" in block]
        self.assertEqual(1, len(checks), "the spring job should have one step that reads the gateway test's report")
        script = textwrap.dedent(checks[0])
        ran = ("fallsBackWhenExternalCompilerFails", "consumesJsonFromOcamlCompilerProcess(Path)")
        cases = {
            "the test ran": (surefire_report(0, ran), 0),
            "the test skipped": (surefire_report(1, ran), 1),
            "the test is missing": (surefire_report(0, ran[:1]), 1),
            "no report": (None, 1),
        }
        for case, (report, expected) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                if report is not None:
                    path = Path(directory) / GATEWAY_REPORT
                    path.parent.mkdir(parents=True)
                    path.write_text(report, encoding="utf-8")
                # GitHub runs a step's script with `bash -e`.
                completed = subprocess.run(
                    ["bash", "-e", "-c", script], cwd=directory, capture_output=True, text=True, check=False
                )
                self.assertEqual(expected, completed.returncode, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
