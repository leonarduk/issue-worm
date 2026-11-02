"""Check a built wheel before release.yml attaches it to a GitHub release.

A wheel missing a module, the ``issue-worm`` console script, or its
metadata still uploads fine, and version_checker.py would then point every
user at a broken release (#197). The expectations come from pyproject.toml
itself - ``[tool.setuptools] py-modules`` and ``[project.scripts]`` - so a
new module or entry point is checked without editing this script.

Usage: python scripts/check_wheel_contents.py dist/<name>.whl [pyproject.toml]
"""

from __future__ import annotations

import configparser
import sys
import tomllib
import zipfile
from pathlib import Path

# Top-level directories that are repo tooling, never part of the package.
_FORBIDDEN_TOP_LEVEL = ("tests", "scripts", "docs", ".github")


def expected_from_pyproject(pyproject: dict) -> tuple[list[str], dict[str, str]]:
    """Return (module filenames, console scripts) the wheel must ship."""
    modules = pyproject.get("tool", {}).get("setuptools", {}).get("py-modules", [])
    scripts = pyproject.get("project", {}).get("scripts", {})
    return [f"{name}.py" for name in modules], dict(scripts)


def check_wheel(names: list[str], entry_points: str | None, pyproject: dict) -> list[str]:
    """Return the problems with a wheel whose archive holds ``names``."""
    problems: list[str] = []
    modules, scripts = expected_from_pyproject(pyproject)
    present = set(names)
    for module in modules:
        if module not in present:
            problems.append(f"missing module {module}")

    dist_info = sorted({n.split("/", 1)[0] for n in names if n.split("/", 1)[0].endswith(".dist-info")})
    if len(dist_info) != 1:
        problems.append(f"expected one .dist-info directory, found {dist_info or 'none'}")
    else:
        for required in ("METADATA", "WHEEL", "RECORD"):
            if f"{dist_info[0]}/{required}" not in present:
                problems.append(f"missing {dist_info[0]}/{required}")

    if scripts:
        if entry_points is None:
            problems.append("missing entry_points.txt (console scripts)")
        else:
            parser = configparser.ConfigParser()
            parser.read_string(entry_points)
            found = dict(parser.items("console_scripts")) if parser.has_section("console_scripts") else {}
            for name, target in scripts.items():
                if found.get(name) != target:
                    problems.append(
                        f"console script {name} should be {target!r}, "
                        f"got {found.get(name)!r}"
                    )

    for name in names:
        top = name.split("/", 1)[0]
        if top in _FORBIDDEN_TOP_LEVEL:
            problems.append(f"unexpected repo file in wheel: {name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not 1 <= len(args) <= 2:
        print("usage: check_wheel_contents.py <wheel> [pyproject.toml]", file=sys.stderr)
        return 2
    wheel = Path(args[0])
    pyproject_path = Path(args[1]) if len(args) == 2 else Path("pyproject.toml")
    try:
        pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            entry_names = [n for n in names if n.endswith(".dist-info/entry_points.txt")]
            entry_points = archive.read(entry_names[0]).decode("utf-8") if entry_names else None
    except (OSError, zipfile.BadZipFile, tomllib.TOMLDecodeError) as exc:
        print(f"::error::cannot check {wheel}: {exc}")
        return 1

    problems = check_wheel(names, entry_points, pyproject)
    for problem in problems:
        print(f"::error file={wheel}::{problem}")
    if not problems:
        print(f"{wheel.name}: {len(names)} files, all expected modules and scripts present")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
