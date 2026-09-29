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


def _with_declared_requirements(repo: Path, declared: str) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    _python_project(repo)
    with (repo / "pyproject.toml").open("a", encoding="utf-8") as f:
        f.write(f"[tool.issue-worm]\nverifier-requirements = {declared}\n")
    return repo


def test_install_args_add_declared_verifier_requirements_from_a_sibling(tmp_path):
    """allotmint-pro's tests import a sibling allotmint checkout, so its CI
    also installs that repo's backend/requirements.txt."""
    repo = _with_declared_requirements(
        tmp_path / "pro", '["../shared/backend/requirements.txt"]'
    )
    (tmp_path / "shared" / "backend").mkdir(parents=True)
    (tmp_path / "shared" / "backend" / "requirements.txt").write_text("pyyaml\n")

    assert verifier_venv_install_args(str(repo)) == [
        ["install", "-e", "."],
        ["install", "-r", "../shared/backend/requirements.txt"],
    ]


def test_install_args_skip_a_declared_requirement_that_does_not_exist(tmp_path):
    repo = _with_declared_requirements(tmp_path / "pro", '["../missing/requirements.txt"]')

    assert verifier_venv_install_args(str(repo)) == [["install", "-e", "."]]


def test_install_args_ignore_a_malformed_verifier_requirements_value(tmp_path):
    repo = _with_declared_requirements(tmp_path / "pro", '"not-a-list.txt"')

    assert verifier_venv_install_args(str(repo)) == [["install", "-e", "."]]


def test_rebuilds_when_a_declared_sibling_requirement_appears_or_changes(tmp_path, venv_root):
    repo = _with_declared_requirements(tmp_path / "pro", '["../shared/requirements.txt"]')
    venv_dir = verifier_venv_dir(str(repo))

    def fake_step(command, cwd, env):
        if command[1:3] == ["-m", "venv"]:
            _fake_built_venv(venv_dir)

    with patch("workspace._run_venv_setup_step", side_effect=fake_step) as step:
        ensure_verifier_venv(str(repo))  # sibling not cloned yet
        (tmp_path / "shared").mkdir()
        (tmp_path / "shared" / "requirements.txt").write_text("pyyaml\n")
        ensure_verifier_venv(str(repo))  # it appeared: rebuild
        ensure_verifier_venv(str(repo))  # unchanged: cached
        (tmp_path / "shared" / "requirements.txt").write_text("pyyaml\nbotocore\n")
        ensure_verifier_venv(str(repo))  # upstream pin change: rebuild

    venv_creations = [c for c in step.call_args_list if c.args[0][1:3] == ["-m", "venv"]]
    assert len(venv_creations) == 3


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
        # Its own which("bash") lookup on Windows is covered separately.
        patch("workspace._prefer_git_bash", side_effect=lambda path: path),
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


# --- JavaScript dependencies (workspace.ensure_node_deps) ---------------------


def _node_repo(path: Path, *, ignore_node_modules: bool = True) -> Path:
    """A git checkout with a frontend/ npm project, like allotmint's."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    frontend = path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"name": "fe"}\n', encoding="utf-8")
    (frontend / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    if ignore_node_modules:
        (frontend / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    return path


def _fake_npm_ci(command, cwd, env):
    """What `npm ci` leaves behind, as far as ensure_node_deps looks."""
    (Path(cwd) / "node_modules").mkdir(exist_ok=True)


@pytest.fixture
def node_deps_on(monkeypatch):
    monkeypatch.delenv(workspace.VERIFIER_NODE_DEPS_ENV, raising=False)


def test_node_deps_runs_npm_ci_where_node_modules_is_ignored_then_caches(tmp_path, node_deps_on):
    repo = _node_repo(tmp_path)

    with (
        patch("workspace.shutil.which", return_value="/usr/bin/npm"),
        patch("workspace._run_venv_setup_step", side_effect=_fake_npm_ci) as step,
    ):
        assert workspace.ensure_node_deps(str(repo)) == []
        assert workspace.ensure_node_deps(str(repo)) == []  # stamped: no reinstall

    step.assert_called_once()
    command, cwd = step.call_args.args[0], step.call_args.args[1]
    assert command == ["/usr/bin/npm", "ci", "--no-audit", "--no-fund"]
    assert Path(cwd) == (repo / "frontend").resolve()


def test_node_deps_reinstalls_when_the_lockfile_changes(tmp_path, node_deps_on):
    repo = _node_repo(tmp_path)

    with (
        patch("workspace.shutil.which", return_value="/usr/bin/npm"),
        patch("workspace._run_venv_setup_step", side_effect=_fake_npm_ci) as step,
    ):
        workspace.ensure_node_deps(str(repo))
        (repo / "frontend" / "package-lock.json").write_text('{"lockfileVersion": 4}\n')
        workspace.ensure_node_deps(str(repo))

    assert step.call_count == 2


def test_node_deps_skips_a_dir_whose_node_modules_is_not_gitignored(tmp_path, node_deps_on):
    """Installing there would leave the checkout full of untracked files,
    which git clean -fd then deletes and the scheduler reads as dirty."""
    repo = _node_repo(tmp_path, ignore_node_modules=False)

    with patch("workspace._run_venv_setup_step") as step:
        assert workspace.ensure_node_deps(str(repo)) == []

    step.assert_not_called()


def test_node_deps_reports_a_missing_npm_instead_of_raising(tmp_path, node_deps_on):
    repo = _node_repo(tmp_path)

    with (
        patch("workspace.shutil.which", return_value=None),
        patch("workspace._run_venv_setup_step") as step,
    ):
        notes = workspace.ensure_node_deps(str(repo))

    step.assert_not_called()
    assert len(notes) == 1 and "npm is not on PATH" in notes[0] and "frontend/" in notes[0]


def test_node_deps_a_failed_install_leaves_no_stamp_so_the_next_call_retries(tmp_path, node_deps_on):
    repo = _node_repo(tmp_path)

    def failing(command, cwd, env):
        _fake_npm_ci(command, cwd, env)
        raise WorkspaceError("npm ci exited 1: ETIMEDOUT")

    with patch("workspace.shutil.which", return_value="/usr/bin/npm"):
        with patch("workspace._run_venv_setup_step", side_effect=failing):
            notes = workspace.ensure_node_deps(str(repo))
        with patch("workspace._run_venv_setup_step", side_effect=_fake_npm_ci) as step:
            assert workspace.ensure_node_deps(str(repo)) == []

    assert len(notes) == 1 and "ETIMEDOUT" in notes[0]
    step.assert_called_once()


def test_node_deps_disabled_by_env_installs_nothing(tmp_path, monkeypatch):
    repo = _node_repo(tmp_path)
    monkeypatch.setenv(workspace.VERIFIER_NODE_DEPS_ENV, "0")

    with patch("workspace._run_venv_setup_step") as step:
        assert workspace.ensure_node_deps(str(repo)) == []

    step.assert_not_called()


def test_run_ci_checks_prefixes_a_node_deps_note(tmp_path):
    with (
        patch("workspace.ensure_verifier_venv", return_value=None),
        patch("workspace.ensure_node_deps", return_value=["note: npm is not on PATH"]),
        patch(
            "workspace.subprocess.run",
            return_value=subprocess.CompletedProcess(["x"], 1, "lint failed", ""),
        ),
    ):
        passed, output = run_ci_checks(str(tmp_path), ["npm", "run", "lint"])

    assert not passed
    assert output.startswith("note: npm is not on PATH\n\n")
    assert output.endswith("lint failed")


def test_node_deps_installs_at_the_repo_root_too(tmp_path, node_deps_on):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "package.json").write_text('{"name": "root"}\n', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (tmp_path / ".gitignore").write_text("node_modules\n", encoding="utf-8")

    with (
        patch("workspace.shutil.which", return_value="/usr/bin/npm"),
        patch("workspace._run_venv_setup_step", side_effect=_fake_npm_ci) as step,
    ):
        assert workspace.ensure_node_deps(str(tmp_path)) == []

    assert Path(step.call_args.args[1]) == tmp_path.resolve()


def test_node_deps_skips_a_package_json_without_a_lockfile(tmp_path, node_deps_on):
    repo = _node_repo(tmp_path)
    (repo / "frontend" / "package-lock.json").unlink()

    with patch("workspace._run_venv_setup_step") as step:
        assert workspace.ensure_node_deps(str(repo)) == []

    step.assert_not_called()


def test_node_deps_passes_the_setup_env_without_the_tools_api_keys(
    tmp_path, node_deps_on, monkeypatch
):
    repo = _node_repo(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")

    with (
        patch("workspace.shutil.which", return_value="/usr/bin/npm"),
        patch("workspace._run_venv_setup_step", side_effect=_fake_npm_ci) as step,
    ):
        workspace.ensure_node_deps(str(repo))

    env = step.call_args.args[2]
    assert "ANTHROPIC_API_KEY" not in env
    assert "VIRTUAL_ENV" not in env


def test_node_deps_never_raises_when_the_manifests_cannot_be_read(tmp_path, node_deps_on):
    repo = _node_repo(tmp_path)

    with (
        patch("workspace._node_deps_digest", side_effect=PermissionError("denied")),
        patch("workspace._run_venv_setup_step") as step,
    ):
        notes = workspace.ensure_node_deps(str(repo))

    step.assert_not_called()
    assert len(notes) == 1 and "denied" in notes[0] and "frontend/" in notes[0]


def test_node_deps_never_raises_when_the_stamp_cannot_be_written(tmp_path, node_deps_on):
    repo = _node_repo(tmp_path)
    real_write_text = Path.write_text

    def write_text(self, *args, **kwargs):
        if self.name == workspace._NODE_DEPS_STAMP:
            raise OSError("disk full")
        return real_write_text(self, *args, **kwargs)

    with (
        patch("workspace.shutil.which", return_value="/usr/bin/npm"),
        patch("workspace._run_venv_setup_step", side_effect=_fake_npm_ci),
        patch.object(Path, "write_text", write_text),
    ):
        assert workspace.ensure_node_deps(str(repo)) == []


def test_node_deps_skips_when_git_cannot_say_whether_node_modules_is_ignored(
    tmp_path, node_deps_on
):
    repo = _node_repo(tmp_path)

    with (
        patch(
            "workspace._run_git",
            return_value=subprocess.CompletedProcess(["git"], 128, "", "fatal"),
        ),
        patch("workspace._run_venv_setup_step") as step,
    ):
        assert workspace.ensure_node_deps(str(repo)) == []

    step.assert_not_called()


def test_node_deps_does_nothing_for_a_repo_without_package_json(tmp_path, node_deps_on):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    with (
        patch("workspace.shutil.which") as which,
        patch("workspace._run_venv_setup_step") as step,
    ):
        assert workspace.ensure_node_deps(str(tmp_path)) == []

    which.assert_not_called()
    step.assert_not_called()


def test_node_deps_one_failing_dir_does_not_stop_the_others(tmp_path, node_deps_on):
    repo = _node_repo(tmp_path)
    admin = repo / "admin"
    admin.mkdir()
    (admin / "package.json").write_text('{"name": "admin"}\n', encoding="utf-8")
    (admin / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (admin / ".gitignore").write_text("node_modules\n", encoding="utf-8")

    def admin_fails(command, cwd, env):
        if Path(cwd).name == "admin":
            raise WorkspaceError("npm ci exited 1")
        _fake_npm_ci(command, cwd, env)

    with (
        patch("workspace.shutil.which", return_value="/usr/bin/npm"),
        patch("workspace._run_venv_setup_step", side_effect=admin_fails) as step,
    ):
        notes = workspace.ensure_node_deps(str(repo))

    assert sorted(Path(c.args[1]).name for c in step.call_args_list) == ["admin", "frontend"]
    assert len(notes) == 1 and "admin/" in notes[0]
    assert (repo / "frontend" / "node_modules" / workspace._NODE_DEPS_STAMP).is_file()


def test_node_deps_skips_dot_directories(tmp_path, node_deps_on):
    repo = _node_repo(tmp_path)
    tooling = repo / ".tooling"
    tooling.mkdir()
    (tooling / "package.json").write_text('{"name": "t"}\n', encoding="utf-8")
    (tooling / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")

    assert [d.name for d in workspace._node_project_dirs(repo.resolve())] == ["frontend"]


def test_node_deps_stamps_a_project_with_no_dependencies(tmp_path, node_deps_on):
    """npm ci creates no node_modules for a dependency-free project; without
    one the stamp could not be written and it would reinstall every run."""
    repo = _node_repo(tmp_path)

    with (
        patch("workspace.shutil.which", return_value="/usr/bin/npm"),
        patch("workspace._run_venv_setup_step") as step,  # creates nothing
    ):
        workspace.ensure_node_deps(str(repo))
        workspace.ensure_node_deps(str(repo))

    step.assert_called_once()


# --- Git Bash over a WSL bash.exe launcher (workspace._prefer_git_bash) -------


def _fake_which(mapping):
    """shutil.which stand-in: the first entry of ``path`` that maps ``name``."""

    def which(name, path=None):
        for entry in (path or "").split(os.pathsep):
            hit = mapping.get((entry, name))
            if hit:
                return hit
        return None

    return which


def _git_for_windows(root: Path) -> Path:
    """A Git for Windows layout: cmd/git.exe plus usr/bin/bash.exe."""
    (root / "cmd").mkdir(parents=True)
    (root / "cmd" / "git.exe").write_text("")
    (root / "usr" / "bin").mkdir(parents=True)
    (root / "usr" / "bin" / "bash.exe").write_text("")
    return root


def test_prefer_git_bash_puts_git_bash_ahead_of_the_wsl_launcher(tmp_path):
    git_root = _git_for_windows(tmp_path / "Git")
    system32 = str(tmp_path / "Windows" / "System32")
    git_cmd = str(git_root / "cmd")
    path = os.pathsep.join((system32, git_cmd))
    which = _fake_which(
        {
            (system32, "bash"): system32 + os.sep + "bash.exe",
            (git_cmd, "git"): str(git_root / "cmd" / "git.exe"),
        }
    )

    with patch("workspace.shutil.which", side_effect=which):
        result = workspace._prefer_git_bash(path, is_windows=True)

    assert result.split(os.pathsep) == [
        str((git_root / "usr" / "bin").resolve()),
        system32,
        git_cmd,
    ]


def test_prefer_git_bash_adds_it_when_there_is_no_bash_at_all(tmp_path):
    git_root = _git_for_windows(tmp_path / "Git")
    git_cmd = str(git_root / "cmd")
    which = _fake_which({(git_cmd, "git"): str(git_root / "cmd" / "git.exe")})

    with patch("workspace.shutil.which", side_effect=which):
        result = workspace._prefer_git_bash(git_cmd, is_windows=True)

    assert result.split(os.pathsep)[0] == str((git_root / "usr" / "bin").resolve())


def test_prefer_git_bash_leaves_a_working_bash_alone(tmp_path):
    git_bin = str(tmp_path / "Git" / "usr" / "bin")
    which = _fake_which({(git_bin, "bash"): git_bin + os.sep + "bash.exe"})

    with patch("workspace.shutil.which", side_effect=which):
        assert workspace._prefer_git_bash(git_bin, is_windows=True) == git_bin


def test_prefer_git_bash_leaves_path_alone_without_git_for_windows(tmp_path):
    system32 = str(tmp_path / "Windows" / "System32")
    which = _fake_which({(system32, "bash"): system32 + os.sep + "bash.exe"})

    with patch("workspace.shutil.which", side_effect=which):
        assert workspace._prefer_git_bash(system32, is_windows=True) == system32


def test_prefer_git_bash_is_a_no_op_off_windows(tmp_path):
    with patch("workspace.shutil.which") as which:
        assert workspace._prefer_git_bash("/usr/bin:/bin", is_windows=False) == "/usr/bin:/bin"
    which.assert_not_called()


def test_ci_check_env_applies_the_git_bash_preference(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "original")
    with patch("workspace._prefer_git_bash", return_value="git-bash;original") as prefer:
        env = ci_check_env(str(tmp_path), home=str(tmp_path / "home"))

    prefer.assert_called_once_with("original")
    assert env["PATH"] == "git-bash;original"
