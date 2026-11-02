"""Tests for scripts/check_issue_templates.py (#363)."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_issue_templates  # noqa: E402
from check_issue_templates import (  # noqa: E402
    canonical_problems,
    load_local,
    scope_line_problems,
)


def test_real_templates_have_no_scope_lines():
    """What CI runs: the shipped templates carry FILES:/DONE: only as
    indented guidance inside a comment, never at a line start."""
    local = load_local(REPO_ROOT)

    assert set(local) >= {
        ".github/ISSUE_TEMPLATE/bug_report.md",
        ".github/ISSUE_TEMPLATE/feature_request.md",
        ".github/ISSUE_TEMPLATE/task_chore.md",
    }
    assert scope_line_problems(local) == []


@pytest.mark.parametrize("key", ["FILES", "DONE"])
def test_a_scope_line_at_a_line_start_is_reported_with_its_line(key):
    templates = {"t.md": f"## Implementation notes\n<!--\n  - one beginning `{key}:`\n-->\n{key}: x\n"}

    (problem,) = scope_line_problems(templates)

    assert problem.startswith(f"t.md:5: starts with {key}:")


def test_canonical_comparison_reports_each_kind_of_drift():
    canonical = {"a.md": "A", "b.md": "B", "c.md": "C"}
    local = {"a.md": "A", "b.md": "b edited", "d.md": "D"}

    problems = canonical_problems(local, canonical)

    assert [p.split(":")[0] for p in problems] == ["b.md", "c.md", "d.md"]
    assert "differs from the canonical copy" in problems[0]
    assert "missing here" in problems[1]
    assert "not a canonical template" in problems[2]
    assert canonical_problems(canonical, dict(canonical)) == []


def test_main_canonical_mode_compares_against_setup_review_actions(monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)
    fake = types.ModuleType("cicaid_devtools.setup_review_actions")
    fake.ISSUE_TEMPLATE_FILES = dict(load_local(REPO_ROOT))
    monkeypatch.setitem(sys.modules, "cicaid_devtools.setup_review_actions", fake)

    assert check_issue_templates.main(["--canonical"]) == 0

    fake.ISSUE_TEMPLATE_FILES[".github/ISSUE_TEMPLATE/bug_report.md"] += "drift\n"
    assert check_issue_templates.main(["--canonical"]) == 1
    assert "bug_report.md: differs from the canonical copy" in capsys.readouterr().out


def test_main_canonical_mode_fails_when_the_package_is_missing(monkeypatch, capsys):
    monkeypatch.chdir(REPO_ROOT)

    def _missing(name):
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(check_issue_templates.importlib, "import_module", _missing)

    assert check_issue_templates.main(["--canonical"]) == 1
    assert "cannot load the canonical templates" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [None, ["not", "pairs"]], ids=["none", "list"])
def test_main_canonical_mode_reports_a_malformed_template_map(monkeypatch, capsys, bad):
    """ISSUE_TEMPLATE_FILES that isn't a path -> text mapping is an error
    line, not a traceback (#824)."""
    monkeypatch.chdir(REPO_ROOT)
    fake = types.ModuleType("cicaid_devtools.setup_review_actions")
    fake.ISSUE_TEMPLATE_FILES = bad
    monkeypatch.setitem(sys.modules, "cicaid_devtools.setup_review_actions", fake)

    assert check_issue_templates.main(["--canonical"]) == 1
    assert "cannot load the canonical templates" in capsys.readouterr().out


def test_main_reports_a_missing_template_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert check_issue_templates.main([]) == 1
    assert "::error::cannot read .github/ISSUE_TEMPLATE" in capsys.readouterr().out
