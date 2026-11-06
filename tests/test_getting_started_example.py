"""The guide in docs/guide and the example it is built on stay in step.

The guide teaches by showing code from `examples/getting-started/` and the
output of its scripts. A copy in a document drifts as soon as the code
changes, so this file checks both directions:

- A code block marked `<!-- source: PATH -->` must appear, line for line, in
  that file. Every Python block in the guide must carry such a marker, so no
  Python in the guide is untested.
- An output block marked `<!-- output: PATH -->` must equal what that script
  prints when it runs against the real planner.
- Every module and script of the example appears in the guide at least once.
- Step 5 prints the guide's output again when its agents run as separate
  uvicorn processes, started with the settings the guide's commands use. A
  dry run of those commands, with stand-ins that record each process, checks
  that the guide passes each port and token to the right setting.

The scripts run against the real `dagentsc`, not a stub, because the Guard
fails closed: without the planner every request is denied and the output
would show nothing the guide describes. Those tests skip when the planner is
missing; the CI job that runs this file forbids that skip.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

REPO_ROOT = Path(__file__).resolve().parents[1]
GUIDE_DIR = REPO_ROOT / "docs" / "guide"
EXAMPLE_DIR = REPO_ROOT / "examples" / "getting-started"
DUNE_BUILT_BINARY = REPO_ROOT / "bindings" / "ocaml" / "_build" / "default" / "bin" / "dagentsc.exe"

#: A marker on the line directly above a fenced code block.
MARKER = re.compile(r"^<!--\s*(source|output):\s*(\S+)\s*-->$")
FENCE = re.compile(r"^(`{3,})(\w*)\s*$")

#: Identifiers that belong to the example's domain. None may appear in dagents/.
DOMAIN_WORDS = ("example-retail-returns", "retail-orders", "order_returns", "store-north", "store-south")


def example_constant(path: str, name: str) -> object:
    """The literal value the example's module ``path`` assigns to ``name``, read without importing it."""
    tree = ast.parse((EXAMPLE_DIR / path).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"examples/getting-started/{path} assigns no {name}")


STEP5 = "examples/getting-started/step5_agents_over_http.py"
#: The guide section that starts step 5's agents as separate processes.
SEPARATE_PROCESSES_PAGE = GUIDE_DIR / "06-federated-rounds.md"
SEPARATE_PROCESSES_HEADING = "### Run the agents as separate processes"
#: The example's stores and study, and the extension each agent process loads.
#: They come from the example's code, so the guide's commands must follow it.
STORES = tuple(example_constant("shop/store.py", "SITE_IDS"))  # type: ignore[call-overload]
STUDY_ID = str(example_constant("shop/coordinator.py", "STUDY_ID"))
EXTENSION_SPEC = "shop.extension:ReturnsExtension"
#: Variables that make step 5 reach agents at an address instead of building them.
STEP5_VARIABLE_PREFIX = "SHOP_"
#: The settings of the agents and of step 5.
SETTING_PREFIXES = ("LMA_", "GMA_", STEP5_VARIABLE_PREFIX)


class Block:
    """One fenced code block in a guide page."""

    def __init__(self, page: Path, line: int, language: str, lines: list[str], marker: tuple[str, str] | None):
        self.page = page
        self.line = line
        self.language = language
        self.lines = lines
        self.marker = marker

    def where(self) -> str:
        return f"{self.page.relative_to(REPO_ROOT)}:{self.line}"


def guide_blocks() -> list[Block]:
    """Every fenced code block in docs/guide, with the marker above it, if any."""
    blocks: list[Block] = []
    for page in sorted(GUIDE_DIR.glob("*.md")):
        lines = page.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            opening = FENCE.match(lines[index])
            if not opening:
                index += 1
                continue
            fence, language = opening.groups()
            marker = MARKER.match(lines[index - 1].strip()) if index > 0 else None
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index].rstrip() != fence:
                body.append(lines[index])
                index += 1
            blocks.append(
                Block(page, index - len(body), language, body, marker.groups() if marker else None)  # type: ignore[arg-type]
            )
            index += 1
    return blocks


def contains_lines(text: str, block: list[str]) -> bool:
    """Whether `block` appears in `text` as a run of whole, consecutive lines."""
    lines = text.splitlines()
    size = len(block)
    return size > 0 and any(lines[start : start + size] == block for start in range(len(lines) - size + 1))


def find_planner() -> str | None:
    """The planner the scripts should use: DAGENTSC_BIN, then PATH, then the dune build."""
    configured = os.environ.get("DAGENTSC_BIN")
    if configured and os.path.isfile(configured):
        return configured
    on_path = shutil.which("dagentsc")
    if on_path:
        return on_path
    if DUNE_BUILT_BINARY.is_file():
        return str(DUNE_BUILT_BINARY)
    return None


PLANNER = find_planner()


def run_script(script: str, planner: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run one example script from the repository root, as the guide does.

    ``SHOP_*`` variables from the caller's shell are left out, so step 5
    builds its agents in process unless ``extra_env`` points it at processes.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith(STEP5_VARIABLE_PREFIX)}
    env.update(PYTHONPATH=str(REPO_ROOT), DAGENTSC_BIN=planner)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def output_block(script: str) -> list[str]:
    """The lines the guide records as the output of ``script``."""
    for block in guide_blocks():
        if block.marker == ("output", script):
            return [line.rstrip() for line in block.lines]
    raise AssertionError(f"the guide records no output for {script}")


def separate_process_settings(gma_url: str, lma_urls: dict[str, str], tokens: dict[str, str]) -> dict[str, dict[str, str]]:
    """The settings of each agent process in the guide's "Run the agents as separate processes", and of step 5.

    ``tokens`` holds ``admin`` and, for each store, ``<store>/staff``,
    ``<store>/coordinator`` and ``<store>/upload``: the four kinds of token
    that ``shop/agents.py`` describes.
    """

    def by_store(values: dict[str, str]) -> str:
        return ",".join(f"{store}={values[store]}" for store in STORES)

    def store_tokens(kind: str) -> str:
        return by_store({store: tokens[f"{store}/{kind}"] for store in STORES})

    settings = {
        "gma": {
            "GMA_ENGINE": "http",
            "GMA_EXTENSIONS": EXTENSION_SPEC,
            "GMA_ALLOW_SOURCE_READS": "0",
            "GMA_REQUIRE_TOKENS": "1",
            "GMA_SITE_ENDPOINTS": by_store(lma_urls),
            "GMA_ADMIN_TOKEN": tokens["admin"],
            "GMA_LMA_TOKENS": store_tokens("coordinator"),
            "GMA_SITE_TOKENS": store_tokens("upload"),
        },
        "step5": {
            "SHOP_GMA_URL": gma_url,
            "SHOP_LMA_URLS": by_store(lma_urls),
            "SHOP_ADMIN_TOKEN": tokens["admin"],
            "SHOP_STORE_TOKENS": store_tokens("staff"),
        },
    }
    for store in STORES:
        settings[f"lma:{store}"] = {
            "LMA_SITE_ID": store,
            "LMA_CONDITION_ID": "order_returns",
            "LMA_STUDY_ID": STUDY_ID,
            "LMA_CAPABILITIES": "evaluate",
            "LMA_EXTENSIONS": EXTENSION_SPEC,
            "LMA_SITE_SOURCE": "shop_orders",
            "LMA_REQUIRE_TOKENS": "1",
            "LMA_SITE_TOKEN": tokens[f"{store}/staff"],
            "LMA_COORDINATOR_TOKEN": tokens[f"{store}/coordinator"],
            "LMA_GMA_ENDPOINT": gma_url,
            "LMA_GMA_TOKEN": tokens[f"{store}/upload"],
        }
    return settings


def separate_processes_commands() -> str:
    """The shell commands in the guide's "Run the agents as separate processes": its ``bash`` blocks, joined."""
    page_lines = SEPARATE_PROCESSES_PAGE.read_text(encoding="utf-8").splitlines()
    commands: list[str] = []
    for block in guide_blocks():
        if block.page != SEPARATE_PROCESSES_PAGE or block.language != "bash":
            continue
        headings = [line for line in page_lines[: block.line] if line.startswith(("## ", "### "))]
        if headings and headings[-1] == SEPARATE_PROCESSES_HEADING:
            commands.extend(block.lines)
    if not commands:
        raise AssertionError(f"{SEPARATE_PROCESSES_PAGE.name} has no bash block under {SEPARATE_PROCESSES_HEADING!r}")
    return "\n".join(commands)


#: The stand-in for ``.venv/bin/uvicorn``, ``.venv/bin/python`` and ``curl`` in a dry run.
_RECORDER = '''\
import json, os, sys, uuid

program, args = sys.argv[1], sys.argv[2:]
if program == "python" and args[:1] == ["-c"]:
    # token() in the guide: run its own code, which prints a new token.
    os.execv(sys.executable, [sys.executable, *args])
env = {
    key: value
    for key, value in os.environ.items()
    if key.startswith(("LMA_", "GMA_", "SHOP_")) or key in ("PYTHONPATH", "DAGENTSC_BIN")
}
path = os.path.join(os.environ["GUIDE_DRY_RUN_RECORDS"], uuid.uuid4().hex + ".json")
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"program": program, "args": args, "env": env}, handle)
'''


def dry_run_separate_processes(test: unittest.TestCase) -> list[dict]:
    """Run the guide's commands for separate processes with bash, and record each process they would start.

    The commands run in an empty folder. ``.venv/bin/uvicorn``,
    ``.venv/bin/python`` and ``curl`` there are stand-ins that write their
    arguments and their ``LMA_``, ``GMA_`` and ``SHOP_`` variables to a file
    and start nothing, so no port is used. ``.venv/bin/python -c`` runs its
    code, so the guide's ``token()`` makes real tokens. Each record holds
    ``program`` (``uvicorn``, ``python`` or ``curl``), ``args`` and ``env``.
    """
    folder = tempfile.TemporaryDirectory(prefix="dagents-guide-dry-run-")
    test.addCleanup(folder.cleanup)
    root = Path(folder.name).resolve()
    records = root / "records"
    records.mkdir()
    recorder = root / "recorder.py"
    recorder.write_text(_RECORDER, encoding="utf-8")
    stand_ins = {
        root / ".venv" / "bin" / "uvicorn": "uvicorn",
        root / ".venv" / "bin" / "python": "python",
        root / "bin" / "curl": "curl",
    }
    for path, program in stand_ins.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'#!/bin/sh\nexec "$GUIDE_DRY_RUN_PYTHON" "$GUIDE_DRY_RUN_RECORDER" {program} "$@"\n', encoding="utf-8")
        path.chmod(0o755)
    env = {
        "PATH": os.pathsep.join([str(root / "bin"), os.environ.get("PATH", os.defpath)]),
        "HOME": str(root),
        "GUIDE_DRY_RUN_PYTHON": sys.executable,
        "GUIDE_DRY_RUN_RECORDER": str(recorder),
        "GUIDE_DRY_RUN_RECORDS": str(records),
    }
    # ``wait``: the agents start in the background, and each stand-in must finish writing.
    script = f"set -eu\n{separate_processes_commands()}\nwait\n"
    result = subprocess.run(
        ["bash", "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    test.assertEqual(0, result.returncode, f"the guide's commands failed in a dry run:\n{result.stdout}\n{result.stderr}")
    # The guide exports DAGENTSC_BIN from $PWD, which is the dry run's folder here.
    loaded = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(records.glob("*.json"))]
    for record in loaded:
        planner = record["env"].get("DAGENTSC_BIN")
        if planner and Path(planner).is_relative_to(root):
            record["env"]["DAGENTSC_BIN"] = str(REPO_ROOT / Path(planner).relative_to(root))
    return loaded


def _stop(process: subprocess.Popen) -> None:
    """Stop a process started with ``start_new_session=True``, and every process in its group."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    # A planner call the agent started can outlive the agent; none may keep running.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def _healthy(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}/health", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def start_agent_processes(test: unittest.TestCase, planner: str, timeout_s: float = 90.0) -> dict[str, str]:
    """Start the GMA and one LMA per store with uvicorn, each from its settings, and return step 5's settings.

    Each process gets a socket bound here on a free loopback port (``--fd``),
    so no other process can take the port between choosing it and serving
    on it. Each runs in its own process group, and ``test`` stops every group
    when it ends, whether it passed or not.
    """
    folder = tempfile.TemporaryDirectory(prefix="dagents-guide-http-")
    test.addCleanup(folder.cleanup)
    names = ["gma", *(f"lma:{store}" for store in STORES)]
    sockets: list[socket.socket] = []
    try:
        for _ in names:
            bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(bound)
            bound.bind(("127.0.0.1", 0))
            bound.listen(64)
        urls = {name: f"http://127.0.0.1:{bound.getsockname()[1]}" for name, bound in zip(names, sockets)}
        tokens = {"admin": secrets.token_urlsafe(24)}
        for store in STORES:
            for kind in ("staff", "coordinator", "upload"):
                tokens[f"{store}/{kind}"] = secrets.token_urlsafe(24)
        settings = separate_process_settings(
            urls["gma"], {store: urls[f"lma:{store}"] for store in STORES}, tokens
        )
        base = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(SETTING_PREFIXES)
            and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
        }
        base.update(
            PYTHONPATH=os.pathsep.join([str(REPO_ROOT), str(EXAMPLE_DIR)]),
            DAGENTSC_BIN=planner,
            NO_PROXY="127.0.0.1,localhost",
            no_proxy="127.0.0.1,localhost",
        )
        processes: list[tuple[str, subprocess.Popen, Path]] = []
        for name, bound in zip(names, sockets):
            target = "dagents.gma.main:app" if name == "gma" else "dagents.lma.main:app"
            log = Path(folder.name) / f"{name.replace(':', '-')}.log"
            with log.open("wb") as handle:
                process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", target, "--fd", str(bound.fileno()), "--log-level", "warning"],
                    cwd=REPO_ROOT,
                    env={**base, **settings[name]},
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    pass_fds=(bound.fileno(),),
                    start_new_session=True,
                )
            test.addCleanup(_stop, process)
            processes.append((name, process, log))
    finally:
        # Each child holds its own copy of its socket.
        for bound in sockets:
            bound.close()

    deadline = time.monotonic() + timeout_s
    waiting = list(processes)
    while waiting:
        for name, process, log in waiting:
            if process.poll() is not None:
                test.fail(f"the {name} process exited with {process.returncode}:\n{log.read_text(errors='replace')[-3000:]}")
        waiting = [entry for entry in waiting if not _healthy(urls[entry[0]])]
        if waiting and time.monotonic() > deadline:
            test.fail(f"{', '.join(name for name, _, _ in waiting)} did not answer /health within {timeout_s:.0f} s")
        if waiting:
            time.sleep(0.2)
    test.addCleanup(_report_crashes, test, processes)
    return settings["step5"]


def _report_crashes(test: unittest.TestCase, processes: list[tuple[str, subprocess.Popen, Path]]) -> None:
    """Fail when an agent process stopped before the test stopped it."""
    for name, process, log in processes:
        if process.poll() is not None:
            test.fail(f"the {name} process exited with {process.returncode}:\n{log.read_text(errors='replace')[-3000:]}")


class GuideBlockTests(unittest.TestCase):
    """The code in the guide is the code in the repository."""

    blocks = guide_blocks()

    def test_the_guide_has_pages_and_marked_blocks(self) -> None:
        self.assertTrue(list(GUIDE_DIR.glob("*.md")), "docs/guide has no pages")
        self.assertTrue([block for block in self.blocks if block.marker], "no block in the guide carries a marker")

    def test_every_python_block_names_its_source(self) -> None:
        for block in self.blocks:
            if block.language == "python":
                with self.subTest(block=block.where()):
                    self.assertIsNotNone(block.marker, "add <!-- source: PATH --> on the line above this block")
                    self.assertEqual("source", block.marker[0])  # type: ignore[index]

    def test_source_blocks_match_their_files(self) -> None:
        for block in self.blocks:
            if not block.marker or block.marker[0] != "source":
                continue
            path = (REPO_ROOT / block.marker[1]).resolve()
            with self.subTest(block=block.where(), source=block.marker[1]):
                self.assertTrue(path.is_relative_to(REPO_ROOT), "a source must be inside the repository")
                self.assertTrue(path.is_file(), "the source file does not exist")
                self.assertTrue(
                    contains_lines(path.read_text(encoding="utf-8"), block.lines),
                    "this block differs from its source file; copy it again from the file",
                )

    def test_output_blocks_name_an_example_script(self) -> None:
        for block in self.blocks:
            if block.marker and block.marker[0] == "output":
                with self.subTest(block=block.where()):
                    path = REPO_ROOT / block.marker[1]
                    self.assertTrue(path.is_file(), f"{block.marker[1]} does not exist")
                    self.assertEqual(EXAMPLE_DIR, path.parent, "output blocks come from the example's scripts")

    def test_every_example_file_is_shown_in_the_guide(self) -> None:
        shown = {block.marker[1] for block in self.blocks if block.marker and block.marker[0] == "source"}
        files = sorted(EXAMPLE_DIR.glob("shop/*.py")) + sorted(EXAMPLE_DIR.glob("step*.py"))
        self.assertTrue(files)
        for path in files:
            if path.name == "__init__.py":
                continue
            name = str(path.relative_to(REPO_ROOT))
            with self.subTest(file=name):
                self.assertIn(name, shown, "the guide never shows this file")

    @unittest.skipUnless(shutil.which("bash"), "bash is not available")
    def test_the_guide_starts_the_agents_with_the_settings_tested_here(self) -> None:
        """The commands in "Run the agents as separate processes" start what the process test starts.

        The process test cannot run the guide's commands, because they serve
        on fixed ports. So bash runs them here with stand-ins that record each
        process instead of starting it (``dry_run_separate_processes``). Each
        LMA's port and tokens, and the GMA's port and admin token, then fix
        every other setting: the GMA must call each store's LMA at that LMA's
        port with that LMA's coordinator token, and so on. A guide that passes
        one store's token or port to the other store's setting fails here.
        """
        records = dry_run_separate_processes(self)
        agents = [record for record in records if record["program"] == "uvicorn"]
        gma = [record for record in agents if record["args"][:1] == ["dagents.gma.main:app"]]
        lmas = [record for record in agents if record["args"][:1] == ["dagents.lma.main:app"]]
        self.assertEqual(1, len(gma), "the guide must start one GMA with uvicorn dagents.gma.main:app")
        self.assertEqual(len(agents), len(gma) + len(lmas), "the guide starts a uvicorn app that is not an agent")
        lma_by_store = {record["env"].get("LMA_SITE_ID"): record for record in lmas}
        self.assertEqual(len(lmas), len(lma_by_store), "the guide starts two LMAs for one store")
        self.assertEqual(sorted(STORES), sorted(lma_by_store), "the guide must start one LMA for each store")
        step5 = [record for record in records if record["program"] == "python"]
        self.assertEqual([[STEP5]], [record["args"] for record in step5], "the guide must run step 5 once")

        def port(record: dict) -> str:
            args = record["args"]
            self.assertEqual(5, len(args), f"uvicorn {' '.join(args)}")
            self.assertEqual(("--port", "--log-level", "warning"), (args[1], args[3], args[4]), f"uvicorn {' '.join(args)}")
            return args[2]

        gma_env = gma[0]["env"]
        gma_url = f"http://127.0.0.1:{port(gma[0])}"
        lma_urls = {store: f"http://127.0.0.1:{port(lma_by_store[store])}" for store in STORES}
        tokens = {"admin": gma_env.get("GMA_ADMIN_TOKEN", "<missing>")}
        for store in STORES:
            lma_env = lma_by_store[store]["env"]
            for kind, setting in (("staff", "LMA_SITE_TOKEN"), ("coordinator", "LMA_COORDINATOR_TOKEN"), ("upload", "LMA_GMA_TOKEN")):
                tokens[f"{store}/{kind}"] = lma_env.get(setting, "<missing>")
        self.assertEqual(len(tokens), len(set(tokens.values())), "each token must differ from the others")
        for name, token in tokens.items():
            with self.subTest(token=name):
                self.assertGreaterEqual(len(token), 16, "an agent refuses a token shorter than 16 characters")

        expected = separate_process_settings(gma_url, lma_urls, tokens)
        recorded = {"gma": gma[0], **{f"lma:{store}": lma_by_store[store] for store in STORES}, "step5": step5[0]}
        for name, record in recorded.items():
            with self.subTest(process=name):
                settings = {key: value for key, value in record["env"].items() if key.startswith(SETTING_PREFIXES)}
                self.assertEqual(expected[name], settings)
                if name != "step5":
                    # The process test loads the example the same way.
                    entries = [(REPO_ROOT / entry).resolve() for entry in record["env"].get("PYTHONPATH", "").split(os.pathsep)]
                    self.assertEqual([REPO_ROOT, EXAMPLE_DIR], entries, "PYTHONPATH must hold the root and the example")
                    self.assertEqual(str(DUNE_BUILT_BINARY), record["env"].get("DAGENTSC_BIN"), "each agent needs the planner")
        health = {f"{url}/health" for url in (gma_url, *lma_urls.values())}
        for record in records:
            if record["program"] == "curl":
                with self.subTest(curl=" ".join(record["args"])):
                    self.assertIn(record["args"][-1], health, "the guide checks an address no agent serves")
        self.assertEqual(
            set(),
            {record["program"] for record in records} - {"uvicorn", "python", "curl"},
            "the dry run recorded a program it does not check",
        )

    def test_the_example_adds_no_domain_word_to_the_framework(self) -> None:
        """CLAUDE.md: nothing in dagents/ knows the application's domain."""
        for path in sorted((REPO_ROOT / "dagents").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for word in DOMAIN_WORDS:
                with self.subTest(file=str(path.relative_to(REPO_ROOT)), word=word):
                    self.assertNotIn(word, text)


class FailClosedTests(unittest.TestCase):
    """Without a planner the example's Guard denies every read."""

    def test_the_guard_denies_every_read_without_the_planner(self) -> None:
        result = run_script("examples/getting-started/step2_guard_check.py", "/nonexistent/dagentsc")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(3, result.stdout.count("The planner could not be reached, so the Guard denied the request."))
        self.assertNotIn("permitted True", result.stdout)


@unittest.skipUnless(PLANNER, "dagentsc binary is not available")
class ExampleRunTests(unittest.TestCase):
    """The example runs against the real planner and prints what the guide shows."""

    outputs: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.outputs = {}
        for script in sorted(EXAMPLE_DIR.glob("step*.py")):
            name = str(script.relative_to(REPO_ROOT))
            result = run_script(name, PLANNER)  # type: ignore[arg-type]
            if result.returncode != 0:
                raise AssertionError(f"{name} failed:\n{result.stdout}\n{result.stderr}")
            cls.outputs[name] = result.stdout

    def test_every_step_runs(self) -> None:
        self.assertEqual(5, len(self.outputs))

    def test_the_guide_shows_the_real_output(self) -> None:
        marked = [block for block in guide_blocks() if block.marker and block.marker[0] == "output"]
        self.assertEqual(
            sorted(self.outputs), sorted({block.marker[1] for block in marked}),  # type: ignore[index]
            "the guide shows the output of every step, once or more",
        )
        for block in marked:
            with self.subTest(block=block.where()):
                actual = [line.rstrip() for line in self.outputs[block.marker[1]].splitlines()]  # type: ignore[index]
                self.assertEqual(actual, [line.rstrip() for line in block.lines], "run the script and paste its output")

    def test_the_extension_registers_and_the_orders_match_the_contract(self) -> None:
        output = self.outputs["examples/getting-started/step1_register.py"]
        self.assertIn("example-retail-returns 1.0.0", output)
        self.assertIn("store-north: 60 orders, matches the contract: True", output)
        self.assertIn("store-south: 45 orders, matches the contract: True", output)

    def test_the_guard_narrows_and_denies_by_trust(self) -> None:
        output = self.outputs["examples/getting-started/step2_guard_check.py"]
        self.assertIn("analyst-anna: trust high, decision narrow, permitted True", output)
        self.assertIn("new-hire-ben: trust moderate, decision narrow, permitted True", output)
        self.assertIn("contractor-cy: trust low, decision deny, permitted False", output)
        self.assertIn("chain intact: True", output)

    def test_stores_report_summaries_not_orders(self) -> None:
        output = self.outputs["examples/getting-started/step3_profile.py"]
        self.assertIn("order_value aggregate_only:20", output)
        self.assertNotIn("order_id", output)
        self.assertIn("Combined: 105 orders", output)

    def test_the_round_reaches_a_release_decision(self) -> None:
        output = self.outputs["examples/getting-started/step4_federated_round.py"]
        self.assertIn("store-north: completed, 60 examples", output)
        self.assertIn("store-south: completed, 45 examples", output)
        self.assertIn("Aggregation permitted: True", output)
        self.assertRegex(output, r"Decision: (release|another_round|reject)")

    def test_the_agents_run_the_same_round_over_their_apis(self) -> None:
        output = self.outputs["examples/getting-started/step5_agents_over_http.py"]
        in_process = self.outputs["examples/getting-started/step4_federated_round.py"]
        # Each store's LMA runs the same rule on the same guarded orders, so
        # the results equal the in-process round of step 4.
        for site_id in ("store-north", "store-south"):
            with self.subTest(site=site_id):
                ran_here = [line for line in in_process.splitlines() if line.startswith(f"  {site_id}: completed")]
                self.assertEqual(1, len(ran_here))
                self.assertIn(ran_here[0], output.splitlines())
                # One jobs:run call per store: the offer and the execution.
                self.assertRegex(output, rf"{site_id} run 200 completed")
        self.assertIn("Aggregation permitted: True", output)
        self.assertIn("store-east, which the round did not select: 403 forbidden", output)
        # The GMA and the caller see counts and metrics, never an order.
        for word in ("order_id", "customer_postcode", "order_value"):
            self.assertNotIn(word, output)
        # The call log holds route templates, never an address.
        self.assertIn("GMA POST /api/v1/federation/rounds 202", output)
        self.assertNotIn("://", output)
        self.assertNotIn(".internal", output)

    def test_step5_prints_the_same_round_when_the_agents_are_separate_processes(self) -> None:
        """Guide Step 6, "Run the agents as separate processes": the calls stay the same.

        A GMA and one LMA per store run as uvicorn processes started from
        their settings, as the guide's commands start them. Each LMA builds
        its store's source from the extension's ``shop_orders`` factory, and
        the GMA reaches the LMAs only over loopback HTTP. Step 5 then prints
        the guide's output, except the line that names how the agents are
        called.
        """
        step5_settings = start_agent_processes(self, PLANNER)  # type: ignore[arg-type]
        result = run_script(STEP5, PLANNER, step5_settings)  # type: ignore[arg-type]
        self.assertEqual(0, result.returncode, f"{STEP5} failed:\n{result.stdout}\n{result.stderr}")
        printed = [line.rstrip() for line in result.stdout.splitlines()]
        expected = output_block(STEP5)
        self.assertEqual("Agents: gma, lma:store-north, lma:store-south, called in-process", expected[0])
        self.assertEqual("Agents: gma, lma:store-north, lma:store-south, called network", printed[0])
        self.assertEqual(expected[1:], printed[1:], "over HTTP, step 5 printed a different round from the guide's")
        # No token reaches the output.
        store_tokens = [entry.partition("=")[2] for entry in step5_settings["SHOP_STORE_TOKENS"].split(",")]
        for token in [step5_settings["SHOP_ADMIN_TOKEN"], *store_tokens]:
            self.assertNotIn(token, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
