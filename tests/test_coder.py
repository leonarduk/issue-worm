"""Tests for the coders used by the free-tier `build` flow."""

import sys
from unittest.mock import MagicMock, patch

import pytest
import requests

import coder
from coder import (
    DEFAULT_DEEPSEEK_ENDPOINT,
    DEFAULT_DEEPSEEK_MAX_TOKENS,
    DEFAULT_DEEPSEEK_MODEL,
    DEFAULT_LMSTUDIO_ENDPOINT,
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OLLAMA_MODEL,
    REQUEST_TIMEOUT_SECONDS,
    CoderConfigError,
    LocalOllamaCoder,
    RemoteOpenAICoder,
    _default_ollama_model,
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
    local_coder = LocalOllamaCoder()

    assert local_coder.endpoint == DEFAULT_OLLAMA_ENDPOINT
    assert local_coder.model == DEFAULT_OLLAMA_MODEL


def test_default_ollama_model_falls_back_without_ollama_tools():
    """ollama-tools is an optional extra - it isn't installed in this test
    environment, so _default_ollama_model() must return the pre-existing
    fixed default rather than raising ImportError."""
    assert _default_ollama_model() == DEFAULT_OLLAMA_MODEL


def test_default_ollama_model_uses_get_coder_model_when_ollama_tools_installed(monkeypatch):
    fake_coder_model = type(
        "FakeModule", (), {"get_coder_model": staticmethod(lambda strategy: "qwen3.8-216k")}
    )()
    fake_package = type("FakePackage", (), {})()

    monkeypatch.setitem(sys.modules, "ollama_tools", fake_package)
    monkeypatch.setitem(sys.modules, "ollama_tools.coder_model", fake_coder_model)
    assert _default_ollama_model() == "qwen3.8-216k"


def test_default_ollama_model_falls_back_when_get_coder_model_raises(monkeypatch):
    """get_coder_model() probes live GPU state (nvidia-smi) - a driver
    hiccup or any other unexpected failure must not raise out of coder
    construction (#458 review)."""

    def _raise(strategy):
        raise RuntimeError("nvidia-smi exploded")

    fake_coder_model = type("FakeModule", (), {"get_coder_model": staticmethod(_raise)})()
    fake_package = type("FakePackage", (), {})()

    monkeypatch.setitem(sys.modules, "ollama_tools", fake_package)
    monkeypatch.setitem(sys.modules, "ollama_tools.coder_model", fake_coder_model)
    assert _default_ollama_model() == DEFAULT_OLLAMA_MODEL


def test_default_ollama_model_falls_back_when_get_coder_model_returns_empty(monkeypatch):
    fake_coder_model = type(
        "FakeModule", (), {"get_coder_model": staticmethod(lambda strategy: "")}
    )()
    fake_package = type("FakePackage", (), {})()

    monkeypatch.setitem(sys.modules, "ollama_tools", fake_package)
    monkeypatch.setitem(sys.modules, "ollama_tools.coder_model", fake_coder_model)
    assert _default_ollama_model() == DEFAULT_OLLAMA_MODEL


def _fake_ollama_tools(monkeypatch, capture: list[str]):
    """Install a fake ollama_tools.coder_model whose get_coder_model records
    the strategy it was called with and returns a fixed model name."""

    def _record(strategy):
        capture.append(strategy)
        return "qwen3.8-216k"

    fake_coder_model = type("FakeModule", (), {"get_coder_model": staticmethod(_record)})()
    fake_package = type("FakePackage", (), {})()
    monkeypatch.setitem(sys.modules, "ollama_tools", fake_package)
    monkeypatch.setitem(sys.modules, "ollama_tools.coder_model", fake_coder_model)
    monkeypatch.setitem(
        sys.modules,
        "ollama_tools.gpu",
        type("FakeGpuModule", (), {"STRATEGIES": ("conservative", "proportional", "even")})(),
    )


def test_gpu_strategy_defaults_to_conservative_when_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_GPU_STRATEGY", raising=False)
    calls: list[str] = []
    _fake_ollama_tools(monkeypatch, calls)
    _default_ollama_model()
    assert calls == ["conservative"]


def test_gpu_strategy_reads_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_GPU_STRATEGY", "proportional")
    calls: list[str] = []
    _fake_ollama_tools(monkeypatch, calls)
    model = _default_ollama_model()
    assert calls == ["proportional"]
    assert model == "qwen3.8-216k"


def test_gpu_strategy_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("OLLAMA_GPU_STRATEGY", "Proportional")
    calls: list[str] = []
    _fake_ollama_tools(monkeypatch, calls)
    _default_ollama_model()
    assert calls == ["proportional"]


def test_gpu_strategy_falls_back_on_invalid_value(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_GPU_STRATEGY", "yolo")
    calls: list[str] = []
    _fake_ollama_tools(monkeypatch, calls)
    _default_ollama_model()
    assert calls == ["conservative"]
    assert "not one of" in caplog.text


def test_gpu_strategy_whitespace_only_value_falls_back_to_conservative(monkeypatch):
    monkeypatch.setenv("OLLAMA_GPU_STRATEGY", "   ")
    calls: list[str] = []
    _fake_ollama_tools(monkeypatch, calls)
    _default_ollama_model()
    assert calls == ["conservative"]


def test_gpu_strategy_falls_back_when_ollama_tools_gpu_missing(monkeypatch):
    """OLLAMA_GPU_STRATEGY set, but ollama_tools.gpu isn't importable (e.g.
    an older ollama-tools install without the STRATEGIES constant) - this
    must still fall back to conservative rather than raising."""
    monkeypatch.setenv("OLLAMA_GPU_STRATEGY", "proportional")
    calls: list[str] = []
    _fake_ollama_tools(monkeypatch, calls)
    monkeypatch.delitem(sys.modules, "ollama_tools.gpu", raising=False)
    _default_ollama_model()
    assert calls == ["conservative"]


def test_local_ollama_coder_uses_default_ollama_model_when_unset(monkeypatch):
    monkeypatch.setattr(coder, "_default_ollama_model", lambda: "picked-by-vram")
    local_coder = LocalOllamaCoder()
    assert local_coder.model == "picked-by-vram"


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
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
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
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "user"
    assert "do the thing" in payload["messages"][0]["content"]
    assert "print(1)" in payload["messages"][0]["content"]
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


def _lmstudio_models_response(*model_ids):
    """A `/v1/models` body in LM Studio's (OpenAI-compatible) shape."""
    return _mock_response({"data": [{"id": model_id} for model_id in model_ids]})


def test_build_coder_lmstudio_uses_local_defaults(monkeypatch):
    """The zero-config case: LM Studio's own port, no API key, and no
    `/v1/models` lookup once LMSTUDIO_MODEL says which model to use."""
    monkeypatch.delenv("LMSTUDIO_ENDPOINT", raising=False)
    monkeypatch.setenv("LMSTUDIO_MODEL", "qwen2.5-coder-14b-instruct")

    with patch("coder.requests.get") as mock_get:
        coder = build_coder(RoleConfig(model_source="lmstudio"))

    assert isinstance(coder, RemoteOpenAICoder)
    assert coder.endpoint == DEFAULT_LMSTUDIO_ENDPOINT
    assert coder.model == "qwen2.5-coder-14b-instruct"
    assert not coder.api_key
    mock_get.assert_not_called()


def test_build_coder_lmstudio_reads_endpoint_env_var(monkeypatch):
    """A trailing slash is stripped so the `/v1/...` paths built from this
    endpoint don't end up doubled."""
    monkeypatch.setenv("LMSTUDIO_ENDPOINT", "http://gpu-box:1234/")
    monkeypatch.setenv("LMSTUDIO_MODEL", "qwen2.5-coder-7b-instruct")

    coder = build_coder(RoleConfig(model_source="lmstudio"))

    assert coder.endpoint == "http://gpu-box:1234"


def test_build_coder_lmstudio_discovers_model_preferring_coder(monkeypatch):
    """With no LMSTUDIO_MODEL, the model is read from the server - and a
    coder model wins, matching cicaid-pro's get_lmstudio_model so both
    tiers resolve the same server to the same model."""
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)
    monkeypatch.delenv("LMSTUDIO_ENDPOINT", raising=False)

    with patch(
        "coder.requests.get",
        return_value=_lmstudio_models_response("tinyllama-1.1b", "qwen2.5-coder-7b"),
    ) as mock_get:
        coder = build_coder(RoleConfig(model_source="lmstudio"))

    assert coder.model == "qwen2.5-coder-7b"
    assert mock_get.call_args.args[0] == f"{DEFAULT_LMSTUDIO_ENDPOINT}/v1/models"


def test_build_coder_lmstudio_discovery_falls_back_to_first_model(monkeypatch):
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)

    with patch(
        "coder.requests.get",
        return_value=_lmstudio_models_response("mistral-7b", "tinyllama-1.1b"),
    ):
        coder = build_coder(RoleConfig(model_source="lmstudio"))

    assert coder.model == "mistral-7b"


def test_build_coder_lmstudio_unreachable_server_raises(monkeypatch):
    """No model and no server is a configuration problem, not a runtime
    one: fail the build with the endpoint named rather than return a coder
    whose every request will 404."""
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)
    monkeypatch.setenv("LMSTUDIO_ENDPOINT", "http://localhost:9999")

    with patch("coder.requests.get", side_effect=requests.ConnectionError("refused")):
        with pytest.raises(CoderConfigError, match="http://localhost:9999"):
            build_coder(RoleConfig(model_source="lmstudio"))


def test_build_coder_lmstudio_no_loaded_models_raises(monkeypatch):
    monkeypatch.delenv("LMSTUDIO_MODEL", raising=False)

    with patch("coder.requests.get", return_value=_lmstudio_models_response()):
        with pytest.raises(CoderConfigError, match="LMSTUDIO_MODEL"):
            build_coder(RoleConfig(model_source="lmstudio"))


def test_lmstudio_coder_sends_no_authorization_header(monkeypatch):
    """LM Studio's local server takes no API key; sending an empty bearer
    token is worse than sending none."""
    monkeypatch.setenv("LMSTUDIO_MODEL", "qwen2.5-coder-7b-instruct")
    coder = build_coder(RoleConfig(model_source="lmstudio"))

    with patch(
        "coder.requests.post",
        return_value=_mock_response({"choices": [{"message": {"content": "hi"}}]}),
    ) as mock_post:
        assert coder.complete("prompt") == "hi"

    assert "Authorization" not in mock_post.call_args.kwargs["headers"]


def _set_env_for_model_source(monkeypatch, model_source: str) -> None:
    """Set whatever env vars build_coder requires for `model_source` to
    succeed, so the timeout tests below can be parametrized across all
    four env-backed sources symmetrically."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("REMOTE_LLM_ENDPOINT", "https://api.openai.com")
    monkeypatch.setenv("REMOTE_LLM_MODEL", "gpt-5")
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "sk-test")
    # Explicit, so no test in this group hits the network for /v1/models.
    monkeypatch.setenv("LMSTUDIO_MODEL", "qwen2.5-coder-7b-instruct")


@pytest.mark.parametrize("model_source", ["local", "lmstudio", "remote", "cloud"])
def test_build_coder_uses_default_timeout_when_unset(model_source, monkeypatch):
    monkeypatch.delenv("CODER_REQUEST_TIMEOUT_SECONDS", raising=False)
    _set_env_for_model_source(monkeypatch, model_source)

    coder = build_coder(RoleConfig(model_source=model_source))

    assert coder.timeout == REQUEST_TIMEOUT_SECONDS


@pytest.mark.parametrize("model_source", ["local", "lmstudio", "remote", "cloud"])
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


def test_local_complete_returns_response_text():
    coder = LocalOllamaCoder(endpoint="http://example.invalid", model="test-model")

    with patch(
        "coder.requests.post", return_value=_mock_response({"response": "drafted text"})
    ) as mock_post:
        result = coder.complete("draft me a section")

    assert result == "drafted text"
    called_url = mock_post.call_args[0][0]
    assert called_url == "http://example.invalid/api/generate"
    payload = mock_post.call_args[1]["json"]
    assert payload["prompt"] == "draft me a section"


def test_local_complete_returns_empty_string_on_request_exception():
    coder = LocalOllamaCoder(endpoint="http://example.invalid")

    with patch("coder.requests.post", side_effect=requests.ConnectionError("down")):
        result = coder.complete("draft me a section")

    assert result == ""


def test_remote_complete_returns_message_content():
    coder = RemoteOpenAICoder(
        endpoint="http://example.invalid", model="test-model", api_key="key"
    )

    with patch(
        "coder.requests.post", return_value=_mock_chat_response("drafted text")
    ) as mock_post:
        result = coder.complete("draft me a section")

    assert result == "drafted text"
    payload = mock_post.call_args[1]["json"]
    assert payload["messages"] == [{"role": "user", "content": "draft me a section"}]


def test_remote_complete_returns_empty_string_on_request_exception():
    coder = RemoteOpenAICoder(endpoint="http://example.invalid", model="test-model")

    with patch("coder.requests.post", side_effect=requests.ConnectionError("down")):
        result = coder.complete("draft me a section")

    assert result == ""


# --- max_tokens / truncation (leonarduk/issue-worm run 34811134456) ----


def test_remote_complete_omits_max_tokens_when_unset():
    coder = RemoteOpenAICoder(endpoint="http://example.invalid", model="m")

    with patch(
        "coder.requests.post", return_value=_mock_chat_response("x")
    ) as mock_post:
        coder.complete("p")

    assert "max_tokens" not in mock_post.call_args[1]["json"]


def test_remote_complete_sends_max_tokens_when_set():
    coder = RemoteOpenAICoder(
        endpoint="http://example.invalid", model="m", max_tokens=1234
    )

    with patch(
        "coder.requests.post", return_value=_mock_chat_response("x")
    ) as mock_post:
        coder.complete("p")

    assert mock_post.call_args[1]["json"]["max_tokens"] == 1234


def test_remote_complete_warns_when_output_hit_token_cap(caplog):
    coder = RemoteOpenAICoder(
        endpoint="http://example.invalid", model="m", max_tokens=10
    )
    body = {
        "choices": [
            {"message": {"content": "partial"}, "finish_reason": "length"}
        ]
    }

    with patch("coder.requests.post", return_value=_mock_response(body)):
        with caplog.at_level("WARNING", logger="coder"):
            result = coder.complete("p")

    # Still returned - parse_coder_output decides whether it is usable.
    assert result == "partial"
    assert "finish_reason=length" in caplog.text
    assert "CODER_MAX_TOKENS" in caplog.text


def test_build_coder_cloud_defaults_max_tokens(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.delenv("CODER_MAX_TOKENS", raising=False)

    coder = build_coder(RoleConfig(model_source="cloud"))

    assert coder.max_tokens == DEFAULT_DEEPSEEK_MAX_TOKENS


def test_build_coder_lmstudio_leaves_max_tokens_unset_by_default(monkeypatch):
    """Unlike `cloud`, there is no small provider cap to work around: LM
    Studio's default is the loaded model's own context window."""
    _set_env_for_model_source(monkeypatch, "lmstudio")
    monkeypatch.delenv("CODER_MAX_TOKENS", raising=False)

    coder = build_coder(RoleConfig(model_source="lmstudio"))

    assert coder.max_tokens is None


def test_build_coder_remote_leaves_max_tokens_unset_by_default(monkeypatch):
    _set_env_for_model_source(monkeypatch, "remote")
    monkeypatch.delenv("CODER_MAX_TOKENS", raising=False)

    coder = build_coder(RoleConfig(model_source="remote"))

    assert coder.max_tokens is None


@pytest.mark.parametrize("model_source", ["lmstudio", "remote", "cloud"])
def test_build_coder_reads_max_tokens_env_var(model_source, monkeypatch):
    _set_env_for_model_source(monkeypatch, model_source)
    monkeypatch.setenv("CODER_MAX_TOKENS", "4096")

    coder = build_coder(RoleConfig(model_source=model_source))

    assert coder.max_tokens == 4096


@pytest.mark.parametrize("raw", ["0", "-5", "lots"])
def test_build_coder_cloud_ignores_invalid_max_tokens(raw, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("CODER_MAX_TOKENS", raw)

    coder = build_coder(RoleConfig(model_source="cloud"))

    assert coder.max_tokens == DEFAULT_DEEPSEEK_MAX_TOKENS
