"""Check that a package index serves exactly the files a release built.

After an upload, the release workflow runs this against TestPyPI before it
publishes to PyPI, and against PyPI before it creates the GitHub Release.
It reads the index's JSON API (``<index>/pypi/dagents/<version>/json``),
waits until the release lists every local file, and then fails when:

- a local file is missing from the index after the wait;
- the index lists a file for this version that was not built in this run;
- the SHA-256 the index reports for a file differs from the local file's;
- a file downloaded from the index differs from the local file.

Usage:
    python scripts/check_published_files.py --index https://test.pypi.org --version 0.1.0a1 dist/

Exit status 0 means the index serves the same bytes as ``dist/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PACKAGE = "dagents"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str, timeout: float = 30.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "dagents-release-check"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def listed_files(index: str, version: str) -> dict[str, tuple[str, str]]:
    """``filename -> (sha256, url)`` for every file the index lists for this version; empty when none yet."""
    url = f"{index.rstrip('/')}/pypi/{PACKAGE}/{version}/json"
    try:
        document = json.loads(fetch(url).decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {}
        raise
    return {entry["filename"]: (entry["digests"]["sha256"], entry["url"]) for entry in document.get("urls", [])}


def check(index: str, version: str, dist: Path, *, wait_seconds: float = 300.0, poll_seconds: float = 15.0) -> list[str]:
    """Compare the index with ``dist``; return the problems found."""
    local = {path.name: sha256(path.read_bytes()) for path in sorted(dist.iterdir()) if path.suffix in {".whl", ".gz"}}
    if not local:
        return [f"{dist} holds no wheel and no sdist"]
    deadline = time.monotonic() + wait_seconds
    while True:
        served = listed_files(index, version)
        missing = sorted(set(local) - set(served))
        if not missing or time.monotonic() >= deadline:
            break
        print(f"{index} does not list {', '.join(missing)} yet; checking again in {poll_seconds:.0f} seconds")
        time.sleep(poll_seconds)
    problems = [f"{index} does not list {name}" for name in missing]
    problems += [f"{index} lists {name}, which this run did not build" for name in sorted(set(served) - set(local))]
    for name in sorted(set(local) & set(served)):
        digest, url = served[name]
        if digest != local[name]:
            problems.append(f"{name}: the index reports sha256 {digest}, the local file is {local[name]}")
            continue
        if sha256(fetch(url, timeout=120.0)) != local[name]:
            problems.append(f"{name}: the file downloaded from the index differs from the local file")
            continue
        print(f"ok  {name}  sha256 {digest}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", required=True, help="the index's base address, such as https://test.pypi.org")
    parser.add_argument("--version", required=True)
    parser.add_argument("--wait", type=float, default=300.0, help="seconds to wait for the index to list every file")
    parser.add_argument("--poll", type=float, default=15.0, help=argparse.SUPPRESS)
    parser.add_argument("dist", type=Path)
    args = parser.parse_args(argv)
    if not args.index.startswith("https://") and not args.index.startswith("http://127.0.0.1:"):
        print("check_published_files: the index address must be https", file=sys.stderr)
        return 2
    problems = check(args.index, args.version, args.dist, wait_seconds=args.wait, poll_seconds=args.poll)
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        return 1
    count = sum(1 for path in args.dist.iterdir() if path.suffix in {".whl", ".gz"})
    print(f"check_published_files: {args.index} serves the {count} files built for {PACKAGE} {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
