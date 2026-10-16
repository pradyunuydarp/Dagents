"""Tests for applying one rendered Compose service.

The process applier starts real child processes (Python itself, with ``-c``),
so its tests check real process groups, logs and signals. The Docker Compose
applier runs every command through a runner; its tests pass a fake runner and
check the exact commands, so they start no container.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agents.common.infrastructure.compose_apply import (
    AppliedService,
    CommandResult,
    ComposeApplier,
    ComposeApplyError,
    DockerComposeApplier,
    LocalProcessApplier,
    compose_service,
    format_env_file,
    parse_env_file,
    unescape_compose,
)

PYTHON = sys.executable
SERVICE = "agent-site-d"


def rendered(service: str = SERVICE, command: list[str] | None = None, *, environment=None, extra: str = "") -> str:
    """A file in the shape core-service renders: one service, quoted strings, a network."""
    lines = ["services:", f'  "{service}":', '    image: "example/agent:1"']
    if command is not None:
        lines.append(f"    command: {json.dumps(command)}")
    if environment is not None:
        lines.append("    environment:")
        lines.extend(f"      {json.dumps(key)}: {json.dumps(value)}" for key, value in environment.items())
    lines += [
        "    ports:",
        '      - "127.0.0.1:8085:8085"',
        "    healthcheck:",
        '      test: ["CMD", "python", "-c", "print(1)"]',
        "      interval: 5s",
        '    networks:',
        '      - "default"',
        "    restart: unless-stopped",
    ]
    if extra:
        lines.append(extra)
    lines += ["networks:", '  "default": {}']
    return "\n".join(lines) + "\n"


def alive(pid: int) -> bool:
    """Whether ``pid`` is a running process (a zombie counts as gone)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state != "Z"


def wait_until(condition, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


class ComposeFileTests(unittest.TestCase):
    """``compose_service`` reads one service, and ``unescape_compose`` undoes ``$$``."""

    def test_the_one_service_is_returned(self) -> None:
        service = compose_service(rendered(command=[PYTHON, "-V"]), SERVICE)
        self.assertEqual(service["command"], [PYTHON, "-V"])
        self.assertEqual(service["restart"], "unless-stopped")

    def test_zero_or_two_services_are_refused(self) -> None:
        two = rendered(command=["a"]).replace("networks:\n  \"default\"", '  "other":\n    image: "x"\nnetworks:\n  "default"')
        for text, words in (
            ("services: {}\n", "exactly one service, not 0"),
            ("networks: {}\n", "exactly one service, not 0"),
            (two, "exactly one service, not 2"),
        ):
            with self.subTest(words=words), self.assertRaises(ComposeApplyError) as raised:
                compose_service(text, SERVICE)
            self.assertIn(words, raised.exception.message)

    def test_another_service_name_another_top_level_key_and_bad_yaml_are_refused(self) -> None:
        cases = (
            (rendered(command=["a"]), "other-service", "not other-service"),
            (rendered(command=["a"]) + "volumes: {}\n", SERVICE, "top-level key"),
            ("- a list\n", SERVICE, "not a Compose mapping"),
            ("services: [unclosed\n", SERVICE, "not valid YAML"),
            ('services:\n  "agent-site-d": "text"\n', SERVICE, "not a mapping"),
        )
        for text, service, words in cases:
            with self.subTest(words=words), self.assertRaises(ComposeApplyError) as raised:
                compose_service(text, service)
            self.assertIn(words, raised.exception.message)

    def test_a_double_dollar_becomes_one(self) -> None:
        self.assertEqual(unescape_compose("$$HOME and $${VAR} cost $$5"), "$HOME and ${VAR} cost $5")
        self.assertEqual(unescape_compose("no dollar"), "no dollar")
        self.assertEqual(unescape_compose("$$$$"), "$$")

    def test_a_lone_dollar_is_refused(self) -> None:
        for value in ("$HOME", "${VAR}", "ends with $", "$$$"):
            with self.subTest(value=value), self.assertRaises(ComposeApplyError) as raised:
                unescape_compose(value)
            self.assertEqual(
                raised.exception.message, "the file uses variable interpolation, which this applier does not support"
            )

    def test_an_error_keeps_the_last_twenty_lines(self) -> None:
        error = ComposeApplyError("failed", "\n".join(f"line {n}" for n in range(30)) + "\n")
        self.assertEqual(str(error), "failed")
        self.assertEqual(error.output_tail.splitlines(), [f"line {n}" for n in range(10, 30)])


class LocalProcessApplierTests(unittest.TestCase):
    """The process applier with real child processes."""

    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="compose-apply-"))
        self.cwd = Path(tempfile.mkdtemp(prefix="compose-cwd-"))
        self.applier = LocalProcessApplier(
            self.workdir / "deploy",
            cwd=self.cwd,
            programs={PYTHON},
            base_env={"PATH": os.environ.get("PATH", ""), "BASE_ONLY": "base", "SHARED": "base"},
            stop_seconds=5.0,
        )
        self.addCleanup(self.applier.close)

    def start(self, code: str, *args: str, environment=None, service: str = SERVICE) -> AppliedService:
        text = rendered(service, [PYTHON, "-c", code, *args], environment=environment)
        return self.applier.apply(text, service, project="ignored", wait_seconds=30)

    def test_it_is_a_compose_applier_named_process(self) -> None:
        self.assertIsInstance(self.applier, ComposeApplier)
        self.assertEqual(self.applier.name, "process")

    def test_only_an_allowed_program_starts(self) -> None:
        for command in (["/bin/sh", "-c", "true"], [PYTHON + "3", "-V"], ["python", "-V"]):
            with self.subTest(program=command[0]), self.assertRaises(ComposeApplyError) as raised:
                self.applier.check(rendered(command=command), SERVICE)
            self.assertEqual(raised.exception.message, f"`{command[0]}` is not a program this applier may start")
        self.assertEqual(self.applier.status(SERVICE, project=""), "not_started")

    def test_a_key_it_cannot_apply_is_refused(self) -> None:
        for extra, key in (("    volumes: [\"/:/host\"]", "volumes"), ("    privileged: true", "privileged")):
            with self.subTest(key=key), self.assertRaises(ComposeApplyError) as raised:
                self.applier.check(rendered(command=[PYTHON, "-V"], extra=extra), SERVICE)
            self.assertEqual(raised.exception.message, f"the process applier cannot apply `{key}`")

    def test_a_service_that_depends_on_another_is_refused(self) -> None:
        extra = '    depends_on:\n      "gma":\n        condition: service_healthy'
        with self.assertRaises(ComposeApplyError) as raised:
            self.applier.check(rendered(command=[PYTHON, "-V"], extra=extra), SERVICE)
        self.assertIn("`depends_on`", raised.exception.message)
        self.applier.check(rendered(command=[PYTHON, "-V"], extra="    depends_on: {}"), SERVICE)

    def test_command_and_environment_must_have_the_rendered_shape(self) -> None:
        for extra_file in (
            rendered(),  # no command
            rendered().replace('    image: "example/agent:1"', '    image: "example/agent:1"\n    command: "python -V"'),
            rendered(command=[PYTHON, "-V"]).replace("    ports:", "    environment: [\"A=1\"]\n    ports:"),
            rendered(command=[PYTHON, "-V"]).replace("    ports:", "    environment:\n      A: 1\n    ports:"),
        ):
            with self.subTest(text=extra_file[:80]), self.assertRaises(ComposeApplyError):
                self.applier.check(extra_file, SERVICE)

    def test_an_early_exit_raises_with_the_end_of_the_log(self) -> None:
        code = "import sys\nfor n in range(30): print('line', n)\nsys.exit(3)"
        with self.assertRaises(ComposeApplyError) as raised:
            self.start(code)
        self.assertEqual(raised.exception.message, "the process exited with status 3")
        self.assertEqual(raised.exception.output_tail.splitlines(), [f"line {n}" for n in range(10, 30)])
        self.assertEqual(raised.exception.log_path, str(self.applier.log_path(SERVICE)))
        self.assertEqual(self.applier.status(SERVICE, project=""), "not_started")

    def test_status_is_running_then_stopped_and_unknown_services_are_not_started(self) -> None:
        applied = self.start("import time; time.sleep(60)")
        pid = int(applied.detail.rsplit(" ", 1)[1])
        self.assertEqual(applied, AppliedService(SERVICE, "process", False, f"started process {pid}", applied.log_path))
        self.assertEqual(self.applier.status(SERVICE, project=""), "running")
        self.assertEqual(self.applier.status("other", project=""), "not_started")
        os.kill(pid, 9)
        self.assertTrue(wait_until(lambda: self.applier.status(SERVICE, project="") == "stopped"))

    def test_a_live_child_is_adopted_not_started_again(self) -> None:
        first = self.start("import time; time.sleep(60)")
        second = self.start("import time; time.sleep(60)")
        pid = first.detail.rsplit(" ", 1)[1]
        self.assertFalse(first.adopted)
        self.assertTrue(second.adopted)
        self.assertEqual(second.detail, f"already running as process {pid}")
        self.assertEqual(second.log_path, first.log_path)

    def test_a_stopped_child_is_started_again(self) -> None:
        first = self.start("import time; time.sleep(60)")
        self.applier.stop(SERVICE, project="")
        second = self.start("import time; time.sleep(60)")
        self.assertFalse(second.adopted)
        self.assertNotEqual(second.detail, first.detail)

    def test_stop_ends_every_process_in_the_group(self) -> None:
        pid_file = self.workdir / "grandchild.pid"
        code = (
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "open(sys.argv[1], 'w').write(str(child.pid))\n"
            "time.sleep(60)\n"
        )
        applied = self.start(code, str(pid_file))
        self.assertTrue(wait_until(lambda: pid_file.exists() and pid_file.read_text().strip() != ""))
        grandchild = int(pid_file.read_text())
        child = int(applied.detail.rsplit(" ", 1)[1])
        self.assertTrue(alive(grandchild))

        self.applier.stop(SERVICE, project="")

        self.assertTrue(wait_until(lambda: not alive(grandchild)), "the grandchild outlived stop")
        self.assertFalse(alive(child))
        self.assertEqual(self.applier.status(SERVICE, project=""), "not_started")
        self.applier.stop(SERVICE, project="")  # an unknown service: nothing happens

    def test_stop_uses_sigkill_when_sigterm_is_ignored(self) -> None:
        applier = LocalProcessApplier(self.workdir, cwd=self.cwd, programs={PYTHON}, base_env={}, stop_seconds=0.5)
        self.addCleanup(applier.close)
        ready = self.workdir / "ready"
        code = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "open(sys.argv[1], 'w').write('ready')\n"
            "time.sleep(60)\n"
        )
        applied = applier.apply(rendered(command=[PYTHON, "-c", code, str(ready)]), SERVICE, project="", wait_seconds=5)
        self.assertTrue(wait_until(ready.exists))
        started = time.monotonic()
        applier.stop(SERVICE, project="")
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(alive(int(applied.detail.rsplit(" ", 1)[1])))

    def test_stop_ends_a_stopped_process_without_waiting_for_sigkill(self) -> None:
        # A hung service may be stopped with SIGSTOP. uvicorn handles SIGTERM,
        # and a stopped process acts on a handled signal only when it continues.
        applier = LocalProcessApplier(self.workdir, cwd=self.cwd, programs={PYTHON}, base_env={}, stop_seconds=10)
        self.addCleanup(applier.close)
        ready = self.workdir / "ready-stopped"
        code = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
            "open(sys.argv[1], 'w').write('ready')\n"
            "time.sleep(60)\n"
        )
        applied = applier.apply(rendered(command=[PYTHON, "-c", code, str(ready)]), SERVICE, project="", wait_seconds=5)
        self.assertTrue(wait_until(ready.exists))
        pid = int(applied.detail.rsplit(" ", 1)[1])
        os.kill(pid, signal.SIGSTOP)
        started = time.monotonic()
        applier.stop(SERVICE, project="")
        self.assertLess(time.monotonic() - started, 5, "stop waited for SIGKILL")
        self.assertFalse(alive(pid))

    def test_close_stops_every_child(self) -> None:
        pids = [
            int(self.start("import time; time.sleep(60)", service=name).detail.rsplit(" ", 1)[1])
            for name in ("agent-one", "agent-two")
        ]
        self.applier.close()
        self.assertTrue(wait_until(lambda: not any(alive(pid) for pid in pids)))
        self.assertEqual(self.applier.status("agent-one", project=""), "not_started")

    def test_dollars_environment_and_working_directory_reach_the_process(self) -> None:
        out = self.workdir / "seen.json"
        code = (
            "import json, os, sys, time\n"
            "json.dump({'argv': sys.argv[1:], 'price': os.environ.get('PRICE'), 'base': os.environ.get('BASE_ONLY'),"
            " 'shared': os.environ.get('SHARED'), 'cwd': os.getcwd()}, open(sys.argv[1], 'w'))\n"
            "time.sleep(60)\n"
        )
        self.start(
            code.replace("$", "$$"), str(out), "$$HOME", "a $$5 note",
            environment={"PRICE": "$$5 and $${VAR}", "SHARED": "service"},
        )
        self.assertTrue(wait_until(lambda: out.exists() and out.read_text().endswith("}")))
        seen = json.loads(out.read_text())
        self.assertEqual(seen["argv"], [str(out), "$HOME", "a $5 note"])
        self.assertEqual(seen["price"], "$5 and ${VAR}")
        self.assertEqual((seen["base"], seen["shared"]), ("base", "service"))
        self.assertEqual(os.path.realpath(seen["cwd"]), os.path.realpath(self.cwd))

    def test_the_process_id_is_in_a_file_while_the_process_runs(self) -> None:
        # The process runs in its own session, so it outlives an owner that dies
        # without calling close. The file lets a supervisor stop it then.
        pid_file = self.workdir / "deploy" / f"{SERVICE}.pid"
        self.assertEqual(self.applier.pid_path(SERVICE), pid_file)
        applied = self.start("import time; time.sleep(60)")
        pid = int(applied.detail.rsplit(" ", 1)[1])
        self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), str(pid))
        self.assertEqual(os.getpgid(pid), pid, "the process does not lead its own process group")
        self.applier.stop(SERVICE, project="")
        self.assertFalse(pid_file.exists())

    def test_an_early_exit_leaves_no_process_id_file(self) -> None:
        with self.assertRaises(ComposeApplyError):
            self.start("import sys; sys.exit(3)")
        self.assertFalse(self.applier.pid_path(SERVICE).exists())

    def test_the_output_goes_to_the_services_log(self) -> None:
        applied = self.start("import sys, time; print('hello from the service', flush=True); time.sleep(60)")
        log = self.workdir / "deploy" / f"{SERVICE}.log"
        self.assertEqual(applied.log_path, str(log))
        self.assertEqual(self.applier.log_path(SERVICE), log)
        self.assertTrue(wait_until(lambda: log.exists() and "hello from the service" in log.read_text()))

    def test_without_configured_folders_an_env_file_is_refused(self) -> None:
        env_file = Path(tempfile.mkdtemp(prefix="compose-env-")) / "site.env"
        env_file.write_text("A=1\n", encoding="utf-8")
        text = rendered(command=[PYTHON, "-V"], extra=f"    env_file:\n      - {json.dumps(str(env_file))}")
        with self.assertRaises(ComposeApplyError) as raised:
            self.applier.check(text, SERVICE)
        self.assertEqual(raised.exception.message, "this applier reads `env_file` only from its configured folders")


def with_env_file(paths: list[str], command: list[str], environment=None) -> str:
    listed = "\n".join(f"      - {json.dumps(path)}" for path in paths)
    return rendered(command=command, environment=environment, extra=f"    env_file:\n{listed}")


SECRET = "hunter2-do-not-print"


class EnvFileFormatTests(unittest.TestCase):
    """The ``NAME=VALUE`` subset both appliers read, and the writer that produces it."""

    def test_bare_and_single_quoted_values_are_read(self) -> None:
        text = "# a comment\n\nPLAIN=value-1.2:3/x@y\nQUOTED='a b = c # d $e \"f\" \\g'\nEMPTY=\nEMPTY_QUOTED=''\n"
        self.assertEqual(
            parse_env_file(text),
            {
                "PLAIN": "value-1.2:3/x@y",
                "QUOTED": 'a b = c # d $e "f" \\g',
                "EMPTY": "",
                "EMPTY_QUOTED": "",
            },
        )

    def test_a_line_outside_the_subset_is_refused_without_quoting_it(self) -> None:
        for line in (
            f"SECRET={SECRET} more",
            f'SECRET="{SECRET}"',
            f"SECRET=${SECRET}",
            f"SECRET='{SECRET}'s'",
            f"SECRET='{SECRET}",
            f"SECRET={SECRET}#x",
            f"SECRET={SECRET}\\n",
            f"export SECRET={SECRET}",
            f" SECRET={SECRET}",
            f"1SECRET={SECRET}",
            f"{SECRET}",
        ):
            with self.subTest(line=line), self.assertRaises(ComposeApplyError) as raised:
                parse_env_file(f"FIRST=1\n{line}\n")
            self.assertEqual(
                raised.exception.message, "line 2 of the env file is not NAME=VALUE in the form this applier reads"
            )
            self.assertNotIn(SECRET, str(raised.exception) + raised.exception.output_tail)

    def test_the_writer_quotes_only_when_it_must_and_refuses_what_it_cannot_write(self) -> None:
        text = format_env_file({"B": "plain", "A": "has space", "C": ""})
        self.assertEqual(text, "A='has space'\nB=plain\nC=\n")
        self.assertEqual(parse_env_file(text), {"A": "has space", "B": "plain", "C": ""})
        for name, value in (("A", "it's"), ("A", "two\nlines"), ("1A", "x"), ("A-B", "x")):
            with self.subTest(name=name), self.assertRaises(ValueError) as raised:
                format_env_file({name: value})
            self.assertNotIn(value, str(raised.exception))


class LocalProcessEnvFileTests(unittest.TestCase):
    """The process applier reads ``env_file`` from its configured folders only."""

    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="compose-apply-env-"))
        self.env_dir = Path(tempfile.mkdtemp(prefix="compose-env-"))
        self.outside = Path(tempfile.mkdtemp(prefix="compose-outside-"))
        self.applier = LocalProcessApplier(
            self.workdir,
            cwd=self.workdir,
            programs={PYTHON},
            base_env={"PATH": os.environ.get("PATH", ""), "FROM_BASE": "base", "SHARED": "base", "FILE_WINS": "base"},
            stop_seconds=5.0,
            env_dirs=[self.env_dir],
        )
        self.addCleanup(self.applier.close)

    def write(self, folder: Path, name: str, text: str) -> Path:
        path = folder / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_an_env_file_in_the_folder_reaches_the_process_and_environment_wins(self) -> None:
        env_file = self.write(self.env_dir, "site.env", "FROM_FILE=file\nSHARED=file\nFILE_WINS=file\nQUOTED='a b'\n")
        out = self.workdir / "seen.json"
        code = (
            "import json, os, sys, time\n"
            "json.dump({k: os.environ.get(k) for k in ('FROM_BASE', 'FROM_FILE', 'SHARED', 'FILE_WINS', 'QUOTED')},"
            " open(sys.argv[1], 'w'))\n"
            "time.sleep(60)\n"
        )
        text = with_env_file([str(env_file)], [PYTHON, "-c", code, str(out)], environment={"SHARED": "service"})
        self.applier.apply(text, SERVICE, project="", wait_seconds=30)
        self.assertTrue(wait_until(lambda: out.exists() and out.read_text().endswith("}")))
        self.assertEqual(
            json.loads(out.read_text()),
            {"FROM_BASE": "base", "FROM_FILE": "file", "SHARED": "service", "FILE_WINS": "file", "QUOTED": "a b"},
        )

    def test_an_env_file_outside_the_folders_is_refused(self) -> None:
        outside = self.write(self.outside, "site.env", "A=1\n")
        link = self.env_dir / "link.env"
        link.symlink_to(outside)
        for path in (str(outside), str(link), "site.env", str(self.env_dir / ".." / self.outside.name / "site.env")):
            with self.subTest(path=path), self.assertRaises(ComposeApplyError) as raised:
                self.applier.check(with_env_file([path], [PYTHON, "-V"]), SERVICE)
            self.assertEqual(raised.exception.message, "an env file is not in a folder this applier reads")

    def test_a_missing_or_unreadable_env_file_is_refused(self) -> None:
        with self.assertRaises(ComposeApplyError) as raised:
            self.applier.check(with_env_file([str(self.env_dir / "missing.env")], [PYTHON, "-V"]), SERVICE)
        self.assertEqual(raised.exception.message, "an env file is not in a folder this applier reads")

    def test_a_bad_line_is_refused_before_anything_starts(self) -> None:
        env_file = self.write(self.env_dir, "site.env", f"GOOD=1\nBAD={SECRET} two\n")
        with self.assertRaises(ComposeApplyError) as raised:
            self.applier.apply(with_env_file([str(env_file)], [PYTHON, "-V"]), SERVICE, project="", wait_seconds=5)
        self.assertNotIn(SECRET, str(raised.exception) + raised.exception.output_tail)
        self.assertEqual(self.applier.status(SERVICE, project=""), "not_started")

    def test_env_file_must_be_a_list_of_paths(self) -> None:
        text = rendered(command=[PYTHON, "-V"], extra='    env_file: "site.env"')
        with self.assertRaises(ComposeApplyError) as raised:
            self.applier.check(text, SERVICE)
        self.assertEqual(raised.exception.message, "this applier needs `env_file` as a list of paths")


class FakeRunner:
    """Records each command and answers from a function of its arguments."""

    def __init__(self, answer=None) -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.answer = answer or (lambda args: CommandResult(0, "", ""))

    def __call__(self, args: list[str], *, timeout: float) -> CommandResult:
        self.calls.append((list(args), timeout))
        result = self.answer(args)
        if isinstance(result, BaseException):
            raise result
        return result


class DockerComposeApplierTests(unittest.TestCase):
    """The Compose applier's commands, checked against a fake runner."""

    PROJECT = "demo-agent-site-d"

    def setUp(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="compose-docker-"))
        self.text = rendered(command=["uvicorn", "app.main:app"])

    def applier(self, answer=None, **options) -> tuple[DockerComposeApplier, FakeRunner]:
        runner = FakeRunner(answer)
        return DockerComposeApplier(self.workdir, runner=runner, **options), runner

    def filters(self) -> list[str]:
        return [
            "--filter", f"label=com.docker.compose.project={self.PROJECT}",
            "--filter", f"label=com.docker.compose.service={SERVICE}",
        ]

    def test_it_is_a_compose_applier_named_compose(self) -> None:
        applier, runner = self.applier()
        self.assertIsInstance(applier, ComposeApplier)
        self.assertEqual(applier.name, "compose")
        applier.close()
        self.assertEqual(runner.calls, [])

    def test_check_runs_config_quiet_on_the_written_file(self) -> None:
        applier, runner = self.applier(timeout=33.0)
        applier.check(self.text, SERVICE)
        check_file = self.workdir / f"check-{SERVICE}.yml"
        self.assertEqual(
            runner.calls, [(["docker", "compose", "--file", str(check_file), "config", "--quiet", SERVICE], 33.0)]
        )
        self.assertEqual(check_file.read_text(), self.text)

    def test_check_refuses_a_file_with_another_service_before_running_docker(self) -> None:
        applier, runner = self.applier()
        with self.assertRaises(ComposeApplyError):
            applier.check(self.text, "other")
        self.assertEqual(runner.calls, [])

    def test_check_refuses_a_key_the_planner_never_renders_before_running_docker(self) -> None:
        # With the Docker socket, any of these would give the container the host.
        for extra in (
            "    privileged: true",
            '    pid: "host"',
            '    volumes: ["/:/host"]',
            '    network_mode: "host"',
            '    cap_add: ["SYS_ADMIN"]',
        ):
            key = extra.split(":")[0].strip()
            with self.subTest(key=key):
                applier, runner = self.applier()
                with self.assertRaises(ComposeApplyError) as raised:
                    applier.check(rendered(command=["uvicorn", "app.main:app"], extra=extra), SERVICE)
                self.assertEqual(raised.exception.message, f"the compose applier cannot apply `{key}`")
                self.assertEqual(runner.calls, [])
                with self.assertRaises(ComposeApplyError):
                    applier.apply(
                        rendered(command=["uvicorn", "app.main:app"], extra=extra),
                        SERVICE,
                        project=self.PROJECT,
                        wait_seconds=5,
                    )
                self.assertEqual(runner.calls, [])

    def test_check_accepts_every_key_the_planner_renders(self) -> None:
        env_dir = Path(tempfile.mkdtemp(prefix="compose-docker-env-"))
        (env_dir / "site.env").write_text("A=1\n", encoding="utf-8")
        text = rendered(
            command=["uvicorn", "app.main:app"],
            environment={"A": "1"},
            extra=f'    env_file:\n      - {json.dumps(str(env_dir / "site.env"))}\n    expose:\n      - "9000"\n'
            '    depends_on:\n      "gma":\n        condition: service_healthy',
        )
        applier, runner = self.applier(env_dirs=[env_dir])
        applier.check(text, SERVICE)
        self.assertEqual(1, len(runner.calls))

    def test_check_refuses_an_env_file_outside_its_folders_before_running_docker(self) -> None:
        env_dir = Path(tempfile.mkdtemp(prefix="compose-docker-env-"))
        outside = Path(tempfile.mkdtemp(prefix="compose-docker-outside-")) / "site.env"
        outside.write_text("A=1\n", encoding="utf-8")
        (env_dir / "link.env").symlink_to(outside)
        bad = env_dir / "bad.env"
        bad.write_text(f"A={SECRET} b\n", encoding="utf-8")
        cases = (
            ([], [str(outside)], "this applier reads `env_file` only from its configured folders"),
            ([env_dir], [str(outside)], "an env file is not in a folder this applier reads"),
            ([env_dir], [str(env_dir / "link.env")], "an env file is not in a folder this applier reads"),
            ([env_dir], ["site.env"], "an env file is not in a folder this applier reads"),
            ([env_dir], [str(bad)], "line 1 of the env file is not NAME=VALUE in the form this applier reads"),
        )
        for env_dirs, paths, message in cases:
            with self.subTest(paths=paths, env_dirs=env_dirs):
                applier, runner = self.applier(env_dirs=env_dirs)
                text = with_env_file(paths, ["uvicorn", "app.main:app"])
                with self.assertRaises(ComposeApplyError) as raised:
                    applier.apply(text, SERVICE, project=self.PROJECT, wait_seconds=5)
                self.assertEqual(raised.exception.message, message)
                self.assertNotIn(SECRET, str(raised.exception) + raised.exception.output_tail)
                self.assertEqual(runner.calls, [])

    def test_check_refuses_a_network_the_planner_never_renders(self) -> None:
        for network in ('{"name": "host", "external": true}', '{"driver": "macvlan"}'):
            with self.subTest(network=network):
                text = rendered(command=["uvicorn", "app.main:app"]).replace('"default": {}', f'"default": {network}')
                applier, runner = self.applier()
                with self.assertRaises(ComposeApplyError) as raised:
                    applier.check(text, SERVICE)
                self.assertEqual(
                    raised.exception.message, "the compose applier applies a network only as {} or external: true"
                )
                self.assertEqual(runner.calls, [])
        external = rendered(command=["uvicorn", "app.main:app"]).replace('"default": {}', '"default":\n    external: true')
        applier, runner = self.applier()
        applier.check(external, SERVICE)
        self.assertEqual(1, len(runner.calls))

    def test_a_refused_file_raises_with_the_end_of_stderr(self) -> None:
        stderr = "\n".join(f"error {n}" for n in range(25))
        applier, _ = self.applier(lambda args: CommandResult(15, "", stderr))
        with self.assertRaises(ComposeApplyError) as raised:
            applier.check(self.text, SERVICE)
        self.assertEqual(raised.exception.message, "docker compose refused the file")
        self.assertEqual(raised.exception.output_tail.splitlines(), [f"error {n}" for n in range(5, 25)])

    def test_apply_starts_the_one_service_without_dependencies_builds_or_pulls(self) -> None:
        applier, runner = self.applier()
        applied = applier.apply(self.text, SERVICE, project=self.PROJECT, wait_seconds=42.5)
        project_file = self.workdir / f"{self.PROJECT}.yml"
        self.assertEqual(project_file.read_text(), self.text)
        commands = [args for args, _ in runner.calls]
        self.assertEqual(commands[0][:3], ["docker", "compose", "--file"])
        self.assertEqual(commands[1], ["docker", "ps", "--quiet", "--filter", "status=running", *self.filters()])
        self.assertEqual(
            runner.calls[2],
            (
                [
                    "docker", "compose", "--project-name", self.PROJECT, "--file", str(project_file),
                    "up", "--detach", "--no-build", "--pull", "never", "--no-deps",
                    "--wait", "--wait-timeout", str(math.ceil(42.5)), SERVICE,
                ],
                72.5,
            ),
        )
        self.assertEqual(commands[3], commands[1])
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(
            applied,
            AppliedService(SERVICE, "compose", False, f"started container for {SERVICE} in project {self.PROJECT}", None),
        )

    def test_a_running_container_compose_keeps_is_adopted(self) -> None:
        # `up` runs anyway: Compose keeps a container whose file and image are unchanged.
        def answer(args):
            return CommandResult(0, "abc123\n", "") if args[1] == "ps" else CommandResult(0, "", "")

        applier, runner = self.applier(answer)
        applied = applier.apply(self.text, SERVICE, project=self.PROJECT, wait_seconds=10)
        self.assertEqual(applied, AppliedService(SERVICE, "compose", True, "already running", None))
        self.assertIn("up", [word for args, _ in runner.calls for word in args])

    def test_a_running_container_compose_recreates_is_not_adopted(self) -> None:
        # A container started from an older file or image is recreated by `up`,
        # so the applier reports a new start, not the old container kept.
        ids = iter(["old111\n", "new222\n"])

        def answer(args):
            return CommandResult(0, next(ids), "") if args[1] == "ps" else CommandResult(0, "", "")

        applier, _ = self.applier(answer)
        applied = applier.apply(self.text, SERVICE, project=self.PROJECT, wait_seconds=10)
        self.assertFalse(applied.adopted)
        self.assertEqual(
            applied.detail,
            f"recreated the container for {SERVICE} in project {self.PROJECT}, because its file or image changed",
        )

    def test_a_failed_up_leaves_a_container_that_ran_before(self) -> None:
        def answer(args):
            if "up" in args:
                return CommandResult(1, "", "image not found")
            return CommandResult(0, "abc123\n", "") if args[1] == "ps" else CommandResult(0, "", "")

        applier, runner = self.applier(answer)
        with self.assertRaises(ComposeApplyError):
            applier.apply(self.text, SERVICE, project=self.PROJECT, wait_seconds=10)
        self.assertNotIn("rm", [word for args, _ in runner.calls for word in args])

    def test_a_failed_start_removes_the_container_and_raises(self) -> None:
        def answer(args):
            if "up" in args:
                return CommandResult(1, "", "pulling\nimage not found: example/agent:1")
            return CommandResult(0, "", "")

        applier, runner = self.applier(answer, timeout=20.0)
        with self.assertRaises(ComposeApplyError) as raised:
            applier.apply(self.text, SERVICE, project=self.PROJECT, wait_seconds=10)
        self.assertEqual(raised.exception.message, f"docker compose could not start {SERVICE}")
        self.assertIn("image not found", raised.exception.output_tail)
        project_file = self.workdir / f"{self.PROJECT}.yml"
        self.assertEqual(
            runner.calls[-1],
            (
                ["docker", "compose", "--project-name", self.PROJECT, "--file", str(project_file),
                 "rm", "--stop", "--force", SERVICE],
                20.0,
            ),
        )

    def test_a_start_that_times_out_is_removed_too(self) -> None:
        def answer(args):
            if "up" in args:
                return subprocess.TimeoutExpired(args, 40, stderr=b"waiting for the container")
            return CommandResult(0, "", "")

        applier, runner = self.applier(answer)
        with self.assertRaises(ComposeApplyError) as raised:
            applier.apply(self.text, SERVICE, project=self.PROJECT, wait_seconds=10)
        self.assertIn("waiting for the container", raised.exception.output_tail)
        self.assertIn("timed out after 40 seconds", raised.exception.output_tail)
        self.assertEqual(runner.calls[-1][0][-4:], ["rm", "--stop", "--force", SERVICE])

    def test_status_reads_the_container_states(self) -> None:
        cases = (
            (CommandResult(0, "", ""), "not_started"),
            (CommandResult(0, "\n\n", ""), "not_started"),
            (CommandResult(0, "exited\nrunning\n", ""), "running"),
            (CommandResult(0, "exited\ncreated\n", ""), "stopped"),
            (CommandResult(1, "", "Cannot connect to the Docker daemon"), "unknown"),
            (FileNotFoundError("docker"), "unknown"),
        )
        for result, expected in cases:
            with self.subTest(expected=expected, result=result):
                applier, runner = self.applier(lambda args, result=result: result)
                self.assertEqual(applier.status(SERVICE, project=self.PROJECT), expected)
                self.assertEqual(runner.calls[0][0], ["docker", "ps", "--all", *self.filters(), "--format", "{{.State}}"])

    def test_stop_removes_the_services_containers(self) -> None:
        def answer(args):
            return CommandResult(0, "abc123\ndef456\n", "") if args[1] == "ps" else CommandResult(0, "", "")

        applier, runner = self.applier(answer, timeout=9.0)
        applier.stop(SERVICE, project=self.PROJECT)
        self.assertEqual(
            runner.calls,
            [
                (["docker", "ps", "--all", "--quiet", *self.filters()], 9.0),
                (["docker", "rm", "--force", "abc123", "def456"], 9.0),
            ],
        )

    def test_stop_with_no_container_removes_nothing(self) -> None:
        applier, runner = self.applier()
        applier.stop(SERVICE, project=self.PROJECT)
        self.assertEqual([args[1] for args, _ in runner.calls], ["ps"])

    def test_a_missing_docker_command_is_named(self) -> None:
        applier, _ = self.applier(lambda args: FileNotFoundError(args[0]))
        for call in (
            lambda: applier.check(self.text, SERVICE),
            lambda: applier.apply(self.text, SERVICE, project=self.PROJECT, wait_seconds=5),
            lambda: applier.stop(SERVICE, project=self.PROJECT),
        ):
            with self.assertRaises(ComposeApplyError) as raised:
                call()
            self.assertEqual(raised.exception.message, "the docker command was not found")

    def test_another_docker_command_is_used_when_given(self) -> None:
        applier, runner = self.applier(docker="/usr/local/bin/docker")
        applier.check(self.text, SERVICE)
        self.assertEqual(runner.calls[0][0][0], "/usr/local/bin/docker")


if __name__ == "__main__":
    unittest.main()
