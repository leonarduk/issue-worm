#!/usr/bin/env python3
"""Check that local media files referenced from documentation exist in the repo.

Parses markdown files (default: README.md) for references to local files via:
  - HTML attributes: src="...", href="..."
  - Markdown images: ![alt](path)
  - Markdown links:  [text](path)

External URLs (http://, https://, mailto:, tel:, data:, etc.) and pure anchors
(#section) are ignored. Each remaining local path is resolved relative to the
repository root and checked for existence. Exits non-zero if any are missing.
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
    """Return list of (reference, resolved_path) for missing files."""
    try:
        text = md_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"warning: {md_path} not found; skipping", file=sys.stderr)
        return []

    missing: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in extract_refs(text):
        rel = normalize_path(raw)
        if rel is None or rel in seen:
            continue
        seen.add(rel)
        # Resolve relative to the markdown file's directory (standard for docs).
        candidate = (md_path.parent / rel).resolve()
        try:
            candidate.relative_to(repo_root.resolve())
        except ValueError:
            # Reference escapes the repo; skip rather than fail.
            continue
        if not candidate.exists():
            missing.append((raw, str(candidate.relative_to(repo_root.resolve()))))
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files",
        nargs="*",
        default=["README.md"],
        help="Markdown files to check (default: README.md)",
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
    for f in files:
        md_path = Path(f)
        if not md_path.is_absolute():
            md_path = repo_root / md_path
        for ref, resolved in check_file(md_path, repo_root):
            all_missing.append((str(md_path.relative_to(repo_root)), ref, resolved))

    if all_missing:
        print("Error: referenced local media files are missing from the repository:")
        for md, ref, resolved in all_missing:
            print(f"  - {md}: {ref}  ->  {resolved}")
        print()
        print("Commit the missing files or update the references.")
        return 1

    print(f"OK: all local media references in {', '.join(files)} exist.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
