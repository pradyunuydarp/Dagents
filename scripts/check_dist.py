"""Check the built distributions of the ``dagents`` package before they are published.

``twine check`` reads the metadata; this script reads the files. It fails when:

- a file in a wheel is outside ``dagents/`` and its ``.dist-info`` folder, so
  no other top-level module (such as ``agents``) can ship;
- a wheel holds tests, Markdown, a Dockerfile, a requirements file or bytecode;
- a wheel's version, its ``WHEEL`` tag or its ``RECORD`` hashes disagree with
  the file name or the files;
- a platform wheel lacks the planner at ``dagents/_bin/dagentsc``, the planner
  is not executable, or its format and processor differ from the tag (the
  same checks as ``hatch_build.py``);
- a pure wheel (``py3-none-any``) holds a planner;
- the metadata does not have exactly one ``Development Status`` classifier,
  the one for the version's stage (``release_version.status_classifier``);
- the description, which PyPI shows and never lets a release change, does
  not show ``pip install dagents``;
- the sdist holds anything but the package, ``pyproject.toml``,
  ``hatch_build.py``, the README, the licence and ``PKG-INFO``.

Usage:
    python scripts/check_dist.py dist/                      # every wheel and sdist in dist/
    python scripts/check_dist.py dist/ --list               # also print every file in each wheel
    python scripts/check_dist.py dist/ --require-platform manylinux_2_17_x86_64
    python scripts/check_dist.py dist/ --build-constraints .github/build-constraints.txt

``--require-platform TAG`` fails unless a wheel whose platform tags include
TAG is present. ``--build-constraints FILE`` fails unless each wheel was
built by the hatchling version that file pins, which shows that the build
used the pinned backend. The version must equal ``__version__`` in
``dagents/__init__.py``.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import re
import sys
import tarfile
import zipfile
from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from hatch_build import BUNDLED_PATH, PlannerBinaryError, check_platform  # noqa: E402
from release_version import ReleaseError, read_version, status_classifier  # noqa: E402

PACKAGE = "dagents"
WHEEL_NAME = re.compile(r"^dagents-(?P<version>[^-]+)-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$")
SDIST_NAME = re.compile(r"^dagents-(?P<version>[^-]+)\.tar\.gz$")

#: Paths a wheel must contain.
REQUIRED_MODULES = (
    "dagents/__init__.py",
    "dagents/__main__.py",
    "dagents/common/infrastructure/dagents_runner.py",
    "dagents/common/application/ethical_guard.py",
    "dagents/common/application/federation.py",
    "dagents/lma/main.py",
    "dagents/lma/__main__.py",
    "dagents/gma/main.py",
    "dagents/gma/__main__.py",
)

FORBIDDEN_IN_WHEEL = re.compile(r"(^|/)(tests/|__pycache__/)|\.pyc$|\.md$|(^|/)Dockerfile$|(^|/)requirements[^/]*\.txt$")

SDIST_TOP_LEVEL = {"PKG-INFO", "pyproject.toml", "hatch_build.py", "README.md", "LICENSE", ".gitignore"}

#: Words the description must hold, so the PyPI page says how to install the package.
INSTALL_COMMAND = "pip install dagents"


def pinned_backend(constraints: Path) -> str:
    """The ``Generator`` a wheel built with these constraints names, such as ``hatchling 1.32.4``.

    Raises ``ValueError`` when the file has no exact ``hatchling==`` pin.
    """
    match = re.search(r"^hatchling==(\S+)\s*$", constraints.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise ValueError(f"{constraints} has no exact pin of the form hatchling==X.Y.Z")
    return f"hatchling {match.group(1)}"


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.errors.append(message)


def check_wheel(path: Path, version: str, report: Report, *, list_files: bool = False, backend: str | None = None) -> list[str]:
    """Check one wheel; return its platform tags.

    ``backend``, when given, is the ``Generator`` the wheel's ``WHEEL`` file must name.
    """
    name = WHEEL_NAME.match(path.name)
    if name is None:
        report.fail(f"{path.name}: not a dagents wheel file name")
        return []
    if name["version"] != version:
        report.fail(f"{path.name}: version {name['version']} differs from __version__ {version}")
    platforms = name["platform"].split(".")
    pure = platforms == ["any"]
    dist_info = f"{PACKAGE}-{name['version']}.dist-info/"
    with zipfile.ZipFile(path) as archive:
        infos = {info.filename: info for info in archive.infolist() if not info.is_dir()}
        for member in sorted(infos):
            if not (member.startswith(f"{PACKAGE}/") or member.startswith(dist_info)):
                report.fail(f"{path.name}: {member} is outside {PACKAGE}/ and {dist_info}")
            elif member.startswith(f"{PACKAGE}/") and FORBIDDEN_IN_WHEEL.search(member):
                report.fail(f"{path.name}: {member} must not be in a wheel")
        for required in REQUIRED_MODULES:
            if required not in infos:
                report.fail(f"{path.name}: {required} is missing")
        _check_record(path.name, archive, infos, dist_info, report)
        wheel = Parser().parsestr(archive.read(dist_info + "WHEEL").decode("utf-8"))
        expected_tags = {f"{name['python']}-{name['abi']}-{platform}" for platform in platforms}
        if set(wheel.get_all("Tag") or []) != expected_tags:
            report.fail(f"{path.name}: WHEEL tags {wheel.get_all('Tag')} differ from the file name")
        generator = (wheel.get("Generator") or "").strip()
        if backend is not None and generator != backend:
            report.fail(f"{path.name}: built by {generator or 'an unnamed backend'}, not by the pinned {backend}")
        if (wheel.get("Root-Is-Purelib", "").strip().lower() == "true") != pure:
            report.fail(f"{path.name}: Root-Is-Purelib does not match a {'pure' if pure else 'platform'} wheel")
        metadata = Parser().parsestr(archive.read(dist_info + "METADATA").decode("utf-8"))
        _check_metadata(path.name, metadata, version, report)
        planner = infos.get(BUNDLED_PATH)
        if pure:
            if planner is not None:
                report.fail(f"{path.name}: a py3-none-any wheel must not hold {BUNDLED_PATH}")
            planner_text = "no planner"
        elif planner is None:
            report.fail(f"{path.name}: a platform wheel must hold the planner at {BUNDLED_PATH}")
            planner_text = "planner missing"
        else:
            if not (planner.external_attr >> 16) & 0o111:
                report.fail(f"{path.name}: {BUNDLED_PATH} is not marked executable")
            try:
                check_platform(name["platform"], archive.read(BUNDLED_PATH)[: 64 * 1024])
            except PlannerBinaryError as error:
                report.fail(f"{path.name}: {error}")
            planner_text = f"planner {BUNDLED_PATH} ({planner.file_size:,} bytes)"
        python_files = sum(1 for member in infos if member.endswith(".py"))
        report.lines.append(
            f"{path.name}: {path.stat().st_size:,} bytes, {len(infos)} files, {python_files} Python modules, {planner_text}"
        )
        if list_files:
            for member in sorted(infos):
                report.lines.append(f"    {infos[member].file_size:>9,}  {member}")
    return platforms


def _check_record(wheel: str, archive: zipfile.ZipFile, infos: dict, dist_info: str, report: Report) -> None:
    record_name = dist_info + "RECORD"
    if record_name not in infos:
        report.fail(f"{wheel}: {record_name} is missing")
        return
    listed: dict[str, str] = {}
    for row in csv.reader(io.StringIO(archive.read(record_name).decode("utf-8"))):
        if row:
            listed[row[0]] = row[1]
    for member in infos:
        if member == record_name:
            continue
        expected = listed.get(member)
        if not expected:
            report.fail(f"{wheel}: {member} is not listed in RECORD")
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(archive.read(member)).digest()).rstrip(b"=").decode()
        if expected != f"sha256={digest}":
            report.fail(f"{wheel}: the RECORD hash of {member} is wrong")
    for member in listed:
        if member not in infos:
            report.fail(f"{wheel}: RECORD lists {member}, which is not in the wheel")


def _check_metadata(source: str, metadata, version: str, report: Report) -> None:
    if metadata.get("Name") != PACKAGE:
        report.fail(f"{source}: metadata Name is {metadata.get('Name')!r}, not {PACKAGE!r}")
    if metadata.get("Version") != version:
        report.fail(f"{source}: metadata Version is {metadata.get('Version')!r}, not {version!r}")
    if metadata.get("License-Expression") != "MIT":
        report.fail(f"{source}: metadata License-Expression is {metadata.get('License-Expression')!r}, not 'MIT'")
    if not metadata.get("Requires-Python"):
        report.fail(f"{source}: metadata has no Requires-Python")
    statuses = [value for value in metadata.get_all("Classifier") or [] if value.startswith("Development Status ::")]
    try:
        wanted = status_classifier(version)
    except ReleaseError as error:
        report.fail(f"{source}: {error}")
    else:
        if statuses != [wanted]:
            report.fail(f"{source}: version {version} needs exactly one Development Status classifier, {wanted!r}; found {statuses}")
    if INSTALL_COMMAND not in (metadata.get_payload() or ""):
        report.fail(f"{source}: the description does not show {INSTALL_COMMAND!r}; PyPI shows it for this release forever")


def check_sdist(path: Path, version: str, report: Report) -> None:
    name = SDIST_NAME.match(path.name)
    if name is None:
        report.fail(f"{path.name}: not a dagents sdist file name")
        return
    if name["version"] != version:
        report.fail(f"{path.name}: version {name['version']} differs from __version__ {version}")
    root = f"{PACKAGE}-{name['version']}/"
    with tarfile.open(path) as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        for member in members:
            if not member.name.startswith(root):
                report.fail(f"{path.name}: {member.name} is outside {root}")
                continue
            inner = member.name[len(root):]
            if "/" not in inner:
                if inner not in SDIST_TOP_LEVEL:
                    report.fail(f"{path.name}: {inner} must not be in the sdist")
            elif not inner.startswith(f"{PACKAGE}/"):
                report.fail(f"{path.name}: {inner} must not be in the sdist")
            elif re.search(r"^dagents/(tests|_bin)/|__pycache__/|\.pyc$", inner):
                report.fail(f"{path.name}: {inner} must not be in the sdist")
        names = {member.name[len(root):] for member in members}
        for required in ("pyproject.toml", "hatch_build.py", "README.md", "LICENSE", "PKG-INFO", *REQUIRED_MODULES):
            if required not in names:
                report.fail(f"{path.name}: {required} is missing")
        if f"{root}PKG-INFO" in archive.getnames():
            metadata = Parser().parsestr(archive.extractfile(f"{root}PKG-INFO").read().decode("utf-8"))
            _check_metadata(path.name, metadata, version, report)
        report.lines.append(f"{path.name}: {path.stat().st_size:,} bytes, {len(members)} files")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dist", type=Path, help="folder with the wheels and the sdist")
    parser.add_argument("--list", action="store_true", help="print every file in each wheel")
    parser.add_argument(
        "--require-platform",
        action="append",
        default=[],
        metavar="TAG",
        help="fail unless a wheel for this platform tag is present (repeatable)",
    )
    parser.add_argument(
        "--build-constraints",
        type=Path,
        metavar="FILE",
        help="fail unless each wheel was built by the hatchling version this constraints file pins",
    )
    parser.add_argument("--version-file", type=Path, default=REPO_ROOT / "dagents" / "__init__.py", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    report = Report()
    try:
        version = read_version(args.version_file)
        backend = pinned_backend(args.build_constraints) if args.build_constraints is not None else None
    except (ReleaseError, ValueError, OSError) as error:
        print(f"check_dist: {error}", file=sys.stderr)
        return 1
    wheels = sorted(args.dist.glob("*.whl"))
    sdists = sorted(args.dist.glob("*.tar.gz"))
    if not wheels and not sdists:
        report.fail(f"{args.dist} holds no wheel and no sdist")
    seen_platforms: set[str] = set()
    for wheel in wheels:
        seen_platforms.update(check_wheel(wheel, version, report, list_files=args.list, backend=backend))
    for sdist in sdists:
        check_sdist(sdist, version, report)
    for platform in args.require_platform:
        if platform not in seen_platforms:
            report.fail(f"no wheel for the platform {platform}")
    for line in report.lines:
        print(line)
    for error in report.errors:
        print(f"FAIL {error}", file=sys.stderr)
    if report.errors:
        print(f"check_dist: {len(report.errors)} problem(s)", file=sys.stderr)
        return 1
    print(f"check_dist: {len(wheels)} wheel(s) and {len(sdists)} sdist(s) for dagents {version} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
