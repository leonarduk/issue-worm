"""Tests for scripts/update_dependency_pins.py's cicaid pin updater.

All HTTP is mocked — no network, no live GitHub calls. File-rewriting tests
run against a tmp_path fixture, never the repo's real pins; the one test that
does read the real files only reads them, to catch the pins and the regexes
drifting apart.
"""

import ast
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from update_dependency_pins import (  # noqa: E402
    AI_REVIEW_WORKFLOW,
    PinError,
    _versions_equal,
    apply_update,
    current_pin,
    latest_version,
    main,
)

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "update-dependencies.yml"


def _workflow_check_regex() -> re.Pattern:
    """The CHECK_RE the update workflow validates the --check line with.

    Read out of the workflow rather than copied here: a hand copy would drift
    silently, and the point is that a change to either side fails a test
    instead of a nightly run.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"^\s*CHECK_RE='([^']+)'", text, re.MULTILINE)
    assert match, f"no CHECK_RE assignment found in {WORKFLOW}"
    return re.compile(match.group(1))


CHECK_LINE_RE = _workflow_check_regex()

REQUIREMENTS = (
    "cicaid-devtools[dotenv] @ git+https://github.com/leonarduk/cicaid.git@v0.8.1\n"
    "python-dotenv>=1.0\n"
    "\n"
    "# comment line stays untouched\n"
    "requests>=2.34.2,<3\n"
)
PYPROJECT = (
    "dependencies = [\n"
    '    "cicaid-devtools[dotenv] @ git+https://github.com/leonarduk/'
    'cicaid.git@v0.8.1",\n'
    '    "python-dotenv>=1.0",\n'
    "]\n"
)
# Both pins share a line in the real workflow's install step; keep that here,
# since "rewrite one without touching the other" is the sharp edge.
AI_REVIEW = (
    "      - name: Install cicaid-devtools + cicaid-devtools-pro\n"
    "        run: bash .github/scripts/pip_install_cicaid_pro.sh pip install "
    '"cicaid-devtools @ git+https://github.com/leonarduk/cicaid.git@v0.8.1" '
    '"cicaid-devtools-pro @ git+https://github.com/leonarduk/'
    'cicaid-pro.git@v0.11.4"\n'
)

PIN_FILES = {
    "requirements.txt": REQUIREMENTS,
    "pyproject.toml": PYPROJECT,
    AI_REVIEW_WORKFLOW: AI_REVIEW,
}


def _read(path: Path) -> str:
    # Mirrors the helper's newline="" I/O so comparisons are byte-faithful
    # regardless of the host platform's newline translation.
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write_repo(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


@pytest.fixture
def repo(tmp_path):
    return _write_repo(tmp_path, PIN_FILES)


def test_free_update_rewrites_every_file_that_pins_it(repo):
    changed = apply_update("cicaid-devtools", "0.9.0", root=repo)
    assert changed == ["requirements.txt", "pyproject.toml", AI_REVIEW_WORKFLOW]
    for name in PIN_FILES:
        assert "cicaid.git@v0.9.0" in _read(repo.joinpath(*name.split("/")))


def test_pro_update_rewrites_the_workflow_only(repo):
    # cicaid-devtools-pro is not an issue-worm dependency: it is pinned in
    # the review workflow and nowhere else.
    changed = apply_update("cicaid-devtools-pro", "0.14.1", root=repo)
    assert changed == [AI_REVIEW_WORKFLOW]
    assert "cicaid-pro.git@v0.14.1" in _read(
        repo.joinpath(*AI_REVIEW_WORKFLOW.split("/"))
    )
    assert _read(repo / "requirements.txt") == REQUIREMENTS
    assert _read(repo / "pyproject.toml") == PYPROJECT


def test_free_update_does_not_touch_the_pro_pin_on_the_same_line(repo):
    apply_update("cicaid-devtools", "0.9.0", root=repo)
    text = _read(repo.joinpath(*AI_REVIEW_WORKFLOW.split("/")))
    assert "cicaid.git@v0.9.0" in text
    assert "cicaid-pro.git@v0.11.4" in text
    assert "cicaid-pro.git@v0.9.0" not in text


def test_pro_update_does_not_touch_the_free_pin_on_the_same_line(repo):
    apply_update("cicaid-devtools-pro", "0.14.1", root=repo)
    text = _read(repo.joinpath(*AI_REVIEW_WORKFLOW.split("/")))
    assert "cicaid-pro.git@v0.14.1" in text
    assert "cicaid.git@v0.8.1" in text


def test_update_preserves_crlf(tmp_path):
    _write_repo(
        tmp_path, {name: text.replace("\n", "\r\n") for name, text in PIN_FILES.items()}
    )
    apply_update("cicaid-devtools", "0.9.0", root=tmp_path)
    text = (tmp_path / "requirements.txt").read_bytes()
    assert b"\r\n" in text
    assert b"cicaid.git@v0.9.0\r\n" in text


def test_up_to_date_is_noop(repo):
    assert apply_update("cicaid-devtools", "0.8.1", root=repo) == []
    assert apply_update("cicaid-devtools-pro", "0.11.4", root=repo) == []
    for name, original in PIN_FILES.items():
        assert _read(repo.joinpath(*name.split("/"))) == original


def test_dry_run_writes_nothing(repo):
    changed = apply_update("cicaid-devtools", "0.9.0", root=repo, dry_run=True)
    assert changed == ["requirements.txt", "pyproject.toml", AI_REVIEW_WORKFLOW]
    for name, original in PIN_FILES.items():
        assert _read(repo.joinpath(*name.split("/"))) == original


def test_current_pin_reads_the_shared_version(repo):
    assert current_pin("cicaid-devtools", root=repo) == "0.8.1"
    assert current_pin("cicaid-devtools-pro", root=repo) == "0.11.4"


def test_latest_strips_leading_v():
    with patch(
        "update_dependency_pins._http_json", return_value={"tag_name": "v0.9.0"}
    ):
        assert latest_version("cicaid-devtools") == "0.9.0"
        assert latest_version("cicaid-devtools-pro", token="secret") == "0.9.0"


def test_latest_pro_passes_token():
    # cicaid-pro is private; the releases call must forward GITHUB_TOKEN.
    with patch(
        "update_dependency_pins._http_json", return_value={"tag_name": "v0.9.0"}
    ) as mock_http:
        latest_version("cicaid-devtools-pro", token="secret")
        mock_http.assert_called_once_with(
            "https://api.github.com/repos/leonarduk/cicaid-pro/releases/latest",
            token="secret",
        )


def test_latest_pro_without_token_raises_clear_error():
    # A missing token would otherwise surface as an opaque 404 from GitHub
    # (the same response a private repo gives for "no releases exist").
    with patch("update_dependency_pins._http_json") as mock_http:
        with pytest.raises(PinError, match="GITHUB_TOKEN"):
            latest_version("cicaid-devtools-pro")
        mock_http.assert_not_called()


def test_latest_pro_404_error_names_the_likely_cause():
    with patch(
        "update_dependency_pins._http_json",
        side_effect=PinError("failed to fetch https://example.invalid: HTTP Error 404"),
    ):
        with pytest.raises(PinError, match="token has expired or lost read access"):
            latest_version("cicaid-devtools-pro", token="secret")


def test_latest_free_does_not_pass_token():
    with patch(
        "update_dependency_pins._http_json", return_value={"tag_name": "v0.9.0"}
    ) as mock_http:
        latest_version("cicaid-devtools", token="secret")
        mock_http.assert_called_once_with(
            "https://api.github.com/repos/leonarduk/cicaid/releases/latest",
            token=None,
        )


def test_latest_rejects_non_v_tag():
    with patch("update_dependency_pins._http_json", return_value={"tag_name": "0.9.0"}):
        with pytest.raises(PinError):
            latest_version("cicaid-devtools")


def test_http_failure_raises_pin_error():
    with patch(
        "update_dependency_pins._http_json",
        side_effect=PinError("failed to fetch https://example.invalid: boom"),
    ):
        with pytest.raises(PinError):
            latest_version("cicaid-devtools")


def test_check_mode_reports_update_without_writing(repo, capsys):
    with (
        patch("update_dependency_pins._http_json", return_value={"tag_name": "v0.9.0"}),
        patch("update_dependency_pins.ROOT", repo),
    ):
        assert main(["cicaid-devtools", "--check"]) == 0
    out = capsys.readouterr().out
    # The workflow parses this exact line (UPDATE <dep> <old> -> <new>); the
    # assertion pins the contract so the two cannot silently drift apart.
    assert out.splitlines() == ["UPDATE cicaid-devtools 0.8.1 -> 0.9.0"]
    assert CHECK_LINE_RE.match(out.splitlines()[0])
    for name, original in PIN_FILES.items():
        assert _read(repo.joinpath(*name.split("/"))) == original


def test_check_mode_reports_up_to_date(repo, capsys):
    with (
        patch("update_dependency_pins._http_json", return_value={"tag_name": "v0.8.1"}),
        patch("update_dependency_pins.ROOT", repo),
    ):
        assert main(["cicaid-devtools", "--check"]) == 0
    assert capsys.readouterr().out.splitlines() == ["UP-TO-DATE cicaid-devtools 0.8.1"]


def test_write_mode_updates_and_exits_zero(repo):
    with (
        patch(
            "update_dependency_pins._http_json", return_value={"tag_name": "v0.14.1"}
        ),
        patch("update_dependency_pins.ROOT", repo),
    ):
        assert main(["cicaid-devtools-pro"]) == 0
    assert "cicaid-pro.git@v0.14.1" in _read(
        repo.joinpath(*AI_REVIEW_WORKFLOW.split("/"))
    )


def test_rewrites_all_pin_occurrences_in_a_file(repo):
    # A second install step pinning the same package must be rewritten too,
    # not just the first.
    doubled = AI_REVIEW + AI_REVIEW
    repo.joinpath(*AI_REVIEW_WORKFLOW.split("/")).write_bytes(doubled.encode("utf-8"))
    apply_update("cicaid-devtools", "0.9.0", root=repo)
    text = _read(repo.joinpath(*AI_REVIEW_WORKFLOW.split("/")))
    assert text.count("cicaid.git@v0.9.0") == 2
    assert "cicaid.git@v0.8.1" not in text
    assert text.count("cicaid-pro.git@v0.11.4") == 2


def test_drift_between_files_raises(tmp_path, capsys):
    files = dict(PIN_FILES)
    files["pyproject.toml"] = PYPROJECT.replace("cicaid.git@v0.8.1", "cicaid.git@v0.8.0")
    _write_repo(tmp_path, files)
    with pytest.raises(PinError, match="drifted"):
        current_pin("cicaid-devtools", root=tmp_path)
    with (
        patch("update_dependency_pins._http_json", return_value={"tag_name": "v0.9.0"}),
        patch("update_dependency_pins.ROOT", tmp_path),
    ):
        assert main(["cicaid-devtools", "--check"]) == 1
    err = capsys.readouterr().err
    assert "drifted" in err
    # The message must name the files, so the fix is obvious from the log.
    assert "pyproject.toml has v0.8.0" in err
    assert "requirements.txt has v0.8.1" in err


def test_missing_pin_raises(tmp_path):
    files = dict(PIN_FILES)
    files["requirements.txt"] = "python-dotenv>=1.0\n"
    _write_repo(tmp_path, files)
    with pytest.raises(PinError, match="no cicaid-devtools pin found"):
        current_pin("cicaid-devtools", root=tmp_path)


def test_unknown_dependency_rejected():
    with pytest.raises(PinError):
        latest_version("not-a-dependency")


def test_real_repo_pins_are_readable_and_agree():
    """The regexes must keep matching the repo's actual files.

    This is the check the pins themselves are for: a rename, a reformat, or a
    hand-edited pin in one file only fails here rather than in a nightly run.
    """
    assert current_pin("cicaid-devtools", root=REPO_ROOT)
    assert current_pin("cicaid-devtools-pro", root=REPO_ROOT)


def test_script_imports_only_stdlib():
    """The helper must stay stdlib-only: it runs when pins are stale/broken,
    so it must never import cicaid-devtools or any project module."""
    source = (REPO_ROOT / "scripts" / "update_dependency_pins.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    stdlib = {
        "__future__",
        "argparse",
        "dataclasses",
        "json",
        "os",
        "re",
        "sys",
        "urllib",
        "pathlib",
        "typing",
    }
    assert imported <= stdlib, f"non-stdlib imports found: {imported - stdlib}"


@pytest.mark.parametrize(
    ("left", "right", "equal"),
    [
        # Zero-padding differences name the same release.
        ("1.0", "1.0.0", True),
        ("1", "1.0.0", True),
        ("0.9.0.0", "0.9", True),
        # Real differences stay different.
        ("1.0.0", "1.0.1", False),
        ("2.10", "2.1", False),
        # A pre-release is never its final release.
        ("0.9.0", "0.9.0rc1", False),
        ("0.9.0rc1", "0.9.0rc2", False),
        # PEP 440 local versions are significant...
        ("1.0.0+abc123", "1.0.0", False),
        # ...but their case is not (PEP 440 normalizes it).
        ("1.0.0+ABC", "1.0.0+abc", True),
        ("0.9.0RC1", "0.9.0rc1", True),
        # Unparseable versions fall back to string equality.
        ("nightly", "nightly", True),
        ("nightly", "weekly", False),
    ],
)
def test_versions_equal(left, right, equal):
    assert _versions_equal(left, right) is equal
    # Equality is symmetric whichever branch handled it.
    assert _versions_equal(right, left) is equal


def test_zero_padded_latest_reports_up_to_date(repo, capsys):
    """A latest of 0.8.1.0 against a pin of 0.8.1 is not an update.

    String equality reported this as an update and would have opened the same
    no-op PR every night.
    """
    with (
        patch(
            "update_dependency_pins._http_json", return_value={"tag_name": "v0.8.1.0"}
        ),
        patch("update_dependency_pins.ROOT", repo),
    ):
        assert main(["cicaid-devtools", "--check"]) == 0
    assert capsys.readouterr().out.splitlines() == ["UP-TO-DATE cicaid-devtools 0.8.1"]
    assert _read(repo / "requirements.txt") == REQUIREMENTS


def test_check_wins_over_dry_run(repo, capsys):
    """`--check --dry-run` behaves exactly like `--check` alone.

    The workflow only ever passes --check, but the precedence is worth
    pinning: --check returns before apply_update is reached, so --dry-run can
    never change the line the workflow parses.
    """
    with (
        patch("update_dependency_pins._http_json", return_value={"tag_name": "v0.9.0"}),
        patch("update_dependency_pins.ROOT", repo),
    ):
        assert main(["cicaid-devtools", "--check", "--dry-run"]) == 0

    out = capsys.readouterr().out.splitlines()
    # The --check line, not --dry-run's "WOULD UPDATE ... (files)" form.
    assert out == ["UPDATE cicaid-devtools 0.8.1 -> 0.9.0"]
    assert CHECK_LINE_RE.match(out[0])
    for name, original in PIN_FILES.items():
        assert _read(repo.joinpath(*name.split("/"))) == original


def test_check_line_matches_workflow_regex_for_local_version(repo, capsys):
    """A PEP 440 local version still yields a line the workflow accepts.

    The workflow's regex constrains versions to the PEP 440 character set, so
    `+` must pass while a shell metacharacter must not.
    """
    with (
        patch(
            "update_dependency_pins._http_json",
            return_value={"tag_name": "v0.99.0+abc123"},
        ),
        patch("update_dependency_pins.ROOT", repo),
    ):
        assert main(["cicaid-devtools", "--check"]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert line == "UPDATE cicaid-devtools 0.8.1 -> 0.99.0+abc123"
    assert CHECK_LINE_RE.match(line)
    # A version carrying shell syntax must be rejected by that same regex
    # rather than reaching `git commit -m` or a branch name.
    assert not CHECK_LINE_RE.match("UPDATE cicaid-devtools 0.8.1 -> 1.0;rm -rf /")
