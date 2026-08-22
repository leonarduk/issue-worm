"""Tests for the free-shell CLI: history/create/build work standalone; the
issue-worm-pro commands (triage/poll) report themselves unavailable
rather than crashing on a missing private package.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

import cli
from review import ReviewResult
from workspace import FileChange, MalformedOutputError, WorkspaceError


@pytest.mark.parametrize("command", ["triage", "poll"])
def test_core_command_reports_unavailable(command, capsys):
    with patch.object(sys, "argv", ["issue-worm", command]), pytest.raises(
        SystemExit
    ) as exc:
        cli.main()

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert command in captured.err
    assert "issue-worm-pro" in captured.err


def test_history_with_no_runs_prints_message(tmp_path, capsys):
    history_path = str(tmp_path / "history.jsonl")
    with patch.object(
        sys, "argv", ["issue-worm", "history", "--history-path", history_path]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "No runs recorded yet." in capsys.readouterr().out


def test_no_command_prints_help_and_exits_nonzero(capsys):
    with patch.object(sys, "argv", ["issue-worm"]), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1


def test_version_flag_prints_name_and_exits_zero(capsys):
    with patch.object(
        sys, "argv", ["issue-worm", "--version"]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "issue-worm" in out
    assert "public shell" in out


def test_build_without_repo_fails_fast(capsys):
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5"]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "--repo" in capsys.readouterr().err


def test_build_without_issue_fails_fast(capsys):
    with patch.object(
        sys, "argv", ["issue-worm", "build", "--repo", "o/r"]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "issue number" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bad_repo", ["no-slash", "too/many/slashes", "owner/", "/name"]
)
def test_build_rejects_malformed_repo(bad_repo, capsys):
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", bad_repo]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "Invalid --repo" in capsys.readouterr().err


def test_build_accepts_well_formed_repo(capsys):
    # Valid --repo should get past validation and fail later, at the
    # (mocked) fetch step, not at the format check.
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "octocat/Hello-World"]
    ), patch("cli._fetch_issue_body", return_value=None), pytest.raises(
        SystemExit
    ) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "Invalid --repo" not in capsys.readouterr().err


def test_build_multiple_issues_warns_and_uses_first(capsys):
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "1", "2", "3", "--repo", "o/r", "--dry-run"]
    ), patch("cli._fetch_issue_body", return_value="body") as mock_fetch, patch(
        "cli.review_issue", return_value=ready
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    mock_fetch.assert_called_once_with("o/r", 1)
    err = capsys.readouterr().err
    assert "ignoring" in err
    assert "[2, 3]" in err


def test_build_reports_fetch_failure(capsys):
    # _fetch_issue_body prints its own specific reason before returning
    # None; _run_build must not print a second, redundant message.
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value=None), pytest.raises(
        SystemExit
    ) as exc:
        cli.main()

    assert exc.value.code == 1
    assert capsys.readouterr().err == ""


def test_build_reports_not_ready_issue(capsys):
    not_ready = ReviewResult(ready=False, message="needs FILES/DONE")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="plain body"), patch(
        "cli.review_issue", return_value=not_ready
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "needs FILES/DONE" in capsys.readouterr().err


def test_build_dry_run_stops_before_coder(capsys):
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r", "--dry-run"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch("cli.LocalOllamaCoder") as mock_coder_cls, pytest.raises(
        SystemExit
    ) as exc:
        cli.main()

    assert exc.value.code == 0
    mock_coder_cls.assert_not_called()
    out = capsys.readouterr().out
    assert "a.py" in out
    assert "it works" in out


def test_build_reports_empty_coder_output(tmp_path, capsys):
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch("cli.ensure_base_clone", return_value=str(tmp_path)), patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls, pytest.raises(SystemExit) as exc:
        mock_coder_cls.return_value.propose.return_value = ""
        cli.main()

    assert exc.value.code == 1
    assert "no output" in capsys.readouterr().err


def test_build_applies_changes_end_to_end(tmp_path, capsys):
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    change = FileChange(path="a.py", mode="FULL", body="print(1)\n")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch("cli.ensure_base_clone", return_value=str(tmp_path)), patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls, patch(
        "cli.parse_coder_output", return_value=[change]
    ), patch("cli.apply_file_change") as mock_apply, pytest.raises(
        SystemExit
    ) as exc:
        mock_coder_cls.return_value.propose.return_value = "raw coder output"
        cli.main()

    assert exc.value.code == 0
    mock_apply.assert_called_once_with(str(tmp_path), change)
    out = capsys.readouterr().out
    assert "a.py" in out


def test_build_workspace_flag_overrides_default(tmp_path):
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    custom = str(tmp_path / "custom-ws")
    with patch.object(
        sys,
        "argv",
        ["issue-worm", "build", "5", "--repo", "o/r", "--workspace", custom],
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch(
        "cli.ensure_base_clone", return_value=custom
    ) as mock_clone, patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls, patch(
        "cli.parse_coder_output", return_value=[]
    ), pytest.raises(SystemExit):
        mock_coder_cls.return_value.propose.return_value = "raw coder output"
        cli.main()

    mock_clone.assert_called_once_with(custom, "o/r")


def test_build_workspace_flag_beats_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "from-env"))
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    from_flag = str(tmp_path / "from-flag")
    with patch.object(
        sys,
        "argv",
        ["issue-worm", "build", "5", "--repo", "o/r", "--workspace", from_flag],
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch(
        "cli.ensure_base_clone", return_value=from_flag
    ) as mock_clone, patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls, patch(
        "cli.parse_coder_output", return_value=[]
    ), pytest.raises(SystemExit):
        mock_coder_cls.return_value.propose.return_value = "raw coder output"
        cli.main()

    mock_clone.assert_called_once_with(from_flag, "o/r")


def test_build_env_var_used_when_no_workspace_flag(tmp_path, monkeypatch):
    from_env = str(tmp_path / "from-env")
    monkeypatch.setenv("WORKSPACE_ROOT", from_env)
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch(
        "cli.ensure_base_clone", return_value=from_env
    ) as mock_clone, patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls, patch(
        "cli.parse_coder_output", return_value=[]
    ), pytest.raises(SystemExit):
        mock_coder_cls.return_value.propose.return_value = "raw coder output"
        cli.main()

    mock_clone.assert_called_once_with(from_env, "o/r")


def test_build_reports_malformed_coder_output(tmp_path, capsys):
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch("cli.ensure_base_clone", return_value=str(tmp_path)), patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls, patch(
        "cli.parse_coder_output", side_effect=MalformedOutputError("bad output")
    ), pytest.raises(SystemExit) as exc:
        mock_coder_cls.return_value.propose.return_value = "raw coder output"
        cli.main()

    assert exc.value.code == 1
    assert "bad output" in capsys.readouterr().err


def test_build_reports_apply_failure_without_crashing(tmp_path, capsys):
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    change = FileChange(path="a.py", mode="FULL", body="print(1)\n")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch("cli.ensure_base_clone", return_value=str(tmp_path)), patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls, patch(
        "cli.parse_coder_output", return_value=[change]
    ), patch(
        "cli.apply_file_change", side_effect=WorkspaceError("git apply failed")
    ), pytest.raises(SystemExit) as exc:
        mock_coder_cls.return_value.propose.return_value = "raw coder output"
        cli.main()

    assert exc.value.code == 1
    assert "git apply failed" in capsys.readouterr().err


def _mock_get_response(status_code, json_body=None, headers=None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = json_body or {}
    if status_code < 400:
        response.raise_for_status.return_value = None
    else:
        response.raise_for_status.side_effect = requests.HTTPError("bad status")
    return response


def test_fetch_issue_body_returns_body_on_success():
    with patch(
        "cli.requests.get",
        return_value=_mock_get_response(200, {"body": "the issue text"}),
    ):
        result = cli._fetch_issue_body("o/r", 5)

    assert result == "the issue text"


def test_fetch_issue_body_reports_not_found(capsys):
    with patch("cli.requests.get", return_value=_mock_get_response(404)):
        result = cli._fetch_issue_body("o/r", 999)

    assert result is None
    assert "not found" in capsys.readouterr().err


def test_fetch_issue_body_reports_rate_limit(capsys):
    with patch(
        "cli.requests.get",
        return_value=_mock_get_response(
            403, headers={"X-RateLimit-Remaining": "0"}
        ),
    ):
        result = cli._fetch_issue_body("o/r", 5)

    assert result is None
    assert "rate limit" in capsys.readouterr().err


def test_fetch_issue_body_reports_scope_error_for_403_without_rate_limit_header(capsys):
    with patch(
        "cli.requests.get", return_value=_mock_get_response(403, headers={})
    ):
        result = cli._fetch_issue_body("o/r", 5)

    assert result is None
    err = capsys.readouterr().err
    assert "403" in err
    assert "GITHUB_TOKEN" in err


def test_fetch_issue_body_reports_generic_request_error(capsys):
    with patch(
        "cli.requests.get", side_effect=requests.ConnectionError("dns failure")
    ):
        result = cli._fetch_issue_body("o/r", 5)

    assert result is None
    assert "GitHub request failed" in capsys.readouterr().err
