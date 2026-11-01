"""Structural checks on the composite action's own manifest, action.yml."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ACTION = Path(__file__).resolve().parent.parent / "action.yml"


def _run_steps() -> list[dict]:
    """Each step's `name` and `run: |` script, read by indentation.

    No YAML parser: PyYAML is not a dependency of this package, and every
    script in action.yml is a literal block (`run: |`), which ends at the
    first non-blank line indented no deeper than the `run:` key.
    """
    lines = ACTION.read_text(encoding="utf-8").splitlines()
    steps, name = [], "?"
    i = 0
    while i < len(lines):
        stripped = lines[i].lstrip()
        if stripped.startswith("- name:"):
            name = stripped.removeprefix("- name:").strip()
        if stripped == "run: |":
            key_indent = len(lines[i]) - len(stripped)
            body = []
            i += 1
            while i < len(lines) and (
                not lines[i].strip() or len(lines[i]) - len(lines[i].lstrip()) > key_indent
            ):
                body.append(lines[i])
                i += 1
            steps.append({"name": name, "run": "\n".join(body)})
            continue
        i += 1
    return steps


def test_every_run_script_is_a_literal_block():
    """Guards the parser above: a `run:` written any other way would be
    silently skipped by every check in this file."""
    text = ACTION.read_text(encoding="utf-8")
    run_keys = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("run:")]
    assert run_keys and all(key == "run: |" for key in run_keys)
    assert len(_run_steps()) == len(run_keys)


def test_no_expression_is_spliced_into_a_run_script():
    """GitHub substitutes `${{ }}` into the script text before bash parses
    it, so any value there - above all a caller-supplied input - is an
    injection surface. Values reach scripts through `env:` instead (#366)."""
    offenders = [
        step.get("name", "?")
        for step in _run_steps()
        if "${{" in step["run"]
    ]
    assert offenders == []


def _close_issue_line() -> str:
    (step,) = [s for s in _run_steps() if "CLOSE_ISSUE" in s["run"]]
    (line,) = [
        ln.strip() for ln in step["run"].splitlines() if ln.strip().startswith("close_issue=")
    ]
    return line


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("true", "true"), ("True", "true"), ("TRUE", "true"), ("false", "false"), ("", "")],
)
def test_close_issue_input_is_case_insensitive(raw, expected):
    """Any casing of 'true' closes the issue on merge (#324); the step
    compares this normalized value, never CLOSE_ISSUE itself."""
    result = subprocess.run(
        ["bash", "-c", _close_issue_line() + '\nprintf "%s" "$close_issue"'],
        capture_output=True,
        text=True,
        env={"CLOSE_ISSUE": raw, "PATH": "/usr/bin:/bin"},
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == expected


def test_close_issue_comparisons_use_the_normalized_value():
    (step,) = [s for s in _run_steps() if "CLOSE_ISSUE" in s["run"]]
    script = step["run"]
    assert '"$CLOSE_ISSUE" =' not in script
    assert '"$CLOSE_ISSUE" !=' not in script
    assert script.count('"$close_issue"') == 2


def _pro_cli_check() -> str:
    """The `if ... import pro_cli ...; fi` block from the install step."""
    (step,) = [s for s in _run_steps() if "import pro_cli" in s["run"]]
    lines = step["run"].splitlines()
    start = next(i for i, ln in enumerate(lines) if "import pro_cli" in ln)
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "fi")
    return "\n".join(ln.strip() for ln in lines[start : end + 1])


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("importable", [True, False], ids=["leaked", "isolated"])
def test_install_step_fails_when_pro_cli_is_importable(tmp_path, importable):
    """The venv exists to keep pro_cli out of reach, or `issue-worm build`
    silently delegates to the pro engine; the install step checks that
    isolation held (#291)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # -S: no site-packages, so a pro_cli installed in the developer's own
    # environment (e.g. alongside issue-worm-pro) can't leak into the
    # "isolated" case; PYTHONPATH below is still honoured for "leaked".
    fake_python = bin_dir / "python"
    fake_python.write_text(f'#!/bin/sh\nexec "{sys.executable}" -S "$@"\n')
    fake_python.chmod(0o755)
    site = tmp_path / "site"
    site.mkdir()
    if importable:
        (site / "pro_cli.py").write_text("")

    result = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\nvenv_bin="{bin_dir}"\n{_pro_cli_check()}'],
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(site)},
        check=False,
    )

    if importable:
        assert result.returncode == 1
        assert "::error::pro_cli is importable" in result.stdout
    else:
        assert result.returncode == 0, result.stderr
