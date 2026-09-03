"""Tests for the free-shell CLI: history/create/build work standalone; the
issue-worm-pro commands (triage/poll) delegate to the installed `pro_cli`
module when issue-worm-pro is present, and report themselves unavailable
rather than crashing when it isn't (#352).
"""

import json
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest
import requests

import cli
import registry
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


@pytest.mark.parametrize("command", ["history", "create", "status"])
def test_free_only_command_never_dispatches_even_with_pro_installed(
    command, monkeypatch
):
    """`history` and `create` are this shell's alone and must never dispatch.
    `status` (#180) joins them - it's a read-only consumer of this shell's
    own registry/history, with no pro-specific version to upgrade to.

    `build` used to be in this list. It moved out under #372: pro ships a
    fuller build, so installing pro now upgrades it the way it already
    upgrades triage/poll - see test_build_dispatches_to_pro_when_installed.
    """
    fake_pro_cli = MagicMock()
    monkeypatch.setitem(sys.modules, "pro_cli", fake_pro_cli)

    argv = {
        "history": ["issue-worm", "history"],
        "create": ["issue-worm", "create"],
        "status": ["issue-worm", "status"],
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


# --- registry wiring around `build` (#179) ------------------------------


@pytest.fixture
def _state_dir(tmp_path, monkeypatch):
    """Point registry.py at an isolated state dir under tmp_path."""
    state_dir = tmp_path / "agents"
    monkeypatch.setenv(registry.STATE_DIR_ENV, str(state_dir))
    return state_dir


def _read_registry_record(state_dir, task_id):
    return json.loads((state_dir / f"{task_id}.json").read_text(encoding="utf-8"))


def test_build_registers_running_record_mid_run(tmp_path, _state_dir):
    """A `running` record must exist for the task while the coder runs."""
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    change = FileChange(path="a.py", mode="FULL", body="print(1)\n")
    seen = {}

    def _propose(*_args, **_kwargs):
        seen["record"] = _read_registry_record(_state_dir, "o_r-5")
        return "raw coder output"

    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch("cli.ensure_base_clone", return_value=str(tmp_path)), patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls, patch(
        "cli.parse_coder_output", return_value=[change]
    ), patch("cli.apply_file_change"), pytest.raises(SystemExit) as exc:
        mock_coder_cls.return_value.propose.side_effect = _propose
        cli.main()

    assert exc.value.code == 0
    assert seen["record"]["status"] == "running"
    assert seen["record"]["command"] == "build"
    assert seen["record"]["phase"] == "coder"


def test_build_terminal_record_is_done_on_success(tmp_path, _state_dir):
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
    ), patch("cli.apply_file_change"), pytest.raises(SystemExit) as exc:
        mock_coder_cls.return_value.propose.return_value = "raw coder output"
        cli.main()

    assert exc.value.code == 0
    record = _read_registry_record(_state_dir, "o_r-5")
    assert record["status"] == "done"


def test_build_terminal_record_is_failed_when_coder_produces_no_output(
    tmp_path, _state_dir
):
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
    record = _read_registry_record(_state_dir, "o_r-5")
    assert record["status"] == "failed"


def test_build_terminal_record_is_failed_and_reraises_on_exception(
    tmp_path, _state_dir
):
    """finish(..., "failed") must be recorded AND the original error must
    still propagate — the registry write is observational, never a way to
    swallow a real build failure."""
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch("cli.ensure_base_clone", return_value=str(tmp_path)), patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls:
        mock_coder_cls.return_value.propose.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError, match="boom"):
            cli.main()

    record = _read_registry_record(_state_dir, "o_r-5")
    assert record["status"] == "failed"


def test_build_terminal_record_is_failed_on_keyboard_interrupt(tmp_path, _state_dir):
    """Ctrl-C mid-build must not leave the record stuck at `running`."""
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), patch("cli.ensure_base_clone", return_value=str(tmp_path)), patch(
        "cli.LocalOllamaCoder"
    ) as mock_coder_cls:
        mock_coder_cls.return_value.propose.side_effect = KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            cli.main()

    record = _read_registry_record(_state_dir, "o_r-5")
    assert record["status"] == "failed"


def test_build_dry_run_never_registers(tmp_path, _state_dir):
    """No workspace is ever prepared for --dry-run, so there is nothing to
    register - the state dir must stay untouched."""
    ready = ReviewResult(ready=True, files=["a.py"], done="it works")
    with patch.object(
        sys, "argv", ["issue-worm", "build", "5", "--repo", "o/r", "--dry-run"]
    ), patch("cli._fetch_issue_body", return_value="body"), patch(
        "cli.review_issue", return_value=ready
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert not _state_dir.exists()


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


# --- `build` is pro-upgraded, not pro-only (#372) -----------------------


def test_build_dispatches_to_pro_when_installed(monkeypatch):
    """With pro installed, `build` must reach pro's orchestrator pipeline.

    The #372 regression: `build` was left out of the dispatch set, so a pro
    user silently got this shell's single-pass heuristic build instead of
    the Coder -> Verifier -> Analyser loop both READMEs describe.
    """
    fake_pro_cli = ModuleType("pro_cli")
    seen = []
    fake_pro_cli.main = lambda: seen.append(list(sys.argv)) or 0
    monkeypatch.setattr(cli, "_try_import_pro_cli", lambda: fake_pro_cli)

    ran_free_build = []
    monkeypatch.setattr(cli, "_run_build", lambda *a: ran_free_build.append(a) or 0)

    argv = ["issue-worm", "build", "--dry-run"]
    with patch.object(sys, "argv", argv), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert seen == [argv], "pro_cli.main() did not receive the invocation"
    assert not ran_free_build, "the free-tier build ran despite pro being installed"


def test_build_dispatch_happens_before_this_shells_parser(monkeypatch):
    """A pro-only flag must reach pro, not be rejected by the stub parser.

    Dispatch is pre-parse for exactly this reason (the flag sets differ:
    this shell has --workspace, pro doesn't). Asserting it here stops a
    future refactor from moving the check after parse_args().
    """
    fake_pro_cli = ModuleType("pro_cli")
    fake_pro_cli.main = lambda: 0
    monkeypatch.setattr(cli, "_try_import_pro_cli", lambda: fake_pro_cli)

    with patch.object(
        sys, "argv", ["issue-worm", "build", "--a-pro-only-flag"]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0


def test_build_falls_back_to_this_shell_when_pro_absent(monkeypatch, capsys):
    """Unlike triage/poll, `build` has a free-tier implementation to use."""
    monkeypatch.setattr(cli, "_try_import_pro_cli", lambda: None)

    ran = []
    monkeypatch.setattr(cli, "_run_build", lambda *a: ran.append(a) or 0)

    with patch.object(
        sys, "argv", ["issue-worm", "build", "1", "--repo", "owner/name"]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert len(ran) == 1
    assert "issue-worm-pro" not in capsys.readouterr().err


def test_build_is_dispatched_but_never_reported_unavailable():
    """`build` is pro-upgraded, so it must be in one set and not the other."""
    assert "build" in cli._PRO_COMMANDS
    assert "build" not in cli._CORE_COMMANDS
    assert set(cli._CORE_COMMANDS) < set(cli._PRO_COMMANDS)


# --- `status` command (#180) ---------------------------------------------


def test_status_with_nothing_prints_empty_states(tmp_path, _state_dir, capsys):
    """Zero registry files, a missing state dir, and a missing history file
    are all normal - none may traceback, and the empty case must print the
    exact `No active runs.` message rather than a blank screen."""
    history_path = str(tmp_path / "history.jsonl")
    with patch.object(
        sys, "argv", ["issue-worm", "status", "--history-path", history_path]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "No active runs." in out
    assert "No runs recorded yet." in out


def test_status_missing_state_dir_does_not_traceback(tmp_path, monkeypatch, capsys):
    """A state dir that has never been created (nothing has registered a
    run yet) is normal, not an error."""
    monkeypatch.setenv(
        registry.STATE_DIR_ENV, str(tmp_path / "does" / "not" / "exist")
    )
    history_path = str(tmp_path / "history.jsonl")
    with patch.object(
        sys, "argv", ["issue-worm", "status", "--history-path", history_path]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert "No active runs." in capsys.readouterr().out


def test_status_lists_active_run_from_registry(tmp_path, _state_dir, capsys):
    history_path = str(tmp_path / "history.jsonl")
    registry.register("o_r-5", command="build", workspace=str(tmp_path))
    registry.heartbeat("o_r-5", phase="coder")

    with patch.object(
        sys, "argv", ["issue-worm", "status", "--history-path", history_path]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "No active runs." not in out
    assert "o_r-5" in out
    assert "[build]" in out
    assert "coder" in out


def test_status_lists_completed_runs_from_history(tmp_path, _state_dir, capsys):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps(
            {
                "task_id": "task-1",
                "source": "cli",
                "description": "did a thing",
                "status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with patch.object(
        sys, "argv", ["issue-worm", "status", "--history-path", str(history_path)]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "✓ task-1  [cli]  completed  did a thing" in out


def test_status_limit_flag_caps_completed_runs(tmp_path, _state_dir, capsys):
    history_path = tmp_path / "history.jsonl"
    lines = [
        json.dumps(
            {
                "task_id": f"task-{i}",
                "source": "cli",
                "description": "x",
                "status": "completed",
            }
        )
        for i in range(5)
    ]
    history_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with patch.object(
        sys,
        "argv",
        ["issue-worm", "status", "--history-path", str(history_path), "-n", "3"],
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert sum(1 for line in out.splitlines() if line.startswith("✓ task-")) == 3
    # the most recent 3, i.e. task-2..task-4 (most-recent-first isn't
    # required within the completed section - matching history's own
    # oldest-to-newest tail slicing, see #180's "match history" note).
    assert "task-4" in out and "task-0" not in out


def test_status_json_emits_parseable_payload_with_both_sections(
    tmp_path, _state_dir, capsys
):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text(
        json.dumps(
            {
                "task_id": "task-1",
                "source": "cli",
                "description": "did a thing",
                "status": "completed",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry.register("o_r-5", command="build", workspace=str(tmp_path))

    with patch.object(
        sys,
        "argv",
        ["issue-worm", "status", "--history-path", str(history_path), "--json"],
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["task_id"] for r in payload["active"]] == ["o_r-5"]
    assert [r["task_id"] for r in payload["completed"]] == ["task-1"]


def test_status_never_writes_or_prunes_registry_files(tmp_path, _state_dir, capsys):
    """Read-only constraint (#180): `status` must never write, prune, or
    repair registry files, even a malformed one it can't fully parse."""
    history_path = str(tmp_path / "history.jsonl")
    registry.register("good", command="build", workspace=str(tmp_path))
    malformed_path = _state_dir / "malformed.json"
    malformed_path.write_text("not json", encoding="utf-8")
    before = {
        p.name: p.read_text(encoding="utf-8") for p in _state_dir.glob("*.json")
    }

    with patch.object(
        sys, "argv", ["issue-worm", "status", "--history-path", history_path]
    ), pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    after = {p.name: p.read_text(encoding="utf-8") for p in _state_dir.glob("*.json")}
    assert after == before
    # the malformed file is skipped rather than crashing the command
    assert "good" in capsys.readouterr().out


def test_status_dispatches_to_run_status(monkeypatch):
    monkeypatch.setattr(cli, "_try_import_pro_cli", lambda: None)
    with patch.object(
        sys, "argv", ["issue-worm", "status"]
    ), patch("cli._run_status", return_value=0) as mock_run_status, pytest.raises(
        SystemExit
    ) as exc:
        cli.main()

    mock_run_status.assert_called_once()
    assert exc.value.code == 0
