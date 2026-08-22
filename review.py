"""Non-AI review: is an issue body scoped enough to dispatch to the
free-tier Coder?

No LLM call, no GitHub comment/label side effects — this free shell has no
scheduler or label lifecycle, so `review_issue` is a pure text check that
hands its verdict back to the caller (`cli.py`'s `build` command).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_SECTION_RE = re.compile(
    r"##\s*Implementation notes\s*\r?\n(?P<body>.*?)(?=\r?\n##\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_FILES_RE = re.compile(r"^FILES:[ \t]*(?P<value>.*)$", re.IGNORECASE | re.MULTILINE)
_DONE_RE = re.compile(r"^DONE:[ \t]*(?P<value>.*)$", re.IGNORECASE | re.MULTILINE)

NOT_READY_MESSAGE = (
    "This issue isn't scoped for automatic dispatch yet. Add an "
    "`## Implementation notes` section to the issue body with:\n\n"
    "FILES: path/one.py, path/two.py\n"
    "DONE: what a passing result looks like"
)


@dataclass
class ReviewResult:
    """Verdict from `review_issue`: ready to dispatch, or not (with a
    fixed, actionable message — never an LLM-authored one).
    """

    ready: bool
    files: list[str] = field(default_factory=list)
    done: str = ""
    message: str = ""


def review_issue(issue_body: str) -> ReviewResult:
    """Deterministically decide whether `issue_body` is ready for the
    free-tier Coder: does it have a `## Implementation notes` section with
    non-empty `FILES:` and `DONE:` lines?

    File existence under `FILES:` is intentionally not checked — a target
    file may not exist yet if the issue is asking to create it.
    """
    if issue_body and issue_body.strip():
        section_match = _SECTION_RE.search(issue_body)
    else:
        section_match = None

    if section_match is None:
        return ReviewResult(ready=False, message=NOT_READY_MESSAGE)

    section = section_match.group("body")

    files_match = _FILES_RE.search(section)
    files = _split_files(files_match.group("value")) if files_match else []

    done_match = _DONE_RE.search(section)
    done = done_match.group("value").strip() if done_match else ""

    if not files or not done:
        return ReviewResult(ready=False, message=NOT_READY_MESSAGE)

    return ReviewResult(ready=True, files=files, done=done)


def _split_files(raw: str) -> list[str]:
    return [f.strip() for f in raw.split(",") if f.strip()]
