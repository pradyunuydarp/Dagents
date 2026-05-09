"""Subprocess wrapper for the OCaml dagentsc compiler."""

import json
import os
import subprocess
import tempfile
from typing import Any

DATA_RECORD_KEYS = {"records", "inlineRecords"}
DATA_MAP_KEYS = {"schemaHint", "options", "connectionOptions", "config", "configJson"}

def to_camel_case(snake_str: str) -> str:
    components = snake_str.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])

def to_snake_case(camel_str: str) -> str:
    return ''.join(['_' + c.lower() if c.isupper() else c for c in camel_str]).lstrip('_')

def convert_keys(obj: Any, convert_func) -> Any:
    if isinstance(obj, list):
        return [convert_keys(item, convert_func) for item in obj]
    elif isinstance(obj, dict):
        converted: dict[str, Any] = {}
        for key, value in obj.items():
            converted_key = convert_func(key)
            if converted_key in DATA_RECORD_KEYS or converted_key in DATA_MAP_KEYS:
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
