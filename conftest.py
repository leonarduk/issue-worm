"""Shared pytest fixtures for the issue-worm test suite.

The suite must pass identically whether or not `issue-worm-pro` is
installed alongside `issue-worm` in editable mode. `cli.main()` probes
for an importable `pro_cli` before running any of this shell's own
free-tier logic and dispatches to it when present (#352). On a dev
machine with both halves installed, that probe succeeds, so tests that
mean to exercise the core-only path (build/history/status) would
silently dispatch into the real `pro_cli.main()` instead.

#227 review note: `cli.py`'s dispatch (`_try_import_pro_cli`, called as
`pro_cli = _try_import_pro_cli()` from the `_PRO_COMMANDS` check) does a
real `import pro_cli` - not `importlib.util.find_spec("pro_cli")` -
confirmed by reading `cli.py:104-124`. That's why the `sys.modules`
sentinel below works: it's exactly what a real `ModuleNotFoundError`
looks like, not a probe this idiom would leave unaffected.

The autouse fixture below neutralizes `pro_cli` by default for every
test in the suite, using the same `monkeypatch.setitem(sys.modules,
"pro_cli", None)` sentinel idiom already established in
`test_core_command_reports_unavailable` (#192) and
`test_core_command_dispatches_to_pro_cli_when_installed` (#352).

Tests that specifically want to exercise the pro-dispatch path opt back
in by setting `sys.modules["pro_cli"]` themselves (e.g. to a MagicMock
or a ModuleType) - `monkeypatch.setitem` in the test body runs after
this fixture and simply overwrites the sentinel. Tests that want to
assert the "pro genuinely absent" path can rely on the default.

This fixture is deliberately scoped to `function` (the default) so each
test gets a fresh sentinel and no state leaks between tests.
"""

import sys
from pathlib import Path

import pytest

import registry


@pytest.fixture(autouse=True)
def _neutralize_pro_cli(monkeypatch):
    """Make `import pro_cli` fail by default for every test.

    `monkeypatch.setitem(sys.modules, "pro_cli", None)` makes `import
    pro_cli` raise `ModuleNotFoundError` (name="pro_cli"), exactly like a
    genuinely-absent module. Tests that need pro present overwrite this
    entry via their own `monkeypatch.setitem` call, which runs after this
    fixture and therefore wins.
    """
    monkeypatch.setitem(sys.modules, "pro_cli", None)


@pytest.fixture(autouse=True)
def _isolate_registry_state_dir(tmp_path_factory, monkeypatch):
    """Point the run registry at a fresh temp dir for every test (#184).

    Without this, any test that reaches `registry.register` - directly or
    through `cli._run_build` - wrote into the developer's real
    `~/.issue-worm/agents/`: fabricated runs showed up in the monitoring
    UI, and `register()`'s pruning could delete genuine run history. Suite
    -wide and autouse so a new test can't reintroduce the leak by
    forgetting a fixture. A `tmp_path_factory` dir, not `tmp_path`, so it
    never appears inside a test's own workspace listing. Tests that need a
    specific state dir (tests/test_registry.py, test_cli's `_state_dir`)
    set the variable again in their own fixture, which wins.
    """
    monkeypatch.setenv(registry.STATE_DIR_ENV, str(tmp_path_factory.mktemp("issue-worm-state")))


class RealRegistryWriteError(RuntimeError):
    """A test reached the developer's real run registry (#184)."""


def _is_in_real_state_dir(path) -> bool:
    real = registry.DEFAULT_STATE_DIR.resolve()
    target = Path(path).resolve()
    return target == real or real in target.parents


@pytest.fixture(autouse=True)
def _forbid_real_registry_writes(monkeypatch):
    """Tripwire behind `_isolate_registry_state_dir`: fail any test that
    tries to write or prune inside the real `registry.DEFAULT_STATE_DIR`.

    Wraps the registry's only two mutators - `_write_run_atomic` (behind
    register/heartbeat/finish) and `_prune_terminal_records` (the deletes
    in register) - so the write is blocked before it happens. Each attempt
    is also recorded and asserted on at teardown, because `register()` and
    friends swallow exceptions by design and would otherwise hide the
    raise. Yields the list of attempts so the tripwire's own tests can
    inspect (and clear) it.
    """
    attempts: list[Path] = []
    real_write = registry._write_run_atomic
    real_prune = registry._prune_terminal_records

    def _check(path) -> None:
        if _is_in_real_state_dir(path):
            attempts.append(Path(path))
            raise RealRegistryWriteError(f"test reached the real run registry: {path}")

    def _guarded_write(path, record):
        _check(path)
        real_write(path, record)

    def _guarded_prune():
        _check(registry._state_dir())
        real_prune()

    monkeypatch.setattr(registry, "_write_run_atomic", _guarded_write)
    monkeypatch.setattr(registry, "_prune_terminal_records", _guarded_prune)
    yield attempts
    assert not attempts, f"tests must not touch the real run registry: {attempts}"
