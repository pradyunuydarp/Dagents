"""Dagents model service package."""
"""Bootstrap shared Dagents imports for the model service."""

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))
