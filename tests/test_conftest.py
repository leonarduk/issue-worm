"""Tests for the suite-wide run-registry fixtures in the root conftest.py.

Deliberately in a module with no state-dir fixture of its own (unlike
tests/test_registry.py), so what these see is conftest's doing alone.
"""

import pytest

import registry
from conftest import RealRegistryWriteError, _is_in_real_state_dir


def test_suite_state_dir_is_never_the_real_one():
    """conftest points every test at a temp state dir; a build test once
    leaked a record into ~/.issue-worm/agents/ that showed up in the
    monitoring UI (#184)."""
    assert not _is_in_real_state_dir(registry._state_dir())


def test_tripwire_blocks_a_write_to_the_real_state_dir(_forbid_real_registry_writes):
    target = registry.DEFAULT_STATE_DIR / "tripwire-test.json"

    with pytest.raises(RealRegistryWriteError):
        registry._write_run_atomic(target, {"task_id": "tripwire-test"})

    assert _forbid_real_registry_writes == [target]
    _forbid_real_registry_writes.clear()  # the tripwire did its job


def test_tripwire_catches_register_despite_its_swallowed_errors(
    monkeypatch, _forbid_real_registry_writes
):
    """register() swallows every Exception, so the tripwire has to record
    the attempt for teardown, not rely on the raise reaching the test."""
    monkeypatch.delenv(registry.STATE_DIR_ENV, raising=False)

    assert registry.register("tripwire-test", "build", ".") is None

    assert _forbid_real_registry_writes == [registry.DEFAULT_STATE_DIR]
    _forbid_real_registry_writes.clear()  # the tripwire did its job
