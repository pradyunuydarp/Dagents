"""Subprocess wrapper for the OCaml dagentsc compiler."""

import json
import os
import subprocess
import tempfile
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
}

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

def dagentsc_binary() -> str:
    """Resolve the dagentsc executable used by Python service adapters."""
    return os.getenv("DAGENTSC_BIN", "dagentsc")

def run_dagentsc(command: list[str], payload: dict[str, Any]) -> Any:
    """Execute the dagentsc binary with the given command and JSON payload."""
    camel_payload = convert_keys(payload, to_camel_case)
    input_str = json.dumps(camel_payload)
    
    # Base command is something like ['manifest', 'compile', '--input', '-', '--output', 'json'].
    # Containers place dagentsc on PATH; local demos can set DAGENTSC_BIN to the dune-built binary.
    full_cmd = [dagentsc_binary()] + command
    
    result = subprocess.run(
        full_cmd,
        input=input_str,
        text=True,
        capture_output=True,
    )
    
    if result.returncode != 0:
        raise RuntimeError(f"dagentsc execution failed: {result.stderr.strip() or result.stdout.strip()}")
        
    try:
        output_json = json.loads(result.stdout)
        return convert_keys(output_json, to_snake_case)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"dagentsc returned invalid json: {e}")

def run_dagentsc_with_files(command: list[str], payloads: dict[str, Any]) -> Any:
    """Execute dagentsc commands that require multiple JSON file inputs."""
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
        result = subprocess.run(full_cmd, text=True, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"dagentsc execution failed: {result.stderr.strip() or result.stdout.strip()}")
        return convert_keys(json.loads(result.stdout), to_snake_case)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"dagentsc returned invalid json: {e}")
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
    :class:`~agents.common.application.ethical_guard.EthicalGuard`, which also
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


def evaluate_aggregation_readiness(manifest: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Decide whether returned contributions may be aggregated."""
    return run_dagentsc(
        ["federation", "aggregate", "readiness", "--input", "-"],
        {"manifest": manifest, "results": results},
    )


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
