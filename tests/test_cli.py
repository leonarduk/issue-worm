"""Tests for the free-shell CLI: history/create/build work standalone; the
issue-worm-pro commands (triage/poll) report themselves unavailable
rather than crashing on a missing private package.
"""

import sys
from unittest.mock import patch

import pytest

import cli
from review import ReviewResult
from workspace import FileChange, MalformedOutputError


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


def test_build_reports_fetch_failure(capsys):
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value=None), pytest.raises(
        SystemExit
    ) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "Could not fetch issue" in capsys.readouterr().err


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
