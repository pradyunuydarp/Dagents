"""Subprocess wrapper for the OCaml dagentsc compiler.

:func:`dagentsc_binary` says which planner executable is started.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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
    an error, and when its output is not the JSON the caller expects. It is a
    RuntimeError, so callers that already catch RuntimeError keep working. A
    service maps it to a 503, because the request may succeed once the planner
    is back.
    """


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


def planner_problem(found: tuple[str | None, str] | None = None) -> str | None:
    """Why the runner cannot start a planner, or None when it can.

    Params:
    - `found`: the result of :func:`find_planner`; ``None`` looks it up now.

    It checks that the file exists and may be run, and does not start it. The
    bundled planner and ``dagentsc`` on PATH are checked by the lookup itself,
    so only a ``DAGENTSC_BIN`` setting can name a file that cannot run.
    """
    path, source = find_planner() if found is None else found
    if path is None:
        return "no planner was found: DAGENTSC_BIN is not set, this installation bundles none, and dagentsc is not on PATH"
    if source == "DAGENTSC_BIN" and not (os.path.isfile(path) and os.access(path, os.X_OK)):
        return f"DAGENTSC_BIN names {path}, which is not a file this process can run"
    return None


def warn_if_no_planner(log: logging.Logger, agent_name: str) -> str | None:
    """Log one warning when the runner cannot start a planner, and return the reason.

    An agent starts and answers ``/health`` without a planner, and pip hides
    the build's notice when a wheel without one is installed. This warning is
    the one place that tells the operator why every governed request is
    denied. Returns None, and logs nothing, when a planner can be started.
    """
    problem = planner_problem()
    if problem is not None:
        log.warning(
            "%s has no planner: %s. The Ethical Guard denies every request until DAGENTSC_BIN names a "
            "dagentsc binary or dagentsc is on PATH. Run python -m dagents to see which planner the runner finds.",
            agent_name,
            problem,
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

def _execute(full_cmd: list[str], input_str: str | None = None) -> Any:
    """Run the planner once and return its output with snake_case keys.

    Raises :class:`PlannerUnavailableError` when the binary cannot be started,
    exits with an error or prints output that is not JSON.
    """
    try:
        if input_str is None:
            result = subprocess.run(full_cmd, text=True, capture_output=True)
        else:
            result = subprocess.run(full_cmd, input=input_str, text=True, capture_output=True)
    except OSError as error:
        # FileNotFoundError, PermissionError and the like: the binary is missing
        # or cannot be run.
        raise PlannerUnavailableError(f"dagentsc could not be started: {error}") from error
    if result.returncode != 0:
        raise PlannerUnavailableError(
            f"dagentsc execution failed: {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        output_json = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PlannerUnavailableError(f"dagentsc returned invalid json: {error}") from error
    return convert_keys(output_json, to_snake_case)


def run_dagentsc(command: list[str], payload: dict[str, Any]) -> Any:
    """Execute the dagentsc binary with the given command and JSON payload.

    Raises :class:`PlannerUnavailableError` when the planner cannot be run or
    its output is not JSON.
    """
    camel_payload = convert_keys(payload, to_camel_case)
    input_str = json.dumps(camel_payload)
    # Base command is something like ['manifest', 'compile', '--input', '-', '--output', 'json'].
    # dagentsc_binary() says which planner runs: DAGENTSC_BIN, the bundled one, or PATH.
    full_cmd = [dagentsc_binary()] + command
    return _execute(full_cmd, input_str)

def run_dagentsc_with_files(command: list[str], payloads: dict[str, Any]) -> Any:
    """Execute dagentsc commands that require multiple JSON file inputs.

    Raises :class:`PlannerUnavailableError` like :func:`run_dagentsc`.
    """
    temp_paths: list[str] = []
    try:
        resolved_command = list(command)
        for flag, payload in payloads.items():
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
                if flag == "--records":
                    json.dump(payload, handle)
                else:
                    json.dump(convert_keys(payload, to_camel_case), handle)
                temp_paths.append(handle.name)
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
