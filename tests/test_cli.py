"""Tests for the free-shell CLI: history/create/build work standalone; the
issue-worm-pro commands (triage/poll) delegate to the installed `pro_cli`
module when issue-worm-pro is present, and report themselves unavailable
rather than crashing when it isn't (#352).
"""

import os
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


@pytest.mark.parametrize("command", ["triage", "poll"])
def test_core_command_dispatches_to_pro_cli_when_installed(command, monkeypatch, capsys):
    """#352: pro_cli.main() re-parses the original sys.argv itself (it has
    its own full argparse setup), so this shell's placeholder subparser
    for `command` never needs to forward specific flags - it just has to
    get out of the way."""
    fake_pro_cli = MagicMock()
    fake_pro_cli.main.side_effect = SystemExit(0)
    monkeypatch.setitem(sys.modules, "pro_cli", fake_pro_cli)

    with patch.object(
        sys, "argv", ["issue-worm", command, "--repo", "owner/name"]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    fake_pro_cli.main.assert_called_once_with()
    assert exc.value.code == 0
    assert "issue-worm-pro" not in capsys.readouterr().err


@pytest.mark.parametrize("command", ["triage", "poll"])
def test_core_command_help_dispatches_before_this_shells_own_parser(command, monkeypatch, capsys):
    """The bug #352 flags as the reason it survived review: --help used to
    be swallowed by this shell's own placeholder subparser, silently
    showing the wrong (stub) flag set even when pro was installed. It must
    now reach pro_cli.main() like any other flag.

    Load-bearing for #352 in a way the --repo variant above isn't: --repo
    parses fine under the placeholder subparser too, so that test alone
    can't tell "dispatched before this shell's own parser ran" apart from
    "dispatched after parse_args() picked it out of args.command." Only
    --help (unknown to the placeholder subparser under the old design)
    actually distinguishes the two.
    """
    fake_pro_cli = MagicMock()
    fake_pro_cli.main.side_effect = SystemExit(0)
    monkeypatch.setitem(sys.modules, "pro_cli", fake_pro_cli)

    with patch.object(
        sys, "argv", ["issue-worm", command, "--help"]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    fake_pro_cli.main.assert_called_once_with()
    assert exc.value.code == 0


@pytest.mark.parametrize("command", ["triage", "poll"])
def test_core_command_help_falls_back_to_this_shell_when_pro_not_installed(
    command, monkeypatch, capsys
):
    """Without issue-worm-pro installed, --help still works and shows this
    shell's placeholder flag set, rather than erroring. Patches
    _try_import_pro_cli directly (rather than relying on `pro_cli` simply
    not being on sys.path) so this test is correct even in an environment
    where issue-worm-pro genuinely is installed alongside issue-worm."""
    monkeypatch.setattr(cli, "_try_import_pro_cli", lambda: None)

    with patch.object(
        sys, "argv", ["issue-worm", command, "--help"]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "--repo" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--version", "--vers"])
@pytest.mark.parametrize("command", ["triage", "poll"])
def test_version_flag_wins_over_dispatch_regardless_of_abbreviation(
    flag, command, monkeypatch, capsys
):
    """`--version` (and argparse's own abbreviations of it, like --vers)
    must always mean "print this shell's version and exit," even combined
    with a core command and even with pro_cli installed - dispatching to
    pro here would silently print pro's version instead."""
    fake_pro_cli = MagicMock()
    monkeypatch.setitem(sys.modules, "pro_cli", fake_pro_cli)

    with patch.object(
        sys, "argv", ["issue-worm", flag, command]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    fake_pro_cli.main.assert_not_called()
    assert exc.value.code == 0
    assert "issue-worm" in capsys.readouterr().out


@pytest.mark.parametrize("command", ["build", "history", "create"])
def test_non_core_command_never_dispatches_even_with_pro_installed(command, monkeypatch):
    """Only triage/poll are issue-worm-pro-only; every other command must
    keep running standalone in this shell regardless of whether pro_cli
    happens to be importable."""
    fake_pro_cli = MagicMock()
    monkeypatch.setitem(sys.modules, "pro_cli", fake_pro_cli)

    argv = {
        "build": ["issue-worm", "build", "--dry-run"],
        "history": ["issue-worm", "history"],
        "create": ["issue-worm", "create"],
    }[command]
    with patch.object(sys, "argv", argv), patch("subprocess.run"):
        with pytest.raises(SystemExit):
            cli.main()

    fake_pro_cli.main.assert_not_called()


@pytest.mark.parametrize("command", ["triage", "poll"])
def test_check_and_prompt_runs_once_either_way(command, monkeypatch):
    """check_and_prompt (this shell's self-update check) is skipped on the
    early-dispatch path in main() - but not lost: pro_cli.main() calls the
    same check itself. Confirms both halves of that claim: this shell's
    own check_and_prompt is skipped when dispatching, and still runs on
    the non-dispatch (pro not installed) path."""
    fake_pro_cli = MagicMock()
    fake_pro_cli.main.side_effect = SystemExit(0)
    monkeypatch.setitem(sys.modules, "pro_cli", fake_pro_cli)

    with patch.object(
        sys, "argv", ["issue-worm", command, "--repo", "owner/name"]
    ), patch("cli.check_and_prompt") as mock_check:
        with pytest.raises(SystemExit):
            cli.main()
    mock_check.assert_not_called()

    monkeypatch.setattr(cli, "_try_import_pro_cli", lambda: None)
    with patch.object(
        sys, "argv", ["issue-worm", command]
    ), patch("cli.check_and_prompt") as mock_check:
        with pytest.raises(SystemExit):
            cli.main()
    mock_check.assert_called_once()


def test_dispatch_to_pro_exits_if_pro_cli_main_returns_normally():
    """pro_cli.main() should always sys.exit itself; this is only a safety
    net in case a future version of it doesn't - forwarding None (like
    `sys.exit()` with no arguments) is a clean exit, same as returning 0."""
    fake_pro_cli = MagicMock()
    fake_pro_cli.main.return_value = None

    with pytest.raises(SystemExit) as exc:
        cli._dispatch_to_pro(fake_pro_cli)

    assert exc.value.code is None


def test_dispatch_to_pro_forwards_pro_cli_mains_return_code():
    """If a future pro_cli.main() returns an int instead of calling
    sys.exit itself, that code must propagate - not be silently reported
    as success."""
    fake_pro_cli = MagicMock()
    fake_pro_cli.main.return_value = 2

    with pytest.raises(SystemExit) as exc:
        cli._dispatch_to_pro(fake_pro_cli)

    assert exc.value.code == 2


def test_dispatch_to_pro_propagates_unexpected_exceptions():
    """A non-SystemExit exception from pro_cli.main() must propagate, not be
    silently swallowed by a future refactor wrapping the dispatch in a
    catch-all try/except."""
    fake_pro_cli = MagicMock()
    fake_pro_cli.main.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        cli._dispatch_to_pro(fake_pro_cli)


def test_dispatch_to_pro_propagates_system_exit_with_its_code():
    """SystemExit from pro_cli.main() (its normal, expected way of exiting)
    must keep propagating with its own code - not be caught, converted, or
    reported as this shell's own exit status by a future refactor."""
    fake_pro_cli = MagicMock()
    fake_pro_cli.main.side_effect = SystemExit(42)

    with pytest.raises(SystemExit) as exc:
        cli._dispatch_to_pro(fake_pro_cli)

    assert exc.value.code == 42


@pytest.mark.parametrize("command", ["triage", "poll"])
def test_main_propagates_unexpected_exceptions_through_dispatch(command, monkeypatch):
    """The exception-propagation contract must hold through the full
    main() -> _dispatch_to_pro() -> pro_cli.main() call chain, not just
    through _dispatch_to_pro() in isolation - guards against a future
    refactor wrapping the dispatch call *inside* main() in a swallowing
    try/except, which test_dispatch_to_pro_propagates_unexpected_exceptions
    alone would not catch."""
    fake_pro_cli = MagicMock()
    fake_pro_cli.main.side_effect = RuntimeError("boom")
    monkeypatch.setitem(sys.modules, "pro_cli", fake_pro_cli)

    with patch.object(sys, "argv", ["issue-worm", command]):
        with pytest.raises(RuntimeError, match="boom"):
            cli.main()

    fake_pro_cli.main.assert_called_once_with()


def test_try_import_pro_cli_reraises_unrelated_module_not_found_error(monkeypatch):
    """A ModuleNotFoundError for one of pro_cli's own missing dependencies
    must not be reported as "issue-worm-pro isn't installed" - that would
    point a real installation problem at the wrong fix."""

    def _broken_import(name, *args, **kwargs):
        if name == "pro_cli":
            raise ModuleNotFoundError("No module named 'some_pro_dependency'", name="some_pro_dependency")
        return real_import(name, *args, **kwargs)

    import builtins

    real_import = builtins.__import__
    monkeypatch.setattr(builtins, "__import__", _broken_import)

    with pytest.raises(ModuleNotFoundError, match="some_pro_dependency"):
        cli._try_import_pro_cli()


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
    "bad_repo",
    ["no-slash", "too/many/slashes", "owner/", "/name", "owner/name\n"],
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


def test_build_workspace_flag_overrides_default(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
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


def test_build_default_workspace_used_when_neither_flag_nor_env_set(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch(
        "cli.ensure_base_clone", return_value=str(tmp_path)
    ) as mock_clone, patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls, patch(
        "cli.parse_coder_output", return_value=[]
    ), pytest.raises(SystemExit):
        mock_coder_cls.return_value.propose.return_value = "raw coder output"
        cli.main()

    mock_clone.assert_called_once_with(
        os.path.join(".issue-worm-workspace", "o_r"), "o/r"
    )


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
