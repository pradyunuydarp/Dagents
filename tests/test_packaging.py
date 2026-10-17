"""The PyPI package: its metadata, the build hook, the release version check and the distribution checker.

None of these tests builds a wheel, because a build downloads hatchling. The
``packaging`` job in ``.github/workflows/ci.yml`` builds the sdist and a Linux
wheel with the planner, runs ``scripts/check_dist.py`` on them and installs the
wheel into a clean virtual environment (``scripts/smoke_installed_package.py``).
These tests check the pieces those steps rely on, with synthetic inputs.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import http.server
import io
import json
import threading
import os
import re
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from email.parser import Parser
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import check_dist  # noqa: E402
import check_published_files  # noqa: E402
import hatch_build  # noqa: E402
import release_version  # noqa: E402

import dagents  # noqa: E402
from dagents.common.infrastructure import dagents_runner  # noqa: E402

PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = dagents.__version__


def elf(machine: int = 0x3E, *, interpreter: bool = False) -> bytes:
    """A minimal little-endian ELF64 header with one or two program headers."""
    phoff, phentsize = 64, 56
    kinds = [1] + ([3] if interpreter else [])  # PT_LOAD, then PT_INTERP for a dynamic binary
    header = bytearray(64)
    header[:4] = b"\x7fELF"
    header[4], header[5], header[6] = 2, 1, 1
    struct.pack_into("<HHI", header, 16, 2, machine, 1)
    struct.pack_into("<Q", header, 32, phoff)
    struct.pack_into("<HH", header, 54, phentsize, len(kinds))
    tables = b"".join(struct.pack("<I", kind) + bytes(phentsize - 4) for kind in kinds)
    return bytes(header) + tables


def macho(cpu: int = 0x0100000C, minimum: tuple[int, int] | None = (11, 0)) -> bytes:
    """A minimal 64-bit Mach-O header, with an LC_BUILD_VERSION command when ``minimum`` is given."""
    commands = b""
    if minimum is not None:
        packed = (minimum[0] << 16) | (minimum[1] << 8)
        commands = struct.pack("<IIIIII", 0x32, 24, 1, packed, packed, 0)
    header = b"\xcf\xfa\xed\xfe" + struct.pack("<IIIIIII", cpu, 0, 2, 1 if commands else 0, len(commands), 0, 0)
    return header + commands


LINUX_X86 = "manylinux_2_17_x86_64.manylinux2014_x86_64.musllinux_1_1_x86_64"


class BuildHookTests(unittest.TestCase):
    """``hatch_build.py`` puts only a matching planner into a wheel."""

    def test_a_static_x86_64_binary_fits_the_linux_tags(self) -> None:
        self.assertEqual(LINUX_X86, hatch_build.check_platform(LINUX_X86, elf()))

    def test_an_aarch64_binary_fits_the_aarch64_tag_only(self) -> None:
        hatch_build.check_platform("manylinux_2_17_aarch64", elf(0xB7))
        with self.assertRaisesRegex(hatch_build.PlannerBinaryError, "aarch64"):
            hatch_build.check_platform("manylinux_2_17_x86_64", elf(0xB7))

    def test_a_dynamic_linux_binary_is_refused(self) -> None:
        """A manylinux tag promises the binary runs on any distribution, which a dynamic one may not."""
        with self.assertRaisesRegex(hatch_build.PlannerBinaryError, "statically linked"):
            hatch_build.check_platform("manylinux_2_17_x86_64", elf(interpreter=True))

    def test_a_macos_binary_needs_a_macos_tag(self) -> None:
        hatch_build.check_platform("macosx_11_0_arm64", macho())
        with self.assertRaisesRegex(hatch_build.PlannerBinaryError, "macos arm64"):
            hatch_build.check_platform("manylinux_2_17_aarch64", macho())
        with self.assertRaisesRegex(hatch_build.PlannerBinaryError, "linux x86_64"):
            hatch_build.check_platform("macosx_11_0_arm64", elf())

    def test_a_macos_tag_may_not_be_older_than_the_binary(self) -> None:
        hatch_build.check_platform("macosx_11_0_arm64", macho(minimum=(11, 0)))
        hatch_build.check_platform("macosx_14_0_arm64", macho(minimum=(11, 0)))
        hatch_build.check_platform("macosx_11_0_arm64", macho(minimum=None))
        with self.assertRaisesRegex(hatch_build.PlannerBinaryError, "needs macOS 15.0 or later"):
            hatch_build.check_platform("macosx_11_0_arm64", macho(minimum=(15, 0)))

    def test_unknown_tags_and_files_are_refused(self) -> None:
        for tag in ("any", "win_amd64", "linux_x86_64", ""):
            with self.subTest(tag=tag), self.assertRaises(hatch_build.PlannerBinaryError):
                hatch_build.check_platform(tag, elf())
        with self.assertRaisesRegex(hatch_build.PlannerBinaryError, "neither"):
            hatch_build.check_platform(LINUX_X86, b"#!/bin/sh\n")

    def test_the_variables_must_be_set_together(self) -> None:
        self.assertIsNone(hatch_build.planner_build({}))
        with self.assertRaisesRegex(hatch_build.PlannerBinaryError, "set both or neither"):
            hatch_build.planner_build({hatch_build.PLATFORM_VARIABLE: LINUX_X86})
        with self.assertRaisesRegex(hatch_build.PlannerBinaryError, "names no file"):
            hatch_build.planner_build({hatch_build.BINARY_VARIABLE: "/nonexistent/dagentsc", hatch_build.PLATFORM_VARIABLE: LINUX_X86})

    def test_the_binary_must_be_executable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            binary = Path(folder) / "dagentsc"
            binary.write_bytes(elf())
            binary.chmod(0o644)
            environ = {hatch_build.BINARY_VARIABLE: str(binary), hatch_build.PLATFORM_VARIABLE: LINUX_X86}
            with self.assertRaisesRegex(hatch_build.PlannerBinaryError, "not executable"):
                hatch_build.planner_build(environ)
            binary.chmod(0o755)
            self.assertEqual((binary, LINUX_X86), hatch_build.planner_build(environ))

    def test_the_hook_writes_where_the_runner_looks(self) -> None:
        package = Path(dagents_runner.__file__).resolve().parents[2]
        self.assertEqual(package.parent / hatch_build.BUNDLED_PATH, dagents_runner.BUNDLED_PLANNER)


class ReleaseVersionTests(unittest.TestCase):
    """``scripts/release_version.py`` stops a tag that differs from the version."""

    def test_the_repository_version_is_read_without_importing(self) -> None:
        self.assertEqual(VERSION, release_version.read_version())

    def test_pre_releases_and_releases(self) -> None:
        for version, prerelease in (("0.1.0a1", True), ("0.2.0b3", True), ("1.0.0rc1", True), ("1.0.0", False)):
            with self.subTest(version=version):
                self.assertEqual(prerelease, release_version.is_prerelease(version))

    def test_versions_this_project_does_not_publish_are_refused(self) -> None:
        for version in ("1.0", "1.0.0.dev1", "1.0.0.post1", "1.0.0+local", "01.0.0", "1.0.0-alpha", "v1.0.0", "1.0.0a"):
            with self.subTest(version=version), self.assertRaises(release_version.ReleaseError):
                release_version.is_prerelease(version)

    def test_the_tag_must_be_v_and_the_version(self) -> None:
        release_version.check_tag("v0.1.0a1", "0.1.0a1")
        for tag in ("0.1.0a1", "v0.1.0a2", "v0.1.0", "refs/tags/v0.1.0a1", "v0.1.0a1 "):
            with self.subTest(tag=tag), self.assertRaises(release_version.ReleaseError):
                release_version.check_tag(tag, "0.1.0a1")

    def test_each_stage_has_one_status_classifier(self) -> None:
        expected = {
            "0.1.0a1": "Development Status :: 3 - Alpha",
            "0.3.0b2": "Development Status :: 4 - Beta",
            "1.0.0rc1": "Development Status :: 4 - Beta",
            "1.0.0": "Development Status :: 5 - Production/Stable",
            "2.1.0": "Development Status :: 5 - Production/Stable",
        }
        for version, classifier in expected.items():
            with self.subTest(version=version):
                self.assertEqual(classifier, release_version.status_classifier(version))
        with self.assertRaisesRegex(release_version.ReleaseError, "before 1.0.0"):
            release_version.status_classifier("0.2.0")

    def test_a_release_must_be_a_commit_on_main(self) -> None:
        """A tag on a branch that was never merged must not publish."""
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)

            def git(*args: str) -> str:
                command = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", *args]
                return subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True).stdout.strip()

            git("init", "--quiet", "--initial-branch=main")
            git("commit", "--quiet", "--allow-empty", "--message", "on main")
            on_main = git("rev-parse", "HEAD")
            git("checkout", "--quiet", "-b", "feature")
            git("commit", "--quiet", "--allow-empty", "--message", "not merged")
            off_main = git("rev-parse", "HEAD")
            release_version.check_on_branch("main", on_main, cwd=repo)
            with self.assertRaisesRegex(release_version.ReleaseError, "is not on main"):
                release_version.check_on_branch("main", off_main, cwd=repo)
            with self.assertRaisesRegex(release_version.ReleaseError, "no-such-branch"):
                release_version.check_on_branch("no-such-branch", on_main, cwd=repo)

    def test_the_command_writes_the_outputs_and_fails_on_a_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "github-output"
            script = str(REPO_ROOT / "scripts" / "release_version.py")
            ok = subprocess.run([sys.executable, script, "--tag", f"v{VERSION}", "--github-output", str(output)], capture_output=True, text=True)
            self.assertEqual(0, ok.returncode, ok.stderr)
            prerelease = "true" if release_version.is_prerelease(VERSION) else "false"
            self.assertEqual(f"version={VERSION}\nprerelease={prerelease}\n", output.read_text(encoding="utf-8"))
            bad = subprocess.run([sys.executable, script, "--tag", "v9.9.9"], capture_output=True, text=True)
            self.assertEqual(1, bad.returncode)
            self.assertIn("does not match the package version", bad.stderr)

    def test_create_tag_cannot_be_combined_with_a_tag_check(self) -> None:
        script = str(REPO_ROOT / "scripts" / "release_version.py")
        for extra in (["--tag", f"v{VERSION}"], ["--require-on-branch", "origin/main"]):
            with self.subTest(extra=extra):
                completed = subprocess.run([sys.executable, script, "--create-tag", *extra], capture_output=True, text=True)
                self.assertEqual(2, completed.returncode)
                self.assertIn("not allowed with argument --create-tag", completed.stderr)

    def test_each_option_is_refused_without_the_mode_it_belongs_to(self) -> None:
        script = str(REPO_ROOT / "scripts" / "release_version.py")
        for extra, needed in (
            (["--requested-by", "octocat"], "--create-tag"),
            (["--run-url", "https://github.com/owner/repo/actions/runs/1"], "--create-tag"),
            (["--summary", "summary.md"], "--tag"),
            (["--create-tag", "--summary", "summary.md"], "--tag"),
        ):
            with self.subTest(extra=extra):
                completed = subprocess.run([sys.executable, script, *extra], capture_output=True, text=True)
                self.assertEqual(2, completed.returncode)
                self.assertIn(f"needs {needed}", completed.stderr)

    def test_the_tag_message_records_who_asked_for_the_release(self) -> None:
        """A run started by the tag job has github-actions[bot] as its actor, so the tag names the person and the run."""
        url = "https://github.com/owner/repo/actions/runs/123"
        self.assertEqual("dagents 0.1.0a1", release_version.tag_message("0.1.0a1"))
        for login in ("octocat", "octo-cat", "a", "x" * 39, "renovate[bot]"):
            with self.subTest(login=login):
                self.assertEqual(f"dagents 0.1.0a1\n\nRequested by {login} in {url}", release_version.tag_message("0.1.0a1", login, url))
        for address in (url, f"{url}/attempts/2", "https://github.example.com/owner/my.repo/actions/runs/9"):
            with self.subTest(address=address):
                self.assertTrue(release_version.tag_message("0.1.0a1", "octocat", address).endswith(f" in {address}"))

    def test_the_tag_message_refuses_text_that_is_not_a_login_or_a_run_address(self) -> None:
        url = "https://github.com/owner/repo/actions/runs/123"
        for login, address in (
            ("octo cat", url),
            ("octocat\nSigned-off-by: someone", url),
            ("", url),
            ("-octocat", url),
            ("x" * 40, url),
            ("octocat", "http://github.com/owner/repo/actions/runs/123"),
            ("octocat", f"{url} and more"),
            ("octocat", f"{url}\nmore"),
            ("octocat", "https://github.com/owner/repo/pull/1"),
            ("octocat", None),
            (None, url),
        ):
            with self.subTest(login=login, address=address), self.assertRaises(release_version.ReleaseError):
                release_version.tag_message("0.1.0a1", login, address)


#: The identity the release workflow gives git for the tag it creates.
TAGGER = {"GIT_COMMITTER_NAME": "github-actions[bot]", "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com"}

#: The test clones name their remote this, never ``origin``: if a bug made the
#: script run git in this repository instead of the test clone, git would find
#: no such remote and stop, instead of pushing a tag to GitHub.
TEST_REMOTE = "release-test-remote"


def git_in(repo: Path, *args: str) -> str:
    command = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args]
    return subprocess.run(command, cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


class CreateTagTests(unittest.TestCase):
    """``release_version.py --create-tag`` tags the head of main once and never moves a tag.

    The release workflow's ``release-tag`` job runs it on a manual run on main.
    Each test pushes to a bare repository in a temporary folder.
    """

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.remote = root / "remote.git"
        git_in(root, "init", "--quiet", "--bare", "--initial-branch=main", str(self.remote))
        self.work = root / "work"
        git_in(root, "clone", "--quiet", "--origin", TEST_REMOTE, str(self.remote), str(self.work))
        (self.work / "dagents").mkdir()
        self.version_file = self.work / "dagents" / "__init__.py"
        self.version_file.write_text('__version__ = "0.1.0a1"\n', encoding="utf-8")
        git_in(self.work, "add", "-A")
        self.head = self.commit_and_push("version 0.1.0a1")
        environment = mock.patch.dict(os.environ, TAGGER)
        environment.start()
        self.addCleanup(environment.stop)

    def commit_and_push(self, message: str) -> str:
        git_in(self.work, "commit", "--quiet", "--allow-empty", "--message", message)
        git_in(self.work, "push", "--quiet", TEST_REMOTE, "HEAD:main")
        return git_in(self.work, "rev-parse", "HEAD")

    def create(self, commit: str) -> bool:
        return release_version.create_tag("0.1.0a1", commit, remote=TEST_REMOTE, branch="main", cwd=self.work)

    def remote_tag(self) -> str | None:
        """The commit the remote's tag v0.1.0a1 names, or None."""
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "refs/tags/v0.1.0a1^{commit}"], cwd=self.remote, capture_output=True, text=True
        )
        return completed.stdout.strip() or None

    def test_it_pushes_an_annotated_tag_on_the_head_of_main(self) -> None:
        self.assertTrue(self.create(self.head))
        self.assertEqual(self.head, self.remote_tag())
        self.assertEqual("tag", git_in(self.remote, "cat-file", "-t", "refs/tags/v0.1.0a1"), "an annotated tag, with a tagger and a date")
        body = git_in(self.remote, "cat-file", "-p", "refs/tags/v0.1.0a1")
        self.assertIn("tagger github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>", body)
        self.assertTrue(body.endswith("dagents 0.1.0a1"))

    def test_the_tag_names_who_asked_for_the_release_and_the_run(self) -> None:
        url = "https://github.com/owner/repo/actions/runs/123"
        created = release_version.create_tag(
            "0.1.0a1", self.head, remote=TEST_REMOTE, branch="main", cwd=self.work, requested_by="octo-cat", run_url=url
        )
        self.assertTrue(created)
        body = git_in(self.remote, "cat-file", "-p", "refs/tags/v0.1.0a1")
        self.assertTrue(body.endswith(f"dagents 0.1.0a1\n\nRequested by octo-cat in {url}"), body)

    def test_a_requester_it_cannot_record_creates_no_tag(self) -> None:
        with self.assertRaisesRegex(release_version.ReleaseError, "login"):
            release_version.create_tag(
                "0.1.0a1", self.head, remote=TEST_REMOTE, branch="main", cwd=self.work,
                requested_by="octocat\nSigned-off-by: someone", run_url="https://github.com/owner/repo/actions/runs/1",
            )  # fmt: skip
        self.assertIsNone(self.remote_tag())

    def test_a_second_run_finds_the_tag_and_leaves_it_alone(self) -> None:
        self.assertTrue(self.create(self.head))
        before = git_in(self.remote, "rev-parse", "refs/tags/v0.1.0a1")
        self.assertFalse(self.create(self.head))
        self.assertEqual(before, git_in(self.remote, "rev-parse", "refs/tags/v0.1.0a1"))

    def test_a_lightweight_tag_on_the_same_commit_counts_as_the_tag(self) -> None:
        git_in(self.work, "tag", "v0.1.0a1")
        git_in(self.work, "push", "--quiet", TEST_REMOTE, "refs/tags/v0.1.0a1")
        self.assertFalse(self.create(self.head))

    def test_it_refuses_a_commit_that_is_no_longer_the_head_of_main(self) -> None:
        """actions/checkout moves its origin/main to the run's commit, so the script asks the remote."""
        newer = self.commit_and_push("a later merge")
        git_in(self.work, "checkout", "--quiet", "--detach", self.head)
        with self.assertRaisesRegex(release_version.ReleaseError, f"is not the head of main.*{newer}"):
            self.create(self.head)
        self.assertIsNone(self.remote_tag())

    def test_it_refuses_a_tag_that_already_names_another_commit(self) -> None:
        """A released version never moves: the version must change instead."""
        git_in(self.work, "tag", "--annotate", "--message", "earlier", "v0.1.0a1")
        git_in(self.work, "push", "--quiet", TEST_REMOTE, "refs/tags/v0.1.0a1")
        newer = self.commit_and_push("a later merge")
        with self.assertRaisesRegex(release_version.ReleaseError, f"already exists on commit {self.head}"):
            self.create(newer)
        self.assertEqual(self.head, self.remote_tag())

    def test_it_refuses_when_the_checkout_is_not_the_commit_to_tag(self) -> None:
        """The version is read from the checkout, so it must be the commit that gets the tag."""
        with self.assertRaisesRegex(release_version.ReleaseError, "the checkout is at"):
            self.create("0" * 40)
        self.assertIsNone(self.remote_tag())

    def test_it_refuses_a_remote_without_the_branch(self) -> None:
        with self.assertRaisesRegex(release_version.ReleaseError, "has no branch trunk"):
            release_version.create_tag("0.1.0a1", self.head, remote=TEST_REMOTE, branch="trunk", cwd=self.work)

    def test_a_refused_push_fails_with_the_remote_message(self) -> None:
        """A rule that refuses the tag, such as a tag ruleset, stops the run and names the reason."""
        hook = self.remote / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'tags are protected here' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        with self.assertRaisesRegex(release_version.ReleaseError, "(?s)could not push tag v0.1.0a1.*tags are protected here"):
            self.create(self.head)
        self.assertIsNone(self.remote_tag())

    def test_a_tag_another_run_pushed_first_on_the_same_commit_is_accepted(self) -> None:
        """Two runs at once: one push wins, and the other run finds the tag on its commit."""
        original = release_version.push_tag

        def lose_the_race(remote: str, tag: str, cwd: Path) -> subprocess.CompletedProcess:
            other = Path(self.remote).parent / "other"
            git_in(other.parent, "clone", "--quiet", "--origin", TEST_REMOTE, str(self.remote), str(other))
            git_in(other, "tag", "--annotate", "--message", "the other run", tag, self.head)
            original(remote, tag, other)
            return subprocess.CompletedProcess([], 1, "", " ! [rejected] (already exists)")

        with mock.patch.object(release_version, "push_tag", side_effect=lose_the_race):
            self.assertFalse(self.create(self.head))
        self.assertEqual(self.head, self.remote_tag())

    def test_the_command_creates_the_tag_once_and_writes_the_outputs(self) -> None:
        script = str(REPO_ROOT / "scripts" / "release_version.py")
        output = self.work.parent / "github-output"
        url = "https://github.com/owner/repo/actions/runs/123"
        command = [
            sys.executable, script, "--create-tag", "--remote", TEST_REMOTE, "--branch", "main", "--commit", self.head,
            "--requested-by", "octocat", "--run-url", url,
            "--github-output", str(output), "--repo", str(self.work), "--version-file", str(self.version_file),
        ]  # fmt: skip
        first = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertIn("created tag v0.1.0a1", first.stdout)
        self.assertEqual("version=0.1.0a1\nprerelease=true\ntag=v0.1.0a1\ncreated=true\n", output.read_text(encoding="utf-8"))
        self.assertTrue(git_in(self.remote, "cat-file", "-p", "refs/tags/v0.1.0a1").endswith(f"Requested by octocat in {url}"))
        output.unlink()
        second = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertIn("already", second.stdout)
        self.assertEqual("version=0.1.0a1\nprerelease=true\ntag=v0.1.0a1\ncreated=false\n", output.read_text(encoding="utf-8"))
        self.assertEqual(self.head, self.remote_tag())

    def test_the_command_refuses_a_version_this_project_does_not_release(self) -> None:
        """No tag is created for a version the release run would refuse."""
        self.version_file.write_text('__version__ = "0.2.0"\n', encoding="utf-8")
        script = str(REPO_ROOT / "scripts" / "release_version.py")
        completed = subprocess.run(
            [sys.executable, script, "--create-tag", "--remote", TEST_REMOTE, "--commit", self.head,
             "--repo", str(self.work), "--version-file", str(self.version_file)],
            capture_output=True, text=True,
        )  # fmt: skip
        self.assertEqual(1, completed.returncode)
        self.assertIn("before 1.0.0", completed.stderr)
        self.assertIsNone(subprocess.run(["git", "rev-parse", "--verify", "--quiet", "refs/tags/v0.2.0"], cwd=self.remote, capture_output=True, text=True).stdout.strip() or None)


class TagSummaryTests(unittest.TestCase):
    """``release_version.py --tag ... --summary FILE`` shows the reviewer the tag a release run is on.

    A run the tag job starts has github-actions[bot] as its actor, so the
    summary is where the tagger and the message, which names the person who
    asked for the release, become visible in the run that asks for approval.
    """

    URL = "https://github.com/owner/repo/actions/runs/123"

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.repo = Path(folder.name) / "repo"
        self.repo.mkdir()
        git_in(self.repo, "init", "--quiet", "--initial-branch=main")
        (self.repo / "dagents").mkdir()
        self.version_file = self.repo / "dagents" / "__init__.py"
        self.version_file.write_text('__version__ = "0.1.0a1"\n', encoding="utf-8")
        git_in(self.repo, "add", "-A")
        git_in(self.repo, "commit", "--quiet", "--message", "version 0.1.0a1")
        self.head = git_in(self.repo, "rev-parse", "HEAD")
        self.summary = Path(folder.name) / "summary.md"
        environment = mock.patch.dict(os.environ, TAGGER)
        environment.start()
        self.addCleanup(environment.stop)

    def run_check(self, tag: str = "v0.1.0a1") -> int:
        argv = ["--tag", tag, "--require-on-branch", "main", "--summary", str(self.summary), "--repo", str(self.repo), "--version-file", str(self.version_file)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return release_version.main(argv)

    def test_an_annotated_tag_shows_its_tagger_and_message(self) -> None:
        git_in(self.repo, "tag", "--annotate", "--message", release_version.tag_message("0.1.0a1", "octocat", self.URL), "v0.1.0a1")
        self.assertEqual(0, self.run_check())
        summary = self.summary.read_text(encoding="utf-8")
        self.assertIn(f"The tag v0.1.0a1 names commit {self.head}, which is on main.", summary)
        self.assertIn("\n    tagger github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com> ", summary)
        self.assertIn("\n    dagents 0.1.0a1\n", summary)
        self.assertIn(f"\n    Requested by octocat in {self.URL}\n", summary)

    def test_a_lightweight_tag_says_it_records_no_tagger(self) -> None:
        git_in(self.repo, "tag", "v0.1.0a1")
        self.assertEqual(0, self.run_check())
        summary = self.summary.read_text(encoding="utf-8")
        self.assertIn(f"The tag v0.1.0a1 names commit {self.head}, which is on main.", summary)
        self.assertIn("lightweight tag, so it records no tagger and no message", summary)
        self.assertNotIn("    tagger", summary)

    def test_text_from_the_tag_stays_inside_a_code_block(self) -> None:
        """Anyone who can push a tag writes its message, so none of it is read as Markdown."""
        message = "dagents 0.1.0a1\n\n```\n# Approved by the owner\n<b>yes</b>"
        git_in(self.repo, "tag", "--annotate", "--cleanup=verbatim", "--message", message, "v0.1.0a1")
        self.assertEqual(0, self.run_check())
        block = self.summary.read_text(encoding="utf-8").split("annotated tag:\n\n", 1)[1]
        self.assertIn("    # Approved by the owner", block)
        for line in block.rstrip("\n").splitlines():
            with self.subTest(line=line):
                self.assertTrue(line.startswith("    "), line)

    def test_a_failed_check_writes_no_summary(self) -> None:
        git_in(self.repo, "tag", "v0.1.0a2")
        self.assertEqual(1, self.run_check("v0.1.0a2"))
        self.assertFalse(self.summary.exists())


class ProjectMetadataTests(unittest.TestCase):
    """``pyproject.toml`` describes the package the code is."""

    def test_one_distribution_name_and_one_version_source(self) -> None:
        project = PYPROJECT["project"]
        self.assertEqual("dagents", project["name"])
        self.assertIn("version", project["dynamic"])
        self.assertNotIn("version", project)
        self.assertEqual("dagents/__init__.py", PYPROJECT["tool"]["hatch"]["version"]["path"])

    def test_the_status_classifier_matches_the_version(self) -> None:
        classifiers = PYPROJECT["project"]["classifiers"]
        statuses = [c for c in classifiers if c.startswith("Development Status ::")]
        self.assertEqual(1, len(statuses), statuses)
        self.assertEqual(release_version.status_classifier(VERSION), statuses[0])

    def test_the_licence_is_an_spdx_expression_with_its_file(self) -> None:
        self.assertEqual("MIT", PYPROJECT["project"]["license"])
        self.assertEqual(["LICENSE"], PYPROJECT["project"]["license-files"])
        self.assertFalse([c for c in PYPROJECT["project"]["classifiers"] if c.startswith("License ::")])

    def test_requires_python_is_the_version_ci_tests(self) -> None:
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        ci_version = re.search(r'PYTHON_VERSION: "(\d+\.\d+)"', workflow).group(1)
        self.assertEqual(f">={ci_version}", PYPROJECT["project"]["requires-python"])
        listed = sorted(c.rsplit(" ", 1)[1] for c in PYPROJECT["project"]["classifiers"] if re.match(r"Programming Language :: Python :: 3\.\d+$", c))
        self.assertEqual(ci_version, listed[0])

    def test_only_the_dagents_package_goes_into_a_wheel(self) -> None:
        wheel = PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]
        self.assertEqual(["dagents"], wheel["packages"])
        self.assertIn("dagents/tests", wheel["exclude"])
        self.assertEqual("hatch_build.py", wheel["hooks"]["custom"]["path"])

    def test_the_commands_point_at_functions_that_exist(self) -> None:
        import importlib

        for command, target in PYPROJECT["project"]["scripts"].items():
            module, function = target.split(":")
            with self.subTest(command=command):
                self.assertTrue(callable(getattr(importlib.import_module(module), function)))

    def test_the_agent_extras_hold_what_the_agents_import(self) -> None:
        extras = PYPROJECT["project"]["optional-dependencies"]
        for extra in ("lma", "gma"):
            names = {re.split(r"[<>=\[ ]", requirement)[0] for requirement in extras[extra]}
            self.assertEqual({"fastapi", "uvicorn", "httpx"}, names, extra)
        self.assertEqual(["pydantic>=2.11,<3"], PYPROJECT["project"]["dependencies"])

    def test_the_compose_extra_holds_what_the_appliers_import(self) -> None:
        """compose_apply.py imports PyYAML when it parses a Compose file, so the ``compose`` extra installs it.

        The range is the one the healthcare demo lists and the agents job
        installs (tests/test_ci_guards.py ties those two together).
        """
        source = (REPO_ROOT / "dagents" / "common" / "infrastructure" / "compose_apply.py").read_text(encoding="utf-8")
        self.assertIn("import yaml", source)
        demo = (REPO_ROOT / "apps" / "healthcare-demo" / "requirements.txt").read_text(encoding="utf-8")
        pyyaml = re.search(r"^pyyaml[^\s#]*", demo, re.M | re.I).group(0)
        self.assertEqual([pyyaml], PYPROJECT["project"]["optional-dependencies"].get("compose"))

    def test_the_all_extra_installs_every_other_extra(self) -> None:
        """The packaging job installs ``[all]`` to run the agent suite, so a missing extra fails its tests there."""
        extras = PYPROJECT["project"]["optional-dependencies"]
        self.assertEqual(1, len(extras["all"]), extras["all"])
        listed = re.fullmatch(r"dagents\[([a-z,]+)\]", extras["all"][0]).group(1).split(",")
        self.assertEqual(sorted(name for name in extras if name != "all"), sorted(listed))

    def test_ci_tests_the_lowest_version_each_agent_extra_allows(self) -> None:
        """The agents job pins FastAPI and uvicorn; the extras start at those pins and stop below 1.0.

        The packaging job runs the agent suite again on the newest versions
        the extras allow (tests/test_release_workflow.py checks that step).
        """
        pins = {}
        for line in (REPO_ROOT / "dagents" / "lma" / "requirements.txt").read_text(encoding="utf-8").splitlines():
            name, separator, version = line.partition("==")
            if separator:
                pins[name.strip()] = version.strip()
        self.assertLessEqual({"fastapi", "uvicorn"}, set(pins))
        for extra in ("lma", "gma"):
            for requirement in PYPROJECT["project"]["optional-dependencies"][extra]:
                name = re.split(r"[<>=\[ ]", requirement)[0]
                with self.subTest(extra=extra, requirement=requirement):
                    self.assertIn(",<1", requirement)
                    if name in pins:
                        self.assertTrue(requirement.startswith(f"{name}>={pins[name]},"), requirement)

    def test_the_build_backend_is_pinned_for_every_build(self) -> None:
        """Every build-system requirement has an exact pin that meets its lower bound."""
        pins = {}
        for line in (REPO_ROOT / ".github" / "build-constraints.txt").read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if line:
                name, separator, version = line.partition("==")
                self.assertEqual("==", separator, f"{line!r} is not an exact pin")
                pins[name.strip().lower()] = version.strip()
        for requirement in PYPROJECT["build-system"]["requires"]:
            name, lower = re.fullmatch(r"([A-Za-z0-9_.-]+)>=([\d.]+)", requirement).groups()
            with self.subTest(requirement=requirement):
                self.assertIn(name.lower(), pins)
                pinned = tuple(int(part) for part in pins[name.lower()].split("."))
                self.assertGreaterEqual(pinned, tuple(int(part) for part in lower.split(".")))

    def test_the_readme_tells_a_pypi_reader_how_to_install(self) -> None:
        """The README is the PyPI description, which PyPI never lets a release change after upload."""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        for words in ("**Status: alpha.**", "pip install dagents", 'pip install "dagents[lma,gma]"', "DAGENTSC_BIN", "python -m dagents"):
            with self.subTest(words=words):
                self.assertIn(words, readme)
        self.assertNotIn("being prepared", readme)

    def test_no_code_imports_the_old_package_name(self) -> None:
        """The package was called ``agents``; that name belongs to another project on PyPI."""
        tracked = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout.split()
        pattern = re.compile(r"^\s*(from agents(\.| import)|import agents(\.|\s|$))", re.M)
        offenders = [path for path in tracked if pattern.search((REPO_ROOT / path).read_text(encoding="utf-8", errors="replace"))]
        self.assertEqual([], offenders)
        self.assertFalse((REPO_ROOT / "agents").exists(), "the old agents/ folder is back")


#: The description every synthetic distribution carries; check_dist requires the install command in it.
DESCRIPTION = "# Dagents\n\npip install dagents\n"

BACKEND = "hatchling 1.32.4"


def write_wheel(
    folder: Path,
    platform: str = "any",
    files: dict[str, bytes] | None = None,
    *,
    executable: bool = True,
    record_files: dict[str, bytes] | None = None,
    generator: str = BACKEND,
) -> Path:
    """A wheel with the required modules, the given extra files and a correct RECORD."""
    dist_info = f"dagents-{VERSION}.dist-info"
    contents = {name: b"" for name in check_dist.REQUIRED_MODULES}
    contents.update(files or {})
    stage = release_version.status_classifier(VERSION)
    contents[f"{dist_info}/METADATA"] = (
        f"Metadata-Version: 2.4\nName: dagents\nVersion: {VERSION}\nLicense-Expression: MIT\n"
        f"Requires-Python: >=3.11\nClassifier: {stage}\n\n{DESCRIPTION}"
    ).encode()
    tags = "".join(f"Tag: py3-none-{tag}\n" for tag in platform.split("."))
    contents[f"{dist_info}/WHEEL"] = (
        f"Wheel-Version: 1.0\nGenerator: {generator}\nRoot-Is-Purelib: {'true' if platform == 'any' else 'false'}\n{tags}"
    ).encode()
    record_source = dict(contents)
    record_source.update(record_files or {})
    record = "".join(
        f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},{len(data)}\n"
        for name, data in record_source.items()
    ) + f"{dist_info}/RECORD,,\n"
    contents[f"{dist_info}/RECORD"] = record.encode()
    path = folder / f"dagents-{VERSION}-py3-none-{platform}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in contents.items():
            info = zipfile.ZipInfo(name)
            mode = 0o755 if (name == hatch_build.BUNDLED_PATH and executable) else 0o644
            info.external_attr = mode << 16
            archive.writestr(info, data)
    return path


class CheckDistTests(unittest.TestCase):
    """``scripts/check_dist.py`` fails on each kind of bad distribution."""

    def check(self, path: Path, **kwargs) -> list[str]:
        report = check_dist.Report()
        if path.suffix == ".whl":
            check_dist.check_wheel(path, VERSION, report, **kwargs)
        else:
            check_dist.check_sdist(path, VERSION, report)
        return report.errors

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def test_a_pure_wheel_passes(self) -> None:
        self.assertEqual([], self.check(write_wheel(self.folder)))

    def test_a_platform_wheel_with_its_planner_passes(self) -> None:
        wheel = write_wheel(self.folder, LINUX_X86, {hatch_build.BUNDLED_PATH: elf()})
        self.assertEqual([], self.check(wheel))

    def test_a_top_level_agents_module_fails(self) -> None:
        errors = self.check(write_wheel(self.folder, files={"agents/__init__.py": b""}))
        self.assertTrue(any("agents/__init__.py is outside" in error for error in errors), errors)

    def test_tests_markdown_and_dockerfiles_fail(self) -> None:
        for name in ("dagents/tests/test_x.py", "dagents/lma/README.md", "dagents/lma/Dockerfile", "dagents/lma/requirements.txt"):
            with self.subTest(name=name):
                folder = self.folder / name.replace("/", "_")
                folder.mkdir()
                errors = self.check(write_wheel(folder, files={name: b"x"}))
                self.assertTrue(any("must not be in a wheel" in error for error in errors), errors)

    def test_a_platform_wheel_without_its_planner_fails(self) -> None:
        errors = self.check(write_wheel(self.folder, LINUX_X86))
        self.assertTrue(any("must hold the planner" in error for error in errors), errors)

    def test_a_planner_that_is_not_executable_or_does_not_match_fails(self) -> None:
        errors = self.check(write_wheel(self.folder, LINUX_X86, {hatch_build.BUNDLED_PATH: elf()}, executable=False))
        self.assertTrue(any("not marked executable" in error for error in errors), errors)
        other = self.folder / "other"
        other.mkdir()
        errors = self.check(write_wheel(other, LINUX_X86, {hatch_build.BUNDLED_PATH: elf(0xB7)}))
        self.assertTrue(any("aarch64" in error for error in errors), errors)

    def test_a_pure_wheel_with_a_planner_fails(self) -> None:
        errors = self.check(write_wheel(self.folder, files={hatch_build.BUNDLED_PATH: elf()}))
        self.assertTrue(any("must not hold" in error for error in errors), errors)

    def test_a_wrong_record_hash_fails(self) -> None:
        wheel = write_wheel(self.folder, files={"dagents/extra.py": b"a = 1\n"}, record_files={"dagents/extra.py": b"a = 2\n"})
        errors = self.check(wheel)
        self.assertTrue(any("RECORD hash of dagents/extra.py" in error for error in errors), errors)

    def test_an_sdist_with_anything_else_fails(self) -> None:
        root = f"dagents-{VERSION}"
        good = {f"{root}/{name}": b"" for name in ("pyproject.toml", "hatch_build.py", "README.md", "LICENSE", *check_dist.REQUIRED_MODULES)}
        good[f"{root}/PKG-INFO"] = (
            f"Metadata-Version: 2.4\nName: dagents\nVersion: {VERSION}\nLicense-Expression: MIT\nRequires-Python: >=3.11\n"
            f"Classifier: {release_version.status_classifier(VERSION)}\n\n{DESCRIPTION}"
        ).encode()
        for extra, expected in ((None, None), (f"{root}/services/core-service/app/main.py", "must not be in the sdist"), (f"{root}/dagents/tests/test_x.py", "must not be in the sdist")):
            with self.subTest(extra=extra):
                path = self.folder / f"{root}.tar.gz"
                members = dict(good)
                if extra:
                    members[extra] = b"x"
                with tarfile.open(path, "w:gz") as archive:
                    for name, data in members.items():
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                errors = self.check(path)
                if expected is None:
                    self.assertEqual([], errors)
                else:
                    self.assertTrue(any(expected in error for error in errors), errors)

    def metadata_problems(self, version: str, *classifiers: str, description: str = DESCRIPTION) -> list[str]:
        lines = "".join(f"Classifier: {classifier}\n" for classifier in classifiers)
        metadata = Parser().parsestr(
            f"Name: dagents\nVersion: {version}\nLicense-Expression: MIT\nRequires-Python: >=3.11\n{lines}\n{description}"
        )
        report = check_dist.Report()
        check_dist._check_metadata("METADATA", metadata, version, report)
        return report.errors

    def test_the_status_classifier_must_match_the_stage(self) -> None:
        alpha, beta, stable = (f"Development Status :: {stage}" for stage in ("3 - Alpha", "4 - Beta", "5 - Production/Stable"))
        self.assertEqual([], self.metadata_problems("0.1.0a1", alpha))
        self.assertEqual([], self.metadata_problems("1.0.0rc1", beta))
        self.assertEqual([], self.metadata_problems("1.0.0", stable))
        for version, classifiers in (
            ("1.0.0", (alpha,)),
            ("1.0.0rc1", (alpha,)),
            ("0.1.0a1", (alpha, beta)),
            ("0.1.0a1", ()),
            ("0.2.0", (beta,)),
        ):
            with self.subTest(version=version, classifiers=classifiers):
                errors = self.metadata_problems(version, *classifiers)
                self.assertTrue(any("Development Status" in error or "before 1.0.0" in error for error in errors), errors)

    def test_the_description_must_show_how_to_install(self) -> None:
        stage = release_version.status_classifier(VERSION)
        errors = self.metadata_problems(VERSION, stage, description="# Dagents\n\nA package is being prepared.\n")
        self.assertTrue(any("pip install dagents" in error for error in errors), errors)

    def test_a_wheel_built_by_another_backend_version_fails(self) -> None:
        errors = self.check(write_wheel(self.folder, generator="hatchling 1.27.0"), backend=BACKEND)
        self.assertTrue(any("hatchling 1.27.0" in error and BACKEND in error for error in errors), errors)
        other = self.folder / "other"
        other.mkdir()
        self.assertEqual([], self.check(write_wheel(other), backend=BACKEND))

    def test_the_backend_comes_from_the_constraints_file(self) -> None:
        constraints = (REPO_ROOT / ".github" / "build-constraints.txt").read_text(encoding="utf-8")
        version = re.search(r"^hatchling==(\S+)$", constraints, re.M).group(1)
        self.assertEqual(f"hatchling {version}", check_dist.pinned_backend(REPO_ROOT / ".github" / "build-constraints.txt"))
        broken = self.folder / "constraints.txt"
        broken.write_text("hatchling>=1.27\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "hatchling=="):
            check_dist.pinned_backend(broken)

    def test_the_command_requires_the_named_platform(self) -> None:
        write_wheel(self.folder)
        script = str(REPO_ROOT / "scripts" / "check_dist.py")
        missing = subprocess.run([sys.executable, script, str(self.folder), "--require-platform", "manylinux_2_17_x86_64"], capture_output=True, text=True)
        self.assertEqual(1, missing.returncode)
        self.assertIn("no wheel for the platform manylinux_2_17_x86_64", missing.stderr)
        passing = subprocess.run([sys.executable, script, str(self.folder)], capture_output=True, text=True)
        self.assertEqual(0, passing.returncode, passing.stderr)


class _Index(http.server.BaseHTTPRequestHandler):
    """A stand-in for a package index: the JSON API and the files it lists."""

    listing: dict[str, tuple[str, bytes]] = {}

    def do_GET(self) -> None:  # noqa: N802 - the handler's API
        if self.path == f"/pypi/dagents/{VERSION}/json":
            port = self.server.server_address[1]
            urls = [
                {"filename": name, "digests": {"sha256": digest}, "url": f"http://127.0.0.1:{port}/files/{name}"}
                for name, (digest, _) in self.listing.items()
            ]
            body = json.dumps({"urls": urls}).encode()
        elif self.path.startswith("/files/") and self.path[7:] in self.listing:
            body = self.listing[self.path[7:]][1]
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # noqa: ANN002 - keep the test output clean
        pass


class CheckPublishedFilesTests(unittest.TestCase):
    """``scripts/check_published_files.py`` fails unless the index serves the built bytes."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dist = Path(folder.name)
        self.files = {f"dagents-{VERSION}.tar.gz": b"sdist", f"dagents-{VERSION}-py3-none-any.whl": b"wheel"}
        for name, data in self.files.items():
            (self.dist / name).write_bytes(data)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Index)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.index = f"http://127.0.0.1:{self.server.server_address[1]}"

    def serve(self, listing: dict[str, bytes], digests: dict[str, str] | None = None) -> None:
        _Index.listing = {
            name: ((digests or {}).get(name) or hashlib.sha256(data).hexdigest(), data) for name, data in listing.items()
        }

    def problems(self) -> list[str]:
        return check_published_files.check(self.index, VERSION, self.dist, wait_seconds=0, poll_seconds=0)

    def test_the_same_files_pass(self) -> None:
        self.serve(self.files)
        self.assertEqual([], self.problems())

    def test_a_missing_file_fails(self) -> None:
        self.serve({f"dagents-{VERSION}.tar.gz": b"sdist"})
        self.assertEqual([f"{self.index} does not list dagents-{VERSION}-py3-none-any.whl"], self.problems())

    def test_a_file_this_run_did_not_build_fails(self) -> None:
        self.serve({**self.files, f"dagents-{VERSION}-py3-none-macosx_11_0_arm64.whl": b"old"})
        self.assertTrue(any("which this run did not build" in problem for problem in self.problems()))

    def test_a_different_digest_or_download_fails(self) -> None:
        self.serve(self.files, digests={f"dagents-{VERSION}.tar.gz": "0" * 64})
        self.assertTrue(any("the index reports sha256" in problem for problem in self.problems()))
        _Index.listing[f"dagents-{VERSION}-py3-none-any.whl"] = (hashlib.sha256(b"wheel").hexdigest(), b"tampered")
        self.assertTrue(any("downloaded from the index differs" in problem for problem in self.problems()))

    def test_an_index_without_the_version_lists_every_file_as_missing(self) -> None:
        self.serve(self.files)
        problems = check_published_files.check(self.index, "9.9.9", self.dist, wait_seconds=0, poll_seconds=0)
        self.assertEqual(2, len(problems), problems)


def install(site: Path, name: str, version: str, files: dict[str, str], commands: tuple[str, ...] = (), *, record: bool = True) -> Path:
    """Write an installed distribution into ``site`` as pip does: its files, a dist-info folder, RECORD and its commands."""
    dist_info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    listed = [f"{dist_info.name}/METADATA"]
    if commands:
        (dist_info / "entry_points.txt").write_text(
            "[console_scripts]\n" + "".join(f"{command} = {name}.cli:main\n" for command in commands), encoding="utf-8"
        )
        listed.append(f"{dist_info.name}/entry_points.txt")
    for path, text in files.items():
        if not path.startswith("../"):
            (site / path).parent.mkdir(parents=True, exist_ok=True)
            (site / path).write_text(text, encoding="utf-8")
        listed.append(path)
    if record:
        listed.append(f"{dist_info.name}/RECORD")
        (dist_info / "RECORD").write_text("".join(f"{path},,\n" for path in listed), encoding="utf-8")
    return dist_info


class SmokeScriptTests(unittest.TestCase):
    """``scripts/smoke_installed_package.py`` judges the files the dagents distribution installed, not the environment.

    The package was renamed from ``agents`` so that it installs next to
    openai-agents, whose top-level module is ``agents``. A check that no
    ``agents`` module exists anywhere fails in exactly that environment, so
    the script reads the distribution's RECORD instead. The ``packaging`` job
    in ci.yml runs the script with openai-agents installed
    (``--agents-from openai-agents``).
    """

    COMMANDS = ("dagents-lma", "dagents-gma")
    DAGENTS_FILES = {
        "dagents/__init__.py": "__version__ = '0'\n",
        "dagents/_bin/dagentsc": "",
        "dagents/__pycache__/__init__.cpython-313.pyc": "",
        "../../../bin/dagents-lma": "",
        "../../../bin/dagents-gma": "",
    }

    def setUp(self) -> None:
        import smoke_installed_package

        self.smoke = smoke_installed_package
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.site = self.folder / "site-packages"
        self.site.mkdir()

    def distribution(self, files: dict[str, str], *, record: bool = True):
        """A ``dagents`` distribution with these files, each in a site-packages folder of its own."""
        import importlib.metadata

        site = Path(tempfile.mkdtemp(dir=self.folder))
        return importlib.metadata.PathDistribution(install(site, "dagents", VERSION, files, self.COMMANDS, record=record))

    def test_the_package_its_metadata_and_its_commands_pass(self) -> None:
        distribution = self.distribution(self.DAGENTS_FILES)
        self.assertEqual([], self.smoke.unexpected_files(distribution))
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.smoke.check_distribution(distribution)
        self.assertIn("the commands dagents-gma, dagents-lma", printed.getvalue())

    def test_a_second_top_level_module_or_an_unknown_command_fails(self) -> None:
        files = {**self.DAGENTS_FILES, "agents/__init__.py": "", "agents.py": "", "../../../bin/other-tool": ""}
        distribution = self.distribution(files)
        self.assertEqual(["../../../bin/other-tool", "agents.py", "agents/__init__.py"], self.smoke.unexpected_files(distribution))
        with self.assertRaisesRegex(self.smoke.SmokeFailure, "agents/__init__.py"):
            self.smoke.check_distribution(distribution)

    def test_bytecode_the_installer_compiled_passes(self) -> None:
        """pip compiles each module it installs and lists the files in RECORD.

        With PYTHONPYCACHEPREFIX set, those files sit outside site-packages.
        Python never imports a file in ``__pycache__`` or under that prefix as
        a module of its own, so only a source file or a sourceless ``.pyc``
        next to the packages counts.
        """
        prefixed = "../../../../../pycache/tmp/venv/lib/python3.13/site-packages/dagents/__init__.cpython-313.pyc"
        distribution = self.distribution({**self.DAGENTS_FILES, prefixed: ""})
        self.assertEqual([], self.smoke.unexpected_files(distribution))
        files = {**self.DAGENTS_FILES, "agents.py": "", "__pycache__/agents.cpython-313.pyc": "", "other.pyc": ""}
        self.assertEqual(["agents.py", "other.pyc"], self.smoke.unexpected_files(self.distribution(files)))

    def test_a_distribution_without_a_record_fails(self) -> None:
        with self.assertRaisesRegex(self.smoke.SmokeFailure, "RECORD"):
            self.smoke.check_distribution(self.distribution(self.DAGENTS_FILES, record=False))

    def other_agents(self, *, owner: str | None) -> subprocess.CompletedProcess[str]:
        """Run ``check_other_agents('openai-agents')`` with ``site`` on the path and ``agents`` owned by ``owner``."""
        install(self.site, "dagents", VERSION, self.DAGENTS_FILES, self.COMMANDS)
        if owner is not None:
            install(self.site, owner, "0.24.0", {"agents/__init__.py": "class Agent:\n    pass\n"})
        code = (
            "import sys\n"
            f"sys.path.insert(0, {str(REPO_ROOT / 'scripts')!r})\n"
            "import smoke_installed_package as smoke\n"
            "try:\n"
            "    smoke.check_other_agents('openai-agents')\n"
            "except smoke.SmokeFailure as failure:\n"
            "    print(failure)\n"
            "    raise SystemExit(1)\n"
        )
        environment = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
        environment["PYTHONPATH"] = f"{self.site}{os.pathsep}{REPO_ROOT}"
        # The working folder holds no agents/ folder, so only the site can provide one.
        return subprocess.run([sys.executable, "-c", code], cwd=self.folder, env=environment, capture_output=True, text=True, timeout=60)

    def test_an_agents_module_from_the_named_distribution_passes(self) -> None:
        result = self.other_agents(owner="openai-agents")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("openai-agents", result.stdout)

    def test_the_named_distribution_must_provide_the_agents_module(self) -> None:
        missing = self.other_agents(owner=None)
        self.assertEqual(1, missing.returncode, missing.stdout + missing.stderr)
        self.assertIn("no top-level 'agents' module", missing.stdout)

    def test_an_agents_module_from_another_distribution_fails(self) -> None:
        result = self.other_agents(owner="agents")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("comes from agents, not openai-agents", result.stdout)


if __name__ == "__main__":
    unittest.main()
