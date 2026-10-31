"""Tests for scripts/check_media_refs.py (the README media reference check).

The helpers are pure string transforms, tested directly; check_file and main
run against tmp_path trees, never the real repo, except the one test that
asserts the real README passes (what CI runs).
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_media_refs import (  # noqa: E402
    check_file,
    extract_refs,
    is_external_or_anchor,
    main,
    normalize_path,
)

# --- extract_refs (#592) ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("![demo](docs/demo.gif)", ["docs/demo.gif"]),
        ("[guide](docs/guide.md)", ["docs/guide.md"]),
        ('![logo](logo.jpg "The logo")', ["logo.jpg"]),
        ('<img src="docs/logo.jpg" width="200">', ["docs/logo.jpg"]),
        ("<a href='docs/a.md'>a</a>", ["docs/a.md"]),
        ('<video SRC="docs/a.mp4" controls></video>', ["docs/a.mp4"]),
        ("plain prose, no references", []),
    ],
)
def test_extract_refs_finds_each_pattern(text, expected):
    assert extract_refs(text) == expected


def test_extract_refs_in_a_mixed_document():
    text = (
        '# Title\n<img src="a.png">\n![b](b.gif)\nSee [c](c.md) and '
        '<a href="https://example.com">site</a>.\n'
    )
    # HTML attributes first, then markdown, each in document order.
    assert extract_refs(text) == ["a.png", "https://example.com", "b.gif", "c.md"]


# --- is_external_or_anchor (#592) --------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/leonarduk/issue-worm",
        "http://example.com/a.png",
        "HTTPS://EXAMPLE.COM",
        "mailto:someone@example.com",
        "data:image/png;base64,AAAA",
        "file:///etc/passwd",
        "//cdn.example.com/a.js",
        "#section",
        "#",
        "{{ site.url }}/a.png",
        "${BASE}/a.png",
        "",
        "   ",
    ],
)
def test_is_external_or_anchor_skips_non_local_refs(url):
    assert is_external_or_anchor(url) is True


@pytest.mark.parametrize("url", ["docs/logo.jpg", "./a.md", "../LICENSE", "a.mp4#t=10"])
def test_is_external_or_anchor_keeps_local_paths(url):
    assert is_external_or_anchor(url) is False


# --- normalize_path (#592) ----------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("docs/logo.jpg", "docs/logo.jpg"),
        ("docs/a.mp4?raw=true", "docs/a.mp4"),
        ("docs/assets/foo.mp4#t=10", "docs/assets/foo.mp4"),
        ("docs/assets/foo.mp4?raw=1#t=10", "docs/assets/foo.mp4"),
        ("./docs/a.md", "docs/a.md"),
        ("././a.md", "a.md"),
        ("../LICENSE", "../LICENSE"),
    ],
)
def test_normalize_path_strips_query_fragment_and_dot_slash(url, expected):
    assert normalize_path(url) == expected


@pytest.mark.parametrize("url", ["https://x.com/a.png", "#top", "/abs/path.png", "?q=1", "#"])
def test_normalize_path_skips_external_anchors_and_absolute_paths(url):
    assert normalize_path(url) is None


# --- check_file: file-relative resolution (#594) -----------------------------


def test_check_file_resolves_relative_to_the_markdown_file(tmp_path):
    """docs/page.md's ``logo.jpg`` is docs/logo.jpg, not <root>/logo.jpg -
    the rule GitHub uses when rendering the link."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "logo.jpg").write_bytes(b"")
    (tmp_path / "logo.jpg").write_bytes(b"")  # a root-level decoy
    page = tmp_path / "docs" / "page.md"
    page.write_text("![l](logo.jpg)\n![r](../root-only.png)\n", encoding="utf-8")

    missing = check_file(page, tmp_path)

    assert missing == [("../root-only.png", "root-only.png")]


def test_check_file_flags_a_ref_that_only_exists_at_the_repo_root(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "assets.png").write_bytes(b"")
    page = tmp_path / "docs" / "page.md"
    page.write_text("![a](assets.png)\n", encoding="utf-8")

    assert check_file(page, tmp_path) == [("assets.png", "docs/assets.png")]


def test_check_file_skips_refs_that_escape_the_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    page = repo / "README.md"
    page.write_text("![sibling](../other-repo/logo.png)\n", encoding="utf-8")

    assert check_file(page, repo) == []


def test_check_file_raises_for_a_missing_markdown_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        check_file(tmp_path / "nope.md", tmp_path)


# --- main: explicitly-passed files must exist (#593) -------------------------


def test_main_fails_when_a_passed_markdown_file_does_not_exist(tmp_path, capsys):
    (tmp_path / "README.md").write_text("no refs\n", encoding="utf-8")

    assert main(["README.mad", "--repo-root", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    assert "error: markdown file not found: README.mad" in err


def test_main_still_checks_the_files_that_do_exist(tmp_path, capsys):
    (tmp_path / "README.md").write_text("![x](missing.png)\n", encoding="utf-8")

    assert main(["README.md", "gone.md", "--repo-root", str(tmp_path)]) == 1

    captured = capsys.readouterr()
    assert "README.md: missing.png" in captured.out
    assert "gone.md" in captured.err


def test_main_passes_when_every_ref_exists(tmp_path, capsys):
    (tmp_path / "a.png").write_bytes(b"")
    (tmp_path / "README.md").write_text("![a](a.png)\n", encoding="utf-8")

    assert main(["README.md", "--repo-root", str(tmp_path)]) == 0
    assert "OK" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["README.md", "docs/README.md"])
def test_the_real_docs_pass(name):
    """What the media-refs CI job runs, plus docs/README.md, whose ./ and
    ../ links only resolve under the file-relative rule."""
    assert main([name, "--repo-root", str(REPO_ROOT)]) == 0
