#!/usr/bin/env python3
"""Check that local media files referenced from documentation exist in the repo.

Parses markdown files (default: README.md) for references to local files via:
  - HTML attributes: src="...", href="..."
  - Markdown images: ![alt](path)
  - Markdown links:  [text](path)

External URLs (http://, https://, mailto:, tel:, data:, etc.) and pure anchors
(#section) are ignored. Each remaining local path is resolved relative to the
directory of the markdown file that contains it -- the same rule GitHub uses
when it renders a relative link -- so docs/README.md's ``./logo.jpg`` means
docs/logo.jpg. A reference that resolves outside the repository is skipped.

Exits non-zero if any referenced file is missing, or if a markdown file passed
on the command line does not exist (a typo there must fail CI, not pass it
silently with nothing checked).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Matches src="..." / href='...' (single or double quotes).
HTML_ATTR_RE = re.compile(
    r"""\b(?:src|href)\s*=\s*(?P<q>["'])(?P<url>.*?)(?P=q)""",
    re.IGNORECASE,
)

# Matches markdown images ![alt](url) and links [text](url).
# Captures the URL portion; handles optional title in quotes after the URL.
MD_LINK_RE = re.compile(
    r"""!?\[[^\]]*\]\(\s*(?P<url>[^)\s]+)(?:\s+["'][^"']*["'])?\s*\)""",
)

# Schemes we always treat as external / non-file.
EXTERNAL_SCHEMES = {
    "http",
    "https",
    "mailto",
    "tel",
    "ftp",
    "ftps",
    "data",
    "javascript",
    "irc",
    "news",
    "sms",
    "ssh",
    "git",
    "ws",
    "wss",
}


def is_external_or_anchor(url: str) -> bool:
    """Return True if the URL should be skipped (external, anchor, template, etc.)."""
    if not url:
        return True
    stripped = url.strip()
    if not stripped:
        return True
    # Pure anchor.
    if stripped.startswith("#"):
        return True
    # Template placeholders like {{ ... }} or ${...}.
    if "{{" in stripped or "${" in stripped:
        return True
    # Protocol-relative URLs (//example.com/...).
    if stripped.startswith("//"):
        return True
    parsed = urlparse(stripped)
    if parsed.scheme and parsed.scheme.lower() in EXTERNAL_SCHEMES:
        return True
    # Any other scheme (e.g. file:, path:) — treat as non-local-repo reference.
    if parsed.scheme:
        return True
    return False


def extract_refs(text: str) -> list[str]:
    """Extract candidate local file references from markdown/HTML text."""
    refs: list[str] = []
    for match in HTML_ATTR_RE.finditer(text):
        refs.append(match.group("url"))
    for match in MD_LINK_RE.finditer(text):
        refs.append(match.group("url"))
    return refs


def normalize_path(url: str) -> str | None:
    """Convert a URL-ish reference into a repo-relative path, or None to skip."""
    if is_external_or_anchor(url):
        return None
    # Strip query string and fragment.
    path = url.split("#", 1)[0].split("?", 1)[0].strip()
    if not path:
        return None
    # Strip leading "./".
    while path.startswith("./"):
        path = path[2:]
    # Reject absolute filesystem paths — they can't be validated portably.
    if path.startswith("/"):
        return None
    return path


def check_file(md_path: Path, repo_root: Path) -> list[tuple[str, str]]:
    """Return list of (reference, resolved_path) for missing files.

    Raises FileNotFoundError when ``md_path`` itself does not exist: every
    file this checks was named explicitly, so a missing one is a broken
    invocation, not something to skip (#593).
    """
    text = md_path.read_text(encoding="utf-8")

    missing: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in extract_refs(text):
        rel = normalize_path(raw)
        if rel is None or rel in seen:
            continue
        seen.add(rel)
        # File-relative, not repo-root-relative: see the module docstring.
        candidate = (md_path.parent / rel).resolve()
        try:
            candidate.relative_to(repo_root.resolve())
        except ValueError:
            # Deliberately skipped, not failed: a path outside the checkout
            # (e.g. a sibling repo) can't be validated from here.
            continue
        if not candidate.exists():
            missing.append((raw, str(candidate.relative_to(repo_root.resolve()))))
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "files",
        nargs="*",
        default=["README.md"],
        help=(
            "Markdown files to check (default: README.md). Each must exist. "
            "References resolve relative to the file that contains them."
        ),
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root (default: current working directory)",
    )
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else Path.cwd().resolve()
    files = args.files or ["README.md"]

    all_missing: list[tuple[str, str, str]] = []
    not_found: list[str] = []
    for f in files:
        md_path = Path(f)
        if not md_path.is_absolute():
            md_path = repo_root / md_path
        try:
            missing = check_file(md_path, repo_root)
        except FileNotFoundError:
            not_found.append(f)
            continue
        for ref, resolved in missing:
            all_missing.append((str(md_path.relative_to(repo_root)), ref, resolved))

    for f in not_found:
        print(f"error: markdown file not found: {f}", file=sys.stderr)

    if all_missing:
        print("Error: referenced local media files are missing from the repository:")
        for md, ref, resolved in all_missing:
            print(f"  - {md}: {ref}  ->  {resolved}")
        print()
        print("Commit the missing files or update the references.")
        return 1
    if not_found:
        return 1

    print(f"OK: all local media references in {', '.join(files)} exist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
