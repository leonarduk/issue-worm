"""The floating action tag (v1) must never be mistaken for a release.

release.yml moves v1 to the newest vX.Y.Z commit so consumers can write
``uses: leonarduk/issue-worm@v1`` (leonarduk/issue-worm-pro#581). Three
places must keep ignoring it: the release trigger (or a "v1" release becomes
releases/latest for version_checker.py), setuptools-scm's describe (or the
wheel is built as version 1), and the "is this the newest release" guard
that decides whether to move v1 at all.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _describe_command() -> list[str]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["setuptools_scm"]["scm"]["git"]["describe_command"]


def _release_tag_filters() -> list[str]:
    text = RELEASE_YML.read_text(encoding="utf-8")
    block = re.search(r"^on:\n  push:\n    tags:\n((?:      .*\n)+)", text, re.MULTILINE)
    assert block, "release.yml no longer has an on.push.tags block"
    return re.findall(r'^      - "([^"]+)"', block.group(1), re.MULTILINE)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


def _github_filter_matches(pattern: str, ref: str) -> bool:
    # GitHub's filter syntax, for the subset release.yml uses or has used:
    # literal characters, [0-9] ranges, "+" (one or more of the preceding
    # character) and "*" (any run of characters except "/"). A filter must
    # match the whole ref name.
    regex = (
        re.escape(pattern)
        .replace(r"\[0\-9\]", "[0-9]")
        .replace(r"\+", "+")
        .replace(r"\*", "[^/]*")
    )
    return re.fullmatch(regex, ref) is not None


def test_release_trigger_ignores_floating_major_tag():
    filters = _release_tag_filters()
    assert filters, "release.yml has no tag filters"
    for ref in ("v1", "v2", "v1.0.0-rc1"):
        assert not any(_github_filter_matches(f, ref) for f in filters), ref
    assert any(_github_filter_matches(f, "v0.2.3") for f in filters)


def test_version_is_derived_from_semver_tag_not_floating_tag(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, "a")
    _git(tmp_path, "tag", "v0.2.3")
    # Annotated: the case where git describe's default --match picks v1.
    _git(tmp_path, "tag", "-a", "v1", "-m", "v1")

    assert _git(tmp_path, *_describe_command()[1:]).startswith("v0.2.3-0-g")


def test_newest_release_guard_ignores_floating_tag(tmp_path):
    step = RELEASE_YML.read_text(encoding="utf-8")
    listing = re.search(r"git tag --list '([^']+)' --sort=-v:refname", step)
    assert listing, "release.yml's newest-release guard changed shape"

    _git(tmp_path, "init", "-q")
    for tag in ("v0.2.3", "v0.3.0", "v0.2.9"):
        _commit(tmp_path, tag)
        _git(tmp_path, "tag", tag)
    _git(tmp_path, "tag", "v1")

    newest = _git(tmp_path, "tag", "--list", listing.group(1), "--sort=-v:refname").splitlines()[0]
    assert newest == "v0.3.0"
