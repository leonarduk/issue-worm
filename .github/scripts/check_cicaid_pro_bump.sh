#!/usr/bin/env bash
# Checks whether leonarduk/cicaid-pro has a newer release tag than the one
# pinned in _ai-pr-review.yml, and rewrites the pin in place if so. Run by
# cicaid-pro-bump-check.yml, which is responsible for turning a changed
# working tree into a PR -- this script only edits the file, it never
# commits, pushes, or opens anything.
#
# Required env: CICAID_PRO_TOKEN (same fine-grained PAT used to install the
# package, reused here with git ls-remote since cicaid-pro is private).
# Writes to $GITHUB_OUTPUT: bumped=true/false, old_version, new_version
set -euo pipefail

WORKFLOW_FILE=".github/workflows/_ai-pr-review.yml"

if [ -z "${CICAID_PRO_TOKEN:-}" ]; then
  echo "::error::CICAID_PRO_TOKEN is empty or unset. Add a fine-grained PAT (Contents: Read-only, scoped to leonarduk/cicaid-pro) as the CICAID_PRO_TOKEN repository secret before this check can list its tags." >&2
  exit 1
fi

CURRENT=$(grep -oP 'cicaid-devtools-pro @ git\+https://github\.com/leonarduk/cicaid-pro\.git@\Kv[0-9]+\.[0-9]+\.[0-9]+' "$WORKFLOW_FILE" || true)

if [ -z "$CURRENT" ]; then
  echo "::error::Could not find a 'cicaid-devtools-pro @ git+...@vX.Y.Z' pin in $WORKFLOW_FILE — has the line been reformatted?" >&2
  exit 1
fi

LATEST=$(git -c "url.https://x-access-token:${CICAID_PRO_TOKEN}@github.com/leonarduk/cicaid-pro.insteadOf=https://github.com/leonarduk/cicaid-pro" \
  ls-remote --tags --refs https://github.com/leonarduk/cicaid-pro.git \
  | sed 's#.*refs/tags/##' \
  | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
  | sort -V \
  | tail -1)

if [ -z "$LATEST" ]; then
  echo "::error::Could not list any vX.Y.Z tags on leonarduk/cicaid-pro." >&2
  exit 1
fi

echo "Current pin: $CURRENT"
echo "Latest tag:  $LATEST"

{
  echo "old_version=$CURRENT"
  echo "new_version=$LATEST"
} >> "$GITHUB_OUTPUT"

if [ "$CURRENT" = "$LATEST" ]; then
  echo "Already on the latest tag."
  echo "bumped=false" >> "$GITHUB_OUTPUT"
  exit 0
fi

sed -i "s#cicaid-devtools-pro @ git+https://github.com/leonarduk/cicaid-pro.git@${CURRENT}#cicaid-devtools-pro @ git+https://github.com/leonarduk/cicaid-pro.git@${LATEST}#" "$WORKFLOW_FILE"
echo "bumped=true" >> "$GITHUB_OUTPUT"
