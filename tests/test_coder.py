"""Tests for the coders used by the free-tier `build` flow."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from coder import (
    DEFAULT_DEEPSEEK_ENDPOINT,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OLLAMA_MODEL,
    REQUEST_TIMEOUT_SECONDS,
    CoderConfigError,
    LocalOllamaCoder,
    RemoteOpenAICoder,
    build_coder,
)
from config import RoleConfig


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


def test_local_propose_passes_timeout_to_requests_post(tmp_path):
    coder = LocalOllamaCoder(endpoint="http://example.invalid", model="test-model", timeout=45)

    with patch("coder.requests.post", return_value=_mock_response({"response": "x"})) as mock_post:
        coder.propose(str(tmp_path), "task", ["a.py"])

    assert mock_post.call_args[1]["timeout"] == 45


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


def test_propose_returns_empty_string_when_build_prompt_raises(tmp_path):
    """propose() must never raise (the Coder protocol's contract) even
    when _build_prompt itself blows up, not just when the HTTP call
    does."""
    coder = LocalOllamaCoder()

    with patch("coder._build_prompt", side_effect=RuntimeError("boom")), patch(
        "coder.requests.post"
    ) as mock_post:
        result = coder.propose(str(tmp_path), "task", ["a.py"])

    assert result == ""
    mock_post.assert_not_called()


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


def test_propose_does_not_read_outside_the_workspace(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("do not leak me", encoding="utf-8")
    coder = LocalOllamaCoder()

    with patch("coder.requests.post", return_value=_mock_response({"response": "x"})) as mock_post:
        coder.propose(str(tmp_path), "task", ["../secret.txt"])

    prompt = mock_post.call_args[1]["json"]["prompt"]
    assert "do not leak me" not in prompt


def test_propose_does_not_read_absolute_path(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("also secret", encoding="utf-8")
    coder = LocalOllamaCoder()

    with patch("coder.requests.post", return_value=_mock_response({"response": "x"})) as mock_post:
        coder.propose(str(tmp_path), "task", [str(outside)])

    prompt = mock_post.call_args[1]["json"]["prompt"]
    assert "also secret" not in prompt


def test_propose_survives_non_utf8_file_content(tmp_path):
    (tmp_path / "binary.dat").write_bytes(b"\xff\xfe\x00\x01")
    coder = LocalOllamaCoder()

    with patch("coder.requests.post", return_value=_mock_response({"response": "x"})):
        result = coder.propose(str(tmp_path), "task", ["binary.dat"])

    assert result == "x"


def _mock_chat_response(content):
    return _mock_response({"choices": [{"message": {"content": content}}]})


# --- RemoteOpenAICoder -------------------------------------------------


def test_remote_endpoint_trailing_slash_is_stripped():
    coder = RemoteOpenAICoder(endpoint="https://api.openai.com/", model="gpt-5")

    assert coder.endpoint == "https://api.openai.com"


def test_remote_propose_posts_openai_chat_completions_shape(tmp_path):
    coder = RemoteOpenAICoder(
        endpoint="https://api.openai.com", model="gpt-5", api_key="sk-test"
    )
    expected = "=== FILE: a.py ===\n=== MODE: FULL ===\nprint(1)\n=== END FILE ===\n"

    with patch(
        "coder.requests.post", return_value=_mock_chat_response(expected)
    ) as mock_post:
        result = coder.propose(str(tmp_path), "do the thing", ["a.py"])

    assert result == expected
    called_url = mock_post.call_args[0][0]
    assert called_url == "https://api.openai.com/v1/chat/completions"
    payload = mock_post.call_args[1]["json"]
    assert payload["model"] == "gpt-5"
    assert payload["messages"] == [{"role": "user", "content": payload["messages"][0]["content"]}]
    assert "do the thing" in payload["messages"][0]["content"]
    headers = mock_post.call_args[1]["headers"]
    assert headers["Authorization"] == "Bearer sk-test"


def test_remote_propose_passes_timeout_to_requests_post(tmp_path):
    coder = RemoteOpenAICoder(
        endpoint="https://api.openai.com", model="gpt-5", api_key="sk-test", timeout=45
    )

    with patch(
        "coder.requests.post", return_value=_mock_chat_response("x")
    ) as mock_post:
        coder.propose(str(tmp_path), "task", ["a.py"])

    assert mock_post.call_args[1]["timeout"] == 45


def test_remote_propose_omits_auth_header_without_api_key(tmp_path):
    coder = RemoteOpenAICoder(endpoint="https://api.openai.com", model="gpt-5")

    with patch(
        "coder.requests.post", return_value=_mock_chat_response("x")
    ) as mock_post:
        coder.propose(str(tmp_path), "task", ["a.py"])

    assert "Authorization" not in mock_post.call_args[1]["headers"]


def test_remote_propose_returns_empty_string_when_build_prompt_raises(tmp_path):
    """propose() must never raise (the Coder protocol's contract) even
    when _build_prompt itself blows up, not just when the HTTP call
    does."""
    coder = RemoteOpenAICoder(endpoint="https://api.openai.com", model="gpt-5")

    with patch("coder._build_prompt", side_effect=RuntimeError("boom")), patch(
        "coder.requests.post"
    ) as mock_post:
        result = coder.propose(str(tmp_path), "task", ["a.py"])

    assert result == ""
    mock_post.assert_not_called()


def test_remote_propose_returns_empty_string_on_request_exception(tmp_path):
    coder = RemoteOpenAICoder(endpoint="https://api.openai.com", model="gpt-5")

    with patch("coder.requests.post", side_effect=requests.ConnectionError("down")):
        result = coder.propose(str(tmp_path), "task", ["a.py"])

    assert result == ""


def test_remote_propose_returns_empty_string_on_bad_status(tmp_path):
    coder = RemoteOpenAICoder(endpoint="https://api.openai.com", model="gpt-5")

    with patch("coder.requests.post", return_value=_mock_response({}, status_ok=False)):
        result = coder.propose(str(tmp_path), "task", ["a.py"])

    assert result == ""


def test_remote_propose_returns_empty_string_on_missing_choices(tmp_path):
    coder = RemoteOpenAICoder(endpoint="https://api.openai.com", model="gpt-5")

    with patch("coder.requests.post", return_value=_mock_response({"choices": []})):
        result = coder.propose(str(tmp_path), "task", ["a.py"])

    assert result == ""


# --- build_coder factory ------------------------------------------------


def test_build_coder_local_returns_local_ollama_coder():
    role_config = RoleConfig(
        model_source="local",
        ollama_endpoint="http://desk:11434",
        ollama_model="qwen2.5-coder:7b",
    )

    coder = build_coder(role_config)

    assert isinstance(coder, LocalOllamaCoder)
    assert coder.endpoint == "http://desk:11434"
    assert coder.model == "qwen2.5-coder:7b"


def test_build_coder_local_uses_ollama_defaults_when_unset():
    coder = build_coder(RoleConfig(model_source="local"))

    assert isinstance(coder, LocalOllamaCoder)
    assert coder.endpoint == DEFAULT_OLLAMA_ENDPOINT
    assert coder.model == DEFAULT_OLLAMA_MODEL


def test_build_coder_remote_reads_remote_llm_env_vars(monkeypatch):
    monkeypatch.setenv("REMOTE_LLM_ENDPOINT", "https://api.openai.com")
    monkeypatch.setenv("REMOTE_LLM_MODEL", "gpt-5")
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "sk-test")

    coder = build_coder(RoleConfig(model_source="remote"))

    assert isinstance(coder, RemoteOpenAICoder)
    assert coder.endpoint == "https://api.openai.com"
    assert coder.model == "gpt-5"
    assert coder.api_key == "sk-test"


def test_build_coder_remote_missing_endpoint_raises(monkeypatch):
    monkeypatch.delenv("REMOTE_LLM_ENDPOINT", raising=False)
    monkeypatch.setenv("REMOTE_LLM_MODEL", "gpt-5")
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "sk-test")

    with pytest.raises(CoderConfigError, match="REMOTE_LLM_ENDPOINT"):
        build_coder(RoleConfig(model_source="remote"))


def test_build_coder_remote_missing_api_key_raises(monkeypatch):
    monkeypatch.setenv("REMOTE_LLM_ENDPOINT", "https://api.openai.com")
    monkeypatch.setenv("REMOTE_LLM_MODEL", "gpt-5")
    monkeypatch.delenv("REMOTE_LLM_API_KEY", raising=False)

    with pytest.raises(CoderConfigError, match="REMOTE_LLM_API_KEY"):
        build_coder(RoleConfig(model_source="remote"))


def test_build_coder_remote_missing_model_raises(monkeypatch):
    monkeypatch.setenv("REMOTE_LLM_ENDPOINT", "https://api.openai.com")
    monkeypatch.delenv("REMOTE_LLM_MODEL", raising=False)
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "sk-test")

    with pytest.raises(CoderConfigError, match="REMOTE_LLM_MODEL"):
        build_coder(RoleConfig(model_source="remote"))


def test_build_coder_cloud_uses_deepseek_default_endpoint_and_model(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)

    coder = build_coder(RoleConfig(model_source="cloud"))

    assert isinstance(coder, RemoteOpenAICoder)
    assert coder.endpoint == DEFAULT_DEEPSEEK_ENDPOINT
    assert coder.model == DEFAULT_DEEPSEEK_MODEL
    assert coder.api_key == "sk-test"


def test_build_coder_cloud_model_override(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_MODEL", "deepseek-v4")

    coder = build_coder(RoleConfig(model_source="cloud"))

    assert coder.model == "deepseek-v4"


def test_build_coder_cloud_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(CoderConfigError, match="DEEPSEEK_API_KEY"):
        build_coder(RoleConfig(model_source="cloud"))


def test_build_coder_cloud_reads_deepseek_endpoint_env_var(monkeypatch):
    """DEEPSEEK_ENDPOINT overrides the default the same way DEEPSEEK_MODEL
    already does - a self-hosted or regional DeepSeek-compatible endpoint
    shouldn't require a code change to use."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPSEEK_ENDPOINT", "https://deepseek.internal.example")

    coder = build_coder(RoleConfig(model_source="cloud"))

    assert coder.endpoint == "https://deepseek.internal.example"


def _set_env_for_model_source(monkeypatch, model_source: str) -> None:
    """Set whatever env vars build_coder requires for `model_source` to
    succeed, so the timeout tests below can be parametrized across all
    three env-backed sources symmetrically."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("REMOTE_LLM_ENDPOINT", "https://api.openai.com")
    monkeypatch.setenv("REMOTE_LLM_MODEL", "gpt-5")
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "sk-test")


@pytest.mark.parametrize("model_source", ["local", "remote", "cloud"])
def test_build_coder_uses_default_timeout_when_unset(model_source, monkeypatch):
    monkeypatch.delenv("CODER_REQUEST_TIMEOUT_SECONDS", raising=False)
    _set_env_for_model_source(monkeypatch, model_source)

    coder = build_coder(RoleConfig(model_source=model_source))

    assert coder.timeout == REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("model_source", ["local", "remote", "cloud"])
def test_build_coder_reads_request_timeout_env_var(model_source, monkeypatch):
    monkeypatch.setenv("CODER_REQUEST_TIMEOUT_SECONDS", "45")
    _set_env_for_model_source(monkeypatch, model_source)

    coder = build_coder(RoleConfig(model_source=model_source))

    assert coder.timeout == 45


def test_build_coder_invalid_timeout_env_var_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("CODER_REQUEST_TIMEOUT_SECONDS", "not-a-number")

    coder = build_coder(RoleConfig(model_source="local"))

    assert coder.timeout == REQUEST_TIMEOUT_SECONDS


def test_build_coder_claude_raises_not_implemented():
    with pytest.raises(CoderConfigError, match="claude"):
        build_coder(RoleConfig(model_source="claude"))


def test_build_coder_unknown_model_source_raises():
    with pytest.raises(CoderConfigError, match="not-a-real-source"):
        build_coder(RoleConfig(model_source="not-a-real-source"))
