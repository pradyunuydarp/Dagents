"""Subprocess wrapper for the OCaml dagentsc compiler."""

import json
import subprocess
from typing import Any

def to_camel_case(snake_str: str) -> str:
    components = snake_str.split('_')
    return components[0] + ''.join(x.title() for x in components[1:])

def to_snake_case(camel_str: str) -> str:
    return ''.join(['_' + c.lower() if c.isupper() else c for c in camel_str]).lstrip('_')

def convert_keys(obj: Any, convert_func) -> Any:
    if isinstance(obj, list):
        return [convert_keys(item, convert_func) for item in obj]
    elif isinstance(obj, dict):
        return {convert_func(key): convert_keys(value, convert_func) for key, value in obj.items()}
    else:
        return obj

def run_dagentsc(command: list[str], payload: dict[str, Any]) -> dict[str, Any]:
    """Execute the dagentsc binary with the given command and JSON payload."""
    camel_payload = convert_keys(payload, to_camel_case)
    input_str = json.dumps(camel_payload)
    
    # Base command is something like ['manifest', 'compile', '--input', '-', '--output', 'json']
    # The dagentsc binary should be in the PATH of the container.
    full_cmd = ["dagentsc"] + command
    
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
