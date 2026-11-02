"""Tests for the release/CI guard scripts and release.yml's dispatch step.

scripts/check_cicaid_pins.py (#391, #444), scripts/check_wheel_contents.py
(#197) and scripts/pro_dependency_requirements.py (#103) are tested as pure
functions with git/pip mocked, never against the network. release.yml's
dispatch step (#513, #515) is run under bash with a stub ``gh``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_cicaid_pins  # noqa: E402
import check_wheel_contents  # noqa: E402
import pro_dependency_requirements  # noqa: E402
from check_cicaid_pins import Pin, check_pin, parse_pins  # noqa: E402

# --- check_cicaid_pins --------------------------------------------------------


def test_parse_pins_reads_the_real_pins_file():
    text = (REPO_ROOT / ".github" / "cicaid-pins.env").read_text(encoding="utf-8")

    pins, errors = parse_pins(text)

    assert errors == []
    assert [pin.key for pin in pins] == ["CICAID_REF", "CICAID_PRO_REF"]


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("CICAID_REF=v1\nCICAID_PRO_REF=$(curl evil)\n", "line 2: expected KEY=<tag>"),
        ("CICAID_REF=v1\nCICAID_PRO_REF=v2\nOTHER_REF=v3\n", "line 3: unknown pin OTHER_REF"),
        ("CICAID_REF=v1\n", "CICAID_PRO_REF is not pinned"),
    ],
)
def test_parse_pins_rejects_what_the_install_step_would(text, error):
    _pins, errors = parse_pins(text)

    assert any(e.startswith(error) for e in errors), errors


def _ls_remote(returncode, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout="", stderr=stderr)


def test_check_pin_ok_and_missing_tag_are_told_apart():
    pin = Pin("CICAID_REF", "v0.6.0", 1)
    with patch("check_cicaid_pins.subprocess.run", return_value=_ls_remote(0)) as run:
        assert check_pin(pin, None)[0] == "ok"
    assert run.call_args.args[0][-2:] == ["https://github.com/leonarduk/cicaid.git", "refs/tags/v0.6.0"]

    with patch("check_cicaid_pins.subprocess.run", return_value=_ls_remote(2)):
        status, message = check_pin(pin, None)
    assert status == "error"
    assert "no such tag in leonarduk/cicaid" in message


def test_check_pin_private_repo_uses_the_token_and_never_prints_it():
    pin = Pin("CICAID_PRO_REF", "v0.20.1", 2)
    with patch(
        "check_cicaid_pins.subprocess.run",
        return_value=_ls_remote(128, "fatal: auth failed for https://x-access-token:s3cret@github.com"),
    ) as run:
        status, message = check_pin(pin, "s3cret")

    env = run.call_args.kwargs["env"]
    assert env["GIT_CONFIG_KEY_0"] == (
        "url.https://x-access-token:s3cret@github.com/leonarduk/cicaid-pro.insteadOf"
    )
    assert status == "error"
    assert "CICAID_PRO_TOKEN is still valid" in message
    assert "s3cret" not in message


def test_check_pin_skips_the_private_pin_without_a_token():
    with patch("check_cicaid_pins.subprocess.run") as run:
        status, message = check_pin(Pin("CICAID_PRO_REF", "v0.20.1", 2), None)

    run.assert_not_called()
    assert status == "skip"
    assert "CICAID_PRO_TOKEN is not set" in message


def test_main_fails_on_a_bad_pin_and_passes_a_skipped_one(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CICAID_PRO_TOKEN", raising=False)
    pins = tmp_path / "pins.env"
    pins.write_text("CICAID_REF=v9.9.9\nCICAID_PRO_REF=v0.20.1\n")

    with patch("check_cicaid_pins.subprocess.run", return_value=_ls_remote(2)):
        assert check_cicaid_pins.main([str(pins)]) == 1
    out = capsys.readouterr().out
    assert "::error" in out and "CICAID_REF=v9.9.9" in out
    assert "::warning::CICAID_PRO_REF" in out

    with patch("check_cicaid_pins.subprocess.run", return_value=_ls_remote(0)):
        assert check_cicaid_pins.main([str(pins)]) == 0


# --- check_wheel_contents -----------------------------------------------------

_PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
_DIST_INFO = "issue_worm-1.0.0.dist-info"
_ENTRY_POINTS = "[console_scripts]\nissue-worm = cli:main\n"


def _good_names() -> list[str]:
    modules, _ = check_wheel_contents.expected_from_pyproject(_PYPROJECT)
    return [*modules, *(f"{_DIST_INFO}/{n}" for n in ("METADATA", "WHEEL", "RECORD", "entry_points.txt"))]


def test_wheel_check_passes_a_complete_wheel():
    assert check_wheel_contents.check_wheel(_good_names(), _ENTRY_POINTS, _PYPROJECT) == []


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda n, e: ([x for x in n if x != "workspace.py"], e), "missing module workspace.py"),
        (lambda n, e: ([x for x in n if not x.endswith("RECORD")], e), f"missing {_DIST_INFO}/RECORD"),
        (lambda n, e: (n, None), "missing entry_points.txt"),
        (lambda n, e: (n, "[console_scripts]\nissue-worm = other:main\n"), "console script issue-worm"),
        (lambda n, e: ([*n, "tests/test_cli.py"], e), "unexpected repo file in wheel: tests/test_cli.py"),
    ],
    ids=["module", "record", "no-entry-points", "wrong-script", "tests-shipped"],
)
def test_wheel_check_reports_each_kind_of_broken_wheel(mutate, problem):
    names, entry_points = mutate(_good_names(), _ENTRY_POINTS)

    problems = check_wheel_contents.check_wheel(names, entry_points, _PYPROJECT)

    assert any(p.startswith(problem) for p in problems), problems


def test_wheel_check_main_reads_a_real_zip(tmp_path, capsys):
    wheel = tmp_path / "issue_worm-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in _good_names():
            archive.writestr(name, _ENTRY_POINTS if name.endswith("entry_points.txt") else "")

    assert check_wheel_contents.main([str(wheel), str(REPO_ROOT / "pyproject.toml")]) == 0

    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("scripts/check_wheel_contents.py", "")
    assert check_wheel_contents.main([str(wheel), str(REPO_ROOT / "pyproject.toml")]) == 1
    assert "unexpected repo file" in capsys.readouterr().out


# --- pro_dependency_requirements -----------------------------------------------


def test_swap_issue_worm_points_pro_at_this_checkout(tmp_path):
    requirements = [
        "issue-worm @ git+https://github.com/leonarduk/issue-worm.git@v0.8.0",
        "cicaid-devtools @ git+https://github.com/leonarduk/cicaid.git@v0.6.0",
        "requests>=2.34.2,<3",
    ]

    out = pro_dependency_requirements.swap_issue_worm(requirements, tmp_path)

    assert out == [f"issue-worm @ {tmp_path.resolve().as_uri()}", *requirements[1:]]


@pytest.mark.parametrize("names", [[], ["issue_worm @ x", "Issue.Worm>=1"]], ids=["none", "two"])
def test_swap_issue_worm_insists_on_exactly_one(tmp_path, names):
    with pytest.raises(ValueError, match="expected one issue-worm requirement"):
        pro_dependency_requirements.swap_issue_worm(["requests", *names], tmp_path)


def test_pro_requirements_main_prints_the_list(tmp_path, capsys):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "issue-worm-pro"\n'
        'dependencies = ["issue-worm @ git+https://x@v1", "mcp>=1.9,<2"]\n'
    )

    assert pro_dependency_requirements.main([str(pyproject), str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"issue-worm @ {tmp_path.resolve().as_uri()}",
        "mcp>=1.9,<2",
    ]


# --- release.yml dispatch step ------------------------------------------------


def _release_step(name: str) -> dict:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "release.yml").read_text())
    (step,) = [s for s in workflow["jobs"]["build-and-release"]["steps"] if s.get("name") == name]
    return step


def test_dispatch_token_prefers_the_app_token_then_the_pat():
    """A per-run GitHub App token scoped to issue-worm-pro's Actions comes
    first; the long-lived PAT is only the fallback (#513)."""
    mint = _release_step("Mint a dispatch token (GitHub App)")
    dispatch = _release_step("Trigger dependency update in issue-worm-pro")

    assert mint["uses"].startswith("actions/create-github-app-token@")
    assert mint["with"]["repositories"] == "issue-worm-pro"
    assert mint["with"]["permission-actions"] == "write"
    assert mint["continue-on-error"] is True
    assert dispatch["env"]["GH_TOKEN"] == (
        "${{ steps.dispatch-app.outputs.token || secrets.DISPATCH_TOKEN }}"
    )


def _run_dispatch(tmp_path: Path, failures: int) -> tuple[subprocess.CompletedProcess, int]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    count = tmp_path / "count"
    count.write_text("0")
    gh = stubs / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(( $(cat "{count}") + 1 )); echo "$n" > "{count}"\n'
        f'[ "$n" -le {failures} ] && exit 1\n'
        "exit 0\n"
    )
    gh.chmod(0o755)
    sleep = stubs / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)
    script = _release_step("Trigger dependency update in issue-worm-pro")["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={"PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "GH_TOKEN": "x"},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, int(count.read_text())


needs_bash = pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="needs bash")


@needs_bash
def test_dispatch_retries_a_transient_failure_once(tmp_path):
    """One retry recovers a network blip or 5xx before the warning (#515)."""
    result, calls = _run_dispatch(tmp_path, failures=1)

    assert result.returncode == 0, result.stderr
    assert calls == 2
    assert "::warning::" not in result.stdout


@needs_bash
def test_dispatch_warns_and_stays_green_after_the_retry_also_fails(tmp_path):
    result, calls = _run_dispatch(tmp_path, failures=5)

    assert result.returncode == 0, result.stderr
    assert calls == 2
    assert "::warning::Could not dispatch" in result.stdout
