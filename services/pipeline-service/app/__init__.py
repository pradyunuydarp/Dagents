"""Dagents pipeline service package."""
"""Bootstrap shared Dagents imports for the pipeline service."""

from pathlib import Path
import sys


current = Path(__file__).resolve()
REPO_ROOT = next((parent for parent in current.parents if (parent / "agents").exists()), current.parents[1])
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))
