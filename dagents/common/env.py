"""Minimal dotenv loader shared across Dagents services.

The agents and services read ``env/.env.shared`` and their own
``env/.env.<service>`` from the folder that holds the ``dagents`` package: the
repository root in a checkout, or ``/app`` in an image that copies
``dagents/`` and ``env/`` side by side.

An installed package (``pip install dagents``) reads no env file. Its folder is
``site-packages``, where any other distribution could install an ``env/``
folder and so change an agent's settings. There the settings come only from
the process environment.
"""

from __future__ import annotations

from pathlib import Path
import os


REPO_ROOT = Path(__file__).resolve().parents[2]

#: Folders installers put packages in.
INSTALL_FOLDERS = frozenset({"site-packages", "dist-packages"})


def reads_env_files(root: Path = REPO_ROOT) -> bool:
    """Whether ``root``, the folder that holds ``dagents/``, is a source tree whose env files may be read.

    False when ``root`` is an installer's folder, or when an installer left a
    ``dagents-<version>.dist-info`` folder next to the package.
    """
    if root.name in INSTALL_FOLDERS:
        return False
    return not any(root.glob("dagents-*.dist-info"))


def load_env_files(*relative_paths: str, root: Path | None = None) -> None:
    """Load env files without overriding already exported variables.

    Params:
    - `relative_paths`: files relative to ``root``, such as ``env/.env.lma``.
      A missing file is skipped.
    - `root`: the folder that holds ``dagents/``; ``None`` means this
      package's own folder. Nothing is read when it is an installed package
      (:func:`reads_env_files`).
    """
    base = REPO_ROOT if root is None else root
    if not reads_env_files(base):
        return
    candidates = [base / path for path in relative_paths]
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if value and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
