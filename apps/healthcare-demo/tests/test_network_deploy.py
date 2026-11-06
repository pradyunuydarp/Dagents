"""Adding Hospital 290 to a network-mode run of real processes, as ``scripts/run_local_demo.sh --deploy`` does.

:class:`NetworkScriptDeployTests` runs the start scripts with a stand-in for
Python. The stand-in records what each ``uvicorn`` process would start with
(its folder, its arguments and its environment) and answers HTTP on its port,
so the script runs to the end without a database or the planner. The tests
check the settings the script gives each process with and without
``--deploy``, the new LMA's env file, that only ``--deploy`` turns the
deployer on, that a process the app left behind is stopped, that nothing
starts while the joining port answers, and that no token is printed.

:class:`NetworkDeployTests` starts core-service, the GMA, the three LMAs and
the API as real processes against a scratch PostgreSQL 16 database, with the
settings the script gives them, and calls only the API, as the frontend does.
``POST /api/v1/deployments`` then starts Hospital 290's LMA as a child process
of the API, from the Compose file core-service's planner rendered. Later, the
new LMA is killed and started again through the same route, and the GMA
process is killed and started again on its port. The tests run in name
order, which is the order of the scenario. It needs
``HEALTHCARE_DEMO_TEST_DATABASE_URL`` and the planner.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1]).resolve()
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(FRAMEWORK_ROOT))

from dagents.common.infrastructure.compose_apply import parse_env_file  # noqa: E402

from app.api.app_header import APP_HEADER, APP_HEADER_VALUE  # noqa: E402
from app.core.config import lma_token_variable, lma_url_variable  # noqa: E402
from app.domain.dataset import JOINING_SITES, SITES  # noqa: E402
from tests.support import dagentsc_available  # noqa: E402
from tests.test_network_mode import _AgentProcesses, http_call, pilot_rounds  # noqa: E402

SCRIPT = APP_ROOT / "scripts" / "run_local_demo.sh"
FRONTEND_SCRIPT = APP_ROOT / "scripts" / "run_frontend_demo.sh"
JOINING = JOINING_SITES[0]
SITE_IDS = [site.site_id for site in SITES]
#: Seconds the script may take to start every stand-in.
SCRIPT_SECONDS = 120

#: Stands in for Python. ``-m uvicorn <app> --host H --port P`` records the
#: process and serves HTTP on H:P, answering every GET with 200; anything else
#: runs the real Python. With ``STAND_IN_ORPHAN`` set, the API's stand-in
#: starts a process in a session of its own and writes its pid file in the
#: deploy folder, as the API does for a new LMA, then writes the pid to
#: ``STAND_IN_ORPHAN``.
STAND_IN = r'''#!{python}
import json, os, signal, subprocess, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REAL = {python!r}
args = sys.argv[1:]
if args[:2] != ["-m", "uvicorn"]:
    os.execv(REAL, [REAL, *args])
app = args[2]
host = args[args.index("--host") + 1]
port = int(args[args.index("--port") + 1])
entry = {{
    "app": app, "host": host, "port": port, "cwd": os.getcwd(), "args": args, "env": dict(os.environ),
    "pid": os.getpid(),
}}
with open(os.environ["STAND_IN_RECORD"], "a", encoding="utf-8") as record:
    record.write(json.dumps(entry) + "\n")
orphan_file = os.environ.get("STAND_IN_ORPHAN")
deploy_dir = os.environ.get("HEALTHCARE_DEMO_DEPLOY_DIR")
if app == "app.main:app" and orphan_file and deploy_dir:
    orphan = subprocess.Popen([REAL, "-c", "import time; time.sleep(120)"], start_new_session=True)
    Path(deploy_dir, "lma-pl-290.pid").write_text(f"{{orphan.pid}}\n", encoding="utf-8")
    Path(orphan_file).write_text(str(orphan.pid), encoding="utf-8")


class Answer(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({{"status": "ok", "ready": True}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def stop(*_):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
ThreadingHTTPServer((host, port), Answer).serve_forever()
'''

#: Stands in for npm: ``install`` does nothing; ``run dev -- ...`` records its
#: arguments and serves HTTP on ``--port``, on ``--host`` or else localhost.
NPM_STAND_IN = r'''#!{python}
import json, os, signal, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

args = sys.argv[1:]
if args[:1] == ["install"]:
    raise SystemExit(0)
with open(os.environ["STAND_IN_RECORD"], "a", encoding="utf-8") as record:
    record.write(json.dumps({{"app": "npm", "args": args, "cwd": os.getcwd(), "env": dict(os.environ)}}) + "\n")
host = args[args.index("--host") + 1] if "--host" in args else "localhost"
port = int(args[args.index("--port") + 1])


class Answer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def stop(*_):
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stop)
ThreadingHTTPServer((host, port), Answer).serve_forever()
'''


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def site_key(site_id: str) -> str:
    return site_id.upper().replace("-", "_")


def site_map(text: str) -> dict[str, str]:
    """``SITE=VALUE,...`` as a dictionary."""
    return dict(item.split("=", 1) for item in text.split(",") if item)


def process_gone(pid: int) -> bool:
    """Whether ``pid`` has exited. A zombie that no parent reaped counts as exited."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state == "Z"


@unittest.skipUnless(shutil.which("bash") and shutil.which("curl"), "bash and curl are needed to run the script")
class NetworkScriptDeployTests(unittest.TestCase):
    """``run_local_demo.sh --agents network --deploy``, with a stand-in for each process."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="network-script-deploy-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.record = self.folder / "record.jsonl"
        self.python = self.folder / "python"
        self.python.write_text(STAND_IN.format(python=sys.executable), encoding="utf-8")
        self.python.chmod(0o755)
        bin_dir = self.folder / "bin"
        bin_dir.mkdir()
        npm = bin_dir / "npm"
        npm.write_text(NPM_STAND_IN.format(python=sys.executable), encoding="utf-8")
        npm.chmod(0o755)
        self.bin_dir = bin_dir
        # The script checks that the planner exists; the stand-ins never run it.
        self.planner = self.folder / "dagentsc"
        self.planner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.planner.chmod(0o755)
        self.ports = {
            "HEALTHCARE_DEMO_API_PORT": free_port(),
            "HEALTHCARE_DEMO_UI_PORT": free_port(),
            "HEALTHCARE_DEMO_GMA_PORT": free_port(),
            "HEALTHCARE_DEMO_CORE_SERVICE_PORT": free_port(),
            f"HEALTHCARE_DEMO_LMA_{site_key(JOINING.site_id)}_PORT": free_port(),
        }
        for site in SITES:
            self.ports[f"HEALTHCARE_DEMO_LMA_{site_key(site.site_id)}_PORT"] = free_port()
        self.joining_port = self.ports[f"HEALTHCARE_DEMO_LMA_{site_key(JOINING.site_id)}_PORT"]

    def environ(self, **changes: str) -> dict[str, str]:
        env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG", "TMPDIR") if name in os.environ}
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        env.update(
            DAGENTS_HOME=str(FRAMEWORK_ROOT),
            DAGENTSC_BIN=str(self.planner),
            PYTHON=str(self.python),
            STAND_IN_RECORD=str(self.record),
            HEALTHCARE_DEMO_DATABASE_URL="postgresql://demo@127.0.0.1:1/demo",
            NO_PROXY="127.0.0.1,localhost",
            no_proxy="127.0.0.1,localhost",
        )
        env.update({name: str(port) for name, port in self.ports.items()})
        env.update(changes)
        return env

    def start(self, *options: str, script: Path = SCRIPT, environ: dict[str, str] | None = None) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            ["bash", str(script), *options],
            cwd=APP_ROOT,
            env=environ or self.environ(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self.stop, process)
        return process

    def stop(self, process: subprocess.Popen[str]) -> str:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        if process.stdout is None or process.stdout.closed:
            return ""
        with process.stdout:
            return process.stdout.read()

    def entries(self) -> dict[str, dict[str, Any]]:
        if not self.record.exists():
            return {}
        found: dict[str, dict[str, Any]] = {}
        for line in self.record.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            app = entry["app"]
            name = {
                "dagents.gma.main:app": "gma",
                "app.main:app": "api" if Path(entry["cwd"]).name != "core-service" else "core-service",
                "npm": "ui",
            }.get(app)
            if app == "dagents.lma.main:app":
                name = f"lma-{entry['env']['LMA_SITE_ID']}"
            found[name or app] = entry
        return found

    def wait_for(self, process: subprocess.Popen[str], name: str) -> dict[str, dict[str, Any]]:
        deadline = time.monotonic() + SCRIPT_SECONDS
        while time.monotonic() < deadline:
            entries = self.entries()
            if name in entries:
                return entries
            if process.poll() is not None:
                self.fail(f"the script exited with {process.returncode} before it started {name}:\n{self.stop(process)}")
            time.sleep(0.2)
        self.fail(f"the script did not start {name}:\n{self.stop(process)}")

    def tokens(self, entries: dict[str, dict[str, Any]]) -> list[str]:
        """Every token a process was given."""
        values: list[str] = []
        for entry in entries.values():
            for name, value in entry["env"].items():
                if name.endswith("_TOKEN"):
                    values.append(value)
                elif name in ("GMA_LMA_TOKENS", "GMA_SITE_TOKENS"):
                    values.extend(site_map(value).values())
        self.assertTrue(values)
        return values

    def assert_no_token_printed(self, output: str, entries: dict[str, dict[str, Any]]) -> None:
        for token in self.tokens(entries):
            self.assertNotIn(token, output, "the script printed a token")

    # --- With --deploy -----------------------------------------------------------------

    def test_deploy_starts_core_service_and_gives_each_process_its_settings(self) -> None:
        process = self.start("--agents", "network", "--deploy")
        entries = self.wait_for(process, "api")
        self.assertEqual(
            {"core-service", "gma", *(f"lma-{site_id}" for site_id in SITE_IDS), "api"}, set(entries)
        )
        joining_url = f"http://127.0.0.1:{self.joining_port}"
        gma_url = f"http://127.0.0.1:{self.ports['HEALTHCARE_DEMO_GMA_PORT']}"

        # core-service runs from the Dagents checkout, on 127.0.0.1, with no token.
        core = entries["core-service"]
        self.assertEqual(str(FRAMEWORK_ROOT / "services" / "core-service"), core["cwd"])
        self.assertEqual(
            os.pathsep.join([str(FRAMEWORK_ROOT), str(FRAMEWORK_ROOT / "services" / "core-service")]),
            core["env"]["PYTHONPATH"],
        )
        self.assertEqual(("127.0.0.1", self.ports["HEALTHCARE_DEMO_CORE_SERVICE_PORT"]), (core["host"], core["port"]))
        self.assertEqual(str(self.planner), core["env"]["DAGENTSC_BIN"])
        for token in self.tokens({name: entry for name, entry in entries.items() if name != "core-service"}):
            self.assertNotIn(token, json.dumps(core["env"]), "core-service was given a token")

        # The GMA accepts the joining hospital's one address, and holds its tokens.
        gma = entries["gma"]["env"]
        self.assertEqual(f"{JOINING.site_id}={joining_url}", gma["GMA_JOINABLE_SITE_ENDPOINTS"])
        self.assertEqual(set(SITE_IDS), set(site_map(gma["GMA_SITE_ENDPOINTS"])))
        self.assertEqual({*SITE_IDS, JOINING.site_id}, set(site_map(gma["GMA_LMA_TOKENS"])))
        self.assertEqual({*SITE_IDS, JOINING.site_id}, set(site_map(gma["GMA_SITE_TOKENS"])))
        self.assertEqual(str(len(SITES) + 1), gma["GMA_DISPATCH_CONCURRENCY"])

        # The API: the deployer on, on 127.0.0.1, with the new LMA's address and site token.
        api = entries["api"]
        env = api["env"]
        self.assertEqual("127.0.0.1", api["host"])
        self.assertEqual("127.0.0.1", env["HEALTHCARE_DEMO_API_HOST"])
        self.assertEqual("process", env["HEALTHCARE_DEMO_DEPLOYER"])
        self.assertEqual("network", env["HEALTHCARE_DEMO_AGENTS"])
        self.assertEqual(joining_url, env[lma_url_variable(JOINING.site_id)])
        self.assertEqual(
            f"http://127.0.0.1:{self.ports['HEALTHCARE_DEMO_CORE_SERVICE_PORT']}", env["HEALTHCARE_DEMO_CORE_SERVICE_URL"]
        )
        self.assertTrue(env[lma_token_variable(JOINING.site_id)])
        self.assertEqual(str(FRAMEWORK_ROOT), env["PYTHONPATH"].split(os.pathsep)[0])

        # The new LMA's env file: only its owner can read it, in a folder only its owner can enter.
        folder = Path(env["HEALTHCARE_DEMO_DEPLOY_ENV_DIR"])
        self.assertEqual(env["HEALTHCARE_DEMO_DEPLOY_DIR"], str(folder))
        self.assertEqual(0o700, stat.S_IMODE(folder.stat().st_mode))
        path = folder / f"lma-{JOINING.site_id}.env"
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        values = parse_env_file(path.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "LMA_DATABASE_URL": "postgresql://demo@127.0.0.1:1/demo",
                "LMA_GMA_ENDPOINT": gma_url,
                "LMA_GMA_TOKEN": site_map(gma["GMA_SITE_TOKENS"])[JOINING.site_id],
                "LMA_SITE_TOKEN": env[lma_token_variable(JOINING.site_id)],
                "LMA_COORDINATOR_TOKEN": site_map(gma["GMA_LMA_TOKENS"])[JOINING.site_id],
            },
            values,
        )
        # Each hospital's tokens are its own.
        joining_tokens = {values["LMA_GMA_TOKEN"], values["LMA_SITE_TOKEN"], values["LMA_COORDINATOR_TOKEN"]}
        self.assertEqual(3, len(joining_tokens))
        for site_id in SITE_IDS:
            self.assertNotIn(entries[f"lma-{site_id}"]["env"]["LMA_SITE_TOKEN"], joining_tokens)

        output = self.stop(process)
        self.assert_no_token_printed(output, entries)
        self.assertFalse(folder.exists(), "the script left the env file behind")

    def test_deploy_implies_network_mode_and_refuses_the_agents_in_process(self) -> None:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--agents", "in-process", "--deploy"],
            cwd=APP_ROOT, env=self.environ(), capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("--deploy needs --agents network", result.stderr)
        process = self.start("--deploy")
        entries = self.wait_for(process, "api")
        self.assertEqual("network", entries["api"]["env"]["HEALTHCARE_DEMO_AGENTS"])
        self.assertEqual("process", entries["api"]["env"]["HEALTHCARE_DEMO_DEPLOYER"])

    def test_the_script_starts_nothing_when_the_joining_port_answers(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", self.joining_port))
            holder.listen()
            result = subprocess.run(
                ["bash", str(SCRIPT), "--agents", "network", "--deploy"],
                cwd=APP_ROOT, env=self.environ(), capture_output=True, text=True, timeout=60,
            )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"Port {self.joining_port} already answers", result.stderr)
        self.assertIn(f"HEALTHCARE_DEMO_LMA_{site_key(JOINING.site_id)}_PORT", result.stderr)
        self.assertFalse(self.record.exists(), "the script started a process before it checked the port")

    def test_a_process_the_api_left_running_is_stopped(self) -> None:
        # The API starts a new LMA in a session of its own. When the API dies
        # without stopping it (a crash, SIGKILL), the LMA is no longer in the
        # API's process tree, so the script finds it by its pid file.
        orphan_file = self.folder / "orphan.pid"
        process = self.start("--agents", "network", "--deploy", environ=self.environ(STAND_IN_ORPHAN=str(orphan_file)))
        entries = self.wait_for(process, "api")
        deadline = time.monotonic() + 30
        while not orphan_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        orphan = int(orphan_file.read_text())
        self.addCleanup(lambda: process_gone(orphan) or os.kill(orphan, signal.SIGKILL))
        self.assertFalse(process_gone(orphan))
        # The API dies: the script stops everything, including what the pid file names.
        os.kill(entries["api"]["pid"], signal.SIGKILL)
        process.wait(timeout=60)
        deadline = time.monotonic() + 30
        while not process_gone(orphan) and time.monotonic() < deadline:
            time.sleep(0.2)
        self.assertTrue(process_gone(orphan), "the script did not stop the process named in the pid file")

    # --- Without --deploy --------------------------------------------------------------

    def test_without_deploy_the_deployer_is_off_and_nothing_joins(self) -> None:
        for name, environ in (
            ("a plain run", self.environ()),
            # The environment cannot turn it on: only --deploy does, which also
            # binds the API and the UI to this machine.
            ("a run with the deployer set in the environment", self.environ(HEALTHCARE_DEMO_DEPLOYER="process")),
        ):
            with self.subTest(name):
                self.record.unlink(missing_ok=True)
                process = self.start("--agents", "network", environ=environ)
                entries = self.wait_for(process, "api")
                self.assertNotIn("core-service", entries)
                self.assertNotIn("GMA_JOINABLE_SITE_ENDPOINTS", entries["gma"]["env"])
                self.assertEqual(set(SITE_IDS), set(site_map(entries["gma"]["env"]["GMA_LMA_TOKENS"])))
                env = entries["api"]["env"]
                self.assertEqual("off", env["HEALTHCARE_DEMO_DEPLOYER"])
                for variable in (
                    lma_url_variable(JOINING.site_id),
                    lma_token_variable(JOINING.site_id),
                    "HEALTHCARE_DEMO_CORE_SERVICE_URL",
                    "HEALTHCARE_DEMO_DEPLOY_DIR",
                ):
                    self.assertNotIn(variable, env)
                self.stop(process)

    # --- The frontend script ----------------------------------------------------------

    def test_the_frontend_script_passes_deploy_on_and_binds_the_ui_to_this_machine(self) -> None:
        process = self.start("--deploy", script=FRONTEND_SCRIPT)
        entries = self.wait_for(process, "ui")
        self.assertEqual("process", entries["api"]["env"]["HEALTHCARE_DEMO_DEPLOYER"])
        self.assertEqual("127.0.0.1", entries["api"]["host"])
        self.assertIn("core-service", entries)
        args = entries["ui"]["args"]
        self.assertEqual("127.0.0.1", args[args.index("--host") + 1], args)
        self.assert_no_token_printed(self.stop(process), entries)


STEPS = ["plan", "start", "answer", "attach", "enrol", "join", "check"]
#: Seconds the new LMA has to stop after the API stops.
STOP_SECONDS = 20.0


def _steps(deployment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {step["step"]: step for step in deployment["steps"]}


def _rounds(report: dict[str, Any]) -> list[dict[str, Any]]:
    rounds = report["rounds"]
    return [rounds["analytics"], rounds["evaluation"], *rounds["training"], rounds["candidate_validation"]]


def _port_answers(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@unittest.skipUnless(
    dagentsc_available(),
    "dagentsc binary is not available, so core-service cannot render the Compose file and the GMA cannot plan a round",
)
class NetworkDeployTests(_AgentProcesses, unittest.TestCase):
    """core-service, the GMA, three LMAs and the API with the deployer on, started once for the class.

    The tests run in name order:

    1. A held port fails the ``start`` step.
    2. The hospital joins, and the next pilot includes it.
    3. A second request is refused.
    4. The new LMA is killed. The overview answers the app's 503, and the
       same request starts the LMA again.
    5. The GMA is killed and started again. The next pilot sends it the new
       LMA's address again.
    6. The GMA is killed and started again a second time. The overview sends
       it the address.
    7. No answer or log holds a token or an agent's address.
    8. The new LMA stops when the API stops.
    """

    restartable = frozenset({"gma"})

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.start_cluster(deploy=True)
        except BaseException:
            cls.tearDownClass()
            raise

    def logs(self) -> str:
        parts = [f"\n--- {process.name}\n{process.log()[-3000:]}" for process in (self.api, self.core, self.gma)]
        joined = self.deploy_dir / f"lma-{JOINING.site_id}.log"
        if joined.exists():
            parts.append(f"\n--- lma-{JOINING.site_id}\n{joined.read_text(errors='replace')[-3000:]}")
        return "".join(parts)

    def get(self, path: str) -> dict[str, Any]:
        status, body = self.public("GET", path)
        self.assertEqual(200, status, f"{path}: {str(body)[:500]}{self.logs()}")
        return body

    def deploy(self) -> tuple[int, Any]:
        return self.public("POST", "/api/v1/deployments", {"site_id": JOINING.site_id})

    def joined_addresses(self) -> list[dict[str, Any]]:
        """The addresses the GMA holds as ones that joined, from its own listing."""
        admin = {"Authorization": f"Bearer {self.tokens['gma admin']}"}
        url = f"{self.gma.url}/api/v1/federation/site-addresses"
        status, listing = http_call("GET", url, headers=admin, timeout=10)
        self.assertEqual(200, status, listing)
        return [row for row in listing["sites"] if row["source"] == "joined"]

    def assert_pilot_includes_every_hospital(self, report: dict[str, Any]) -> None:
        expected = {site_id: "completed" for site_id in [*SITE_IDS, JOINING.site_id]}
        for item in pilot_rounds(report):
            with self.subTest(round=item["round_id"]):
                participation = {result["site_id"]: result["participation"] for result in item["results"]}
                self.assertEqual(expected, participation, item["plan"])

    def test_1_a_held_port_fails_the_start_step_and_nothing_after_it_runs(self) -> None:
        view = self.get("/api/v1/deployments")
        self.assertTrue(view["available"], f"{view['reason']}{self.logs()}")
        self.assertEqual("process", view["applier"])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", self.joining_port))
            holder.listen()
            status, deployment = self.deploy()
        self.assertEqual(200, status, f"{str(deployment)[:1000]}{self.logs()}")
        self.assertEqual("failed", deployment["status"])
        steps = _steps(deployment)
        self.assertEqual("done", steps["plan"]["status"], steps["plan"]["detail"])
        self.assertEqual("failed", steps["start"]["status"])
        self.assertIn("port answers, but not for a service this app started", steps["start"]["detail"])
        for name in STEPS[2:]:
            with self.subTest(step=name):
                self.assertEqual("not_run", steps[name]["status"])
        view = self.get("/api/v1/deployments")
        self.assertEqual([False], [hospital["in_consortium"] for hospital in view["hospitals"]])
        self.assertEqual(SITE_IDS, [row["site_id"] for row in self.get("/api/v1/overview")["hospitals"]])

    def test_2_the_hospital_joins_and_the_next_pilot_includes_it(self) -> None:
        status, deployment = self.deploy()
        self.assertEqual(200, status, f"{str(deployment)[:1000]}{self.logs()}")
        steps = _steps(deployment)
        for name in STEPS:
            with self.subTest(step=name):
                self.assertEqual("done", steps[name]["status"], f"{steps[name]['detail']}{self.logs()}")
        self.assertEqual("joined", deployment["status"])
        self.assertEqual({"selected": True, "reason": None}, deployment["next_round"])
        calls = {(call["service"], call["site_id"], call["method"], call["path"]) for call in deployment["framework_calls"]}
        for call in (
            ("lma", JOINING.site_id, "GET", "/api/v1/health"),
            ("lma", JOINING.site_id, "GET", "/api/v1/site"),
            ("gma", None, "PUT", "/api/v1/federation/sites/{site_id}/address"),
            ("gma", None, "POST", "/api/v1/federation/sites:refresh"),
            ("gma", None, "POST", "/api/v1/federation/rounds:plan"),
        ):
            self.assertIn(call, calls)

        # The hospital's page comes from its own LMA, which reads its own schema.
        page = self.get(f"/api/v1/hospitals/{JOINING.site_id}")
        self.assertEqual(JOINING.site_id, page["site_id"])
        self.assertEqual(JOINING.expected_rows, page["counts"]["total"])
        self.assertEqual({("lma", JOINING.site_id)}, {(c["service"], c["site_id"]) for c in page["framework_calls"]})
        # The GMA holds the address as one that joined.
        admin = {"Authorization": f"Bearer {self.tokens['gma admin']}"}
        status, listing = http_call("GET", f"{self.gma.url}/api/v1/federation/site-addresses", headers=admin, timeout=10)
        self.assertEqual(200, status, listing)
        self.assertIn({"site_id": JOINING.site_id, "source": "joined"}, listing["sites"])
        self.assertEqual(
            [*SITE_IDS, JOINING.site_id], [row["site_id"] for row in self.get("/api/v1/overview")["hospitals"]]
        )
        status_report = self.get("/api/v1/framework/status")
        self.assertEqual("postgres", status_report["cohort_source"])
        self.assertIn(JOINING.site_id, [lma["site_id"] for lma in status_report["agents"]["lmas"]])
        view = self.get("/api/v1/deployments")
        self.assertEqual([(True, "running")], [(h["in_consortium"], h["service"]) for h in view["hospitals"]])

        status, report = self.public("POST", "/api/v1/pilot:run", {"round_prefix": "api"})
        self.assertEqual(200, status, f"{str(report)[:1000]}{self.logs()}")
        for item in _rounds(report):
            with self.subTest(round=item["round_id"]):
                results = {result["site_id"]: result for result in item["results"]}
                self.assertIn(JOINING.site_id, results, f"the round left {JOINING.site_id} out: {item['plan']}")
                self.assertEqual("completed", results[JOINING.site_id]["participation"], results[JOINING.site_id])
        audit = report["site_audits"][JOINING.site_id]
        self.assertTrue(audit["chain_intact"], audit)
        self.assertGreater(audit["record_count"], 0)
        # The new LMA logged its jobs and kept its audit chain as its own role, in its own schema.
        with self.db.admin() as conn:
            logged = conn.execute(
                "SELECT count(*) FROM coordinator.site_contributions WHERE site_role = %s", (JOINING.role,)
            ).fetchone()[0]
            audited = conn.execute(f"SELECT count(*) FROM {JOINING.schema}.guard_audit").fetchone()[0]
        self.assertGreater(logged, 0)
        self.assertGreaterEqual(audited, audit["record_count"])

    def test_3_a_second_request_is_refused_while_the_hospital_answers(self) -> None:
        status, body = self.deploy()
        self.assertEqual(409, status, body)
        self.assertEqual(f"{JOINING.display_name} is already in the consortium.", body["detail"])

    def test_4_a_killed_lma_is_reported_stopped_and_started_again_by_the_same_request(self) -> None:
        pid_file = self.deploy_dir / f"lma-{JOINING.site_id}.pid"
        killed = int(pid_file.read_text(encoding="utf-8"))
        # The API started it in a session of its own, so its group id is its pid.
        os.killpg(killed, signal.SIGKILL)
        deadline = time.monotonic() + STOP_SECONDS
        while _port_answers(self.joining_port) and time.monotonic() < deadline:
            time.sleep(0.25)
        self.assertFalse(_port_answers(self.joining_port), "the killed LMA still answers")

        # The overview needs every hospital's report, so it answers the app's 503, in words.
        from app.main import AGENT_UNAVAILABLE_MESSAGE

        status, body, headers = self.exchange("GET", "/api/v1/overview")
        self.assertEqual(503, status, f"{str(body)[:500]}{self.logs()}")
        self.assertEqual(APP_HEADER_VALUE, headers.get(APP_HEADER.lower()))
        self.assertEqual({"detail": AGENT_UNAVAILABLE_MESSAGE}, body)
        view = self.get("/api/v1/deployments")
        self.assertTrue(view["available"], view["reason"])
        self.assertEqual([(True, "stopped")], [(h["in_consortium"], h["service"]) for h in view["hospitals"]])

        status, deployment = self.deploy()
        self.assertEqual(200, status, f"{str(deployment)[:1000]}{self.logs()}")
        self.assertEqual("joined", deployment["status"], f"{deployment['steps']}{self.logs()}")
        steps = _steps(deployment)
        for name in STEPS:
            with self.subTest(step=name):
                expected = "skipped" if name == "join" else "done"
                self.assertEqual(expected, steps[name]["status"], f"{steps[name]['detail']}{self.logs()}")
        self.assertEqual(f"{JOINING.display_name} is already in the consortium.", steps["join"]["detail"])
        self.assertEqual({"selected": True, "reason": None}, deployment["next_round"])
        started = int(pid_file.read_text(encoding="utf-8"))
        self.assertNotEqual(killed, started)
        self.assertTrue(_port_answers(self.joining_port))

        view = self.get("/api/v1/deployments")
        self.assertEqual([(True, "running")], [(h["in_consortium"], h["service"]) for h in view["hospitals"]])
        hospitals = self.get("/api/v1/overview")["hospitals"]
        self.assertEqual([*SITE_IDS, JOINING.site_id], [row["site_id"] for row in hospitals])
        status, report = self.public("POST", "/api/v1/pilot:run", {"round_prefix": "api"})
        self.assertEqual(200, status, f"{str(report)[:1000]}{self.logs()}")
        self.assert_pilot_includes_every_hospital(report)
        audit = report["site_audits"][JOINING.site_id]
        self.assertTrue(audit["chain_intact"], audit)

    def restart_gma(self) -> None:
        """Kill the GMA and start it again on its port, and check that it forgot the new LMA's address."""
        self.assertIn(JOINING.site_id, [row["site_id"] for row in self.joined_addresses()])
        self.gma.kill()
        self.gma.restart()
        self.assertEqual([], self.joined_addresses(), "the GMA kept a joined address after a restart")

    def test_5_after_the_gma_restarts_the_next_pilot_sends_it_the_joined_address_again(self) -> None:
        """A GMA keeps the addresses it accepted in memory. Its model registry is in Postgres."""
        model = self.get("/api/v1/models")["model_in_use"]["version"]
        self.restart_gma()

        status, report = self.public("POST", "/api/v1/pilot:run", {"round_prefix": "api"})
        self.assertEqual(200, status, f"{str(report)[:1000]}{self.logs()}")
        calls = {(call["service"], call["method"], call["path"]) for call in report["framework_calls"]}
        self.assertIn(("gma", "PUT", "/api/v1/federation/sites/{site_id}/address"), calls)
        self.assertEqual([{"site_id": JOINING.site_id, "source": "joined"}], self.joined_addresses())
        self.assert_pilot_includes_every_hospital(report)
        self.assertEqual(model, report["model_in_use"]["version"])
        self.assertTrue(report["coordinator_audit"]["chain_intact"], report["coordinator_audit"])
        self.assertEqual(model, self.get("/api/v1/models")["model_in_use"]["version"])

    def test_6_after_the_gma_restarts_again_the_overview_sends_it_the_joined_address_again(self) -> None:
        """The overview, the first page a visitor opens, sends the address when the GMA's report lacks the hospital."""
        self.restart_gma()

        overview = self.get("/api/v1/overview")
        self.assertEqual([*SITE_IDS, JOINING.site_id], [row["site_id"] for row in overview["hospitals"]])
        calls = {(call["service"], call["method"], call["path"]) for call in overview["framework_calls"]}
        self.assertIn(("gma", "PUT", "/api/v1/federation/sites/{site_id}/address"), calls)
        self.assertEqual([{"site_id": JOINING.site_id, "source": "joined"}], self.joined_addresses())

    def test_7_no_answer_or_log_holds_a_token_or_an_agent_address(self) -> None:
        texts = [(f"response {index}", json.dumps(answer)) for index, answer in enumerate(self.responses)]
        texts += [(f"the log of {process.name}", process.log()) for process in (*self.processes(), self.core)]
        for path in sorted(self.deploy_dir.glob("*.log")):
            texts.append((f"the log {path.name}", path.read_text(errors="replace")))
        self.assertGreater(len(texts), 6)
        for where, text in texts:
            with self.subTest(where=where):
                self.assertNoAddressOrToken(text, where)
                self.assertNotIn(f"127.0.0.1:{self.joining_port}", text, f"{where} names the new LMA's address")
                self.assertNotIn(f"127.0.0.1:{self.core.port}", text, f"{where} names core-service's address")

    def test_8_the_new_lma_stops_when_the_api_stops(self) -> None:
        self.assertTrue(_port_answers(self.joining_port), f"the new LMA does not answer{self.logs()}")
        self.api.stop()
        deadline = time.monotonic() + STOP_SECONDS
        while _port_answers(self.joining_port) and time.monotonic() < deadline:
            time.sleep(0.25)
        self.assertFalse(_port_answers(self.joining_port), "the new LMA still answers after the API stopped")


if __name__ == "__main__":
    unittest.main()
