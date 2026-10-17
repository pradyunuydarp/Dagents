"""Read the package version, check it against a release tag, and say whether it is a pre-release.

The version is written once, as ``__version__`` in ``dagents/__init__.py``.
The release workflow (``.github/workflows/release.yml``) runs this script
first and stops when the tag differs from the version, so a tag can never
publish a package with another version number.

Usage:
    python scripts/release_version.py                      # print the version
    python scripts/release_version.py --tag v0.1.0a1       # check a tag
    python scripts/release_version.py --tag v0.1.0a1 --require-on-branch origin/main \
        --github-output "$GITHUB_OUTPUT"
    python scripts/release_version.py --create-tag --remote origin --branch main \
        --commit "$GITHUB_SHA" --github-output "$GITHUB_OUTPUT"

With ``--require-on-branch REF`` it also fails unless the checked-out commit is
on that branch, so a tag pushed on a branch that was never merged cannot
publish. With ``--github-output`` it appends ``version=...`` and
``prerelease=true`` or ``prerelease=false`` to that file, for the later jobs.
Exit status 1 means the tag, the commit or the version is wrong; the message
says which.

``--create-tag`` is for a release started from the Actions tab. It creates the
annotated tag ``v<version>`` on ``--commit`` and pushes it to ``--remote``
(:func:`create_tag`). It fails unless the checkout is that commit and the
commit is the head of ``--branch`` on the remote now. If the tag already names
that commit, it leaves the tag alone, so a second run is safe; if the tag names
another commit, it fails, because a released version never moves. It also
appends ``tag=v<version>`` and ``created=true`` or ``created=false`` to the
``--github-output`` file. git takes the tagger's name and email from
``GIT_COMMITTER_NAME`` and ``GIT_COMMITTER_EMAIL``.

The accepted versions are the PEP 440 forms this project uses: ``X.Y.Z`` for a
release, ``X.Y.ZaN`` for an alpha, ``X.Y.ZbN`` for a beta and ``X.Y.ZrcN`` for
a release candidate. Development, post and local versions are refused, because
PyPI would show them next to the releases. Every version before 1.0.0 is a
pre-release, so a final ``0.Y.Z`` version is refused too.

Each stage has one ``Development Status`` classifier (:func:`status_classifier`),
which ``scripts/check_dist.py`` requires in the built metadata.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO_ROOT / "dagents" / "__init__.py"

VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(a|b|rc)(0|[1-9]\d*))?$")


class ReleaseError(ValueError):
    """The version or the tag cannot be released."""


def read_version(path: Path = VERSION_FILE) -> str:
    """The ``__version__`` string assigned in ``path``, read without importing the package."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
            raise ReleaseError(f"__version__ in {path} must be a plain string")
    raise ReleaseError(f"{path} does not assign __version__")


def is_prerelease(version: str) -> bool:
    """Whether ``version`` is an alpha, beta or release candidate. Raises for a version this project does not use."""
    match = VERSION_PATTERN.match(version)
    if match is None:
        raise ReleaseError(
            f"version {version!r} is not X.Y.Z, X.Y.ZaN, X.Y.ZbN or X.Y.ZrcN (PEP 440, no dev, post or local part)"
        )
    return match.group(4) is not None


#: The ``Development Status`` classifier for each kind of pre-release.
STAGE_CLASSIFIERS = {
    "a": "Development Status :: 3 - Alpha",
    "b": "Development Status :: 4 - Beta",
    # A release candidate is still a beta to a user: the API may change
    # before the final release.
    "rc": "Development Status :: 4 - Beta",
}

STABLE_CLASSIFIER = "Development Status :: 5 - Production/Stable"


def status_classifier(version: str) -> str:
    """The one ``Development Status`` classifier ``version`` must carry.

    Raises for a version this project does not use, including a final
    version before 1.0.0, because every version before 1.0.0 is a pre-release.
    """
    is_prerelease(version)  # raises, naming the accepted forms, for any other version
    match = VERSION_PATTERN.match(version)
    stage = match.group(4)
    if stage is not None:
        return STAGE_CLASSIFIERS[stage]
    if match.group(1) == "0":
        raise ReleaseError(
            f"version {version!r} is a final release before 1.0.0; versions before 1.0.0 are pre-releases (aN or bN)"
        )
    return STABLE_CLASSIFIER


def check_on_branch(branch: str, commit: str = "HEAD", cwd: Path = REPO_ROOT) -> None:
    """Raise unless ``commit`` is on ``branch``: the branch's tip or one of its ancestors."""
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", commit, branch], cwd=cwd, capture_output=True, text=True
    )
    if completed.returncode == 1:
        raise ReleaseError(f"commit {commit} is not on {branch}; tag a commit that has been merged into {branch}")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"git exited with {completed.returncode}"
        raise ReleaseError(f"cannot check that commit {commit} is on {branch}: {detail}")


def check_tag(tag: str, version: str) -> None:
    """Raise unless ``tag`` is ``v`` followed by exactly ``version``."""
    if tag != f"v{version}":
        raise ReleaseError(f"tag {tag!r} does not match the package version {version!r}; the tag must be 'v{version}'")


def git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def remote_commit(remote: str, ref: str, cwd: Path = REPO_ROOT) -> str | None:
    """The commit that ``ref`` names on ``remote`` now, with an annotated tag followed to its commit; None if it does not exist.

    It asks the remote, not the checkout: actions/checkout moves its copy of
    the branch to the run's commit, so the checkout cannot show a newer head.
    """
    completed = git(["ls-remote", remote, ref, f"{ref}^{{}}"], cwd)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"git exited with {completed.returncode}"
        raise ReleaseError(f"cannot read {ref} on {remote}: {detail}")
    found = {}
    for line in completed.stdout.splitlines():
        sha, _, name = line.partition("\t")
        found[name] = sha
    # ls-remote matches the end of a ref name, so keep only the exact names.
    return found.get(f"{ref}^{{}}") or found.get(ref)


def push_tag(remote: str, tag: str, cwd: Path) -> subprocess.CompletedProcess:
    """Push one tag, never forced: the remote refuses it when the tag exists there."""
    return git(["push", remote, f"refs/tags/{tag}:refs/tags/{tag}"], cwd)


def create_tag(version: str, commit: str, remote: str = "origin", branch: str = "main", cwd: Path = REPO_ROOT) -> bool:
    """Create the annotated tag ``v<version>`` on ``commit`` and push it to ``remote``.

    Returns True when this call created the tag and False when the remote
    already had it on ``commit``. Raises unless the checkout is ``commit`` and
    ``commit`` is the head of ``branch`` on ``remote``, and when the tag
    already names another commit.
    """
    tag = f"v{version}"
    checkout = git(["rev-parse", "HEAD"], cwd).stdout.strip()
    if checkout != commit:
        raise ReleaseError(
            f"the checkout is at {checkout or 'no commit'}, not {commit}; the version must be read from the commit that gets the tag"
        )
    head = remote_commit(remote, f"refs/heads/{branch}", cwd)
    if head is None:
        raise ReleaseError(f"{remote} has no branch {branch}")
    if head != commit:
        raise ReleaseError(
            f"commit {commit} is not the head of {branch} on {remote}, which is now {head}; "
            f"run the workflow again on the newest commit of {branch}"
        )
    existing = remote_commit(remote, f"refs/tags/{tag}", cwd)
    if existing == commit:
        return False
    if existing is not None:
        raise ReleaseError(
            f"tag {tag} already exists on commit {existing}, not {commit}; a released version never moves, "
            "so change __version__ to release this commit"
        )
    # --force replaces a stale tag of the same name in this checkout only; the
    # push below is not forced, so the remote keeps any tag it has.
    created = git(["tag", "--annotate", "--force", "--message", f"dagents {version}", tag, commit], cwd)
    if created.returncode != 0:
        raise ReleaseError(f"could not create tag {tag}: {created.stderr.strip()}")
    pushed = push_tag(remote, tag, cwd)
    now = remote_commit(remote, f"refs/tags/{tag}", cwd)
    if now != commit:
        detail = pushed.stderr.strip() or f"git exited with {pushed.returncode}"
        raise ReleaseError(f"could not push tag {tag} to {remote}: {detail}")
    # A failed push with the tag on this commit means another run pushed it first.
    return pushed.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--tag", help="the release tag, such as v0.1.0a1")
    parser.add_argument("--require-on-branch", metavar="REF", help="fail unless the checked-out commit is on this branch")
    mode.add_argument(
        "--create-tag",
        action="store_true",
        help="create the tag v<version> on --commit and push it to --remote, if --commit is the head of --branch there",
    )
    parser.add_argument("--remote", default="origin", help="with --create-tag: the remote to push the tag to (default: origin)")
    parser.add_argument("--branch", default="main", help="with --create-tag: the branch whose head gets the tag (default: main)")
    parser.add_argument("--commit", help="with --create-tag: the commit to tag (default: the checked-out commit)")
    parser.add_argument("--github-output", type=Path, help="append version= and prerelease= (and tag= and created=) to this file")
    parser.add_argument("--version-file", type=Path, default=VERSION_FILE, help=argparse.SUPPRESS)
    parser.add_argument("--repo", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.create_tag and args.require_on_branch is not None:
        parser.error("argument --require-on-branch: not allowed with argument --create-tag")
    created = None
    try:
        version = read_version(args.version_file)
        prerelease = is_prerelease(version)
        classifier = status_classifier(version)
        if args.tag is not None:
            check_tag(args.tag, version)
        if args.require_on_branch is not None:
            check_on_branch(args.require_on_branch, cwd=args.repo)
        if args.create_tag:
            commit = args.commit or git(["rev-parse", "HEAD"], args.repo).stdout.strip()
            created = create_tag(version, commit, remote=args.remote, branch=args.branch, cwd=args.repo)
    except ReleaseError as error:
        print(f"release_version: {error}", file=sys.stderr)
        return 1
    kind = "pre-release" if prerelease else "release"
    print(f"version {version} ({kind}, {classifier})" + (f", tag {args.tag} matches" if args.tag else ""))
    if args.require_on_branch is not None:
        print(f"the commit is on {args.require_on_branch}")
    if created is not None:
        if created:
            print(f"created tag v{version} on {commit}, the head of {args.branch}, and pushed it to {args.remote}")
        else:
            print(f"tag v{version} is already on {commit}, the head of {args.branch}; it was left as it is")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"version={version}\n")
            handle.write(f"prerelease={'true' if prerelease else 'false'}\n")
            if created is not None:
                handle.write(f"tag=v{version}\n")
                handle.write(f"created={'true' if created else 'false'}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
