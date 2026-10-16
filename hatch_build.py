"""Hatch build hook: put the OCaml planner into a platform wheel of ``dagents``.

The Python code is the same on every platform. Only the planner, ``dagentsc``,
is a native program, so a wheel that carries it is tagged for one platform.

To build a platform wheel, set two variables before ``python -m build --wheel``:

| Variable | Meaning |
|---|---|
| ``DAGENTS_PLANNER_BINARY`` | Path to a ``dagentsc`` built for the target platform |
| ``DAGENTS_WHEEL_PLATFORM`` | The wheel's platform tag, such as ``manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64`` or ``macosx_11_0_arm64`` |

The hook copies the binary to ``dagents/_bin/dagentsc`` and tags the wheel
``py3-none-<platform>``. It refuses a binary whose format or processor does not
match the tag, and a Linux binary that is not statically linked, because a
manylinux or musllinux tag promises that the binary runs on any distribution.

Without ``DAGENTS_PLANNER_BINARY`` the wheel is pure Python (``py3-none-any``)
and has no planner. That is what pip builds from the sdist. The hook prints a
notice, because without a planner the Ethical Guard denies every request until
``DAGENTSC_BIN`` or ``dagentsc`` on PATH provides one.

The checks are plain functions, so ``tests/test_packaging.py`` runs them
without hatchling installed.
"""

from __future__ import annotations

import os
import re
import struct
import sys
from pathlib import Path
from typing import Any

try:
    from hatchling.builders.hooks.plugin.interface import BuildHookInterface
except ImportError:  # tests/test_packaging.py imports the checks without hatchling
    BuildHookInterface = object  # type: ignore[assignment,misc]

BINARY_VARIABLE = "DAGENTS_PLANNER_BINARY"
PLATFORM_VARIABLE = "DAGENTS_WHEEL_PLATFORM"

#: Where the binary goes inside the wheel. ``dagents_runner.BUNDLED_PLANNER``
#: looks for it here.
BUNDLED_PATH = "dagents/_bin/dagentsc"

NO_PLANNER_NOTICE = (
    "dagents: building a wheel without the planner (dagentsc). The Ethical Guard denies every request "
    "until DAGENTSC_BIN or dagentsc on PATH provides one. Platform wheels on PyPI and the GitHub Release "
    "include a planner for Linux x86_64, Linux aarch64 and macOS arm64."
)

#: One platform tag: the system it names, and the processor at the end.
_LINUX_TAG = re.compile(r"^(manylinux_\d+_\d+|manylinux2014|musllinux_\d+_\d+)_(x86_64|aarch64)$")
_MACOS_TAG = re.compile(r"^macosx_\d+_\d+_(x86_64|arm64)$")

#: ELF e_machine values and Mach-O CPU types, by the processor name a wheel tag uses.
_ELF_MACHINES = {0x3E: "x86_64", 0xB7: "aarch64"}
_MACHO_CPUS = {0x01000007: "x86_64", 0x0100000C: "arm64"}
_PT_INTERP = 3
#: Mach-O load commands that record the oldest macOS a binary runs on.
_LC_VERSION_MIN_MACOSX = 0x24
_LC_BUILD_VERSION = 0x32


class PlannerBinaryError(ValueError):
    """The planner binary or the platform tag cannot make a correct wheel."""


def macos_minimum(data: bytes) -> tuple[int, int] | None:
    """The oldest macOS version a 64-bit Mach-O binary runs on, as ``(major, minor)``.

    Read from ``LC_BUILD_VERSION`` or ``LC_VERSION_MIN_MACOSX``; None when the
    binary records neither.
    """
    (ncmds,) = struct.unpack_from("<I", data, 16)
    offset = 32
    for _ in range(ncmds):
        if offset + 8 > len(data):
            raise PlannerBinaryError("the planner's Mach-O load commands are cut off")
        command, size = struct.unpack_from("<II", data, offset)
        if command == _LC_BUILD_VERSION:
            (packed,) = struct.unpack_from("<I", data, offset + 12)
            return packed >> 16, (packed >> 8) & 0xFF
        if command == _LC_VERSION_MIN_MACOSX:
            (packed,) = struct.unpack_from("<I", data, offset + 8)
            return packed >> 16, (packed >> 8) & 0xFF
        if size < 8:
            raise PlannerBinaryError("the planner has a Mach-O load command shorter than its header")
        offset += size
    return None


def binary_format(data: bytes) -> tuple[str, str, bool]:
    """Read ``(system, processor, static)`` from the start of an executable.

    ``system`` is ``linux`` for an ELF file and ``macos`` for a 64-bit Mach-O
    file. ``static`` is True for an ELF file without an interpreter (no
    ``PT_INTERP`` program header); a Mach-O file always reports False. Raises
    :class:`PlannerBinaryError` for any other file.
    """
    if data[:4] == b"\x7fELF":
        if data[4] != 2:
            raise PlannerBinaryError("the planner is a 32-bit ELF file; only 64-bit builds are packaged")
        endian = "<" if data[5] == 1 else ">"
        (machine,) = struct.unpack_from(endian + "H", data, 18)
        processor = _ELF_MACHINES.get(machine)
        if processor is None:
            raise PlannerBinaryError(f"the planner is built for ELF machine {machine:#x}, which is not packaged")
        (phoff,) = struct.unpack_from(endian + "Q", data, 32)
        phentsize, phnum = struct.unpack_from(endian + "HH", data, 54)
        static = True
        for index in range(phnum):
            offset = phoff + index * phentsize
            if offset + 4 > len(data):
                raise PlannerBinaryError("the planner's ELF program headers are cut off")
            (kind,) = struct.unpack_from(endian + "I", data, offset)
            if kind == _PT_INTERP:
                static = False
        return "linux", processor, static
    if data[:4] == b"\xcf\xfa\xed\xfe":
        (cpu,) = struct.unpack_from("<I", data, 4)
        processor = _MACHO_CPUS.get(cpu)
        if processor is None:
            raise PlannerBinaryError(f"the planner is built for Mach-O CPU type {cpu:#x}, which is not packaged")
        return "macos", processor, False
    raise PlannerBinaryError("the planner is neither a 64-bit ELF nor a 64-bit Mach-O executable")


def check_platform(platform: str, data: bytes) -> str:
    """Check that a binary may go into a wheel with this platform tag; return the tag.

    ``platform`` may hold several tags joined by dots (a compressed tag set).
    Every tag must name the binary's system and processor. A Linux tag needs
    a statically linked binary, and a macOS tag may not name a version older
    than the oldest macOS the binary records.
    """
    platform = platform.strip()
    if not platform:
        raise PlannerBinaryError(f"{PLATFORM_VARIABLE} is empty; it must name the wheel's platform tag")
    system, processor, static = binary_format(data)
    for tag in platform.split("."):
        linux = _LINUX_TAG.match(tag)
        macos = _MACOS_TAG.match(tag)
        if linux:
            tag_system, tag_processor = "linux", linux.group(2)
        elif macos:
            tag_system, tag_processor = "macos", macos.group(1)
        else:
            raise PlannerBinaryError(f"{tag!r} is not a manylinux, musllinux or macosx platform tag this build supports")
        if (tag_system, tag_processor) != (system, processor):
            raise PlannerBinaryError(f"the planner is a {system} {processor} binary, but the tag {tag!r} names {tag_system} {tag_processor}")
        if tag_system == "linux" and not static:
            raise PlannerBinaryError(
                f"the tag {tag!r} needs a statically linked planner; build it with "
                "`dune build --profile static ./bin/dagentsc.exe` in bindings/ocaml"
            )
        if tag_system == "macos":
            minimum = macos_minimum(data)
            tag_version = tuple(int(part) for part in tag.split("_")[1:3])
            if minimum is not None and tag_version < minimum:
                raise PlannerBinaryError(
                    f"the planner needs macOS {minimum[0]}.{minimum[1]} or later, but the tag {tag!r} names "
                    f"{tag_version[0]}.{tag_version[1]}; set MACOSX_DEPLOYMENT_TARGET before building the planner, "
                    "or use a later tag"
                )
    return platform


def planner_build(environ: dict[str, str] | os._Environ[str]) -> tuple[Path, str] | None:
    """The planner binary and platform tag to build with, or None for a wheel without a planner."""
    binary = (environ.get(BINARY_VARIABLE) or "").strip()
    platform = (environ.get(PLATFORM_VARIABLE) or "").strip()
    if not binary:
        if platform:
            raise PlannerBinaryError(f"{PLATFORM_VARIABLE} is set but {BINARY_VARIABLE} is not; set both or neither")
        return None
    path = Path(binary)
    if not path.is_file():
        raise PlannerBinaryError(f"{BINARY_VARIABLE} names no file: {binary}")
    if not os.access(path, os.X_OK):
        raise PlannerBinaryError(f"{BINARY_VARIABLE} names a file that is not executable: {binary}")
    with path.open("rb") as handle:
        head = handle.read(64 * 1024)
    return path, check_platform(platform, head)


class PlannerBuildHook(BuildHookInterface):  # type: ignore[misc,valid-type]
    """Adds the planner to a wheel when ``DAGENTS_PLANNER_BINARY`` is set."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        if self.target_name != "wheel":
            return
        found = planner_build(os.environ)
        if found is None:
            print(NO_PLANNER_NOTICE, file=sys.stderr)
            return
        binary, platform = found
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{platform}"
        build_data["force_include"][str(binary)] = BUNDLED_PATH
