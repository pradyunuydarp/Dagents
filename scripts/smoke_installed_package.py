"""Check an installed ``dagents`` package from outside the repository.

Install a built wheel with its ``lma`` and ``gma`` extras into a new virtual
environment, then run this script with that environment's Python, from a
folder outside the repository, with no ``PYTHONPATH`` and no ``DAGENTSC_BIN``:

    python -m venv /tmp/dagents-smoke
    /tmp/dagents-smoke/bin/pip install "dist/dagents-0.1.0a1-py3-none-manylinux_2_17_x86_64.whl[lma,gma]"
    cd /tmp && /tmp/dagents-smoke/bin/python -I /path/to/Dagents/scripts/smoke_installed_package.py \\
        --repo /path/to/Dagents --expect-planner bundled

``-I`` keeps the repository off ``sys.path``, so ``import dagents`` finds the
installed package. The processes the script starts get no ``DAGENTSC_BIN``
and no folder on ``PATH`` that holds a ``dagentsc``, so the only planner they
can find is one the wheel bundles.

The repository is read only for the expected version, the getting-started
example, the guide's recorded outputs and commands, and the toy extension of
the framework tests (``dagents/tests/fixtures/toy_extension.py``, which the
wheel leaves out).

With ``--expect-planner bundled`` the script checks, in order:

1. ``dagents`` is imported from the environment, not the repository, and the
   ``dagents`` distribution installed only ``dagents/``, its ``.dist-info``
   folder and its commands (read from its ``RECORD``). The check reads that
   one distribution, so another package's top-level ``agents`` module, such
   as openai-agents', does not fail it.
2. The package version equals ``__version__`` in the repository, and the
   installed package reads no ``env/`` file from ``site-packages``.
3. The runner finds the planner bundled in the wheel, and ``python -m dagents``
   reports it.
4. The Ethical Guard gets real decisions from that planner: a trusted and an
   unknown requester get different protection for the same fields.
5. Steps 1 to 5 of the getting-started example, copied to a temporary folder,
   print exactly the outputs the guide records. Step 4 is a federated round on
   the in-process simulator, and step 5 runs it through the LMA and GMA APIs.
6. ``dagents-lma`` and ``python -m dagents.gma`` start, ``/health`` answers,
   and neither logs that it has no planner.
7. The commands stop with the exit status ``docs/guide/08-deploy.md`` states:
   ``DAGENTSC_TIMEOUT_SECONDS=0`` makes ``dagents-lma`` and ``dagents-gma``
   print one line that names the variable and exit with status 1, and
   ``GMA_ENGINE=bogus`` makes ``dagents-gma`` exit with status 1 and name
   ``GMA_ENGINE``.
8. The command in section 5 of ``docs/guide/00-install.md``, read from the
   guide, starts ``dagents-lma`` in the repository's root with the example's
   extension on ``PYTHONPATH``. First, the environment's Python, run with the
   guide's settings and a command's import path, must import ``dagents``
   from the environment and not from the repository. Then ``GET
   /api/v1/extensions`` lists the extension, and the LMA logs no planner
   warning. The script reads the LMA's log for that warning only with
   ``--expect-planner bundled``.
9. A federated study over HTTP, from the installed commands: ``dagents-gma``
   with ``GMA_ENGINE=http`` and one ``dagents-lma`` per site, each a process
   of its own that loads the toy extension from ``PYTHONPATH`` and requires
   tokens. Through the installed ``AgentNetwork.over_http`` the script
   registers a fixed rule (released) and a model's starting point, refreshes
   the sites, and runs an evaluation round, a training round whose updates
   go to the GMA, and a validation round. The GMA evaluates the release from
   the rounds and the candidate is approved. Then each LMA uses the released
   version, each site's job log holds its three jobs, every audit chain
   verifies, a call without a token answers 401, and no agent logs that it
   has no planner.

With ``--expect-planner none`` (a wheel built from the sdist, without a
planner) steps 3, 4, 6 and 9 check the opposite: no planner is found, the
Guard denies the request, each agent of steps 6 and 9 logs a warning that it
has no planner, and in the study a round dispatch answers ``503`` with the
code ``planner_unavailable`` and a site's Guard probe over HTTP is denied.
Step 7 runs as above. Step 8 checks the import source and the extensions as
above. It does not read the LMA's log, so it checks neither that the warning
appears nor that it is absent. Step 5 is left out.

With ``--no-extras`` the wheel was installed without the ``lma`` and ``gma``
extras, as ``pip install dagents`` installs it. Checks 1 to 4 run as above.
Check 5 runs only the example's steps 1 to 4, which the guide says need only
the package. Checks 6 to 9 are replaced by one: ``dagents-lma`` and
``dagents-gma`` must exit with status 2 and print the ``pip install`` command
for their extra.

With ``--agents-from openai-agents`` the script also checks that a top-level
``agents`` module is installed, that it comes from that distribution, and that
it imports in the same process as ``dagents``. The package was renamed from
``agents`` so that the two install together. CI's ``packaging`` job runs this
check in an environment that has both.

The exit status is 0 when every check passed.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

FAIL_CLOSED_MESSAGE = "The planner could not be reached, so the Guard denied the request."
FENCE = re.compile(r"^(`{3,})(\w*)\s*$")
OUTPUT_MARKER = re.compile(r"^<!--\s*output:\s*(\S+)\s*-->$")

#: The example scripts that need the lma and gma extras. The guide says steps 1
#: to 4 need only ``pip install dagents`` (docs/guide/00-install.md, section 5).
NEEDS_AGENT_EXTRAS = frozenset({"step5_agents_over_http.py"})

#: What the lma and gma extras install, besides pydantic, which the package always needs.
AGENT_EXTRA_MODULES = ("fastapi", "uvicorn", "httpx")

#: The guide page whose section 5 starts an LMA with the example's extension.
INSTALL_GUIDE = Path("docs") / "guide" / "00-install.md"

#: The extension the federated study loads. The wheel leaves out dagents/tests,
#: so the script copies the file from the repository. It imports only the
#: standard library and dagents.common (tests/test_packaging.py checks this).
TOY_EXTENSION = Path("dagents") / "tests" / "fixtures" / "toy_extension.py"

#: The sites of the federated study. Two meet the round's minimum of two participants.
ROUND_SITES = ("site-a", "site-b")
STUDY_ID = "smoke-study"
THRESHOLD_VERSION = "toy-threshold-v1"
INITIAL_VERSION = "toy-linear-init"

#: How often a start of the study's processes may fail because another
#: process took one of the chosen ports first.
START_ATTEMPTS = 3

#: Seconds an agent may take to answer /health. A cold runner can be slow,
#: and a flaky failure here would stop a release run before it publishes.
START_TIMEOUT_S = 90.0


class SmokeFailure(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def ok(message: str) -> None:
    print(f"ok    {message}", flush=True)


def repo_version(repo: Path) -> str:
    tree = ast.parse((repo / "dagents" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "__version__" for t in node.targets):
            return node.value.value  # type: ignore[attr-defined]
    raise SmokeFailure("the repository's dagents/__init__.py assigns no __version__")


#: The name the runner looks for on PATH when no other planner is found.
PLANNER_ON_PATH = "dagentsc"


def without_other_planners(path: str) -> str:
    """``path`` (a PATH value) without the folders that hold a ``dagentsc``.

    An agent that imported ``dagents`` from the repository has no bundled
    planner, but it still finds one on PATH and logs no warning. Without
    those folders, the only planner a child process can find is the one the
    installation bundles.
    """
    return os.pathsep.join(folder for folder in path.split(os.pathsep) if not (Path(folder) / PLANNER_ON_PATH).is_file())


def clean_environment() -> dict[str, str]:
    """The environment for child processes: no planner setting, no other planner on PATH, no path tricks, no agent settings."""
    drop = re.compile(r"^(PYTHONPATH|PYTHONHOME|DAGENTSC_BIN|LMA_.*|GMA_.*)$")
    environ = {key: value for key, value in os.environ.items() if not drop.match(key)}
    if "PATH" in environ:
        environ["PATH"] = without_other_planners(environ["PATH"])
    environ["NO_PROXY"] = environ["no_proxy"] = "127.0.0.1,localhost"
    return environ


def check_installed(repo: Path) -> None:
    import dagents

    location = Path(dagents.__file__).resolve()
    check(repo.resolve() not in location.parents, f"dagents was imported from the repository ({location}), not the installed wheel")
    ok(f"dagents {dagents.__version__} imported from {location.parent}")
    distribution = importlib.metadata.distribution("dagents")
    described = Path(distribution.locate_file("dagents/__init__.py")).resolve()
    check(described == location, f"the dagents distribution's metadata describes {described}, not the imported {location}")
    check_distribution(distribution)
    expected = repo_version(repo)
    check(dagents.__version__ == expected, f"installed version {dagents.__version__} differs from the repository's {expected}")
    ok(f"version {dagents.__version__} equals the repository's __version__")
    from dagents.common import env

    check(not env.reads_env_files(), f"the installed package would read env files from {env.REPO_ROOT}")
    ok(f"the installed package reads no env file from {env.REPO_ROOT}")


def canonical(name: str) -> str:
    """A distribution name as PyPI compares it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def unexpected_files(distribution: importlib.metadata.Distribution) -> list[str]:
    """The files a ``dagents`` distribution installed outside ``dagents/``, its ``.dist-info`` and its commands.

    Commands are the console scripts the distribution declares. pip writes
    them to the environment's ``bin`` folder, which ``RECORD`` lists as
    ``../../../bin/NAME``.

    pip also compiles each module it installs and lists the bytecode in
    ``RECORD``. With ``PYTHONPYCACHEPREFIX`` set, that bytecode sits outside
    site-packages. Python never imports a ``.pyc`` file in a ``__pycache__``
    folder or under that prefix as a module of its own, and its source is in
    ``RECORD`` too, so such files are not counted.
    """
    commands = {entry.name for entry in distribution.entry_points if entry.group == "console_scripts"}
    allowed = {"dagents", f"dagents-{distribution.version}.dist-info"}
    unexpected = []
    for path in distribution.files or []:
        if path.parts[0] in allowed:
            continue
        if path.parts[0] == ".." and path.parent.name == "bin" and path.name.removesuffix(".exe") in commands:
            continue
        if path.suffix == ".pyc" and (path.parent.name == "__pycache__" or path.parts[0] == ".."):
            continue
        unexpected.append(path.as_posix())
    return sorted(unexpected)


def check_distribution(distribution: importlib.metadata.Distribution) -> None:
    """The ``dagents`` distribution installed nothing besides its package, its metadata and its commands."""
    check(distribution.files is not None, "the dagents distribution has no RECORD, so its files cannot be checked")
    unexpected = unexpected_files(distribution)
    shown = ", ".join(unexpected[:5]) + (f" and {len(unexpected) - 5} more" if len(unexpected) > 5 else "")
    check(not unexpected, f"the dagents distribution installed files outside dagents/: {shown}")
    commands = sorted(entry.name for entry in distribution.entry_points if entry.group == "console_scripts")
    ok(f"the dagents distribution installed only dagents/, its .dist-info and the commands {', '.join(commands)}")


def check_other_agents(distribution_name: str) -> None:
    """A top-level ``agents`` module from ``distribution_name`` is installed and imports next to ``dagents``."""
    check(
        importlib.util.find_spec("agents") is not None,
        f"no top-level 'agents' module is installed, although {distribution_name} should provide it",
    )
    owners = importlib.metadata.packages_distributions().get("agents", [])
    check(
        [canonical(owner) for owner in owners] == [canonical(distribution_name)],
        f"the top-level 'agents' module comes from {', '.join(owners) or 'no distribution'}, not {distribution_name}",
    )
    import dagents

    agents = importlib.import_module("agents")
    location = Path(agents.__file__).resolve().parent
    check(location != Path(dagents.__file__).resolve().parent, "the 'agents' module and dagents share a folder")
    version = importlib.metadata.version(distribution_name)
    ok(f"the top-level 'agents' module from {distribution_name} {version} imports next to dagents ({location})")


def check_planner(expect: str) -> None:
    import dagents
    from dagents.common.infrastructure.dagents_runner import find_planner

    path, source = find_planner()
    check(source == expect, f"the runner found the planner by rule {source!r}, expected {expect!r} (path {path})")
    if expect == "bundled":
        package = Path(dagents.__file__).resolve().parent
        check(Path(path).resolve() == package / "_bin" / "dagentsc", f"the bundled planner is at {path}, not inside the package")
        ok(f"the runner uses the bundled planner {path}")
    else:
        ok("the runner finds no planner, as expected for a wheel without one")
    report = subprocess.run(
        [sys.executable, "-m", "dagents"], capture_output=True, text=True, env=clean_environment(), timeout=60
    )
    wanted_status = 0 if expect == "bundled" else 1
    check(report.returncode == wanted_status, f"python -m dagents exited {report.returncode}: {report.stdout}{report.stderr}")
    ok("python -m dagents: " + " / ".join(report.stdout.strip().splitlines()))


def check_governance(expect: str) -> None:
    from dagents.common.application.ethical_guard import DagentscRestrictionPlanner, EthicalGuard, InMemoryAuditLog
    from dagents.common.domain.governance import DataClassification, KyuAttribute, Requester, RestrictionRequest

    classification = DataClassification(
        classification_id="smoke-v1",
        field_sensitivity={"score": "high", "region": "low"},
        default_sensitivity="high",
        regulations=["GDPR"],
        minimum_cohort=3,
    )
    trusted = Requester(
        requester_id="smoke-coordinator",
        requester_kind="coordinator",
        affiliation="smoke",
        stated_purpose="smoke_test",
        compliance_history=0.9,
        attributes=[
            KyuAttribute(attribute_id="verified_identity", verified=True),
            KyuAttribute(attribute_id="signed_dua", verified=True),
        ],
    )
    unknown = Requester(requester_id="unknown-caller", requester_kind="service")
    records = [{"score": 14.0, "region": "north"}, {"score": 6.0, "region": "south"}, {"score": 21.0, "region": "north"}]

    def decide(requester: Requester):
        request = RestrictionRequest(
            request_id=f"smoke-{requester.requester_id}",
            boundary="before_read",
            requester=requester,
            classification=classification,
            requested_fields=["score", "region"],
            granularity="row",
            cohort_size=len(records),
            declared_purpose="smoke_test",
            approved_purposes=["smoke_test"],
        )
        guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
        return guard.enforce(request, records)

    first = decide(trusted)
    second = decide(unknown)
    if expect == "none":
        check(not first.permitted and first.message == FAIL_CLOSED_MESSAGE, f"without a planner the Guard must deny: {first.message}")
        ok(f"without a planner the Guard denies: {first.message}")
        return
    for decision in (first, second):
        check(decision.message != FAIL_CLOSED_MESSAGE, "the Guard could not reach the bundled planner")
    strategies = {
        name: {r.field: r.strategy for r in decision.plan.field_restrictions}
        for name, decision in (("trusted", first), ("unknown", second))
    }
    check(strategies["trusted"] != strategies["unknown"], f"the planner gave both requesters the same protection: {strategies}")
    for name, decision in (("trusted", first), ("unknown", second)):
        fields = ", ".join(f"{field} {strategy}" for field, strategy in sorted(strategies[name].items()))
        ok(f"Guard, {name} requester: {decision.plan.decision}, permitted {decision.permitted} ({fields})")


def guide_outputs(repo: Path) -> dict[str, list[str]]:
    """The output the guide records for each example script."""
    outputs: dict[str, list[str]] = {}
    for page in sorted((repo / "docs" / "guide").glob("*.md")):
        lines = page.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            marker = OUTPUT_MARKER.match(line.strip())
            if not marker or index + 1 >= len(lines):
                continue
            opening = FENCE.match(lines[index + 1])
            if not opening:
                continue
            body = []
            for inner in lines[index + 2 :]:
                if inner.rstrip() == opening.group(1):
                    break
                body.append(inner.rstrip())
            outputs[marker.group(1)] = body
    return outputs


def check_example(repo: Path, work: Path, *, skip: frozenset[str] = frozenset()) -> None:
    """Run each example script the guide records output for, except those named in ``skip``."""
    source = repo / "examples" / "getting-started"
    target = work / "getting-started"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    expected = guide_outputs(repo)
    recorded = sorted(name for name in expected if name.startswith("examples/getting-started/"))
    check(len(recorded) >= 5, f"the guide records output for only {len(recorded)} example scripts")
    missing = sorted(skip - {Path(script).name for script in recorded})
    check(not missing, f"the guide records no output for {', '.join(missing)}, which this check leaves out")
    scripts = [script for script in recorded if Path(script).name not in skip]
    printed: dict[str, list[str]] = {}
    for script in scripts:
        name = Path(script).name
        completed = subprocess.run(
            [sys.executable, name], cwd=target, capture_output=True, text=True, env=clean_environment(), timeout=300
        )
        check(completed.returncode == 0, f"{name} failed with exit {completed.returncode}:\n{completed.stderr[-2000:]}")
        actual = [line.rstrip() for line in completed.stdout.splitlines()]
        if actual != expected[script]:
            diff = "\n".join(f"  guide: {g!r}\n  run:   {a!r}" for g, a in zip(expected[script], actual) if g != a)
            raise SmokeFailure(f"{name} printed output that differs from the guide:\n{diff or '(line counts differ)'}")
        printed[name] = actual
        ok(f"{name} printed the guide's {len(actual)} lines")
    for line in printed.get("step4_federated_round.py", []):
        if line.startswith(("Round ", "  quorum met", "Candidate ", "Decision")):
            ok(f"  federated round: {line.strip()}")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: The start of the warning an agent logs when it has no planner (dagents_runner.warn_if_no_planner).
NO_PLANNER_WARNING = re.compile(r"\b(dagents-lma|GMA) has no planner: ")


def start_process(command: list[str], *, cwd: Path, env: dict[str, str], log_path: Path) -> subprocess.Popen:
    """Start ``command`` in a process group of its own, with its output in ``log_path``."""
    with log_path.open("w", encoding="utf-8") as log:
        return subprocess.Popen(
            command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=hasattr(os, "killpg")
        )


def stop_processes(processes: list[subprocess.Popen]) -> None:
    """Stop each process that still runs: SIGTERM to its process group, then SIGKILL after 20 seconds.

    A process this script has already reaped is not signalled, because its id
    may name another process by now.
    """

    def signal_group(process: subprocess.Popen, kill: bool) -> None:
        if process.poll() is not None:
            return
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
            elif kill:
                process.kill()
            else:
                process.terminate()
        except ProcessLookupError:
            pass

    for process in processes:
        signal_group(process, kill=False)
    for process in processes:
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            signal_group(process, kill=True)
            process.wait(timeout=20)


def tail(path: Path, size: int = 2000) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[-size:]


def check_agent(name: str, command: list[str], port: int, work: Path, expect: str) -> None:
    log_path = work / f"{name}.log"
    process = start_process(command + ["--port", str(port)], cwd=work, env=clean_environment(), log_path=log_path)
    try:
        deadline = time.monotonic() + 60
        body = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SmokeFailure(f"{name} stopped with exit {process.returncode}:\n{log_path.read_text()[-2000:]}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    status = response.status
                break
            except OSError:
                time.sleep(0.3)
        check(body is not None, f"{name} did not answer /health within 60 seconds:\n{log_path.read_text()[-2000:]}")
        check(status == 200, f"{name} /health answered {status}")
        shown = " ".join([Path(command[0]).name, *command[1:]])
        ok(f"{shown} --port {port}: GET /health 200 {json.dumps(body, sort_keys=True)[:160]}")
        warned = NO_PLANNER_WARNING.search(log_path.read_text(encoding="utf-8"))
        if expect == "none":
            check(warned is not None, f"{name} started without a planner and did not log a warning:\n{log_path.read_text()[-2000:]}")
            ok(f"{name} logged that it has no planner")
        else:
            check(warned is None, f"{name} logged that it has no planner although the wheel bundles one")
    finally:
        stop_processes([process])


# ---------------------------------------------------------------------------
# The commands' exit statuses
# ---------------------------------------------------------------------------


def run_command(command: list[str], settings: dict[str, str], work: Path) -> subprocess.CompletedProcess[str]:
    """Run an installed command that must stop by itself, with ``settings`` added to the clean environment."""
    try:
        return subprocess.run(
            command + ["--port", str(free_port())],
            cwd=work,
            env={**clean_environment(), **settings},
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as expired:
        raise SmokeFailure(f"{' '.join(command)} with {settings} kept running instead of stopping") from expired


def commands_folder() -> Path:
    """The folder of this environment's commands: pip writes them next to its Python."""
    return Path(sys.executable).parent


def check_command_statuses(work: Path, bin_folder: Path | None = None) -> None:
    """A wrong setting stops each command with status 1 and a message that names the variable."""
    folder = bin_folder or commands_folder()
    commands = {name: str(folder / name) for name in ("dagents-lma", "dagents-gma")}
    for name, command in commands.items():
        completed = run_command([command], {"DAGENTSC_TIMEOUT_SECONDS": "0"}, work)
        output = completed.stdout + completed.stderr
        check(
            completed.returncode == 1,
            f"{name} with DAGENTSC_TIMEOUT_SECONDS=0 exited {completed.returncode}, not 1:\n{output[-2000:]}",
        )
        check("DAGENTSC_TIMEOUT_SECONDS" in completed.stderr, f"{name} did not name DAGENTSC_TIMEOUT_SECONDS:\n{output[-2000:]}")
        lines = completed.stderr.strip().splitlines()
        check(
            len(lines) == 1 and "Traceback" not in output,
            f"{name} printed more than the one line that names the setting:\n{output[-2000:]}",
        )
        ok(f"{name} with DAGENTSC_TIMEOUT_SECONDS=0: exit 1, {lines[0]!r}")
    completed = run_command([commands["dagents-gma"]], {"GMA_ENGINE": "bogus"}, work)
    output = completed.stdout + completed.stderr
    check(completed.returncode == 1, f"dagents-gma with GMA_ENGINE=bogus exited {completed.returncode}, not 1:\n{output[-2000:]}")
    check("GMA_ENGINE" in completed.stderr, f"dagents-gma with GMA_ENGINE=bogus did not name GMA_ENGINE:\n{output[-2000:]}")
    named = next(line for line in reversed(completed.stderr.splitlines()) if "GMA_ENGINE" in line)
    ok(f"dagents-gma with GMA_ENGINE=bogus: exit 1, {named.strip()!r}")


def check_commands_need_extras(bin_folder: Path | None = None) -> None:
    """Without the lma and gma extras, each command says which extra to install and exits with status 2."""
    for module in AGENT_EXTRA_MODULES:
        check(
            importlib.util.find_spec(module) is None,
            f"{module} is installed, so this environment cannot show what the commands do without the extras",
        )
    for name, extra in (("dagents-lma", "lma"), ("dagents-gma", "gma")):
        command = (bin_folder or commands_folder()) / name
        check(command.is_file(), f"the {name} command is not installed in {command.parent}")
        completed = subprocess.run(
            [str(command), "--port", str(free_port())], env=clean_environment(), capture_output=True, text=True, timeout=120
        )
        wanted = f"pip install 'dagents[{extra}]'"
        check(
            completed.returncode == 2 and wanted in completed.stderr,
            f"{name} without the {extra} extra must exit with status 2 and print {wanted!r}; "
            f"it exited {completed.returncode} and printed:\n{(completed.stdout + completed.stderr)[-2000:]}",
        )
        ok(f"{name} without the {extra} extra: exit 2, {completed.stderr.strip()!r}")


# ---------------------------------------------------------------------------
# The guide's command for an LMA with the example's extension
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuideCommand:
    """A command from the guide: the variables set before it, the program and its arguments."""

    settings: dict[str, str]
    program: str
    arguments: list[str]


def bash_blocks(text: str) -> list[str]:
    """The bodies of the ``bash`` code blocks in a Markdown page, with continued lines joined."""
    blocks: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        opening = FENCE.match(lines[index])
        index += 1
        if not opening:
            continue
        body = []
        while index < len(lines) and lines[index].rstrip() != opening.group(1):
            body.append(lines[index])
            index += 1
        index += 1
        if opening.group(2) == "bash":
            blocks.append("\n".join(body).replace("\\\n", " "))
    return blocks


def guide_extension_command(guide_text: str) -> GuideCommand:
    """The one command in the install guide that starts ``dagents-lma`` with ``LMA_EXTENSIONS`` set."""
    found: list[GuideCommand] = []
    for block in bash_blocks(guide_text):
        for line in block.splitlines():
            words = shlex.split(line, comments=True)
            settings: dict[str, str] = {}
            while words and re.fullmatch(r"[A-Z_][A-Z0-9_]*=.*", words[0]):
                name, _, value = words.pop(0).partition("=")
                settings[name] = value
            if words and Path(words[0]).name == "dagents-lma" and "LMA_EXTENSIONS" in settings:
                found.append(GuideCommand(settings=settings, program=words[0], arguments=words[1:]))
    check(
        len(found) == 1,
        f"the install guide has {len(found)} commands that start dagents-lma with LMA_EXTENSIONS, not one",
    )
    return found[0]


def wait_until_healthy(name: str, process: subprocess.Popen, log_path: Path, url: str) -> dict[str, Any]:
    """Poll ``url``/health until it answers 200. Fails when the process stops first."""
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure(f"{name} stopped with exit {process.returncode}:\n{tail(log_path)}")
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as response:
                if response.status == 200:
                    return json.loads(response.read().decode("utf-8"))
        except OSError:
            pass
        time.sleep(0.3)
    raise SmokeFailure(f"{name} did not answer /health within {START_TIMEOUT_S:.0f} seconds:\n{tail(log_path)}")


#: Prints the file ``import dagents`` loads with a command's import path. A
#: command such as ``dagents-lma`` has its own folder first on the path, then
#: PYTHONPATH, then the environment's packages. ``-P`` keeps the current folder
#: off the path, as it is for a command, so the repository's root gets on the
#: path only through PYTHONPATH.
IMPORT_PROBE = "import sys; sys.path.insert(0, sys.argv[1]); import dagents; print(dagents.__file__)"


def command_imports_dagents_from(folder: Path, cwd: Path, environment: dict[str, str]) -> Path:
    """The ``dagents/__init__.py`` a command in ``folder``, started in ``cwd`` with ``environment``, imports."""
    probe = subprocess.run(
        [sys.executable, "-P", "-c", IMPORT_PROBE, str(folder)],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    check(probe.returncode == 0, f"with the guide's settings, Python could not import dagents:\n{probe.stderr[-2000:]}")
    return Path(probe.stdout.strip().splitlines()[-1]).resolve()


def check_guide_imports_installed_dagents(repo: Path, folder: Path, environment: dict[str, str]) -> None:
    """With the guide's settings, a command started in the repository's root imports this environment's ``dagents``."""
    import dagents

    installed = Path(dagents.__file__).resolve()
    found = command_imports_dagents_from(folder, repo, environment)
    check(
        repo.resolve() not in found.parents,
        f"with the guide's settings, dagents-lma in the repository's root imports dagents from the repository ({found}), "
        "not from the environment",
    )
    check(found == installed, f"with the guide's settings, dagents-lma imports dagents from {found}, not this environment's {installed}")
    ok(f"guide section 5 in the repository's root: with its settings dagents-lma imports dagents from {found.parent}, not from the repository")


def check_guide_extension_command(repo: Path, work: Path, expect: str) -> None:
    """Run the install guide's section 5 command in the repository's root and read the LMA's extensions."""
    command = guide_extension_command((repo / INSTALL_GUIDE).read_text(encoding="utf-8"))
    arguments = list(command.arguments)
    check(
        "--port" in arguments and arguments.index("--port") + 1 < len(arguments),
        f"the guide's command {command.arguments} sets no port",
    )
    folder = commands_folder()
    lma = folder / "dagents-lma"
    environment = {**clean_environment(), **command.settings}
    # The shop package lives in the repository; keep its bytecode out of the checkout.
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    check_guide_imports_installed_dagents(repo, folder, environment)
    for attempt in range(1, START_ATTEMPTS + 1):
        port = pick_ports(1)[0]
        arguments[arguments.index("--port") + 1] = str(port)
        log_path = work / f"guide-lma-{attempt}.log"
        process = start_process([str(lma), *arguments], cwd=repo, env=environment, log_path=log_path)
        url = f"http://127.0.0.1:{port}"
        try:
            wait_until_healthy("the guide's dagents-lma", process, log_path, url)
            break
        except BaseException as error:
            stop_processes([process])
            taken = "address already in use" in tail(log_path, 20000).lower()
            if isinstance(error, SmokeFailure) and taken and attempt < START_ATTEMPTS:
                continue
            raise
    try:
        with urllib.request.urlopen(f"{url}/api/v1/extensions", timeout=30) as response:
            summary = json.loads(response.read().decode("utf-8"))
        names = [entry.get("extension_id") for entry in summary.get("extensions", [])]
        check(
            "example-retail-returns" in names,
            f"GET /api/v1/extensions lists {names}, not example-retail-returns:\n{tail(log_path)}",
        )
        shown = " ".join(f"{name}={value}" for name, value in command.settings.items())
        ok(f"guide section 5, {shown} dagents-lma in the repository's root: GET /api/v1/extensions lists {', '.join(names)}")
        if expect == "bundled":
            # The environment has no other planner on PATH, so a warning here
            # would also mean the LMA imported the repository's dagents.
            check(
                NO_PLANNER_WARNING.search(tail(log_path, 20000)) is None,
                f"the guide's dagents-lma logged that it has no planner:\n{tail(log_path)}",
            )
            ok("  it found the bundled planner from the repository's root")
    finally:
        stop_processes([process])


# ---------------------------------------------------------------------------
# A federated study over HTTP, from the installed commands
# ---------------------------------------------------------------------------


def load_toy_extension(repo: Path, folder: Path) -> ModuleType:
    """Copy the toy extension into ``folder`` and import it from there, as the agent processes will."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / TOY_EXTENSION.name
    shutil.copy2(repo / TOY_EXTENSION, target)
    spec = importlib.util.spec_from_file_location(target.stem, target)
    check(spec is not None and spec.loader is not None, f"cannot import {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def pick_ports(count: int) -> list[int]:
    """``count`` different loopback ports that were free a moment ago.

    The agent commands bind their own port, so another process can take one
    before they start. :func:`start_study_agents` then starts them again.
    """
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(sock)
            sock.bind(("127.0.0.1", 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def site_map(values: dict[str, str]) -> str:
    return ",".join(f"{site}={value}" for site, value in values.items())


@dataclass
class StudyAgents:
    """The study's GMA and LMA processes, their tokens and the network that reaches them."""

    network: Any
    processes: dict[str, subprocess.Popen]
    logs: dict[str, Path]
    admin_token: str
    staff_tokens: dict[str, str]
    commands: dict[str, str] = field(default_factory=dict)

    def stop(self) -> None:
        if self.network is not None:
            self.network.close()
        stop_processes(list(self.processes.values()))


def study_settings(toy: ModuleType, gma_url: str, lma_urls: dict[str, str], tokens: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The environment variables of each agent, as docs/guide/00-install.md section 4 lists them."""
    extension = f"{toy.__name__}:ToyExtension"
    settings = {
        "gma": {
            "GMA_ENGINE": "http",
            "GMA_EXTENSIONS": extension,
            "GMA_SITE_ENDPOINTS": site_map(lma_urls),
            "GMA_ADMIN_TOKEN": tokens["admin"],
            "GMA_LMA_TOKENS": site_map(tokens["coordinator"]),
            "GMA_SITE_TOKENS": site_map(tokens["gma_site"]),
            "GMA_REQUIRE_TOKENS": "1",
        }
    }
    for site in ROUND_SITES:
        settings[site] = {
            "LMA_EXTENSIONS": extension,
            "LMA_SITE_ID": site,
            "LMA_CONDITION_ID": toy.TOY_CONDITION_ID,
            "LMA_STUDY_ID": STUDY_ID,
            "LMA_SITE_SOURCE": "toy_static",
            "LMA_CAPABILITIES": "profile,evaluate,train",
            "LMA_SITE_TOKEN": tokens["staff"][site],
            "LMA_COORDINATOR_TOKEN": tokens["coordinator"][site],
            "LMA_GMA_ENDPOINT": gma_url,
            "LMA_GMA_TOKEN": tokens["gma_site"][site],
            "LMA_REQUIRE_TOKENS": "1",
        }
    return settings


def start_study_agents(toy: ModuleType, extension_folder: Path, work: Path, bin_folder: Path | None = None) -> StudyAgents:
    """Start ``dagents-gma`` and one ``dagents-lma`` per site, each from its environment, and wait for /health.

    The ports are chosen before any process starts, because each agent needs
    the others' addresses. When a process stops because another process took
    its port, all of them stop and start again on new ports.
    """
    from dagents.common.infrastructure.agent_client import GMA_AGENT, AgentNetwork, lma_agent

    def token() -> str:
        return secrets.token_hex(16)

    tokens = {
        "admin": token(),
        "staff": {site: token() for site in ROUND_SITES},
        "coordinator": {site: token() for site in ROUND_SITES},
        "gma_site": {site: token() for site in ROUND_SITES},
    }
    folder = bin_folder or commands_folder()
    for attempt in range(1, START_ATTEMPTS + 1):
        ports = pick_ports(1 + len(ROUND_SITES))
        gma_url = f"http://127.0.0.1:{ports[0]}"
        lma_urls = {site: f"http://127.0.0.1:{port}" for site, port in zip(ROUND_SITES, ports[1:])}
        urls = {"gma": gma_url, **lma_urls}
        settings = study_settings(toy, gma_url, lma_urls, tokens)
        base = clean_environment()
        # Only the extension's folder: the commands must find dagents in the environment.
        base["PYTHONPATH"] = str(extension_folder)
        processes: dict[str, subprocess.Popen] = {}
        logs: dict[str, Path] = {}
        commands: dict[str, str] = {}
        try:
            for (name, port) in zip(["gma", *ROUND_SITES], ports):
                program = folder / ("dagents-gma" if name == "gma" else "dagents-lma")
                check(program.is_file(), f"the {program.name} command is not installed in {folder}")
                logs[name] = work / f"study-{name}-{attempt}.log"
                commands[name] = f"{program.name} --port {port}"
                processes[name] = start_process(
                    [str(program), "--port", str(port)], cwd=work, env={**base, **settings[name]}, log_path=logs[name]
                )
            for name in ["gma", *ROUND_SITES]:
                wait_until_healthy(name, processes[name], logs[name], urls[name])
            network = AgentNetwork.over_http(
                {GMA_AGENT: gma_url, **{lma_agent(site): url for site, url in lma_urls.items()}},
                timeout_s=60.0,
                dispatch_timeout_s=180.0,
            )
        except BaseException as error:
            stop_processes(list(processes.values()))
            taken = [name for name in processes if "address already in use" in tail(logs[name], 20000).lower()]
            if isinstance(error, SmokeFailure) and taken and attempt < START_ATTEMPTS:
                print(f"note  another process took the port of {', '.join(taken)}; starting the study's agents again", flush=True)
                continue
            raise
        return StudyAgents(
            network=network,
            processes=processes,
            logs=logs,
            admin_token=tokens["admin"],
            staff_tokens=tokens["staff"],
            commands=commands,
        )
    raise SmokeFailure("the study's agents did not start")  # pragma: no cover - the loop returns or raises


def toy_manifest(toy: ModuleType, round_id: str, phase: str, version: str, digest: str, kind: Any) -> Any:
    """A round manifest for the toy condition, inviting every site of the study."""
    from dagents.common.domain.federation import AggregationMethod, RoundManifest

    return RoundManifest(
        round_id=round_id,
        study_id=STUDY_ID,
        condition_id=toy.TOY_CONDITION_ID,
        phase=phase,
        model_version=version,
        model_artifact_digest=digest,
        training_code_digest=kind.code_digest(),
        feature_contract_version=toy.TOY_CONTRACT.version,
        privacy_profile="smoke-profile-v1",
        aggregation=AggregationMethod(kind="fedavg"),
        minimum_participants=len(ROUND_SITES),
        minimum_cohort_per_site=5,
        required_capabilities=[{"evaluation": "evaluate", "training": "train"}[phase]],
        invited_sites=list(ROUND_SITES),
    )


def release_committee() -> Any:
    """The person who asks for the release: a verified committee with the study's purpose."""
    from dagents.common.domain.governance import KyuAttribute, Requester

    return Requester(
        requester_id="smoke-release-committee",
        requester_kind="human",
        affiliation="smoke-consortium",
        stated_purpose="toy_research",
        compliance_history=0.95,
        attributes=[
            KyuAttribute(attribute_id="verified_committee_identity", verified=True),
            KyuAttribute(attribute_id="signed_study_protocol", verified=True),
            KyuAttribute(attribute_id="governance_training", verified=True),
        ],
    )


def site_probe(toy: ModuleType) -> Any:
    """A site's staff ask their LMA's Guard what the study's coordinator may read."""
    from dagents.common.domain.governance import KyuAttribute, Requester
    from dagents.common.domain.site import ProbeRequest

    coordinator = Requester(
        requester_id=STUDY_ID,
        requester_kind="coordinator",
        affiliation=STUDY_ID,
        stated_purpose=toy.TOY_CONDITION_ID,
        compliance_history=1.0,
        attributes=[
            KyuAttribute(attribute_id="round_manifest_digest", verified=True),
            KyuAttribute(attribute_id="training_code_digest", verified=True),
        ],
    )
    return ProbeRequest(
        boundary="before_read",
        granularity="row",
        requested_fields=["x1", "x2", "group", toy.TOY_LABEL_FIELD],
        record_limit=5,
        requester=coordinator,
        declared_purpose=toy.TOY_CONDITION_ID,
    )


def refuses_without_token(client: Any) -> bool:
    from dagents.common.infrastructure.agent_client import AgentError

    try:
        client.request("GET", "/api/v1/governance/audit:summary")
    except AgentError as error:
        return (error.status, error.code) == (401, "unauthorized")
    return False


def run_study(agents: StudyAgents, toy: ModuleType) -> None:
    """Register the models, run the three rounds, evaluate and approve the release, then read every site."""
    from dagents.common.domain.federation import ModelVersionRegistration, ReleaseEvaluationRequest

    network = agents.network
    gma = network.gma(agents.admin_token)
    linear, threshold = toy.ToyLinearKind(), toy.ToyThresholdKind()
    rule = gma.register_model_version(
        THRESHOLD_VERSION,
        ModelVersionRegistration(kind=threshold.kind, params={"threshold": 0.0}, requested_status="released"),
    )
    start = gma.register_model_version(INITIAL_VERSION, ModelVersionRegistration(kind=linear.kind, params=linear.initial_params()))
    check(rule.status == "released" and start.status == "candidate", f"registered {rule.status} and {start.status}")
    ok(f"registered {THRESHOLD_VERSION} (released) and {INITIAL_VERSION} (candidate) over HTTP")
    enrolled = sorted(registration.site_id for registration in gma.refresh_sites() if registration.enrolled)
    check(enrolled == sorted(ROUND_SITES), f"the GMA enrolled {enrolled}, not {sorted(ROUND_SITES)}")
    ok(f"the GMA read each LMA's registration: {', '.join(enrolled)} enrolled")

    def run_round(round_id: str, phase: str, version: str, digest: str, kind: Any, *, discard: bool = False) -> Any:
        try:
            record = gma.dispatch_round(toy_manifest(toy, round_id, phase, version, digest, kind))
            outcomes = {result.site_id: result.participation for result in record.results}
            check(
                outcomes == {site: "completed" for site in ROUND_SITES},
                f"round {round_id}: the sites returned {outcomes}; dispatch {[call.model_dump() for call in record.dispatch]}",
            )
            readiness = gma.readiness(round_id)
            check(
                readiness.aggregation_permitted and sorted(readiness.accepted_sites) == sorted(ROUND_SITES),
                f"round {round_id} may not be aggregated: {readiness.model_dump()}",
            )
            candidate = gma.aggregate(round_id)
        finally:
            if discard:
                gma.discard_round(round_id, "the round is finished")
        updates = sum(1 for result in record.results if result.update_digest)
        ok(f"round {round_id} ({phase}): {len(record.results)} sites completed, {updates} updates sent to the GMA")
        return record, candidate

    evaluation_id, training_id, validation_id = "smoke-evaluation", "smoke-training", "smoke-validation"
    run_round(evaluation_id, "evaluation", THRESHOLD_VERSION, rule.digest, threshold)
    training, candidate = run_round(training_id, "training", INITIAL_VERSION, start.digest, linear, discard=True)
    check(
        all(result.update_digest for result in training.results),
        f"a site sent no update in the training round: {[result.model_dump() for result in training.results]}",
    )
    version, digest = candidate.candidate_version, candidate.artifact_digest
    run_round(validation_id, "evaluation", version, digest, linear)

    release = gma.evaluate_release(
        training_id,
        ReleaseEvaluationRequest(
            candidate_version=version,
            condition_id=toy.TOY_CONDITION_ID,
            metrics_from_round=validation_id,
            baseline_from_round=evaluation_id,
            change_from_version=INITIAL_VERSION,
            rollback_version=THRESHOLD_VERSION,
            release_requester=release_committee(),
            declared_purpose="toy_research",
        ),
    )
    gates = ", ".join(f"{gate.gate_id} {gate.outcome}" for gate in release.decision.gate_results)
    check(
        release.decision.action == "release" and release.metrics_source == "rounds",
        f"the release evaluation decided {release.decision.action} from {release.metrics_source}: {release.model_dump()}",
    )
    ok(f"release evaluation of {version}: {release.decision.action} from the rounds' metrics ({gates})")
    approval = gma.approve(version)
    check(approval.released.version == version, f"the approval released {approval.released.version}, not {version}")
    ok(f"approved {version}; the version it replaces: {approval.previous or 'none'}")

    expected_rounds = {evaluation_id: "evaluation", training_id: "training", validation_id: "evaluation"}
    for site in ROUND_SITES:
        staff = network.lma(site, agents.staff_tokens[site])
        in_use = staff.model_in_use()
        check(
            (in_use.version, in_use.status, in_use.digest) == (version, "released", digest),
            f"{site} uses {in_use.version} ({in_use.status}), not the released {version}",
        )
        entries = staff.job_log(limit=20)
        logged = {entry.round_id: (entry.phase, entry.participation) for entry in entries}
        check(
            logged == {round_id: (phase, "completed") for round_id, phase in expected_rounds.items()},
            f"{site}'s job log holds {logged}",
        )
        ok(f"{site}'s LMA uses {in_use.version}, and its job log holds the {len(entries)} jobs")
    audit = gma.audit_summary()
    check(audit.chain_intact and audit.record_count > 0, f"the GMA's audit chain: {audit.model_dump()}")
    for entry in gma.site_summaries(include_audit=True):
        check(
            entry.reachable and entry.audit is not None and entry.audit.chain_intact and entry.audit.record_count > 0,
            f"{entry.site_id}'s audit chain, as the GMA reads it: {entry.model_dump()}",
        )
    ok(f"every audit chain verifies; the GMA's chain holds {audit.record_count} record(s)")
    clients = [network.gma()] + [network.lma(site) for site in ROUND_SITES]
    check(all(refuses_without_token(client) for client in clients), "an agent answered a call without a token")
    ok("each agent answers 401 to a call without a token")


def run_study_without_planner(agents: StudyAgents, toy: ModuleType) -> None:
    """Without a planner, a round cannot be planned and a site's Guard denies its probe."""
    from dagents.common.infrastructure.agent_client import AgentError

    gma = agents.network.gma(agents.admin_token)
    linear = toy.ToyLinearKind()
    digest = linear.parameter_digest(linear.initial_params())
    try:
        gma.dispatch_round(toy_manifest(toy, "smoke-no-planner", "training", INITIAL_VERSION, digest, linear))
    except AgentError as error:
        check(
            (error.status, error.code) == (503, "planner_unavailable"),
            f"without a planner a round dispatch answered {error.status} {error.code}, not 503 planner_unavailable",
        )
    else:
        raise SmokeFailure("without a planner the GMA dispatched a round")
    ok("without a planner a round dispatch answers 503 planner_unavailable")
    for site in ROUND_SITES:
        probe = agents.network.lma(site, agents.staff_tokens[site]).probe(site_probe(toy))
        check(
            not probe.permitted and probe.released_count == 0 and probe.message == FAIL_CLOSED_MESSAGE,
            f"without a planner {site}'s Guard answered the probe with {probe.model_dump()}",
        )
    ok(f"without a planner each site's Guard denies a probe over HTTP: {FAIL_CLOSED_MESSAGE}")


def check_study_over_http(repo: Path, work: Path, expect: str) -> None:
    """The federated study over HTTP between the installed commands (see the module's docstring, step 9)."""
    from dagents.common.infrastructure.agent_client import AgentError

    extension_folder = work / "ext"
    toy = load_toy_extension(repo, extension_folder)
    agents = start_study_agents(toy, extension_folder, work)
    try:
        ok(
            "started "
            + ", ".join(f"{name}: {command}" for name, command in agents.commands.items())
            + f", with GMA_ENGINE=http, tokens required and {toy.__name__} from PYTHONPATH"
        )
        if expect == "bundled":
            run_study(agents, toy)
        else:
            run_study_without_planner(agents, toy)
    except AgentError as error:
        logs = "\n".join(f"--- {name}\n{tail(path)}" for name, path in agents.logs.items())
        raise SmokeFailure(f"an agent call failed: {error}\n{logs}") from error
    finally:
        agents.stop()
    for name, log_path in agents.logs.items():
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if expect == "bundled":
            check("has no planner" not in text, f"{name} logged that it has no planner:\n{tail(log_path)}")
        else:
            check(NO_PLANNER_WARNING.search(text) is not None, f"{name} started without a planner and did not log a warning:\n{tail(log_path)}")
    ok("no agent of the study logged that it has no planner" if expect == "bundled" else "each agent of the study logged that it has no planner")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, required=True, help="the Dagents checkout the wheel was built from")
    parser.add_argument("--expect-planner", choices=["bundled", "none"], required=True)
    parser.add_argument(
        "--agents-from",
        metavar="DISTRIBUTION",
        help="a distribution that must provide a top-level 'agents' module here, such as openai-agents",
    )
    parser.add_argument(
        "--ports",
        help=(
            "LMA,GMA: the ports of the LMA and GMA whose /health step 6 checks (default: two free ports the "
            "system picks). The guide check and the study always choose free ports themselves."
        ),
    )
    parser.add_argument(
        "--no-extras",
        action="store_true",
        help="the wheel was installed without the lma and gma extras: check that the commands say which extra to install",
    )
    args = parser.parse_args(argv)
    if args.ports:
        lma_port, gma_port = (int(part) for part in args.ports.split(","))
    else:
        lma_port, gma_port = free_port(), free_port()
    repo = args.repo.resolve()
    if os.environ.get("DAGENTSC_BIN"):
        print("smoke: unset DAGENTSC_BIN; the smoke test checks the planner the package finds itself", file=sys.stderr)
        return 2
    # This process calls the agents on loopback too; a proxy setting must not catch those calls.
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    try:
        with tempfile.TemporaryDirectory(prefix="dagents-smoke-") as folder:
            work = Path(folder)
            check_installed(repo)
            if args.agents_from:
                check_other_agents(args.agents_from)
            check_planner(args.expect_planner)
            check_governance(args.expect_planner)
            if args.expect_planner == "bundled":
                check_example(repo, work, skip=NEEDS_AGENT_EXTRAS if args.no_extras else frozenset())
            if args.no_extras:
                check_commands_need_extras()
            else:
                lma = Path(sys.executable).with_name("dagents-lma")
                check(lma.is_file(), f"the dagents-lma command is not installed next to {sys.executable}")
                check_agent("lma", [str(lma)], lma_port, work, args.expect_planner)
                check_agent("gma", [sys.executable, "-m", "dagents.gma"], gma_port, work, args.expect_planner)
                check_command_statuses(work)
                check_guide_extension_command(repo, work, args.expect_planner)
                check_study_over_http(repo, work, args.expect_planner)
    except SmokeFailure as failure:
        print(f"FAIL  {failure}", file=sys.stderr)
        return 1
    print("smoke: every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
