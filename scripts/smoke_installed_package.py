"""Check an installed ``dagents`` package from outside the repository.

Install a built wheel with its ``lma`` and ``gma`` extras into a new virtual
environment, then run this script with that environment's Python, from a
folder outside the repository, with no ``PYTHONPATH`` and no ``DAGENTSC_BIN``:

    python -m venv /tmp/dagents-smoke
    /tmp/dagents-smoke/bin/pip install "dist/dagents-0.1.0a1-py3-none-manylinux_2_17_x86_64.whl[lma,gma]"
    cd /tmp && /tmp/dagents-smoke/bin/python -I /path/to/Dagents/scripts/smoke_installed_package.py \\
        --repo /path/to/Dagents --expect-planner bundled

``-I`` keeps the repository off ``sys.path``, so ``import dagents`` finds the
installed package. The repository is read only for the expected version and
for the getting-started example and the guide's recorded outputs.

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

With ``--expect-planner none`` (a wheel built from the sdist, without a
planner) steps 3, 4 and 6 check the opposite: no planner is found, the Guard
denies the request, and each agent logs a warning that it has no planner.
Step 5 is left out.

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
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

FAIL_CLOSED_MESSAGE = "The planner could not be reached, so the Guard denied the request."
FENCE = re.compile(r"^(`{3,})(\w*)\s*$")
OUTPUT_MARKER = re.compile(r"^<!--\s*output:\s*(\S+)\s*-->$")


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


def clean_environment() -> dict[str, str]:
    """The environment for child processes: no planner setting, no path tricks, no agent settings."""
    drop = re.compile(r"^(PYTHONPATH|PYTHONHOME|DAGENTSC_BIN|LMA_.*|GMA_.*)$")
    environ = {key: value for key, value in os.environ.items() if not drop.match(key)}
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


def check_example(repo: Path, work: Path) -> None:
    source = repo / "examples" / "getting-started"
    target = work / "getting-started"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    expected = guide_outputs(repo)
    scripts = sorted(name for name in expected if name.startswith("examples/getting-started/"))
    check(len(scripts) >= 5, f"the guide records output for only {len(scripts)} example scripts")
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


def check_agent(name: str, command: list[str], port: int, work: Path, expect: str) -> None:
    log_path = work / f"{name}.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command + ["--port", str(port)], cwd=work, env=clean_environment(), stdout=log, stderr=subprocess.STDOUT)
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
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


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
        help="LMA,GMA: the ports the two agents listen on (default: two free ports the system picks)",
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
    try:
        with tempfile.TemporaryDirectory(prefix="dagents-smoke-") as folder:
            work = Path(folder)
            check_installed(repo)
            if args.agents_from:
                check_other_agents(args.agents_from)
            check_planner(args.expect_planner)
            check_governance(args.expect_planner)
            if args.expect_planner == "bundled":
                check_example(repo, work)
            lma = Path(sys.executable).with_name("dagents-lma")
            check(lma.is_file(), f"the dagents-lma command is not installed next to {sys.executable}")
            check_agent("lma", [str(lma)], lma_port, work, args.expect_planner)
            check_agent("gma", [sys.executable, "-m", "dagents.gma"], gma_port, work, args.expect_planner)
    except SmokeFailure as failure:
        print(f"FAIL  {failure}", file=sys.stderr)
        return 1
    print("smoke: every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
