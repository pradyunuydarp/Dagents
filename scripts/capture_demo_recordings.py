#!/usr/bin/env python3
"""Record real demo-backend responses for the published demos to replay.

The two demo frontends can be published as static sites, with no backend to
call. This script records what they replay: it starts the backends with the
OCaml planner, sends every request each UI can make, and writes the responses
to `<frontend>/public/recording.json`.

In the healthcare demo, every permit, narrowing and denial comes from the
planner. Every decision in the capture is one `dagentsc` returned. Two rules
follow:

- **No planner, no capture.** Without `dagentsc` the Ethical Guard denies
  every request. That is correct, but a capture of nothing but denials would
  show guard controls that change nothing, so the script stops instead.
- **Record every combination of the guard's controls.** Each combination is
  recorded at every hospital, including the cohort sizes either side of the
  minimum, where the decision changes.

The healthcare backend runs with no database, so each hospital reads the
committed IST extract into memory, and with its rate limits off. It runs the
Dagents agents (an LMA per hospital and the GMA) inside the demo server, as
the published demo does, and every successful answer whose body is a JSON
object lists the agent calls behind it in ``framework_calls``. The script
writes no healthcare recording whose answers do not show those calls
(``check_demo_recordings.agent_call_problems`` says which are required).
Charts are recorded as SVG text in both themes: a non-JSON response is stored
as ``{"content_type": ..., "text": ...}`` in the entry's ``response``, and every
entry names its ``content_type``. Each healthcare entry also has a ``phase``:
``before_pilot`` for what the page shows when it opens, and ``after_pilot``
for the pilot itself and the views it changes (the consortium charts, the
models, each hospital's dashboard and audit trail).

Usage::

    scripts/capture_demo_recordings.py --demo all
    scripts/capture_demo_recordings.py --demo healthcare --python .venv/bin/python
    scripts/capture_demo_recordings.py --demo healthcare --port 9850   # another port for the backend

Exits non-zero if the planner is missing, a backend does not come up, a
request the UI depends on fails, or the healthcare answers do not show the
agent calls.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
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
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DUNE_BUILT_PLANNER = REPO_ROOT / "bindings" / "ocaml" / "_build" / "default" / "bin" / "dagentsc.exe"

HEALTHCARE_RECORDING = REPO_ROOT / "apps/healthcare-demo/frontend/public/recording.json"
NL2SQL_RECORDING = REPO_ROOT / "services/nl2sql-demo/frontend/public/recording.json"

#: The guard's controls, and why each value is in the list.
GRANULARITIES = ("cell", "row", "column", "table", "model_update")
#: 5 is far below the classification's floor of 20; 19 and 20 are the pair either
#: side of it, where the same request flips from denied to permitted; 25 is the
#: UI's default; 60 is comfortably clear of the floor.
COHORT_SIZES = (5, 19, 20, 25, 60)

#: The query strings the healthcare UI uses. The replay matches a path
#: character for character, so the UI must send exactly these.
WORKLIST_LIMIT = 12
AUDIT_LIMIT = 50
ENTERED_LIMIT = 20
CHART_THEMES = ("light", "dark")

#: What the panel to add a hospital reads.
DEPLOYMENTS_PATH = "/api/v1/deployments"

#: Settings for the healthcare backend during a capture: the agents inside the
#: demo server, as on the published demo (network mode would also need a
#: database); no database, so the hospitals read the committed extract; and no
#: rate limits or hourly caps, because the capture sends 150 probes in a few
#: seconds. Adding a hospital is off, as on the published demo, so the recorded
#: panel lists the hospital that can join and offers no button.
HEALTHCARE_CAPTURE_ENV = {
    "HEALTHCARE_DEMO_AGENTS": "in-process",
    "HEALTHCARE_DEMO_DEPLOYER": "off",
    "HEALTHCARE_DEMO_DATABASE_URL": "",
    "HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL": "",
    "HEALTHCARE_DEMO_SITE_IT_002_DATABASE_URL": "",
    "HEALTHCARE_DEMO_SITE_CH_245_DATABASE_URL": "",
    "HEALTHCARE_DEMO_RATE_LIMIT_ENTRIES": "0",
    "HEALTHCARE_DEMO_RATE_LIMIT_PROBES": "0",
    "HEALTHCARE_DEMO_RATE_LIMIT_PILOTS": "0",
    "HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS": "0",
    "HEALTHCARE_DEMO_PROBES_PER_SITE_PER_HOUR": "0",
    "HEALTHCARE_DEMO_PILOTS_PER_HOUR": "0",
}


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
        env=dict(HEALTHCARE_CAPTURE_ENV),
    ),
)

#: The NL2SQL trace reaches the whole framework stack, so a capture with these
#: absent would record a trace full of "unavailable" and undersell the thing the
#: demo exists to show. `model-service` is optional because it needs torch, and
#: an environment without it should still produce a usable capture that says so.
NL2SQL_SERVICES = (
    Service(name="core-service", module="app.main:app", port=8040, python_path=(".", "services/core-service")),
    Service(name="pipeline-service", module="app.main:app", port=8030, python_path=(".", "services/pipeline-service")),
    Service(name="lma", module="dagents.lma.main:app", port=8010, python_path=(".",), health_path="/health"),
    Service(name="gma", module="dagents.gma.main:app", port=8020, python_path=(".",), health_path="/health"),
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
        "dagentsc was not found, so a capture would hold only denials.\n"
        "Without the planner the Ethical Guard denies every request, and the published\n"
        "demo would show guard controls that change nothing. Build it first:\n"
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
        raise SystemExit(f"httpx is required to capture recordings: {exc}") from exc

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
    """Collects request/response pairs in the shape the frontends replay.

    ``phase``, when set, is written into every entry recorded until it changes.
    """

    def __init__(self, client: Any, base_url: str) -> None:
        self._client = client
        self.base_url = base_url
        self.entries: list[dict[str, Any]] = []
        self.phase: str | None = None

    def get(self, path: str, *, required: bool = True) -> Any:
        """Record a GET, returning the decoded body (the text for a non-JSON body)."""
        response = self._client.get(f"{self.base_url}{path}")
        return self._record("GET", path, None, response, required)

    def post(self, path: str, body: dict[str, Any], *, required: bool = True) -> Any:
        """Record a POST, returning the decoded body."""
        response = self._client.post(f"{self.base_url}{path}", json=body)
        return self._record("POST", path, body, response, required)

    def _record(self, method: str, path: str, body: Any, response: Any, required: bool) -> Any:
        if required and response.status_code >= 400:
            raise SystemExit(
                f"{method} {path} returned {response.status_code}, so the capture would "
                f"publish a broken demo:\n{response.text[:800]}"
            )
        content_type = response.headers.get("content-type", "application/json").split(";")[0].strip()
        entry: dict[str, Any] = {"method": method, "path": path, "status": response.status_code}
        if self.phase is not None:
            entry["phase"] = self.phase
        entry["content_type"] = content_type
        if content_type == "application/json":
            entry["response"] = response.json()
            returned = entry["response"]
        else:
            # A chart, for example. Kept as text, so the replay can serve it
            # with its own content type.
            entry["response"] = {"content_type": content_type, "text": response.text}
            returned = response.text
        if body is not None:
            entry["request"] = body
        self.entries.append(entry)
        return returned


def capture_charts(recorder: Recorder, list_path: str) -> dict[str, Any]:
    """Record a chart list and every available chart in both themes."""
    listing = recorder.get(list_path)
    for chart in listing["charts"]:
        if not chart["available"]:
            continue
        for theme in CHART_THEMES:
            recorder.get(f"{chart['svg_path']}?theme={theme}")
    return listing


def hospital_view_paths(site_id: str) -> tuple[str, ...]:
    """Every JSON GET the hospital view and the data-entry page send for one hospital.

    The chart images are not listed: their paths come from the chart list.
    ``check_demo_recordings.py`` requires each of these paths, and a test
    compares them with the calls in the frontend source.
    """
    base = f"/api/v1/hospitals/{site_id}"
    return (
        base,
        f"{base}/worklist?limit={WORKLIST_LIMIT}",
        f"{base}/audit?limit={AUDIT_LIMIT}",
        f"{base}/encounters?origin=entered&limit={ENTERED_LIMIT}",
        f"{base}/entry-form",
        f"{base}/charts",
    )


def after_pilot_hospital_paths(site_id: str) -> tuple[str, ...]:
    """The hospital views a pilot changes, recorded again after it.

    The dashboard lists what the hospital sent in each round, and the audit log
    holds the pilot's Guard decisions.
    """
    base = f"/api/v1/hospitals/{site_id}"
    return (base, f"{base}/audit?limit={AUDIT_LIMIT}")


def capture_hospital(recorder: Recorder, site_id: str) -> None:
    """Every GET the hospital view and the data-entry page make for one hospital."""
    charts = f"/api/v1/hospitals/{site_id}/charts"
    for path in hospital_view_paths(site_id):
        if path == charts:
            capture_charts(recorder, path)
        else:
            recorder.get(path)


def capture_healthcare(recorder: Recorder) -> None:
    """Walk every request the healthcare UI can make.

    Data entry is not recorded: a recorded POST could not change the GETs
    after it, so the replay disables the form instead. Adding a hospital is
    not recorded either: it starts a process, which a replay cannot, so the
    replay shows the hospitals that can join and no button.
    """
    recorder.phase = "before_pilot"
    recorder.get("/api/v1/framework/status")
    overview = recorder.get("/api/v1/overview")
    recorder.get("/api/v1/hospitals")
    recorder.get("/api/v1/models")
    # The panel to add a hospital lists the hospitals that can join. With the
    # deployer off the answer says why and lists no attempt.
    recorder.get(DEPLOYMENTS_PATH)
    site_ids = [hospital["site_id"] for hospital in overview["hospitals"]]
    for site_id in site_ids:
        capture_hospital(recorder, site_id)
    capture_charts(recorder, "/api/v1/charts")

    probes = 0
    for site_id in site_ids:
        for verified in (True, False):
            for granularity in GRANULARITIES:
                for cohort in COHORT_SIZES:
                    answer = recorder.post(
                        "/api/v1/governance:probe",
                        {
                            "site_id": site_id,
                            "verified": verified,
                            "granularity": granularity,
                            "cohort_size": cohort,
                            "boundary": "before_read",
                        },
                    )
                    # The published file must not carry patient rows: the
                    # consortium page is the coordinator's view.
                    if isinstance(answer, dict) and "payload" in answer:
                        raise SystemExit(
                            "The guard probe returned patient records in 'payload'. A probe answer gives "
                            "only released_count and released_fields; fix the backend before recording."
                        )
                    probes += 1
    print(f"  recorded {probes} combinations of the guard's controls across {len(site_ids)} hospitals")

    recorder.phase = "after_pilot"
    pilot = recorder.post("/api/v1/pilot:run", {"round_prefix": "ui"})
    print(f"  recorded the pilot: {((pilot.get('release') or {}).get('gates') or {}).get('action', 'no decision')}")
    charts = capture_charts(recorder, "/api/v1/charts")
    recorder.get("/api/v1/models")
    for site_id in site_ids:
        for path in after_pilot_hospital_paths(site_id):
            recorder.get(path)
    svgs = sum(1 for entry in recorder.entries if entry["content_type"] == "image/svg+xml")
    print(f"  recorded {len(charts['charts'])} consortium charts after the pilot; {svgs} SVGs in all")


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


def require_agent_calls(entries: list[dict[str, Any]]) -> None:
    """Stop before writing a healthcare recording whose answers do not show the Dagents agent calls.

    The rules live in ``check_demo_recordings.agent_call_problems``, so the
    capture and the checker refuse the same recordings.

    Raises:
    - `SystemExit` naming every problem.
    """
    # Imported here: the checker imports this module for the paths the UI sends.
    from check_demo_recordings import agent_call_problems, site_ids_in

    document = {"entries": entries}
    problems = agent_call_problems(document, site_ids_in(document))
    if problems:
        raise SystemExit(
            "The healthcare capture does not show the Dagents agent calls behind its answers, so it was not "
            "written. Fix the backend before recording:\n" + "\n".join(f"  - {problem}" for problem in problems)
        )


def capture(demo: str, python: str, planner: str, *, port: int | None = None) -> None:
    """Start the stack one demo needs, capture it, and write the recording.

    Params:
    - `demo`: ``healthcare`` or ``nl2sql``.
    - `python`: interpreter to run uvicorn with.
    - `planner`: value for `DAGENTSC_BIN` in every child.
    - `port`: the port for the demo backend the capture calls; its usual port when `None`.
    """
    print(f"{demo}: starting backends")
    services = HEALTHCARE_SERVICES if demo == "healthcare" else NL2SQL_SERVICES
    # The demo backend, the one the recorder calls, is the last service started.
    if port is not None:
        services = (*services[:-1], replace(services[-1], port=port))
    backend_url = f"http://127.0.0.1:{services[-1].port}"
    with ExitStack() as stack:
        live = running_stack(services, python, planner, stack)
        recorder = Recorder(live.client, backend_url)
        if demo == "healthcare":
            capture_healthcare(recorder)
            require_agent_calls(recorder.entries)
            target = HEALTHCARE_RECORDING
        else:
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
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="port for the demo backend the capture calls (default: its usual port, "
        f"{HEALTHCARE_SERVICES[-1].port} for healthcare and {NL2SQL_SERVICES[-1].port} for NL2SQL)",
    )
    args = parser.parse_args(argv)
    # Each backend starts in its own directory, so a relative interpreter path
    # is made absolute here. `abspath`, not `resolve`: a virtual environment's
    # interpreter is a symlink, and following it would leave the environment.
    python = os.path.abspath(args.python) if os.sep in args.python else args.python

    planner = planner_path()
    print(f"planner: {planner}")
    for demo in (("healthcare", "nl2sql") if args.demo == "all" else (args.demo,)):
        capture(demo, python, planner, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
