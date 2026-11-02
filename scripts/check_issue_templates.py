"""Check this repo's issue templates (#363).

1. No template line starts with ``FILES:`` or ``DONE:``. issue-worm reads the
   first such line anywhere in an issue body as its scope, so a template that
   carried one would make every unfilled issue look already scoped.
2. With ``--canonical`` (CI installs cicaid-devtools-pro at the pinned
   CICAID_PRO_REF first): each template is byte-identical to the copy
   ``cicaid setup-review-actions`` installs, ``ISSUE_TEMPLATE_FILES`` in
   cicaid_devtools.setup_review_actions - the canonical source
   (docs/maintenance.md, "Issue templates").

Usage: python scripts/check_issue_templates.py [--canonical]
"""

from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

TEMPLATE_DIR = Path(".github/ISSUE_TEMPLATE")
_SCOPE_LINE_RE = re.compile(r"^(FILES|DONE):", re.MULTILINE)


def scope_line_problems(templates: dict[str, str]) -> list[str]:
    """Templates (path -> text) with a line issue-worm would read as scope."""
    problems = []
    for path, text in sorted(templates.items()):
        for match in _SCOPE_LINE_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            problems.append(
                f"{path}:{line}: starts with {match.group(1)}: - issue-worm would "
                "read an unfilled issue as already scoped"
            )
    return problems


def canonical_problems(local: dict[str, str], canonical: dict[str, str]) -> list[str]:
    """Differences between this repo's templates and the canonical copies."""
    problems = []
    for path in sorted(canonical.keys() | local.keys()):
        if path not in local:
            problems.append(f"{path}: missing here (setup-review-actions installs it)")
        elif path not in canonical:
            problems.append(f"{path}: not a canonical template - add it in cicaid-pro first")
        elif local[path] != canonical[path]:
            problems.append(
                f"{path}: differs from the canonical copy - run `cicaid "
                "setup-review-actions` here, or change it in cicaid-pro first"
            )
    return problems


def load_local(root: Path = Path(".")) -> dict[str, str]:
    directory = root / TEMPLATE_DIR
    return {
        (TEMPLATE_DIR / p.name).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(directory.iterdir())
        if p.is_file()
    }


def load_canonical() -> dict[str, str]:
    module = importlib.import_module("cicaid_devtools.setup_review_actions")
    return dict(module.ISSUE_TEMPLATE_FILES)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    local = load_local()
    problems = scope_line_problems(local)
    if "--canonical" in args:
        try:
            canonical = load_canonical()
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            problems.append(f"cannot load the canonical templates: {exc}")
        else:
            problems += canonical_problems(local, canonical)
    for problem in problems:
        print(f"::error::{problem}")
    if not problems:
        print(f"{len(local)} issue templates OK")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
