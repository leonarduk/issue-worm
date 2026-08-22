"""Tests for version_checker.py (the #180 port of cicaid's update check)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests
from packaging.version import InvalidVersion

import version_checker
from version_checker import Release


class _FakeStream:
    """Minimal stand-in with a controllable isatty() and a capture buffer.

    io.StringIO is a C type without a __dict__, so it cannot carry an
    isatty attribute; a plain Python class avoids that limitation.
    """

    def __init__(self, isatty: bool = True):
        self._tty = isatty
        self._buf = ""

    def isatty(self) -> bool:
        return self._tty

    def write(self, text: str) -> int:
        self._buf += text
        return len(text)

    def getvalue(self) -> str:
        return self._buf


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def _payload(tag: str | None, assets: list[str] | None = None) -> dict:
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": name,
                "browser_download_url": (
                    f"https://github.com/leonarduk/issue-worm/releases/download/"
                    f"{tag}/{name}"
                ),
            }
            for name in (assets or [])
        ],
    }


@contextmanager
def _patch_tty(tty: bool = True):
    """Replace the module's stdin/stdout with TTY-reporting fakes."""
    stdin = _FakeStream(isatty=tty)
    stdout = _FakeStream(isatty=tty)
    with patch.object(version_checker.sys, "stdin", stdin), patch.object(
        version_checker.sys, "stdout", stdout
    ):
        yield


def _patch_stderr():
    stderr = _FakeStream()
    return patch.object(version_checker.sys, "stderr", stderr), stderr


# --- constants ---------------------------------------------------------------


def test_constants_target_issue_worm():
    assert version_checker.PACKAGE_NAME == "issue-worm"
    assert (
        version_checker.LATEST_RELEASE_URL
        == "https://api.github.com/repos/leonarduk/issue-worm/releases/latest"
    )
    assert version_checker.SKIP_UPDATE_ENV == "ISSUE_WORM_SKIP_UPDATE_CHECK"

    # The port must not leak cicaid's own names (that would check the wrong
    # package/repo or honour the wrong skip env var). The module docstring
    # may name the upstream file, but the distribution name and env var must
    # not survive into the port.
    source = Path(version_checker.__file__).read_text(encoding="utf-8")
    assert "CICAID_SKIP_UPDATE_CHECK" not in source
    assert "cicaid-devtools" not in source
    # The embedded Windows updater is adapted too: it cleans issue-worm's
    # stale dists and installs issue-worm, not cicaid.
    assert "~issue_worm*" in version_checker._WINDOWS_UPDATE_SCRIPT
    assert "cicaid" not in version_checker._WINDOWS_UPDATE_SCRIPT


# --- installed_version -------------------------------------------------------


@patch("version_checker.version", return_value="1.2.3")
def test_installed_version_returns_dist_version(mock_version):
    assert version_checker.installed_version() == "1.2.3"
    mock_version.assert_called_once_with("issue-worm")


def test_installed_version_none_in_source_checkout():
    with patch(
        "version_checker.version", side_effect=version_checker.PackageNotFoundError
    ):
        assert version_checker.installed_version() is None


# --- latest_release ----------------------------------------------------------


@patch("version_checker.requests.get")
def test_latest_release_parses_matching_wheel(mock_get):
    payload = _payload("v1.2.3", ["issue_worm-1.2.3-py3-none-any.whl"])
    mock_get.return_value = _FakeResponse(payload)

    release = version_checker.latest_release()

    assert release == Release("1.2.3", payload["assets"][0]["browser_download_url"])
    mock_get.assert_called_once_with(
        version_checker.LATEST_RELEASE_URL,
        headers={"Accept": "application/vnd.github+json"},
        timeout=2.0,
    )


@patch("version_checker.requests.get")
def test_latest_release_normalizes_pep440_wheel_name(mock_get):
    """setuptools-scm builds tag 0.7.0 as wheel version 0.7."""
    mock_get.return_value = _FakeResponse(
        _payload("v0.7.0", ["issue_worm-0.7-py3-none-any.whl"])
    )

    release = version_checker.latest_release()

    assert release.version == "0.7.0"
    assert release.wheel_url.endswith("issue_worm-0.7-py3-none-any.whl")


@patch("version_checker.requests.get")
def test_latest_release_ignores_unrelated_assets(mock_get):
    assets = [
        "source.zip",
        "cicaid_devtools-1.2.3-py3-none-any.whl",
        "issue_worm-1.2.3-py3-none-any.whl",
        "issue_worm-1.2.3.tar.gz",
    ]
    mock_get.return_value = _FakeResponse(_payload("v1.2.3", assets))

    release = version_checker.latest_release()

    assert release.wheel_url.endswith("issue_worm-1.2.3-py3-none-any.whl")


def test_latest_release_rejects_missing_tag():
    with patch("version_checker.requests.get", return_value=_FakeResponse({"assets": []})):
        with pytest.raises(ValueError, match="no tag_name"):
            version_checker.latest_release()


def test_latest_release_rejects_malformed_tag():
    with patch(
        "version_checker.requests.get", return_value=_FakeResponse(_payload("not-a-version"))
    ):
        with pytest.raises(InvalidVersion):
            version_checker.latest_release()


def test_latest_release_rejects_missing_asset():
    with patch(
        "version_checker.requests.get",
        return_value=_FakeResponse(_payload("v1.2.3", ["issue_worm-9.9.9-py3-none-any.whl"])),
    ):
        with pytest.raises(ValueError, match="no compatible"):
            version_checker.latest_release()


# --- available_update --------------------------------------------------------


@patch("version_checker.latest_release", return_value=Release("1.2.3", "url"))
@patch("version_checker.installed_version", return_value="1.0.0")
def test_available_update_returns_newer_release(mock_installed, mock_latest):
    assert version_checker.available_update() == Release("1.2.3", "url")


@patch("version_checker.latest_release", return_value=Release("1.2.3", "url"))
@patch("version_checker.installed_version", return_value="2.0.0")
def test_available_update_none_when_current_is_newer(mock_installed, mock_latest):
    assert version_checker.available_update() is None


@patch("version_checker.latest_release", return_value=Release("1.2.3", "url"))
@patch("version_checker.installed_version", return_value="1.2.3")
def test_available_update_none_when_current_is_current(mock_installed, mock_latest):
    assert version_checker.available_update() is None


@patch("version_checker.latest_release", side_effect=requests.ConnectionError("offline"))
@patch("version_checker.installed_version", return_value="1.0.0")
def test_available_update_silent_on_network_error(mock_installed, mock_latest, caplog):
    with caplog.at_level(logging.WARNING):
        assert version_checker.available_update() is None
    # RequestException is the offline path: nothing logged, nothing printed.
    assert caplog.text == ""


@patch("version_checker.latest_release", side_effect=ValueError("no tag_name"))
@patch("version_checker.installed_version", return_value="1.0.0")
def test_available_update_warns_on_bad_release_data(mock_installed, mock_latest, caplog):
    with caplog.at_level(logging.WARNING):
        assert version_checker.available_update() is None
    assert "Unable to check for an issue-worm update" in caplog.text


@patch("version_checker.latest_release")
@patch("version_checker.installed_version", return_value=None)
def test_available_update_source_checkout_skips_network(mock_installed, mock_latest):
    assert version_checker.available_update() is None
    mock_latest.assert_not_called()


@patch("version_checker.latest_release", return_value=Release("1.2.3", "url"))
@patch("version_checker.installed_version", return_value="not-a-version")
def test_available_update_none_on_invalid_installed_version(mock_installed, mock_latest):
    assert version_checker.available_update() is None


# --- install_update ----------------------------------------------------------


@patch("version_checker.subprocess.run", return_value=Mock(returncode=0))
def test_install_update_posix_runs_pip(mock_run):
    with patch.object(version_checker.os, "name", "posix"):
        ok = version_checker.install_update(Release("1.2.3", "https://w/issue_worm-1.2.3.whl"))

    assert ok is True
    mock_run.assert_called_once_with(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "issue-worm @ https://w/issue_worm-1.2.3.whl",
        ],
        check=False,
    )


@patch("version_checker.subprocess.run", return_value=Mock(returncode=1))
def test_install_update_posix_pip_failure_returns_false(mock_run):
    with patch.object(version_checker.os, "name", "posix"):
        ok = version_checker.install_update(Release("1.2.3", "url"))

    assert ok is False


@patch("version_checker.subprocess.Popen")
def test_install_update_windows_defers(mock_popen):
    with patch.object(version_checker.os, "name", "nt"):
        ok = version_checker.install_update(Release("1.2.3", "https://w/issue_worm-1.2.3.whl"))

    assert ok is True
    args, kwargs = mock_popen.call_args
    assert args[0][:3] == [sys.executable, "-c", version_checker._WINDOWS_UPDATE_SCRIPT]
    assert args[0][3] == str(os.getpid())
    assert args[0][4] == "https://w/issue_worm-1.2.3.whl"
    assert args[0][5] == os.path.join(tempfile.gettempdir(), "issue-worm-update.log")
    assert kwargs["stdin"] == subprocess.DEVNULL


@patch("version_checker.subprocess.Popen", side_effect=OSError("boom"))
def test_defer_windows_update_popen_failure_returns_false(mock_popen):
    assert version_checker._defer_windows_update(Release("1.2.3", "url")) is False


# --- check_and_prompt --------------------------------------------------------


def test_check_and_prompt_skips_when_env_set(monkeypatch):
    monkeypatch.setenv(version_checker.SKIP_UPDATE_ENV, "1")
    with _patch_tty(tty=True), patch.object(
        version_checker, "available_update"
    ) as mock_avail, patch("builtins.input") as mock_input:
        version_checker.check_and_prompt()

    mock_avail.assert_not_called()
    mock_input.assert_not_called()


def test_check_and_prompt_skips_when_stdin_not_a_tty():
    with patch.object(
        version_checker.sys, "stdin", _FakeStream(isatty=False)
    ), patch.object(
        version_checker.sys, "stdout", _FakeStream(isatty=True)
    ), patch.object(version_checker, "available_update") as mock_avail:
        version_checker.check_and_prompt()

    mock_avail.assert_not_called()


def test_check_and_prompt_skips_when_stdout_not_a_tty():
    with patch.object(
        version_checker.sys, "stdin", _FakeStream(isatty=True)
    ), patch.object(
        version_checker.sys, "stdout", _FakeStream(isatty=False)
    ), patch.object(version_checker, "available_update") as mock_avail:
        version_checker.check_and_prompt()

    mock_avail.assert_not_called()


def test_check_and_prompt_no_update_does_not_prompt():
    with _patch_tty(tty=True), patch.object(
        version_checker, "available_update", return_value=None
    ), patch("builtins.input") as mock_input:
        version_checker.check_and_prompt()

    mock_input.assert_not_called()


def test_check_and_prompt_decline_keeps_installed_version():
    with _patch_tty(tty=True), patch.object(
        version_checker, "available_update", return_value=Release("1.2.3", "url")
    ), patch.object(
        version_checker, "installed_version", return_value="1.0.0"
    ), patch("builtins.input", return_value="n") as mock_input, patch.object(
        version_checker, "install_update"
    ) as mock_install:
        version_checker.check_and_prompt()

    mock_input.assert_called_once_with(
        "issue-worm 1.2.3 is available (installed: 1.0.0). Update now? [y/N] "
    )
    mock_install.assert_not_called()


def test_check_and_prompt_yes_installs_and_restarts_posix():
    with _patch_tty(tty=True), patch.object(
        version_checker.os, "name", "posix"
    ), patch.object(
        version_checker, "available_update", return_value=Release("1.2.3", "url")
    ), patch.object(
        version_checker, "installed_version", return_value="1.0.0"
    ), patch("builtins.input", return_value="y"), patch.object(
        version_checker, "install_update", return_value=True
    ), patch.object(version_checker.os, "execv") as mock_execv:
        version_checker.check_and_prompt()

    mock_execv.assert_called_once_with(sys.executable, [sys.executable, *sys.argv])


def test_check_and_prompt_install_failure_continues_posix():
    stderr = _FakeStream()
    with _patch_tty(tty=True), patch.object(
        version_checker.os, "name", "posix"
    ), patch.object(
        version_checker, "available_update", return_value=Release("1.2.3", "url")
    ), patch.object(
        version_checker, "installed_version", return_value="1.0.0"
    ), patch("builtins.input", return_value="y"), patch.object(
        version_checker, "install_update", return_value=False
    ), patch.object(version_checker.sys, "stderr", stderr), patch.object(
        version_checker.os, "execv"
    ) as mock_execv:
        version_checker.check_and_prompt()  # returns, does not raise

    assert "issue-worm update failed; continuing with the installed version." in stderr.getvalue()
    mock_execv.assert_not_called()


def test_check_and_prompt_windows_success_schedules_update_and_exits():
    stdout = _FakeStream()
    with _patch_tty(tty=True), patch.object(
        version_checker.os, "name", "nt"
    ), patch.object(
        version_checker, "available_update", return_value=Release("1.2.3", "url")
    ), patch.object(
        version_checker, "installed_version", return_value="1.0.0"
    ), patch("builtins.input", return_value="y"), patch.object(
        version_checker, "install_update", return_value=True
    ), patch.object(version_checker.sys, "stdout", stdout):
        with pytest.raises(SystemExit) as exc:
            version_checker.check_and_prompt()

    assert exc.value.code == 0
    assert "issue-worm will update after this process exits." in stdout.getvalue()
    assert "issue-worm-update.log" in stdout.getvalue()


def test_check_and_prompt_windows_defer_failure_continues():
    stderr = _FakeStream()
    with _patch_tty(tty=True), patch.object(
        version_checker.os, "name", "nt"
    ), patch.object(
        version_checker, "available_update", return_value=Release("1.2.3", "url")
    ), patch.object(
        version_checker, "installed_version", return_value="1.0.0"
    ), patch("builtins.input", return_value="y"), patch.object(
        version_checker, "install_update", return_value=False
    ), patch.object(version_checker.sys, "stderr", stderr):
        version_checker.check_and_prompt()  # returns, does not raise

    assert "Unable to start the issue-worm updater" in stderr.getvalue()


# --- #182: an unexpected payload shape must not take down the CLI ---------


@patch("version_checker.installed_version", return_value="1.0.0")
def test_available_update_reports_a_non_list_assets_field(mock_installed, caplog):
    """A dict `assets` is rejected at the source, with a warning (#182).

    Iterating a dict yields its str keys, so `asset.get("name")` used to
    raise AttributeError and take down the CLI at startup. Validating the
    field instead routes it through the existing ValueError handler, so
    the user is told the release could not be read rather than the check
    failing silently.
    """
    payload = {
        "tag_name": "v2.0.0",
        # An object where the code expects a list of asset objects.
        "assets": {"issue_worm-2.0.0-py3-none-any.whl": "https://example/x.whl"},
    }
    response = Mock()
    response.raise_for_status = Mock()
    response.json = Mock(return_value=payload)

    with patch("version_checker.requests.get", return_value=response):
        with pytest.raises(ValueError, match="non-list assets"):
            version_checker.latest_release()

        with caplog.at_level(logging.WARNING):
            assert version_checker.available_update() is None

    assert "Unable to check for an issue-worm update" in caplog.text


@patch("version_checker.latest_release", side_effect=AttributeError("str has no get"))
@patch("version_checker.installed_version", return_value="1.0.0")
def test_available_update_silent_on_attribute_error(mock_installed, mock_latest):
    """The backstop for any other payload shape that trips an attribute
    access (#182): best-effort means None, never a crash at startup."""
    assert version_checker.available_update() is None
