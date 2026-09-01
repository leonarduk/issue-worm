"""Check and update the repo's pinned cicaid versions (issue #142).

Two dependencies are tracked:

- cicaid-devtools — the public leonarduk/cicaid "free shell", pinned as a
  git+https URL in three places: ``requirements.txt``, ``pyproject.toml``
  (the package's own dependency) and the install step of
  ``.github/workflows/_ai-pr-review.yml`` (the PR-review jobs). "latest"
  comes from the GitHub Releases API, no token needed since the repo is
  public.
- cicaid-devtools-pro — the private leonarduk/cicaid-pro, pinned only in
  ``.github/workflows/_ai-pr-review.yml``: the review modules that workflow
  imports (review_diff, review_comment, deepseek_review, gpt_review, ...)
  live in that package, but issue-worm itself never depends on it. "latest"
  comes from the GitHub Releases API, which needs GITHUB_TOKEN since
  cicaid-pro is private.

Every file that pins a dependency must agree on the version — a pin that
silently drifts backwards in one of them is what issue #135 was — so
``current_pin`` treats disagreement as an error rather than picking one.

Stdlib-only by design: this script must run even when the pins are stale or
broken, so it must not import cicaid-devtools or any other project module.
The scheduled workflow (.github/workflows/update-dependencies.yml) uses
``--check`` to decide whether to open an update PR, then runs the script
again without ``--check`` on its own branch to rewrite the pins.

Adapted from issue-worm-pro's script of the same name, which tracks the same
two pins across that repo's own files (plus aider-chat).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Pinned files are named with forward slashes throughout: the names appear
# verbatim in this script's output and in the workflow's `git add`. _path
# joins them against the repo root in a platform-correct way.
REQUIREMENTS = "requirements.txt"
PYPROJECT = "pyproject.toml"
AI_REVIEW_WORKFLOW = ".github/workflows/_ai-pr-review.yml"

DEPS = ("cicaid-devtools", "cicaid-devtools-pro")

CICAID_FREE_RELEASES_API = (
    "https://api.github.com/repos/leonarduk/cicaid/releases/latest"
)
CICAID_PRO_RELEASES_API = (
    "https://api.github.com/repos/leonarduk/cicaid-pro/releases/latest"
)

# The negative lookahead on cicaid-devtools' pattern (not immediately
# followed by "-pro") keeps it from also matching the cicaid-devtools-pro
# pin - both share a "cicaid" prefix in their git+https URL, and in
# _ai-pr-review.yml both pins sit on the same line.
_CICAID_FREE_PIN_RE = re.compile(
    r"(git\+https://github\.com/leonarduk/cicaid(?!-pro)\.git@v)"
    r"([0-9][A-Za-z0-9.+-]*)"
)
_CICAID_PRO_PIN_RE = re.compile(
    r"(git\+https://github\.com/leonarduk/cicaid-pro\.git@v)([0-9][A-Za-z0-9.+-]*)"
)


@dataclass(frozen=True)
class _Spec:
    """Where a dependency is pinned, and where its latest version comes from."""

    pin_re: re.Pattern
    releases_api: str
    # cicaid-pro is private: its releases API call needs GITHUB_TOKEN,
    # unlike the public cicaid repo.
    needs_token: bool
    files: tuple[str, ...]
    repo_slug: str


_SPECS = {
    "cicaid-devtools": _Spec(
        pin_re=_CICAID_FREE_PIN_RE,
        releases_api=CICAID_FREE_RELEASES_API,
        needs_token=False,
        files=(REQUIREMENTS, PYPROJECT, AI_REVIEW_WORKFLOW),
        repo_slug="cicaid",
    ),
    "cicaid-devtools-pro": _Spec(
        pin_re=_CICAID_PRO_PIN_RE,
        releases_api=CICAID_PRO_RELEASES_API,
        needs_token=True,
        files=(AI_REVIEW_WORKFLOW,),
        repo_slug="cicaid-pro",
    ),
}

_USER_AGENT = "issue-worm-update-dependency-pins"


class PinError(Exception):
    """A pin is missing, drifted, or could not be checked."""


def _spec(dep: str) -> _Spec:
    try:
        return _SPECS[dep]
    except KeyError:
        raise PinError(
            f"unknown dependency {dep!r} (expected one of {', '.join(DEPS)})"
        ) from None


def _path(root: Path, name: str) -> Path:
    return root.joinpath(*name.split("/"))


def _read_text(path: Path) -> str:
    # newline="" keeps \r\n intact instead of normalizing to \n (and on
    # write, back again), so the pinned files' CRLF endings survive a
    # rewrite byte-for-byte on every platform — the update PR diff stays
    # clean.
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write_text(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _http_json(url: str, token: str | None = None) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        # OSError covers URLError/HTTPError/timeouts; ValueError covers
        # JSONDecodeError. Any of these means the version is uncheckable.
        # KeyboardInterrupt/SystemExit (BaseException) are intentionally
        # never swallowed.
        raise PinError(f"failed to fetch {url}: {exc}") from exc


def latest_version(dep: str, token: str | None = None) -> str:
    """Latest released version of ``dep`` (never a v-prefixed string)."""
    spec = _spec(dep)
    data = _http_json(spec.releases_api, token=token if spec.needs_token else None)
    tag = data.get("tag_name", "")
    if not tag.startswith("v"):
        raise PinError(
            f"unexpected {dep} release tag {tag!r}; expected a v-prefixed tag"
        )
    return tag[1:]


# A release string we can reason about semantically: dotted numeric
# components, then an optional suffix (a pre-release like "rc1", or a
# PEP 440 local version like "+abc123").
_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(.*)$")


def _parse_version(version: str) -> tuple[tuple[int, ...], str] | None:
    """Split ``version`` into (release components, suffix), or None.

    Trailing zero components are dropped so ``1.0`` and ``1.0.0`` parse
    identically. Returns None for anything that does not start with a dotted
    numeric release, so callers can fall back to string comparison.
    """
    match = _VERSION_RE.match(version.strip())
    if match is None:
        return None
    release = [int(part) for part in match.group(1).split(".")]
    while release and release[-1] == 0:
        release.pop()
    # Suffixes are compared case-insensitively, matching PEP 440's
    # normalization ("RC1" and "rc1" name the same release).
    return tuple(release), match.group(2).strip().lower()


def _versions_equal(left: str, right: str) -> bool:
    """True when two version strings name the same release.

    Exact string equality reports ``1.0`` and ``1.0.0`` as different and
    would open a pointless update PR every day. Compare the parsed release
    components instead, and keep any suffix significant so a pre-release is
    never mistaken for its final release (``0.9.0`` is not ``0.9.0rc1``).
    Versions this cannot parse — a date string, a hash — fall back to string
    equality.
    """
    parsed_left = _parse_version(left)
    parsed_right = _parse_version(right)
    if parsed_left is None or parsed_right is None:
        return left == right
    return parsed_left == parsed_right


def current_pin(dep: str, root: Path | None = None) -> str:
    """Currently pinned version of ``dep``, raising PinError on drift/gaps."""
    if root is None:
        root = ROOT
    spec = _spec(dep)
    found: dict[str, str] = {}
    for name in spec.files:
        text = _read_text(_path(root, name))
        match = spec.pin_re.search(text)
        if match is None:
            raise PinError(f"no {dep} pin found in {name}")
        found[name] = match.group(2)
    if len(set(found.values())) > 1:
        detail = ", ".join(f"{name} has v{version}" for name, version in found.items())
        raise PinError(f"{dep} pins drifted: {detail}")
    return found[spec.files[0]]


def _rewrite(dep: str, text: str, new_version: str) -> str:
    spec = _spec(dep)

    def replace(match: re.Match) -> str:
        return f"{match.group(1)}{new_version}"

    new_text, count = spec.pin_re.subn(replace, text)
    if count == 0:
        raise PinError(
            f"could not find the {dep} git+https pin in the file "
            f"(expected git+https://github.com/leonarduk/{spec.repo_slug}.git@vX.Y.Z)"
        )
    return new_text


def apply_update(
    dep: str, new_version: str, root: Path | None = None, dry_run: bool = False
) -> list[str]:
    """Rewrite every pin for ``dep`` to ``new_version``.

    Returns the names of the files that would change. Already-pinned versions
    (new_version == current) are a no-op returning an empty list. Nothing is
    written when ``dry_run`` is true.
    """
    if root is None:
        root = ROOT
    spec = _spec(dep)
    changed: list[str] = []
    for name in spec.files:
        path = _path(root, name)
        text = _read_text(path)
        new_text = _rewrite(dep, text, new_version)
        if new_text != text:
            if not dry_run:
                _write_text(path, new_text)
            changed.append(name)
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check/update the pinned cicaid-devtools[-pro] versions."
    )
    parser.add_argument("dependency", choices=DEPS)
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report whether an update exists; never writes files",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would change without writing"
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN")
    try:
        new_version = latest_version(args.dependency, token=token)
        current = current_pin(args.dependency)
    except PinError as exc:
        print(f"update_dependency_pins: {exc}", file=sys.stderr)
        return 1

    if _versions_equal(new_version, current):
        print(f"UP-TO-DATE {args.dependency} {current}")
        return 0

    if args.check:
        print(f"UPDATE {args.dependency} {current} -> {new_version}")
        return 0

    try:
        changed = apply_update(args.dependency, new_version, dry_run=args.dry_run)
    except PinError as exc:
        print(f"update_dependency_pins: {exc}", file=sys.stderr)
        return 1

    verb = "WOULD UPDATE" if args.dry_run else "UPDATED"
    detail = ", ".join(changed) if changed else "no files (already up to date)"
    print(f"{verb} {args.dependency} {current} -> {new_version} ({detail})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
