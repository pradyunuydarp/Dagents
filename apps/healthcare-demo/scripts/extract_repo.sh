#!/usr/bin/env bash
# Split this app out into a standalone git repository, keeping its history.
#
# The app is already self-contained: its own README, requirements, pyproject,
# tests, compose file, and .gitignore. What it is not is independent of the
# framework — it imports Dagents, which is the point of a demo app. After
# extraction, point DAGENTS_HOME at a Dagents checkout or install the framework
# as a dependency.
#
#   scripts/extract_repo.sh ~/src/dagents-healthcare-demo
set -euo pipefail

TARGET="${1:-}"
if [[ -z "${TARGET}" ]]; then
  echo "usage: $0 <target-directory> [remote-url]" >&2
  exit 1
fi
REMOTE="${2:-}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "${HERE}/.." && pwd)"
REPO_ROOT="$(git -C "${APP_ROOT}" rev-parse --show-toplevel)"
PREFIX="$(realpath --relative-to="${REPO_ROOT}" "${APP_ROOT}")"

if [[ -e "${TARGET}" ]]; then
  echo "refusing to write into an existing path: ${TARGET}" >&2
  echo "choose a directory that does not exist yet" >&2
  exit 1
fi

if ! git -C "${REPO_ROOT}" diff --quiet || ! git -C "${REPO_ROOT}" diff --cached --quiet; then
  echo "the Dagents working tree has uncommitted changes; commit them first" >&2
  exit 1
fi

echo "Extracting ${PREFIX} into ${TARGET}"

# git subtree split rewrites the history of this subdirectory into a branch
# whose root is the app, keeping every commit that touched it.
BRANCH="extract-$(date +%s)"
git -C "${REPO_ROOT}" subtree split --prefix="${PREFIX}" -b "${BRANCH}" >/dev/null

git init --quiet "${TARGET}"
git -C "${TARGET}" pull --quiet "${REPO_ROOT}" "${BRANCH}"
git -C "${REPO_ROOT}" branch -D "${BRANCH}" >/dev/null

if [[ -n "${REMOTE}" ]]; then
  git -C "${TARGET}" remote add origin "${REMOTE}"
  echo "Added remote origin ${REMOTE}. Push when ready:"
  echo "  git -C ${TARGET} push -u origin HEAD"
fi

cat <<EOF

Done. ${TARGET} is now its own repository with this app's history.

Next:
  export DAGENTS_HOME=${REPO_ROOT}
  cd ${TARGET} && scripts/run_local_demo.sh

The app imports the Dagents framework, so DAGENTS_HOME (or an installed
dagents package) has to point at a checkout with the OCaml planner built.
EOF
