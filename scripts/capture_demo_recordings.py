#!/usr/bin/env python3
"""Record real demo-backend responses so the published demos are not mocked.

The two demo frontends are published as static sites, where there is no backend
to call. The choice there is a dead page, a faked one, or a replay of a real
run. This script produces the replay: it starts the actual backends with the
actual OCaml planner, walks every request each UI can make, and writes what came
back to `<frontend>/public/recording.json`.

That distinction matters most for the healthcare demo. Its whole argument is
that a permit, a narrowing or a denial is computed by a typed planner rather
than improvised, so a published page showing invented verdicts would be
demonstrating the opposite of the framework's point. Every verdict in the
capture is one `dagentsc` really returned.

Two rules follow from that:

- **No planner, no capture.** Without `dagentsc` the Ethical Guard denies
  everything — correct behaviour, and a capture of nothing but denials would
  misrepresent the demo. The script refuses rather than recording that.
- **Capture the matrix, not one path.** The guard's three levers are the
  demo's argument, so every combination of them is recorded, including the pair
  either side of the cohort floor where the answer flips.

Usage::

    scripts/capture_demo_recordings.py --demo all
    scripts/capture_demo_recordings.py --demo healthcare --python .venv/bin/python

Exits non-zero if the planner is missing, a backend does not come up, or a
request the UI depends on fails.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass, field
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
DUNE_BUILT_PLANNER = REPO_ROOT / "bindings" / "ocaml" / "_build" / "default" / "bin" / "dagentsc.exe"

HEALTHCARE_RECORDING = REPO_ROOT / "apps/healthcare-demo/frontend/public/recording.json"
NL2SQL_RECORDING = REPO_ROOT / "services/nl2sql-demo/frontend/public/recording.json"

#: The guard levers, and why each value is in the list.
GRANULARITIES = ("cell", "row", "column", "table", "model_update")
#: 5 is far below the classification's floor of 20; 19 and 20 are the pair either
#: side of it, where the same request flips from denied to permitted; 25 is the
#: UI's default; 60 is comfortably clear of the floor.
COHORT_SIZES = (5, 19, 20, 25, 60)


# --------------------------------------------------------------------------- #
# Process management
# --------------------------------------------------------------------------- #


@dataclass
class Service:
    """One backend process the capture needs running.

    Params:
    - `name`: how the service is named in logs and errors.
    - `module`: the ASGI app, as `module:attribute`.
    - `port`: the port to bind.
    - `python_path`: repo-root-relative entries prepended to `PYTHONPATH`.
    - `cwd`: repo-root-relative working directory.
    - `required`: whether the capture fails if this service will not start.
    - `env`: extra environment variables.
    """

    name: str
    module: str
    port: int
    python_path: tuple[str, ...]
    cwd: str = "."
    required: bool = True
    env: dict[str, str] = field(default_factory=dict)
    health_path: str = "/api/v1/health"


HEALTHCARE_SERVICES = (
    Service(
        name="healthcare-demo-backend",
        module="app.main:app",
        port=8080,
        python_path=(".", "apps/healthcare-demo/backend"),
        cwd="apps/healthcare-demo/backend",
    ),
)

#: The NL2SQL trace reaches the whole framework stack, so a capture with these
#: absent would record a trace full of "unavailable" and undersell the thing the
#: demo exists to show. `model-service` is optional because it needs torch, and
#: an environment without it should still produce a usable capture that says so.
NL2SQL_SERVICES = (
    Service(name="core-service", module="app.main:app", port=8040, python_path=(".", "services/core-service")),
    Service(name="pipeline-service", module="app.main:app", port=8030, python_path=(".", "services/pipeline-service")),
    Service(name="lma", module="agents.lma.main:app", port=8010, python_path=(".",), health_path="/health"),
    Service(name="gma", module="agents.gma.main:app", port=8020, python_path=(".",), health_path="/health"),
    Service(
        name="model-service",
        module="app.main:app",
        port=8000,
        python_path=(".", "services/model-service"),
        required=False,
    ),
    Service(
        name="nl2sql-demo-backend",
        module="app.main:app",
        port=8070,
        python_path=(".", "services/nl2sql-demo/backend"),
    ),
)


def planner_path() -> str:
    """Locate `dagentsc`, or explain why the capture cannot run.

    Returns:
    - An absolute path, or the name found on `PATH`.

    Raises:
    - `SystemExit` when it cannot be found. Capturing without the planner would
      record the Guard denying every request, which is correct behaviour and a
      false picture of the demo.
    """
    configured = os.environ.get("DAGENTSC_BIN")
    if configured and (Path(configured).is_file() or shutil.which(configured)):
        return configured
    if DUNE_BUILT_PLANNER.is_file():
        return str(DUNE_BUILT_PLANNER)
    found = shutil.which("dagentsc")
    if found:
        return found
    raise SystemExit(
        "dagentsc was not found, so there is nothing honest to record.\n"
        "Without the planner the Ethical Guard denies every request, and a capture of\n"
        "nothing but denials would misrepresent the demo. Build it first:\n"
        "  cd bindings/ocaml && opam exec -- dune build ./bin/dagentsc.exe"
    )


def start_service(service: Service, python: str, planner: str, log_dir: Path) -> subprocess.Popen[bytes]:
    """Launch one uvicorn process with the environment that service needs."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(REPO_ROOT / entry) for entry in service.python_path)
    env["DAGENTSC_BIN"] = planner
    env["PYTHONUNBUFFERED"] = "1"
    env.update(service.env)
    log = (log_dir / f"{service.name}.log").open("wb")
    return subprocess.Popen(
        [
            python,
            "-m",
            "uvicorn",
            service.module,
            "--host",
            "127.0.0.1",
            "--port",
            str(service.port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT / service.cwd,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        # Its own process group, so the whole tree can be signalled on teardown.
        start_new_session=True,
    )


def wait_for(client: Any, url: str, timeout: float = 60.0) -> bool:
    """Poll a health endpoint until it answers or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if client.get(url, timeout=2.0).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def stop(process: subprocess.Popen[bytes]) -> None:
    """Signal a service's whole process group, then insist."""
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


@dataclass
class RunningStack:
    """The services that came up, and where their logs are."""

    client: Any
    log_dir: Path
    started: list[str]
    missing: list[str]


def running_stack(
    services: tuple[Service, ...], python: str, planner: str, stack: ExitStack
) -> RunningStack:
    """Start every service, waiting for each to answer before the next request.

    Params:
    - `services`: the services to launch.
    - `python`: interpreter to run uvicorn with.
    - `planner`: value for `DAGENTSC_BIN` in every child.
    - `stack`: an `ExitStack` that owns teardown.

    Returns:
    - A `RunningStack` naming what started and what did not.

    Raises:
    - `SystemExit` when a required service will not come up, with its log tail,
      because a capture against a half-started stack would record failures as
      though they were the demo's behaviour.
    """
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - listed in every demo's requirements
        raise SystemExit(f"httpx is required to capture recordings: {exc}")

    log_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="dagents-capture-")))
    client = stack.enter_context(httpx.Client(timeout=180.0))
    started: list[str] = []
    missing: list[str] = []

    for service in services:
        process = start_service(service, python, planner, log_dir)
        stack.callback(stop, process)
        url = f"http://127.0.0.1:{service.port}{service.health_path}"
        if wait_for(client, url):
            started.append(service.name)
            print(f"  up    {service.name} on :{service.port}")
            continue
        log = (log_dir / f"{service.name}.log").read_text(encoding="utf-8", errors="replace")
        if service.required:
            raise SystemExit(
                f"{service.name} did not come up at {url}.\n--- last log lines ---\n"
                + "\n".join(log.splitlines()[-25:])
            )
        missing.append(service.name)
        print(f"  down  {service.name} — optional, the capture will record it as unavailable")
    return RunningStack(client=client, log_dir=log_dir, started=started, missing=missing)


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


class Recorder:
    """Collects request/response pairs in the shape the frontends replay."""

    def __init__(self, client: Any, base_url: str) -> None:
        self._client = client
        self._base = base_url
        self.entries: list[dict[str, Any]] = []

    def get(self, path: str, *, required: bool = True) -> Any:
        """Record a GET, returning the decoded body."""
        response = self._client.get(f"{self._base}{path}")
        return self._record("GET", path, None, response, required)

    def post(self, path: str, body: dict[str, Any], *, required: bool = True) -> Any:
        """Record a POST, returning the decoded body."""
        response = self._client.post(f"{self._base}{path}", json=body)
        return self._record("POST", path, body, response, required)

    def _record(self, method: str, path: str, body: Any, response: Any, required: bool) -> Any:
        if required and response.status_code >= 400:
            raise SystemExit(
                f"{method} {path} returned {response.status_code}, so the capture would "
                f"publish a broken demo:\n{response.text[:800]}"
            )
        entry: dict[str, Any] = {
            "method": method,
            "path": path,
            "status": response.status_code,
            "response": response.json(),
        }
        if body is not None:
            entry["request"] = body
        self.entries.append(entry)
        return entry["response"]


def capture_healthcare(recorder: Recorder) -> None:
    """Walk every request the healthcare UI can make.

    The guard matrix is the substance here: three levers, every combination, so
    the published demo can answer any of them with a real planner decision
    instead of the one path a happy-path capture would cover.
    """
    overview = recorder.get("/api/v1/overview")
    for hospital in overview["hospitals"]:
        recorder.get(f"/api/v1/hospitals/{hospital['site_id']}/worklist?limit=12")

    # A fixed prefix keeps the capture reproducible enough to review in a diff.
    recorder.post("/api/v1/pilot:run", {"round_prefix": "ui"})

    probes = 0
    for verified in (True, False):
        for granularity in GRANULARITIES:
            for cohort in COHORT_SIZES:
                recorder.post(
                    "/api/v1/governance:probe",
                    {
                        "verified": verified,
                        "granularity": granularity,
                        "cohort_size": cohort,
                        "boundary": "before_read",
                    },
                )
                probes += 1
    print(f"  recorded {probes} guard-lever combinations")


def capture_nl2sql(recorder: Recorder) -> None:
    """Walk every request the NL2SQL UI can make, for each bundled sample."""
    samples = recorder.get("/api/v1/samples")
    models = recorder.get("/api/v1/models")
    recorder.get("/api/v1/dagents/status")
    if not samples or not models:
        raise SystemExit("The NL2SQL backend returned no samples or no models to capture.")

    for sample in samples:
        for model in models:
            recorder.post(
                "/api/v1/generate",
                {
                    "question": sample["question"],
                    "tables": sample["tables"],
                    "model_id": model["model_id"],
                    "use_dagents_services": True,
                },
            )
    print(f"  recorded {len(samples)} samples across {len(models)} model adapters")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def git_commit() -> str:
    """The commit the capture describes, so a reader can check it themselves."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def display_path(path: Path) -> str:
    """Repo-relative where possible, absolute otherwise, never an exception."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def write_recording(target: Path, recorder: Recorder, note: str) -> None:
    """Write the capture where the frontend's static build will fetch it."""
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "captured_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": git_commit(),
        "planner": note,
        "entries": recorder.entries,
    }
    target.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    size = target.stat().st_size / 1024
    print(f"  wrote {len(recorder.entries)} entries ({size:.0f} KB) to {display_path(target)}")


def capture(demo: str, python: str, planner: str) -> None:
    """Start the stack one demo needs, capture it, and write the recording."""
    print(f"{demo}: starting backends")
    services = HEALTHCARE_SERVICES if demo == "healthcare" else NL2SQL_SERVICES
    with ExitStack() as stack:
        live = running_stack(services, python, planner, stack)
        if demo == "healthcare":
            recorder = Recorder(live.client, "http://127.0.0.1:8080")
            capture_healthcare(recorder)
            target = HEALTHCARE_RECORDING
        else:
            recorder = Recorder(live.client, "http://127.0.0.1:8070")
            capture_nl2sql(recorder)
            target = NL2SQL_RECORDING
        note = f"dagentsc at {planner}"
        if live.missing:
            note += f"; not running during capture: {', '.join(live.missing)}"
        write_recording(target, recorder, note)


def main(argv: list[str] | None = None) -> int:
    """Capture one demo or both."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--demo",
        choices=("healthcare", "nl2sql", "all"),
        default="all",
        help="which demo to capture (default: all)",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter used to run the backends (default: this one)",
    )
    args = parser.parse_args(argv)

    planner = planner_path()
    print(f"planner: {planner}")
    for demo in (("healthcare", "nl2sql") if args.demo == "all" else (args.demo,)):
        capture(demo, args.python, planner)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
