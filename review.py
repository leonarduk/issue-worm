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
    r"^##(?!#)[ \t]*Implementation notes[ \t]*\r?\n(?P<body>.*?)(?=\r?\n##[ \t]|\Z)",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
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
    file may not exist yet if the issue is asking to create it. When an
    issue body has more than one `## Implementation notes` heading, the
    first well-formed section wins rather than only ever looking at the
    first occurrence (which could be an unrelated, unfilled example).
    """
    if not issue_body or not issue_body.strip():
        return ReviewResult(ready=False, message=NOT_READY_MESSAGE)

    for match in _SECTION_RE.finditer(issue_body):
        result = _parse_section(match.group("body"))
        if result.ready:
            return result

    return ReviewResult(ready=False, message=NOT_READY_MESSAGE)


def _parse_section(section: str) -> ReviewResult:
    files_match = _FILES_RE.search(section)
    files = _split_files(files_match.group("value")) if files_match else []

    done_match = _DONE_RE.search(section)
    done = done_match.group("value").strip() if done_match else ""

    if not files or not done:
        return ReviewResult(ready=False, message=NOT_READY_MESSAGE)

    return ReviewResult(ready=True, files=files, done=done)


def _split_files(raw: str) -> list[str]:
    return [f.strip() for f in raw.split(",") if f.strip()]
