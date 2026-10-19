"""Subprocess wrapper for the OCaml dagentsc compiler.

:func:`dagentsc_binary` says which planner executable is started,
:func:`planner_limits` how long it may run and how much it may read and print,
and :func:`planner_identity` which planner it is: this package's version and
the SHA-256 of the binary. When ``DAGENTSC_SHA256`` is set, the runner starts
only a binary with that hash.
"""

import errno
import hashlib
import json
import logging
import math
import os
import queue
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from dagents import __version__

# Keys whose VALUES are caller data and must be passed through untouched.
#
# Both sets are written in snake_case and compared against a snake_cased form of
# the key, so they match in both directions. The naive version compared only the
# already-converted key, which worked outbound (snake -> camel, and these are
# checked post-conversion) but silently failed inbound: "siteWeights" converts to
# "site_weights", which was not in a camelCase-only set, so the map's keys — site
# identifiers — were themselves snake_cased. A site called "Mercy_General" came
# back as "mercy__general", the weight lookup missed, and every aggregated metric
# silently became 0.0 on its way into the release gates.
DATA_RECORD_KEYS = {"records", "inline_records"}

DATA_MAP_KEYS = {
    "schema_hint",
    "options",
    "connection_options",
    "config",
    "config_json",
    # Maps whose keys are caller data (field names, metric names, site ids)
    # rather than contract field names.
    "field_sensitivity",
    "metrics",
    "candidate_metrics",
    "baseline_metrics",
    "site_weights",
    "parameters",
    # ConfigMap keys are chosen by whoever authored the workload, so converting
    # them renames the entry: a component asking for "router_mode" was
    # deploying a ConfigMap keyed "routerMode".
    "config_map_data",
    # Scope labels and evidence pointers are caller-chosen in the same way.
    "scope",
    "pointers",
}

class PlannerUnavailableError(RuntimeError):
    """The planner could not be run, or it returned output that cannot be used.

    Raised when the binary is missing or cannot be started, when it exits with
    an error, when it runs past ``DAGENTSC_TIMEOUT_SECONDS`` or prints more
    than ``DAGENTSC_MAX_OUTPUT_BYTES``, when its output is not the JSON the
    caller expects, when the input is larger than ``DAGENTSC_MAX_INPUT_BYTES``
    or the binary's SHA-256 is not the one ``DAGENTSC_SHA256`` requires (the
    planner is then not started), and when one of those settings makes no
    sense. It is a RuntimeError, so callers that already catch RuntimeError
    keep working. A service maps it to a 503, because the request may succeed
    once the planner is back.

    The message names the cause and never repeats the input. When the planner
    exits with an error, the message ends with what it printed on standard
    error, on one line and cut to 500 characters. The planner's own errors
    can quote a value from the input, such as an unknown event name, so the
    message belongs in a log: the Guard and the agents never send it to a
    caller or write it to an audit record.
    """


# The three defaults below were measured on about 8,100 real planner calls: the
# agent suite, the healthcare suite and pilot, the getting-started example, the
# captured API examples (scripts/capture_api_examples.py --check), core-service's
# suite, every docs/demo input, and the manifest compiler on the seven-component
# stack of generate_manifests_local.py.

#: How long one planner call may run, in seconds, when ``DAGENTSC_TIMEOUT_SECONDS``
#: is not set. The median call took 5 ms and the slowest 50 ms, on a 4-core
#: machine that was running another test suite at the same time. Ten seconds
#: is 200 times the slowest call, and it ends a hung call well before an agent
#: client's 30-second timeout, so the caller gets the Guard's denial rather
#: than a timeout of its own.
DEFAULT_TIMEOUT_SECONDS = 10.0

#: The most bytes one call may send the planner, standard input and input files
#: together, when ``DAGENTSC_MAX_INPUT_BYTES`` is not set. The largest real
#: input was 11,277 bytes (the getting-started example's records, sent to
#: ``dataset schema validate``); no Guard request was over 1.6 KB. 16 MiB is
#: about 1,500 times the largest. The planner reads its whole input into memory:
#: a 16 MB records file took it 0.7 seconds and 165 MB of memory.
DEFAULT_MAX_INPUT_BYTES = 16 * 1024 * 1024

#: The most bytes the planner may print on standard output for one call, when
#: ``DAGENTSC_MAX_OUTPUT_BYTES`` is not set. The largest real answer was 14,637
#: bytes (the manifest compiler on the seven-component stack of
#: generate_manifests_local.py). 16 MiB is about 1,150 times that, and it bounds
#: what the runner holds in memory for a planner that misbehaves.
DEFAULT_MAX_OUTPUT_BYTES = 16 * 1024 * 1024

#: How much of the planner's standard error the runner keeps for its message.
#: The rest is read and dropped, so a planner that prints a lot of errors does
#: not block on a full pipe.
_STDERR_KEPT_BYTES = 4096

#: How many characters of the planner's error a message repeats.
_STDERR_IN_MESSAGE = 500

_READ_CHUNK = 65536


class PlannerLimits(NamedTuple):
    """How long one planner call may run, and how much it may read and print."""

    timeout_seconds: float
    max_input_bytes: int
    max_output_bytes: int


def _positive_setting(environ: Mapping[str, str], name: str, default: float, *, whole: bool) -> float:
    """Read one limit, or return its default when the variable is unset or empty.

    Raises ``ValueError`` naming the variable when the value is not a number
    above 0, or not a whole number for a byte count. The message does not
    repeat the value.
    """
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    text = raw.strip()
    if whole:
        if not (text.isascii() and text.isdigit()) or int(text) <= 0:
            raise ValueError(f"{name} must be a whole number of bytes above 0")
        return int(text)
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a number of seconds above 0")
    return value


def planner_limits(environ: Mapping[str, str] | None = None) -> PlannerLimits:
    """The limits each planner call runs under.

    Params:
    - `environ`: the variables to read; ``None`` reads the process
      environment, where the runner reads ``DAGENTSC_BIN`` too.

    The variables are ``DAGENTSC_TIMEOUT_SECONDS`` (a number of seconds,
    fractions allowed), ``DAGENTSC_MAX_INPUT_BYTES`` and
    ``DAGENTSC_MAX_OUTPUT_BYTES`` (whole numbers of bytes). An unset or empty
    variable takes its default (:data:`DEFAULT_TIMEOUT_SECONDS`,
    :data:`DEFAULT_MAX_INPUT_BYTES`, :data:`DEFAULT_MAX_OUTPUT_BYTES`).

    Raises ``ValueError`` naming the variable when a value is zero, negative
    or not a number. The LMA and GMA call this when they are built, so they
    refuse to start with such a value; every planner call reads the limits
    again and raises :class:`PlannerUnavailableError` instead.
    """
    environ = os.environ if environ is None else environ
    return PlannerLimits(
        timeout_seconds=float(_positive_setting(environ, "DAGENTSC_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, whole=False)),
        max_input_bytes=int(_positive_setting(environ, "DAGENTSC_MAX_INPUT_BYTES", DEFAULT_MAX_INPUT_BYTES, whole=True)),
        max_output_bytes=int(_positive_setting(environ, "DAGENTSC_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES, whole=True)),
    )


#: The setting that pins the planner binary's SHA-256.
PINNED_SHA256_SETTING = "DAGENTSC_SHA256"

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def pinned_sha256(environ: Mapping[str, str] | None = None) -> str | None:
    """The SHA-256 the operator requires of the planner binary, or None when none is required.

    Params:
    - `environ`: the variables to read; ``None`` reads the process environment.

    Reads ``DAGENTSC_SHA256``: 64 hexadecimal digits, in either case, as
    ``sha256sum`` prints them and a GitHub Release lists them in
    ``SHA256SUMS``. Returns them in lower case. An unset or empty variable
    requires nothing. Raises ``ValueError`` naming the variable for any other
    value, without repeating it.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get(PINNED_SHA256_SETTING)
    if raw is None or not raw.strip():
        return None
    text = raw.strip().lower()
    if not _SHA256_HEX.fullmatch(text):
        raise ValueError(f"{PINNED_SHA256_SETTING} must be the planner's SHA-256 as 64 hexadecimal digits")
    return text


def check_planner_settings(environ: Mapping[str, str] | None = None) -> PlannerLimits:
    """Read every planner setting and return the limits, or raise for the first that makes no sense.

    The settings are the three limits (see :func:`planner_limits`) and
    ``DAGENTSC_SHA256`` (see :func:`pinned_sha256`). Raises ``ValueError``
    naming the variable. The LMA, the GMA and their commands call this when
    they start, so they refuse to start with such a value.
    """
    limits = planner_limits(environ)
    pinned_sha256(environ)
    return limits


def seconds_text(seconds: float) -> str:
    """A number of seconds in words, such as ``"1 second"`` or ``"2.5 seconds"``."""
    return f"{seconds:g} second" if seconds == 1 else f"{seconds:g} seconds"


def _limits_for_call() -> PlannerLimits:
    """The limits for one call, or :class:`PlannerUnavailableError` when a setting makes no sense."""
    try:
        return planner_limits()
    except ValueError as error:
        raise PlannerUnavailableError(f"dagentsc was not started: {error}") from None


def to_camel_case(snake_str: str) -> str:
    components = snake_str.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])

def to_snake_case(camel_str: str) -> str:
    return ''.join(['_' + c.lower() if c.isupper() else c for c in camel_str]).lstrip('_')

def is_data_key(key: str) -> bool:
    """Whether this key's value must be passed through without key conversion.

    Normalizes to snake_case first so the check works on a camelCase key coming
    back from the planner and a snake_case key going out to it.
    """
    normalized = to_snake_case(key)
    return normalized in DATA_RECORD_KEYS or normalized in DATA_MAP_KEYS

def convert_keys(obj: Any, convert_func) -> Any:
    if isinstance(obj, list):
        return [convert_keys(item, convert_func) for item in obj]
    elif isinstance(obj, dict):
        converted: dict[str, Any] = {}
        for key, value in obj.items():
            converted_key = convert_func(key)
            # Check the ORIGINAL key, not the converted one: the conversion is
            # what differs between the two directions.
            if is_data_key(key):
                converted[converted_key] = value
            else:
                converted[converted_key] = convert_keys(value, convert_func)
        return converted
    else:
        return obj

#: Where a platform wheel of the ``dagents`` package puts the planner. A source
#: checkout and a wheel built from the sdist have no file here.
BUNDLED_PLANNER = Path(__file__).resolve().parents[2] / "_bin" / "dagentsc"

#: The name the runner starts when no other planner is found. The operating
#: system looks it up on PATH; containers install the planner there.
PLANNER_ON_PATH = "dagentsc"


def bundled_planner() -> Path | None:
    """The planner bundled with this installation, or None when there is none."""
    if BUNDLED_PLANNER.is_file() and os.access(BUNDLED_PLANNER, os.X_OK):
        return BUNDLED_PLANNER
    return None


def find_planner() -> tuple[str | None, str]:
    """Where the planner is, and which rule found it.

    Returns ``(path, source)``. ``source`` is ``DAGENTSC_BIN``, ``bundled``,
    ``PATH`` or ``none``; ``path`` is None only with ``none``. The rules are
    the ones :func:`dagentsc_binary` follows. A ``DAGENTSC_BIN`` that names a
    missing file is still returned, because the runner uses it as it is.
    """
    configured = os.environ.get("DAGENTSC_BIN")
    if configured:
        return configured, "DAGENTSC_BIN"
    bundled = bundled_planner()
    if bundled is not None:
        return str(bundled), "bundled"
    found = shutil.which(PLANNER_ON_PATH)
    if found:
        return found, "PATH"
    return None, "none"


#: What fixes each kind of problem :func:`planner_problem` reports, for the agents' warning.
_FIND_A_PLANNER = "until DAGENTSC_BIN names a dagentsc binary or dagentsc is on PATH"
_MATCH_THE_PIN = f"until the planner's SHA-256 is the one {PINNED_SHA256_SETTING} requires"


def _diagnose(found: tuple[str | None, str] | None, *, check_hash: bool = True) -> tuple[str, str] | None:
    """Why the runner cannot start a planner, and what fixes it, or None when it can."""
    path, source = find_planner() if found is None else found
    if path is None:
        return (
            "no planner was found: DAGENTSC_BIN is not set, this installation bundles none, and dagentsc is not on PATH",
            _FIND_A_PLANNER,
        )
    if source == "DAGENTSC_BIN" and not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return f"DAGENTSC_BIN names {path}, which is not a file this process can run", _FIND_A_PLANNER
    if not check_hash:
        return None
    try:
        if pinned_sha256() is None:
            # Nothing to compare, so the binary is not read here.
            return None
        problem = check_planner_hash(path).problem
    except ValueError as error:
        return str(error), f"until {PINNED_SHA256_SETTING} is unset or holds 64 hexadecimal digits"
    return None if problem is None else (problem, _MATCH_THE_PIN)


def planner_problem(found: tuple[str | None, str] | None = None, *, check_hash: bool = True) -> str | None:
    """Why the runner cannot start a planner, or None when it can.

    Params:
    - `found`: the result of :func:`find_planner`; ``None`` looks it up now.
    - `check_hash`: also compare the binary with ``DAGENTSC_SHA256`` when
      that is set (see :func:`check_planner_hash`). ``python -m dagents``
      turns this off and reports the hash on a line of its own.

    It checks that the file exists and may be run, and does not start it. The
    bundled planner and ``dagentsc`` on PATH are checked by the lookup itself,
    so only a ``DAGENTSC_BIN`` setting can name a file that cannot run. A
    ``DAGENTSC_SHA256`` that is not a SHA-256, or that differs from the
    binary's, is a problem too, because the runner then starts nothing.
    """
    diagnosis = _diagnose(found, check_hash=check_hash)
    return None if diagnosis is None else diagnosis[0]


def warn_if_no_planner(log: logging.Logger, agent_name: str) -> str | None:
    """Log one warning when the runner cannot start a planner, and return the reason.

    An agent starts and answers ``/health`` without a planner, and pip hides
    the build's notice when a wheel without one is installed. This warning is
    the one place that tells the operator why every governed request is
    denied. It also covers a binary whose SHA-256 is not the one
    ``DAGENTSC_SHA256`` requires. Returns None, and logs nothing, when a
    planner can be started.
    """
    diagnosis = _diagnose(None)
    if diagnosis is None:
        return None
    problem, remedy = diagnosis
    log.warning(
        "%s has no planner: %s. The Ethical Guard denies every request %s. "
        "Run python -m dagents to see which planner the runner finds.",
        agent_name,
        problem,
        remedy,
    )
    return problem


def dagentsc_binary() -> str:
    """The planner executable the runner starts.

    In this order:

    1. ``DAGENTSC_BIN``, when it is set and not empty. It is used even when it
       names a missing file, so a wrong setting fails instead of quietly
       running a different planner.
    2. The planner a platform wheel bundles at ``dagents/_bin/dagentsc``.
    3. ``dagentsc`` on PATH.

    When none of them exists, starting the name ``dagentsc`` fails, the runner
    raises :class:`PlannerUnavailableError`, and the Guard denies the request.
    """
    configured = os.environ.get("DAGENTSC_BIN")
    if configured:
        return configured
    bundled = bundled_planner()
    if bundled is not None:
        return str(bundled)
    return PLANNER_ON_PATH


class PlannerIdentity(NamedTuple):
    """Which planner a call runs. The Guard writes both values into every audit record.

    ``version`` is the version of this ``dagents`` package, whose runner starts
    the planner. The planner a platform wheel bundles, the one an image builds
    in its ``ocaml/opam`` stage and the one a checkout builds come from the
    same source tree as the package, so they have this version too. A planner
    that ``DAGENTSC_BIN`` names can come from another tree; the hash still
    names it.

    ``sha256`` is the SHA-256 of the binary, in hex. It names the binary
    exactly, however it was found, and each GitHub Release lists its
    planners' hashes in ``SHA256SUMS``. It is ``None`` when there is no file
    to read, for example when the planner is missing.

    The version is the package's rather than one the binary prints, so naming
    the planner starts no process and needs no planner command. The hash is
    what tells two builds of one version apart.
    """

    version: str
    sha256: str | None


#: What says a planner file is unchanged; see :func:`_stat_key`.
_StatKey = tuple[int, int, int, int, int]

#: The SHA-256 of each planner file read so far: path -> (stat key, hex digest).
_HASHES: dict[str, tuple[_StatKey, str]] = {}
_HASHES_LOCK = threading.Lock()

#: How the hash opens a planner file. ``O_NONBLOCK`` stops an ``open`` from
#: waiting on a FIFO with no writer; it changes nothing for a regular file.
_HASH_OPEN_FLAGS = (
    os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)
)


def _stat_key(status: os.stat_result) -> _StatKey:
    """What says a file is unchanged: its size, modification time, change time, inode and device.

    The change time is there because a copy such as ``cp -p`` or ``rsync
    --inplace`` can rewrite a file in place and set its old modification time
    again. No system call sets the change time to a chosen value: every write,
    ``utime``, ``chmod`` or rename sets it to the current time.
    """
    return (status.st_size, status.st_mtime_ns, status.st_ctime_ns, status.st_ino, status.st_dev)


def _require_regular_file(status: os.stat_result, path: str) -> None:
    """Raise ``OSError`` unless the file is a regular file.

    A device such as ``/dev/zero`` never ends and a FIFO can block, so neither
    is read to name the planner. Neither can be started as one either.
    """
    if not stat.S_ISREG(status.st_mode):
        raise OSError(errno.EINVAL, "not a regular file", path)


def _hash_file(path: str) -> tuple[_StatKey, str]:
    """Read one regular file and return its stat key and SHA-256, both from the same open file."""
    digest = hashlib.sha256()
    with open(os.open(path, _HASH_OPEN_FLAGS), "rb") as handle:
        status = os.fstat(handle.fileno())
        # Checked again on the open file, in case the path changed since the stat.
        _require_regular_file(status, path)
        key = _stat_key(status)
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return key, digest.hexdigest()


def planner_sha256(path: str) -> str:
    """The SHA-256 of the planner file at ``path``, in hex.

    The file is read once for each path, size, modification time, change time
    and inode, then the hash is kept, so a call costs one ``stat``. A file
    replaced or rewritten in place gets a new change time, so it is read
    again. On a file system with coarse timestamps, a rewrite within the same
    tick keeps both times and is not noticed. Raises ``OSError`` when the file
    is missing, cannot be read or is not a regular file.
    """
    status = os.stat(path)
    _require_regular_file(status, path)
    key = _stat_key(status)
    with _HASHES_LOCK:
        known = _HASHES.get(path)
    if known is not None and known[0] == key:
        return known[1]
    read_key, digest = _hash_file(path)
    with _HASHES_LOCK:
        _HASHES[path] = (read_key, digest)
    return digest


def _planner_file(binary: str) -> str | None:
    """The file the operating system starts for ``binary``.

    A name with a directory in it is that file; a bare name such as
    ``dagentsc`` is looked up on PATH, as starting it would. None when the
    lookup finds nothing.
    """
    if os.path.dirname(binary):
        return binary
    return shutil.which(binary)


def planner_identity() -> PlannerIdentity:
    """The planner the runner starts now: this package's version and the binary's SHA-256.

    The SHA-256 is ``None`` when there is no file to read. It comes from
    :func:`planner_sha256`, so the binary is read only when it changed.
    """
    path = _planner_file(dagentsc_binary())
    sha256 = None
    if path is not None:
        try:
            sha256 = planner_sha256(path)
        except OSError:
            sha256 = None
    return PlannerIdentity(version=__version__, sha256=sha256)


class PlannerHashCheck(NamedTuple):
    """The planner binary's SHA-256 next to the one ``DAGENTSC_SHA256`` requires."""

    #: The binary's SHA-256, or None when the file could not be read.
    sha256: str | None
    #: The SHA-256 ``DAGENTSC_SHA256`` requires, or None when it is not set.
    pinned: str | None
    #: Why the runner does not start this binary, or None when it may.
    problem: str | None


def check_planner_hash(path: str) -> PlannerHashCheck:
    """Hash the planner file at ``path`` and compare it with ``DAGENTSC_SHA256``.

    The runner does not start a binary whose SHA-256 differs from the pinned
    one, or one it cannot read while a hash is pinned; ``problem`` says which.
    A missing file has no problem here, because starting it fails with its own
    reason. Raises ``ValueError`` when ``DAGENTSC_SHA256`` is not a SHA-256.

    The check is against mistakes such as a stale or wrong build. It does not
    protect against someone who can replace the binary between the check and
    the start, because whoever can write the file can also change the setting.
    """
    pinned = pinned_sha256()
    try:
        sha256 = planner_sha256(path)
    except FileNotFoundError:
        return PlannerHashCheck(None, pinned, None)
    except OSError as error:
        problem = None
        if pinned is not None:
            problem = (
                f"the planner at {path} could not be read to compare it with {PINNED_SHA256_SETTING} "
                f"({error.strerror or type(error).__name__})"
            )
        return PlannerHashCheck(None, pinned, problem)
    if pinned is not None and sha256 != pinned:
        return PlannerHashCheck(
            sha256,
            pinned,
            f"the planner at {path} has SHA-256 {sha256}, not the {pinned} that {PINNED_SHA256_SETTING} requires",
        )
    return PlannerHashCheck(sha256, pinned, None)


def _check_pinned_planner(binary: str) -> None:
    """Refuse to start a binary whose SHA-256 is not the one ``DAGENTSC_SHA256`` requires.

    Without the setting, nothing is read. A binary that cannot be found is
    left to fail when it is started, with the usual reason.
    """
    try:
        if pinned_sha256() is None:
            return
    except ValueError as error:
        raise PlannerUnavailableError(f"dagentsc was not started: {error}") from None
    path = _planner_file(binary)
    if path is None:
        return
    problem = check_planner_hash(path).problem
    if problem is not None:
        raise PlannerUnavailableError(f"{problem}, so dagentsc was not started")

def _check_input_size(size: int, limits: PlannerLimits) -> None:
    """Refuse an input larger than ``DAGENTSC_MAX_INPUT_BYTES`` before the planner starts.

    The planner reads its whole input into memory before it decides. The
    largest input the repository sends is 11 KB, so one past the limit is a
    caller's mistake, and starting the planner on it could end in a timeout or
    an out-of-memory kill instead of a clear reason.
    """
    if size > limits.max_input_bytes:
        raise PlannerUnavailableError(
            f"the planner input is {size} bytes, more than DAGENTSC_MAX_INPUT_BYTES ({limits.max_input_bytes}) "
            "allows, so dagentsc was not started"
        )


def _stop(process: subprocess.Popen) -> None:
    """Kill the planner and every process it started, then wait for the planner to exit.

    The planner runs in a process group of its own (``start_new_session``),
    so one signal reaches the children it started as well. Windows has no
    process groups; there only the planner itself is killed.
    """
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except OSError:
        pass
    process.wait()


def _run_process(
    full_cmd: list[str], *, input: bytes | None = None, limits: PlannerLimits
) -> subprocess.CompletedProcess:
    """Start the planner, feed it ``input`` and collect what it prints, within ``limits``.

    Returns the finished process with ``stdout`` and ``stderr`` as bytes. A
    command that reads files gets no standard input (``input`` is ``None``).

    Three threads write the input and read both outputs, so a planner that
    stops reading, or prints without end, cannot block the caller. Standard
    output is read up to ``limits.max_output_bytes``; one byte more stops the
    planner. Standard error is read to its end, and its first 4 KiB are kept.

    Raises :class:`PlannerUnavailableError` when the planner runs past
    ``limits.timeout_seconds`` or prints too much. The planner and every
    process it started are killed first. Raises ``OSError`` when the planner
    cannot be started.
    """
    process = subprocess.Popen(
        full_cmd,
        stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + limits.timeout_seconds
    finished: queue.SimpleQueue[tuple[str, bool]] = queue.SimpleQueue()
    stdout = bytearray()
    stderr = bytearray()

    def read_stdout() -> None:
        over = False
        try:
            while chunk := process.stdout.read1(_READ_CHUNK):
                if len(stdout) + len(chunk) > limits.max_output_bytes:
                    over = True
                    break
                stdout.extend(chunk)
        except (OSError, ValueError):
            pass
        finally:
            finished.put(("stdout", over))
            process.stdout.close()

    def read_stderr() -> None:
        try:
            while chunk := process.stderr.read1(_READ_CHUNK):
                room = _STDERR_KEPT_BYTES - len(stderr)
                if room > 0:
                    stderr.extend(chunk[:room])
        except (OSError, ValueError):
            pass
        finally:
            finished.put(("stderr", False))
            process.stderr.close()

    def write_stdin() -> None:
        try:
            process.stdin.write(input)
        except OSError:
            # The planner exited or was killed before it read everything; its
            # exit status says what happened.
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    threads = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
    if input is not None:
        threads.append(threading.Thread(target=write_stdin, daemon=True))
    for thread in threads:
        thread.start()

    failure: str | None = None
    try:
        open_streams = {"stdout", "stderr"}
        while open_streams and failure is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            try:
                stream, over = finished.get(timeout=min(remaining, threading.TIMEOUT_MAX))
            except queue.Empty:
                failure = "timeout"
                break
            if over:
                failure = "output"
            open_streams.discard(stream)
        if failure is None:
            try:
                process.wait(timeout=max(deadline - time.monotonic(), 0))
            except subprocess.TimeoutExpired:
                failure = "timeout"
    finally:
        if failure is not None or process.poll() is None:
            _stop(process)
        for thread in threads:
            # After the kill every pipe reaches its end. A process that left
            # the planner's group can keep one open; the threads are daemons,
            # so they never hold up the caller.
            thread.join(timeout=1.0)

    if failure == "timeout":
        raise PlannerUnavailableError(
            f"dagentsc did not finish within {seconds_text(limits.timeout_seconds)} (DAGENTSC_TIMEOUT_SECONDS), "
            "so it was stopped"
        )
    if failure == "output":
        raise PlannerUnavailableError(
            f"dagentsc printed more than {limits.max_output_bytes} bytes (DAGENTSC_MAX_OUTPUT_BYTES), "
            "so it was stopped"
        )
    return subprocess.CompletedProcess(full_cmd, process.returncode, bytes(stdout), bytes(stderr))


def _text(output: bytes | str) -> str:
    return output if isinstance(output, str) else output.decode("utf-8", errors="replace")


def _exit_failure(returncode: int, stderr: bytes | str) -> PlannerUnavailableError:
    """The error for a planner that exited with an error or was stopped by a signal."""
    # One line: a Yojson error puts its reason on the line after the position.
    detail = " ".join(_text(stderr).split())
    if len(detail) > _STDERR_IN_MESSAGE:
        detail = detail[:_STDERR_IN_MESSAGE] + "..."
    if returncode < 0:
        cause = f"dagentsc was stopped by signal {-returncode}"
    else:
        cause = f"dagentsc execution failed with exit status {returncode}"
    return PlannerUnavailableError(f"{cause}: {detail}" if detail else cause)


#: The message for an answer nested deeper than Python's JSON decoder, or the
#: key conversion after it, can go (about 1,000 levels for the decoder, and
#: about 500 nested lists for the conversion).
_NESTED_TOO_DEEPLY = "dagentsc returned JSON nested too deeply"


def _parse_output(stdout: bytes | str) -> Any:
    """Read the planner's answer as JSON, or say why it cannot be read."""
    text = stdout if isinstance(stdout, str) else None
    if text is None:
        try:
            text = stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PlannerUnavailableError(f"dagentsc returned invalid json: {error.reason} at byte {error.start}") from None
    stripped = text.strip()
    if not stripped:
        raise PlannerUnavailableError("dagentsc printed no answer")
    try:
        return json.loads(stripped)
    except RecursionError:
        raise PlannerUnavailableError(_NESTED_TOO_DEEPLY) from None
    except json.JSONDecodeError as error:
        if stripped[0] in "{[" and stripped[-1] not in "}]":
            raise PlannerUnavailableError(
                f"dagentsc output ended before its JSON was complete ({len(stripped)} characters)"
            ) from None
        # The decoder's message names a position, never the text itself.
        raise PlannerUnavailableError(f"dagentsc returned invalid json: {error.msg} at character {error.pos}") from None


def _execute(full_cmd: list[str], input_str: str | None = None) -> Any:
    """Run the planner once and return its output with snake_case keys.

    Raises :class:`PlannerUnavailableError` when a limit setting makes no
    sense, when the input is over ``DAGENTSC_MAX_INPUT_BYTES``, when the
    binary's SHA-256 is not the one ``DAGENTSC_SHA256`` requires, when the
    binary cannot be started, exits with an error, runs past its timeout or
    prints too much, and when its output is not JSON or is nested too deeply
    to read.
    """
    limits = _limits_for_call()
    input_bytes = None if input_str is None else input_str.encode("utf-8")
    if input_bytes is not None:
        _check_input_size(len(input_bytes), limits)
    _check_pinned_planner(full_cmd[0])
    try:
        if input_bytes is None:
            result = _run_process(full_cmd, limits=limits)
        else:
            result = _run_process(full_cmd, input=input_bytes, limits=limits)
    except OSError as error:
        # FileNotFoundError, PermissionError and the like: the binary is missing
        # or cannot be run. core-service reads this cause to choose its Python
        # renderer, so only a planner that never started may carry an OSError.
        raise PlannerUnavailableError(f"dagentsc could not be started: {error}") from error
    if result.returncode != 0:
        raise _exit_failure(result.returncode, result.stderr)
    parsed = _parse_output(result.stdout)
    try:
        return convert_keys(parsed, to_snake_case)
    except RecursionError:
        # The decoder reads nested lists the recursive key conversion cannot.
        raise PlannerUnavailableError(_NESTED_TOO_DEEPLY) from None


def run_dagentsc(command: list[str], payload: dict[str, Any]) -> Any:
    """Execute the dagentsc binary with the given command and JSON payload.

    Raises :class:`PlannerUnavailableError` when the planner cannot be run,
    runs past its limits or its output is not JSON.
    """
    camel_payload = convert_keys(payload, to_camel_case)
    input_str = json.dumps(camel_payload)
    # Base command is something like ['manifest', 'compile', '--input', '-', '--output', 'json'].
    # dagentsc_binary() says which planner runs: DAGENTSC_BIN, the bundled one, or PATH.
    full_cmd = [dagentsc_binary()] + command
    return _execute(full_cmd, input_str)

def run_dagentsc_with_files(command: list[str], payloads: dict[str, Any]) -> Any:
    """Execute dagentsc commands that require multiple JSON file inputs.

    The files count toward ``DAGENTSC_MAX_INPUT_BYTES`` together; when they
    are over it, no file is written and the planner is not started.

    Raises :class:`PlannerUnavailableError` like :func:`run_dagentsc`.
    """
    contents = {
        flag: json.dumps(payload if flag == "--records" else convert_keys(payload, to_camel_case)).encode("utf-8")
        for flag, payload in payloads.items()
    }
    _check_input_size(sum(len(content) for content in contents.values()), _limits_for_call())
    temp_paths: list[str] = []
    try:
        resolved_command = list(command)
        for flag, content in contents.items():
            with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as handle:
                temp_paths.append(handle.name)
                handle.write(content)
                flag_index = resolved_command.index(flag)
                resolved_command[flag_index + 1] = handle.name
        full_cmd = [dagentsc_binary()] + resolved_command
        return _execute(full_cmd)
    finally:
        for path in temp_paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

def validate_dataset_source(source: dict[str, Any]) -> dict[str, Any]:
    """Validate a dataset source spec through the OCaml functional planner."""
    return run_dagentsc(["dataset", "source", "validate", "--input", "-"], source)

def compile_dataset_extraction(source: dict[str, Any]) -> dict[str, Any]:
    """Compile a source spec into a normalized extraction plan."""
    return run_dagentsc(["dataset", "source", "extract", "--input", "-"], source)

def validate_dataset_schema(records: list[dict[str, Any]], contract: dict[str, Any]) -> dict[str, Any]:
    """Validate record shape against a schema contract."""
    return run_dagentsc_with_files(
        ["dataset", "schema", "validate", "--records", "-", "--contract", "-"],
        {"--records": records, "--contract": contract},
    )

def evaluate_dataset_quality(records: list[dict[str, Any]], rules: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate data quality rules and return aggregate blocking status."""
    return run_dagentsc_with_files(
        ["dataset", "quality", "evaluate", "--records", "-", "--rules", "-"],
        {"--records": records, "--rules": rules},
    )

def compile_dataset_transform(records: list[dict[str, Any]], operations: list[dict[str, Any]]) -> dict[str, Any]:
    """Compile record-level transforms and return the planned output schema."""
    return run_dagentsc_with_files(
        ["dataset", "transform", "compile", "--records", "-", "--operations", "-"],
        {"--records": records, "--operations": operations},
    )

def apply_dataset_transform(records: list[dict[str, Any]], operations: list[dict[str, Any]]) -> Any:
    """Apply simple deterministic transforms through the OCaml functional planner."""
    return run_dagentsc_with_files(
        ["dataset", "transform", "apply", "--records", "-", "--operations", "-"],
        {"--records": records, "--operations": operations},
    )


def plan_restrictions(request: dict[str, Any]) -> dict[str, Any]:
    """Ask the Ethical-Restriction Rails what protection a request needs.

    This decides; it does not enforce. Enforcement belongs to
    :class:`~dagents.common.application.ethical_guard.EthicalGuard`, which also
    writes the audit record the returned obligations require.
    """
    return run_dagentsc(["governance", "restrict", "--input", "-"], request)


def assess_requester(requester: dict[str, Any]) -> dict[str, Any]:
    """Score a requester's Know-Your-User attributes into a trust level."""
    return run_dagentsc(["governance", "assess", "--input", "-"], requester)


def compile_round_plan(manifest: dict[str, Any], registrations: list[dict[str, Any]]) -> dict[str, Any]:
    """Select the sites eligible for one federated round."""
    return run_dagentsc(
        ["federation", "round", "plan", "--input", "-"],
        {"manifest": manifest, "registrations": registrations},
    )


def round_digest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Derive the deterministic content digest for a round manifest."""
    return run_dagentsc(["federation", "round", "digest", "--input", "-"], {"manifest": manifest})


def evaluate_aggregation_readiness(
    manifest: dict[str, Any],
    results: list[dict[str, Any]],
    selected_sites: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Decide whether returned contributions may be aggregated.

    ``selected_sites`` is the round plan's ``selected_sites``. When it is
    given, a result from any other site is rejected with the reason "site was
    not selected for this round" and counts toward neither the quorum nor the
    weights; an empty list accepts no result. ``None`` leaves the selection
    unchecked, which is the answer callers got before the key existed.
    """
    payload: dict[str, Any] = {"manifest": manifest, "results": results}
    if isinstance(selected_sites, str):
        # list("site-a") would select the sites "s", "i", "t", ...
        raise TypeError("selected_sites must be a list of site ids, not one string")
    if selected_sites is not None:
        payload["selected_sites"] = list(selected_sites)
    return run_dagentsc(["federation", "aggregate", "readiness", "--input", "-"], payload)


def evaluate_release(
    gates: list[dict[str, Any]],
    candidate_metrics: dict[str, float],
    baseline_metrics: dict[str, float],
    candidate_version: str,
    rollback_version: str | None,
    round_id: str,
) -> dict[str, Any]:
    """Evaluate release gates against a candidate model's metrics."""
    return run_dagentsc(
        ["federation", "release", "evaluate", "--input", "-"],
        {
            "gates": gates,
            "candidate_metrics": candidate_metrics,
            "baseline_metrics": baseline_metrics,
            "candidate_version": candidate_version,
            "rollback_version": rollback_version,
            "round_id": round_id,
        },
    )


def model_lifecycle(version: str, event: dict[str, Any]) -> dict[str, Any]:
    """Decide the status changes that follow one event on one model version.

    ``event["kind"]`` is one of:

    - ``registered``: ``requested_status`` (``candidate``, the default, or
      ``released``) and ``released_version``.
    - ``aggregated``: ``parent_version`` and ``parent_status``.
    - ``release_evaluated``: ``status``, ``action`` and ``guard_permitted``.
    - ``approval_requested``: ``status``, ``decision`` (the stored release
      decision, with ``action``, ``guard_permitted``, ``metrics_source`` and
      ``baseline_version``, or ``None``) and ``released_version``.

    Returns ``allowed``, ``refusal`` (``None`` or a kind such as
    ``not_candidate``), ``reason`` and ``status_changes`` (``version``,
    ``from_status``, ``to_status``), sorted by version. A refusal is a normal
    answer. Raises :class:`PlannerUnavailableError` when the planner cannot be
    run or cannot read the event.
    """
    return run_dagentsc(
        ["federation", "model", "lifecycle", "--input", "-"],
        {"version": version, "event": event},
    )
