#!/usr/bin/env bash
# Prepares a release: sets the version everywhere it appears, turns the changelog's
# Unreleased notes into this version's, commits, and tags.
#
#   scripts/release.sh 0.3.0
#   git push origin main && git push origin v0.3.0
#
# Pushing the tag starts the release build on GitHub (.github/workflows/release.yml).

set -euo pipefail

version=${1:-}
if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]]; then
    echo "Usage: scripts/release.sh X.Y.Z   (for example 0.3.0, or 0.3.0-rc1 for a pre-release)"
    exit 1
fi
cd "$(git rev-parse --show-toplevel)"

if [[ "$(git branch --show-current)" != "main" ]]; then
    echo "Switch to main first: git switch main"
    exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
    echo "There are uncommitted changes. Commit them (or stash them) first:"
    git status --short
    exit 1
fi
if git rev-parse -q --verify "refs/tags/v$version" > /dev/null; then
    echo "Tag v$version already exists."
    exit 1
fi
# The Unreleased section must say what's in this release: it becomes the release notes.
unreleased=$(awk '/^## \[Unreleased\]/ {on=1; next} on && /^## \[/ {exit} on && /^- /' CHANGELOG.md)
if [[ -z "$unreleased" ]]; then
    echo "Describe this release under \"## [Unreleased]\" in CHANGELOG.md first."
    exit 1
fi

sed -i -E "s/^__version__ = \"[^\"]+\"$/__version__ = \"$version\"/" vproc/__init__.py
sed -i -E "s/^(#?VPROC_VERSION=).+$/\1$version/" .env.example
sed -i -E \
    -e "s/^## \[Unreleased\]$/## [Unreleased]\n\n## [$version] - $(date +%F)/" \
    -e "s#^\[Unreleased\]: (.+)/compare/(v.+)\.\.\.HEAD\$#[Unreleased]: \1/compare/v$version...HEAD\n[$version]: \1/compare/\2...v$version#" \
    CHANGELOG.md
git --no-pager diff --stat

git commit -qam "Version $version"
git tag "v$version"

echo
echo "Committed and tagged v$version. To publish it:"
echo "  git push origin main && git push origin v$version"
