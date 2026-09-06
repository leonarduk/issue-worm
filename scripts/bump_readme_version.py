"""Rewrite the pinned issue-worm wheel install URL to a given release version.

Run by the release workflow after a GitHub Release is published, so
``README.md``'s ``## Install`` section always shows the version that was
just released instead of going stale (#193, #194).

Usage: python scripts/bump_readme_version.py v0.1.0
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README_PATH = ROOT / "README.md"

# A pinned wheel install URL, e.g.
#   https://github.com/leonarduk/issue-worm/releases/download/v0.1.0/issue_worm-0.1.0-py3-none-any.whl
# The release tag and the version inside the wheel filename must both move,
# and they must stay identical to each other.
#
# The repo path is pinned to the canonical repo on purpose: installs always
# come from leonarduk/issue-worm, so a wheel URL pointing at any other repo
# (e.g. one of issue-worm-pro's own) must never be rewritten.
_WHEEL_URL_RE = re.compile(
    r"(?P<prefix>https://github\.com/leonarduk/issue-worm/releases/download/)"
    r"v(?P<tag>[^/\s]+)/"
    r"(?P<wheel>issue_worm-)[^/\s]+(?P<suffix>-py3-none-any\.whl)"
)


def bump(text: str, version: str) -> str:
    """Rewrite every pinned wheel URL in ``text`` to ``version`` (e.g. v0.1.0)."""

    def _replace(match: re.Match[str]) -> str:
        tag = version if version.startswith("v") else f"v{version}"
        return (
            f"{match.group('prefix')}{tag}/"
            f"{match.group('wheel')}{tag[1:]}{match.group('suffix')}"
        )

    return _WHEEL_URL_RE.sub(_replace, text)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite README.md's install command to a given release version"
    )
    parser.add_argument("version", nargs="?", help="Release version tag (e.g. v0.1.0)")
    args = parser.parse_args()

    if not args.version:
        print("Usage: python scripts/bump_readme_version.py vX.Y.Z", file=sys.stderr)
        return 1

    original = README_PATH.read_text(encoding="utf-8")
    updated = bump(original, args.version)
    if updated == original:
        print(f"{README_PATH.name} already up to date; nothing to change.")
        return 0
    README_PATH.write_text(updated, encoding="utf-8")
    print(f"Updated {README_PATH.name} to {args.version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
