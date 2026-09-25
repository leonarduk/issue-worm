"""Tests for the verifier's isolated per-target venv (workspace.ensure_verifier_venv)
and how ci_check_env / run_ci_checks use it."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import workspace
from workspace import (
    VERIFIER_VENV_DIR_ENV,
    VERIFIER_VENV_ENV,
    WorkspaceError,
    ci_check_env,
    ensure_verifier_venv,
    run_ci_checks,
    verifier_venv_dir,
    verifier_venv_install_args,
)

SCRIPTS = "Scripts" if os.name == "nt" else "bin"


@pytest.fixture
def venv_root(tmp_path, monkeypatch):
    root = tmp_path / "venvs"
    monkeypatch.setenv(VERIFIER_VENV_DIR_ENV, str(root))
    monkeypatch.delenv(VERIFIER_VENV_ENV, raising=False)
    return root


def _python_project(path: Path, extras: dict[str, list[str]] | None = None) -> Path:
    lines = ['[project]', 'name = "demo"', 'version = "0.1.0"']
    if extras:
        lines.append("[project.optional-dependencies]")
        for name, deps in extras.items():
            lines.append(f"{name} = {deps!r}".replace("'", '"'))
    (path / "pyproject.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fake_built_venv(venv_dir: Path) -> None:
    """What `python -m venv` leaves behind, as far as ensure_verifier_venv looks."""
    scripts = venv_dir / SCRIPTS
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / ("python.exe" if os.name == "nt" else "python")).write_text("")


# --- what gets installed ------------------------------------------------------


def test_install_args_editable_with_declared_test_extras_in_conventional_order(tmp_path):
    _python_project(tmp_path, {"dev": ["ruff"], "docs": ["mkdocs"], "test": ["pytest"]})

    assert verifier_venv_install_args(str(tmp_path)) == [["install", "-e", ".[test,dev]"]]


def test_install_args_plain_editable_when_no_test_extras(tmp_path):
    _python_project(tmp_path)

    assert verifier_venv_install_args(str(tmp_path)) == [["install", "-e", "."]]


def test_install_args_add_base_and_test_requirement_files_only(tmp_path):
    _python_project(tmp_path)
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-video.txt"):
        (tmp_path / name).write_text("")

    assert verifier_venv_install_args(str(tmp_path)) == [
        ["install", "-e", "."],
        ["install", "-r", "requirements.txt", "-r", "requirements-dev.txt"],
    ]


def test_install_args_empty_for_a_non_python_repo(tmp_path):
    (tmp_path / "package.json").write_text("{}")

    assert verifier_venv_install_args(str(tmp_path)) == []


def test_a_tool_config_only_pyproject_is_not_installed(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")

    assert verifier_venv_install_args(str(tmp_path)) == []


# --- building and caching -------------------------------------------------------


def test_disabled_by_env_builds_nothing(tmp_path, venv_root, monkeypatch):
    _python_project(tmp_path)
    monkeypatch.setenv(VERIFIER_VENV_ENV, "0")

    with patch("workspace._run_venv_setup_step") as step:
        assert ensure_verifier_venv(str(tmp_path)) is None
    step.assert_not_called()


def test_non_python_repo_builds_nothing(tmp_path, venv_root):
    with patch("workspace._run_venv_setup_step") as step:
        assert ensure_verifier_venv(str(tmp_path)) is None
    step.assert_not_called()


def test_builds_an_isolated_venv_then_installs_the_target(tmp_path, venv_root):
    _python_project(tmp_path, {"test": ["pytest"]})
    venv_dir = verifier_venv_dir(str(tmp_path))

    def fake_step(command, cwd, env):
        if command[1:3] == ["-m", "venv"]:
            _fake_built_venv(venv_dir)

    with patch("workspace._run_venv_setup_step", side_effect=fake_step) as step:
        assert ensure_verifier_venv(str(tmp_path)) == venv_dir

    commands = [call.args[0] for call in step.call_args_list]
    # A plain venv: no --system-site-packages, or the tool's own packages leak
    # straight back in.
    assert commands[0] == [sys.executable, "-m", "venv", str(venv_dir)]
    assert commands[1][-3:] == ["install", "-e", ".[test]"]
    assert Path(commands[1][0]).parent == venv_dir / SCRIPTS
    assert venv_root in venv_dir.parents  # outside the checkout: git clean -fd


def test_reuses_the_cached_venv_while_manifests_are_unchanged(tmp_path, venv_root):
    _python_project(tmp_path)
    venv_dir = verifier_venv_dir(str(tmp_path))

    def fake_step(command, cwd, env):
        if command[1:3] == ["-m", "venv"]:
            _fake_built_venv(venv_dir)

    with patch("workspace._run_venv_setup_step", side_effect=fake_step):
        ensure_verifier_venv(str(tmp_path))
    (tmp_path / "a.py").write_text("changed = True\n")  # a code change, not a dependency one
    with patch("workspace._run_venv_setup_step") as step:
        assert ensure_verifier_venv(str(tmp_path)) == venv_dir
    step.assert_not_called()


def test_rebuilds_when_a_dependency_manifest_changes(tmp_path, venv_root):
    _python_project(tmp_path)
    venv_dir = verifier_venv_dir(str(tmp_path))

    def fake_step(command, cwd, env):
        if command[1:3] == ["-m", "venv"]:
            _fake_built_venv(venv_dir)

    with patch("workspace._run_venv_setup_step", side_effect=fake_step):
        ensure_verifier_venv(str(tmp_path))
    _python_project(tmp_path, {"test": ["pytest"]})
    with patch("workspace._run_venv_setup_step", side_effect=fake_step) as step:
        ensure_verifier_venv(str(tmp_path))
    assert step.call_count == 2  # venv + install, from scratch


def test_a_failed_install_leaves_no_stamp_so_the_next_call_retries(tmp_path, venv_root):
    _python_project(tmp_path)
    venv_dir = verifier_venv_dir(str(tmp_path))

    def failing_install(command, cwd, env):
        if command[1:3] == ["-m", "venv"]:
            _fake_built_venv(venv_dir)
            return
        raise WorkspaceError("pip exited 1: no network")

    def working_install(command, cwd, env):
        if command[1:3] == ["-m", "venv"]:
            _fake_built_venv(venv_dir)

    with patch("workspace._run_venv_setup_step", side_effect=failing_install):
        with pytest.raises(WorkspaceError):
            ensure_verifier_venv(str(tmp_path))
    with patch("workspace._run_venv_setup_step", side_effect=working_install) as step:
        assert ensure_verifier_venv(str(tmp_path)) == venv_dir
    assert step.call_count == 2  # rebuilt, not reused half-installed


def test_setup_env_does_not_carry_the_tools_api_keys(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirror.example/simple")

    env = workspace._venv_setup_env()

    assert "DEEPSEEK_API_KEY" not in env
    assert env["PIP_INDEX_URL"] == "https://mirror.example/simple"


# --- ci_check_env ------------------------------------------------------------------


def test_ci_check_env_puts_the_venv_first_on_path(tmp_path):
    venv_dir = tmp_path / "venv"

    env = ci_check_env(str(tmp_path), home=str(tmp_path), venv_dir=str(venv_dir))

    assert env["PATH"].split(os.pathsep)[0] == str(venv_dir / SCRIPTS)
    assert env["VIRTUAL_ENV"] == str(venv_dir)


def test_ci_check_env_without_a_venv_leaves_path_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "only-this")

    env = ci_check_env(str(tmp_path), home=str(tmp_path))

    assert env["PATH"] == "only-this"
    assert "VIRTUAL_ENV" not in env


def test_ci_check_env_adds_src_for_a_src_layout_repo(tmp_path):
    (tmp_path / "src").mkdir()

    env = ci_check_env(str(tmp_path), home=str(tmp_path))

    assert env["PYTHONPATH"].split(os.pathsep) == [
        str(tmp_path.resolve()),
        str((tmp_path / "src").resolve()),
    ]


def test_new_module_in_a_src_layout_repo_is_importable(tmp_path):
    """The cicaid#51 failure: a module the patch just added under src/ must be
    importable by the check, not shadowed by (or missing from) an installed copy."""
    package = tmp_path / "src" / "demo_pkg_for_verifier_test"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "brand_new.py").write_text("VALUE = 42\n")

    env = ci_check_env(str(tmp_path), home=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", "from demo_pkg_for_verifier_test.brand_new import VALUE; print(VALUE)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "42", result.stderr


# --- run_ci_checks -----------------------------------------------------------------


def test_run_ci_checks_runs_inside_the_venv(tmp_path):
    venv_dir = tmp_path / "venv"
    captured = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, "ok", "")

    with (
        patch("workspace.ensure_verifier_venv", return_value=venv_dir),
        patch("workspace.subprocess.run", side_effect=fake_run),
    ):
        passed, output = run_ci_checks(str(tmp_path), ["pytest"])

    assert passed and output == "ok"
    assert captured["env"]["VIRTUAL_ENV"] == str(venv_dir)


def test_run_ci_checks_keeps_the_runner_from_the_tools_own_path(tmp_path):
    """cicaid's own repo installs a `cicaid` script into its venv; the verifier
    must still be the tool's cicaid, not the code under test."""
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        patch("workspace.ensure_verifier_venv", return_value=tmp_path / "venv"),
        patch("workspace.shutil.which", return_value="/tool/bin/cicaid") as which,
        patch("workspace.subprocess.run", side_effect=fake_run),
    ):
        run_ci_checks(str(tmp_path), ["cicaid", "run-ci-checks", "--all"])

    which.assert_called_once_with("cicaid")
    assert captured["command"] == ["/tool/bin/cicaid", "run-ci-checks", "--all"]


def test_run_ci_checks_falls_back_with_a_note_when_the_venv_cannot_be_built(tmp_path):
    captured = {}

    def fake_run(command, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 1, "1 failed", "")

    with (
        patch(
            "workspace.ensure_verifier_venv",
            side_effect=WorkspaceError("pip exited 1: no network"),
        ),
        patch("workspace.subprocess.run", side_effect=fake_run),
    ):
        passed, output = run_ci_checks(str(tmp_path), ["pytest"])

    assert not passed
    assert output.startswith("note: issue-worm could not prepare an isolated verifier venv")
    assert "no network" in output
    assert output.endswith("1 failed")
    assert "VIRTUAL_ENV" not in captured["env"]
