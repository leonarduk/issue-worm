"""Tests for the local-Ollama coder used by the free-tier `build` flow."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from coder import DEFAULT_OLLAMA_ENDPOINT, DEFAULT_OLLAMA_MODEL, LocalOllamaCoder


def _mock_response(json_body, status_ok=True):
    response = MagicMock()
    response.json.return_value = json_body
    if status_ok:
        response.raise_for_status.return_value = None
    else:
        response.raise_for_status.side_effect = requests.HTTPError("bad status")
    return response


def test_defaults_used_when_not_configured():
    coder = LocalOllamaCoder()

    assert coder.endpoint == DEFAULT_OLLAMA_ENDPOINT
    assert coder.model == DEFAULT_OLLAMA_MODEL


def test_endpoint_trailing_slash_is_stripped():
    coder = LocalOllamaCoder(endpoint="http://localhost:11434/")

    assert coder.endpoint == "http://localhost:11434"


def test_propose_returns_response_text(tmp_path):
    coder = LocalOllamaCoder(endpoint="http://example.invalid", model="test-model")
    expected = "=== FILE: a.py ===\n=== MODE: FULL ===\nprint(1)\n=== END FILE ===\n"

    with patch("coder.requests.post", return_value=_mock_response({"response": expected})) as mock_post:
        result = coder.propose(str(tmp_path), "do the thing", ["a.py"])

    assert result == expected
    called_url = mock_post.call_args[0][0]
    assert called_url == "http://example.invalid/api/generate"
    payload = mock_post.call_args[1]["json"]
    assert payload["model"] == "test-model"
    assert payload["stream"] is False
    assert "do the thing" in payload["prompt"]


def test_propose_includes_existing_file_contents(tmp_path):
    (tmp_path / "a.py").write_text("existing content\n", encoding="utf-8")
    coder = LocalOllamaCoder()

    with patch("coder.requests.post", return_value=_mock_response({"response": "x"})) as mock_post:
        coder.propose(str(tmp_path), "task", ["a.py"])

    prompt = mock_post.call_args[1]["json"]["prompt"]
    assert "existing content" in prompt


def test_propose_notes_missing_file(tmp_path):
    coder = LocalOllamaCoder()

    with patch("coder.requests.post", return_value=_mock_response({"response": "x"})) as mock_post:
        coder.propose(str(tmp_path), "task", ["new_file.py"])

    prompt = mock_post.call_args[1]["json"]["prompt"]
    assert "does not exist yet" in prompt


def test_propose_returns_empty_string_on_request_exception(tmp_path):
    coder = LocalOllamaCoder()

    with patch("coder.requests.post", side_effect=requests.ConnectionError("down")):
        result = coder.propose(str(tmp_path), "task", ["a.py"])

    assert result == ""


def test_propose_returns_empty_string_on_bad_status(tmp_path):
    coder = LocalOllamaCoder()

    with patch("coder.requests.post", return_value=_mock_response({}, status_ok=False)):
        result = coder.propose(str(tmp_path), "task", ["a.py"])

    assert result == ""


def test_propose_returns_empty_string_on_missing_response_key(tmp_path):
    coder = LocalOllamaCoder()

    with patch("coder.requests.post", return_value=_mock_response({"other": "field"})):
        result = coder.propose(str(tmp_path), "task", ["a.py"])

    assert result == ""
