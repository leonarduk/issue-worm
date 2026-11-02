"""Check that every pin in .github/cicaid-pins.env names a tag that exists.

The AI review workflow installs cicaid-devtools / cicaid-devtools-pro at the
tags pinned there. A typo'd, deleted or never-pushed tag - or a revoked
CICAID_PRO_TOKEN for the private repo - otherwise surfaces only when the
install step runs, as an opaque git clone error (#391, #444). This resolves
each pin with ``git ls-remote`` at PR time and says which of those it is.

Usage: python scripts/check_cicaid_pins.py [pins-file]

CICAID_PRO_TOKEN (Contents: read on leonarduk/cicaid-pro) is needed for the
private pin. Without it - a fork or Dependabot PR, which GitHub gives no
secrets - that pin is skipped with a warning rather than failed, since the
PR can't have broken a token it never had.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PINS_FILE = Path(".github/cicaid-pins.env")

# KEY -> (owner/repo, needs CICAID_PRO_TOKEN). An unknown key is an error:
# nothing would check it, and the install step would never use it either.
PIN_REPOS: dict[str, tuple[str, bool]] = {
    "CICAID_REF": ("leonarduk/cicaid", False),
    "CICAID_PRO_REF": ("leonarduk/cicaid-pro", True),
}

# The same shape the install step and the updater accept: KEY=<tag>, nothing
# else, so a value can never smuggle a shell fragment into either.
_PIN_LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(v?[0-9A-Za-z][0-9A-Za-z._-]*)$")

# git ls-remote --exit-code exits 2 when the remote answered but nothing
# matched; anything else non-zero means it couldn't be reached or read.
_LS_REMOTE_NO_MATCH = 2


@dataclass(frozen=True)
class Pin:
    key: str
    tag: str
    line: int


def parse_pins(text: str) -> tuple[list[Pin], list[str]]:
    """Return (pins, errors) for a pins file's text."""
    pins: list[Pin] = []
    errors: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _PIN_LINE_RE.match(line)
        if not match:
            errors.append(f"line {number}: expected KEY=<tag>, got {line!r}")
            continue
        key, tag = match.groups()
        if key not in PIN_REPOS:
            errors.append(
                f"line {number}: unknown pin {key} (known: {', '.join(PIN_REPOS)})"
            )
            continue
        pins.append(Pin(key, tag, number))
    for key in PIN_REPOS:
        if not any(pin.key == key for pin in pins):
            errors.append(f"{key} is not pinned")
    return pins, errors


def _git_env(repo: str, token: str | None) -> dict[str, str]:
    # actions/checkout leaves GITHUB_TOKEN in the checkout's local git config
    # as an Authorization extraheader for github.com. Run from inside the
    # checkout, git sends that header instead of the token below, and
    # GITHUB_TOKEN can't see another private repo ("Repository not found").
    # An empty value resets the header list, and env config is read after
    # the local config.
    env = dict(
        os.environ,
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
        GIT_CONFIG_VALUE_0="",
    )
    if token:
        # Same URL rewrite as .github/scripts/pip_install_cicaid_pro.sh,
        # scoped to this one process; the token never reaches a config file.
        env.update(
            GIT_CONFIG_COUNT="2",
            GIT_CONFIG_KEY_1=f"url.https://x-access-token:{token}@github.com/{repo}.insteadOf",
            GIT_CONFIG_VALUE_1=f"https://github.com/{repo}",
        )
    return env


def check_pin(pin: Pin, token: str | None) -> tuple[str, str]:
    """Return ("ok" | "skip" | "error", message) for one pin."""
    repo, needs_token = PIN_REPOS[pin.key]
    if needs_token and not token:
        return (
            "skip",
            f"{pin.key}={pin.tag}: CICAID_PRO_TOKEN is not set, so {repo} (private) "
            "can't be checked here",
        )
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--tags",
             f"https://github.com/{repo}.git", f"refs/tags/{pin.tag}"],
            capture_output=True,
            text=True,
            env=_git_env(repo, token if needs_token else None),
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "error", f"{pin.key}={pin.tag}: could not run git ls-remote: {exc}"
    if result.returncode == 0:
        return "ok", f"{pin.key}={pin.tag} exists in {repo}"
    if result.returncode == _LS_REMOTE_NO_MATCH:
        return (
            "error",
            f"{pin.key}={pin.tag}: no such tag in {repo} - a typo, or a tag that "
            "was deleted or never pushed",
        )
    hint = (
        " - check that CICAID_PRO_TOKEN is still valid and can read it"
        if needs_token
        else ""
    )
    detail = (result.stderr or result.stdout).strip().replace(token or "\0", "***")
    return (
        "error",
        f"{pin.key}={pin.tag}: could not read {repo} (git exited "
        f"{result.returncode}){hint}: {detail}",
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0]) if args else DEFAULT_PINS_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"::error::cannot read {path}: {exc}")
        return 1
    pins, errors = parse_pins(text)
    token = os.environ.get("CICAID_PRO_TOKEN", "").strip() or None
    for pin in pins:
        status, message = check_pin(pin, token)
        if status == "ok":
            print(message)
        elif status == "skip":
            print(f"::warning::{message}")
        else:
            errors.append(message)
    for error in errors:
        print(f"::error file={path}::{error}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
