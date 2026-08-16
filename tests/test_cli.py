"""Tests for the free-shell CLI: history/create work standalone; the
issue-worm-core commands (triage/build/poll) report themselves
unavailable rather than crashing on a missing private package.
"""

import sys
from unittest.mock import patch

import pytest

import cli


@pytest.mark.parametrize("command", ["triage", "build", "poll"])
def test_core_command_reports_unavailable(command, capsys):
    with patch.object(sys, "argv", ["issue-worm", command]), pytest.raises(
        SystemExit
    ) as exc:
        cli.main()

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert command in captured.err
    assert "issue-worm-core" in captured.err


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
