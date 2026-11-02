"""Print issue-worm-pro's runtime requirements with issue-worm swapped for
a local checkout, for ci.yml's dependency-graph job to resolve (#103).

issue-worm-pro pins both a released issue-worm and cicaid-devtools, and pip
resolves one version of a git-pinned package across the whole graph. So when
an issue-worm change moves its own cicaid-devtools pin, pro's install breaks
with ResolutionImpossible until both pins move together (#100/#101). That was
only ever found by hand. Resolving pro's requirement list with issue-worm
replaced by this checkout finds it in the PR that causes it.

Usage: python scripts/pro_dependency_requirements.py <pro-pyproject.toml> <issue-worm-dir>
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

# A PEP 508 requirement's distribution name and any extras.
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?")


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def swap_issue_worm(requirements: list[str], checkout: Path) -> list[str]:
    """Return ``requirements`` with the issue-worm entry pointed at ``checkout``.

    Raises ValueError when pro doesn't depend on issue-worm exactly once:
    either way the check would no longer test what it claims to.
    """
    uri = checkout.resolve().as_uri()
    out: list[str] = []
    swapped = 0
    for requirement in requirements:
        match = _NAME_RE.match(requirement)
        if match and _normalise(match.group(1)) == "issue-worm":
            # Keep any extras pro asks for: they add dependencies to resolve.
            extras = (match.group(2) or "").replace(" ", "")
            out.append(f"issue-worm{extras} @ {uri}")
            swapped += 1
        else:
            out.append(requirement.strip())
    if swapped != 1:
        raise ValueError(f"expected one issue-worm requirement in pro, found {swapped}")
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(
            "usage: pro_dependency_requirements.py <pro-pyproject.toml> <issue-worm-dir>",
            file=sys.stderr,
        )
        return 2
    try:
        pyproject = tomllib.loads(Path(args[0]).read_text(encoding="utf-8"))
        requirements = swap_issue_worm(
            pyproject["project"]["dependencies"], Path(args[1])
        )
    except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"::error::cannot build pro's requirement list: {exc}", file=sys.stderr)
        return 1
    print("\n".join(requirements))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
