"""Tests for scripts/bump_readme_version.py (#193, #194).

No network and no writes to the repo's real README — file-rewriting tests
run against a tmp_path fixture. One test reads the real README to catch it
drifting out of a form the script's regex can rewrite.
"""

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bump_readme_version import bump, main  # noqa: E402

OLD_URL = (
    "https://github.com/leonarduk/issue-worm/releases/download/"
    "v0.2.0/issue_worm-0.2.0-py3-none-any.whl"
)
NEW_URL = (
    "https://github.com/leonarduk/issue-worm/releases/download/"
    "v0.3.0/issue_worm-0.3.0-py3-none-any.whl"
)


def test_bump_rewrites_pinned_wheel_url():
    text = f"Install:\n```bash\npip install {OLD_URL}\n```\n"
    updated = bump(text, "v0.3.0")
    assert NEW_URL in updated
    assert OLD_URL not in updated


def test_bump_accepts_version_without_v_prefix():
    text = f"pip install {OLD_URL}\n"
    updated = bump(text, "0.3.0")
    assert NEW_URL in updated


def test_bump_leaves_unrelated_wheel_urls_untouched():
    """A wheel URL pointing at a different repo (e.g. one of
    issue-worm-pro's own) must never be rewritten."""
    other = (
        "https://github.com/leonarduk/issue-worm-pro/releases/download/"
        "v0.2.0/issue_worm-0.2.0-py3-none-any.whl"
    )
    text = f"pip install {other}\n"
    assert bump(text, "v0.3.0") == text


def test_bump_is_idempotent_when_already_current():
    text = f"pip install {NEW_URL}\n"
    assert bump(text, "v0.3.0") == text


def test_main_rewrites_readme_in_tmp_path(tmp_path, monkeypatch):
    readme = tmp_path / "README.md"
    readme.write_text(f"## Install\n\npip install {OLD_URL}\n", encoding="utf-8")

    import bump_readme_version

    monkeypatch.setattr(bump_readme_version, "README_PATH", readme)

    with patch.object(sys, "argv", ["bump_readme_version.py", "v0.3.0"]):
        assert main() == 0

    updated = readme.read_text(encoding="utf-8")
    assert NEW_URL in updated
    assert OLD_URL not in updated


def test_main_without_version_arg_errors(capsys):
    with patch.object(sys, "argv", ["bump_readme_version.py"]):
        assert main() == 1
    assert "Usage" in capsys.readouterr().err


def test_real_readme_has_a_pinned_wheel_url_the_script_can_rewrite():
    """Guards against the README drifting into a form (e.g. back to
    `pip install issue-worm`, #194) that bump() can no longer find and
    rewrite on release.

    Runs the script's logic against the *real* README.md with a realistic
    version string, asserting successful execution and correct wheel-URL
    rewriting — without modifying the actual file.
    """
    readme_path = REPO_ROOT / "README.md"
    original = readme_path.read_text(encoding="utf-8")

    # bump() must complete without raising (successful execution)
    updated = bump(original, "v9.9.9")

    # The script must actually rewrite something (regex still matches)
    assert updated != original, (
        "bump() returned the README unchanged; the pinned wheel URL "
        "may have drifted out of the regex the script expects."
    )

    # The new version must appear in the wheel-URL location
    assert "v9.9.9" in updated
    assert "issue_worm-9.9.9-py3-none-any.whl" in updated

    # Idempotency: running bump again with the same version is a no-op
    assert bump(updated, "v9.9.9") == updated

    # The real README on disk must be unmodified
    assert readme_path.read_text(encoding="utf-8") == original
