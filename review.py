"""Non-AI review: is an issue body scoped enough to dispatch to the
free-tier Coder?

`review_issue` itself makes no LLM call and has no GitHub comment/label
side effects — this free shell has no scheduler or label lifecycle, so it
stays a pure text check that hands its verdict back to the caller
(`cli.py`'s `build` command). When that verdict is "not ready", the
caller may choose to self-heal via `draft_implementation_notes` below,
which *does* call an LLM (the same Coder the build step would use) to
draft the missing section — see that function's docstring.
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


_SCOPE_DRAFT_PROMPT = (
    "An automated dispatcher will only act on an issue that declares "
    "exactly which files to touch and what a passing result looks like. "
    "The issue below is missing that. Read the issue and the list of the "
    "repo's existing top-level files, then reply with ONLY this section - "
    "no preamble, no explanation, nothing after it:\n\n"
    "## Implementation notes\n"
    "FILES: path/one.py, path/two.py\n"
    "DONE: one factual sentence describing what a passing result looks "
    "like\n\n"
    "FILES must name real paths - either files already listed below, or "
    "new ones the issue is clearly asking to create. DONE must describe a "
    "self-checkable outcome, not restate the issue title.\n\n"
    "Existing top-level files in the repo:\n{files}\n\n"
    "Issue:\n{body}\n"
)


def draft_implementation_notes(
    issue_body: str, repo_files: list[str], coder
) -> str | None:
    """Ask `coder` to draft the `## Implementation notes` section an issue
    is missing, so a dispatcher can self-heal instead of just bouncing the
    issue back to a human with `NOT_READY_MESSAGE` (issue-worm-pro#753's
    review thread: "this should be self healing"). This is the one place
    in this module that makes an LLM call - deliberately kept out of
    `review_issue` itself (see the module docstring), and only worth
    reaching for once `review_issue` has already said `ready=False`.

    Returns the drafted section text verbatim (starting with the
    `## Implementation notes` heading), or None if the coder produced
    nothing, or produced something `review_issue` still won't accept -
    callers should fall back to the original `NOT_READY_MESSAGE` in either
    case rather than trust an unparseable or empty draft.
    """
    if coder is None or not issue_body or not issue_body.strip():
        return None
    prompt = _SCOPE_DRAFT_PROMPT.format(
        files="\n".join(repo_files) if repo_files else "(none found)",
        body=issue_body,
    )
    try:
        response = coder.complete(prompt)
    except Exception:  # noqa: BLE001 - a coder failure here must not fail the build; just skip healing
        return None
    if not response or not response.strip():
        return None
    section = response.strip()
    if not review_issue(f"{issue_body}\n\n{section}").ready:
        return None
    return section
