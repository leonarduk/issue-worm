"""Tests for config module."""

import os
import pytest

from config import (
    CoderTarget,
    RoleConfig,
    TargetPool,
    _parse_coder_targets,
    get_role_env_vars,
    load_config,
    target_env_vars,
)


def test_get_role_env_vars_with_all_fields():
    """Test converting RoleConfig to environment variables."""
    config = RoleConfig(
        model_source="cloud",
        ollama_endpoint="http://localhost:11434",
        ollama_model="qwen:7b",
    )

    env_vars = get_role_env_vars(config)

    assert env_vars["MODEL_SOURCE"] == "cloud"
    assert env_vars["OLLAMA_ENDPOINT"] == "http://localhost:11434"
    assert env_vars["OLLAMA_MODEL"] == "qwen:7b"


def test_get_role_env_vars_with_minimal_fields():
    """Test converting RoleConfig with only model_source."""
    config = RoleConfig(model_source="local")

    env_vars = get_role_env_vars(config)

    assert env_vars["MODEL_SOURCE"] == "local"
    assert "OLLAMA_ENDPOINT" not in env_vars
    assert "OLLAMA_MODEL" not in env_vars


def test_load_config_defaults(monkeypatch):
    """Test loading config with environment defaults."""
    # Hermetic: load_config() loads .env itself (config.py:56), and this
    # repo's .env sets the model sources to cloud. load_dotenv uses
    # override=False, so a pre-set value wins over .env and any propagated
    # scheduler env - pin the defaults explicitly (issue #241).
    monkeypatch.setenv("CODER_MODEL_SOURCE", "local")
    monkeypatch.setenv("ANALYSER_MODEL_SOURCE", "local")
    monkeypatch.setenv("TRIAGE_MODEL_SOURCE", "local")
    config = load_config()

    assert config["coder_backend"] == "native"
    assert config["workspace_root"] == "."
    assert config["test_command"] == "pytest"
    assert config["max_rejections"] == 3
    assert config["coder_config"].model_source == "local"
    assert config["analyser_config"].model_source == "local"
    assert config["triage_config"].model_source == "local"


def test_load_config_local_source_leaves_targets_empty_when_unset(monkeypatch):
    """A local coder still needs an explicit CODER_TARGETS - synthesizing
    one would silently point at a nonexistent Ollama host."""
    monkeypatch.setenv("CODER_MODEL_SOURCE", "local")
    monkeypatch.delenv("CODER_TARGETS", raising=False)

    assert load_config()["coder_targets"] == []


def test_load_config_synthesizes_targets_for_cloud_source(monkeypatch):
    """A cloud/remote/lmstudio/claude coder doesn't route to a specific
    host, so CODER_TARGETS being unset shouldn't leave it with no
    dispatch capacity at all - a synthetic placeholder target is
    generated instead."""
    monkeypatch.setenv("CODER_MODEL_SOURCE", "cloud")
    monkeypatch.delenv("CODER_TARGETS", raising=False)

    targets = load_config()["coder_targets"]

    assert len(targets) == 1
    assert targets[0].host == ""
    assert targets[0].model == ""


def test_load_config_synthesized_targets_match_max_concurrent_issues(monkeypatch):
    """The synthesized pool has one slot per configured concurrency, same
    as an explicit CODER_TARGETS would need to provide."""
    monkeypatch.setenv("CODER_MODEL_SOURCE", "claude")
    monkeypatch.setenv("MAX_CONCURRENT_ISSUES", "3")
    monkeypatch.delenv("CODER_TARGETS", raising=False)

    targets = load_config()["coder_targets"]

    assert len(targets) == 3
    assert [t.name for t in targets] == ["claude-1", "claude-2", "claude-3"]


def test_load_config_explicit_coder_targets_not_overridden_for_cloud_source(monkeypatch):
    """An explicitly configured CODER_TARGETS is never replaced, even for
    a non-local source - someone may still want named, load-balanced
    targets (e.g. several REMOTE_LLM endpoints)."""
    monkeypatch.setenv("CODER_MODEL_SOURCE", "cloud")
    monkeypatch.setenv("CODER_TARGETS", "desk:192.168.1.20:11434:qwen2.5-coder")

    targets = load_config()["coder_targets"]

    assert len(targets) == 1
    assert targets[0].name == "desk"


def test_load_config_reads_named_env_file(tmp_path, monkeypatch):
    """ISSUE_WORM_ENV_FILE selects a named env file instead of `.env`, so
    several provider configs (.env-local, .env-deepseek, ...) can coexist
    without copying one over `.env` to switch."""
    (tmp_path / ".env-deepseek").write_text(
        "CODER_MODEL_SOURCE=cloud\nDEEPSEEK_API_KEY=sk-from-named-file\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ISSUE_WORM_ENV_FILE", ".env-deepseek")
    monkeypatch.delenv("CODER_MODEL_SOURCE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    config = load_config()

    assert config["coder_config"].model_source == "cloud"
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-from-named-file"


def test_load_config_defaults_to_plain_dotenv_when_env_file_unset(tmp_path, monkeypatch):
    """With ISSUE_WORM_ENV_FILE unset, behaviour is unchanged: `.env`."""
    (tmp_path / ".env").write_text("CODER_MODEL_SOURCE=cloud\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ISSUE_WORM_ENV_FILE", raising=False)
    monkeypatch.delenv("CODER_MODEL_SOURCE", raising=False)

    config = load_config()

    assert config["coder_config"].model_source == "cloud"


def test_load_config_with_env_vars(monkeypatch):
    """Test loading config with custom environment variables."""
    monkeypatch.setenv("CODER_MODEL_SOURCE", "cloud")
    monkeypatch.setenv("CODER_OLLAMA_MODEL", "gpt-4")
    monkeypatch.setenv("ANALYSER_MODEL", "claude-sonnet")
    monkeypatch.setenv("WORKSPACE_ROOT", "/tmp/test")
    monkeypatch.setenv("MAX_REJECTIONS", "5")

    config = load_config()

    assert config["coder_config"].model_source == "cloud"
    assert config["coder_config"].ollama_model == "gpt-4"
    assert config["analyser_config"].ollama_model == "claude-sonnet"
    assert config["workspace_root"] == "/tmp/test"
    assert config["max_rejections"] == 5


def test_load_config_model_fallback(monkeypatch):
    """Test that CODER_MODEL falls back from CODER_OLLAMA_MODEL."""
    monkeypatch.setenv("CODER_MODEL", "qwen:14b")
    monkeypatch.delenv("CODER_OLLAMA_MODEL", raising=False)

    config = load_config()

    assert config["coder_config"].ollama_model == "qwen:14b"


def test_load_config_endpoint_fallback(monkeypatch):
    """Test that CODER_ENDPOINT falls back from CODER_OLLAMA_ENDPOINT."""
    monkeypatch.setenv("CODER_ENDPOINT", "http://coder-host:11434")
    monkeypatch.delenv("CODER_OLLAMA_ENDPOINT", raising=False)

    config = load_config()

    assert config["coder_config"].ollama_endpoint == "http://coder-host:11434"


def test_load_config_endpoint_prefers_ollama_endpoint(monkeypatch):
    """Test that CODER_OLLAMA_ENDPOINT wins over CODER_ENDPOINT when both are set."""
    monkeypatch.setenv("CODER_OLLAMA_ENDPOINT", "http://ollama-host:11434")
    monkeypatch.setenv("CODER_ENDPOINT", "http://generic-host:11434")

    config = load_config()

    assert config["coder_config"].ollama_endpoint == "http://ollama-host:11434"


# --- MCP doc lookup (issue #15) --------------------------------------------

_MCP_ENV_VARS = (
    "MCP_DOC_LOOKUP_ENABLED",
    "MCP_SERVER_URL",
    "MCP_TOOL_NAME",
    "MCP_CONTEXT7_API_KEY",
    "MCP_TIMEOUT_SECONDS",
    "MCP_MAX_DOC_CHARS",
)


def test_load_config_mcp_doc_lookup_disabled_by_default(monkeypatch):
    """The feature is off unless MCP_DOC_LOOKUP_ENABLED is explicitly set -
    no MCP keys must leak into role env vars for existing installs."""
    for var in _MCP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    role = load_config()["analyser_config"]

    assert role.mcp_doc_lookup_enabled is False
    assert "MCP_DOC_LOOKUP_ENABLED" not in get_role_env_vars(role)


def test_load_config_mcp_doc_lookup_enabled(monkeypatch):
    """Enabled roles carry the MCP settings in role_env_vars so the Analyser
    (and cicaid_bridge.mcp_doc_lookup) can rely on them."""
    monkeypatch.setenv("MCP_DOC_LOOKUP_ENABLED", "1")
    monkeypatch.setenv("MCP_SERVER_URL", "https://example.com/mcp")
    monkeypatch.setenv("MCP_TOOL_NAME", "lookup_docs")
    monkeypatch.setenv("MCP_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("MCP_MAX_DOC_CHARS", "1000")

    role = load_config()["analyser_config"]
    env_vars = get_role_env_vars(role)

    assert role.mcp_doc_lookup_enabled is True
    assert env_vars["MCP_DOC_LOOKUP_ENABLED"] == "1"
    assert env_vars["MCP_SERVER_URL"] == "https://example.com/mcp"
    assert env_vars["MCP_TOOL_NAME"] == "lookup_docs"
    assert env_vars["MCP_TIMEOUT_SECONDS"] == "5"
    assert env_vars["MCP_MAX_DOC_CHARS"] == "1000"
    # The API key is deliberately not carried in role_env_vars (it would be
    # merged into the aider subprocess env); the bridge reads it from
    # os.environ instead (issue #15).
    assert "MCP_CONTEXT7_API_KEY" not in env_vars


def test_load_config_mcp_defaults_apply_when_enabled(monkeypatch):
    """Enabling the feature with no overrides yields the Context7 defaults."""
    for var in _MCP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MCP_DOC_LOOKUP_ENABLED", "true")

    env_vars = get_role_env_vars(load_config()["analyser_config"])

    assert env_vars["MCP_SERVER_URL"] == "https://mcp.context7.com/mcp"
    assert env_vars["MCP_TOOL_NAME"] == "get_context7_docs"
    assert env_vars["MCP_TIMEOUT_SECONDS"] == "10"
    assert env_vars["MCP_MAX_DOC_CHARS"] == "8000"


def test_load_config_mcp_invalid_int_falls_back(monkeypatch, caplog):
    """A non-integer MCP_TIMEOUT_SECONDS must not brick the feature."""
    monkeypatch.setenv("MCP_DOC_LOOKUP_ENABLED", "1")
    monkeypatch.setenv("MCP_TIMEOUT_SECONDS", "lots")

    role = load_config()["analyser_config"]

    assert role.mcp_timeout_seconds == 10
    assert "not an integer" in caplog.text


def _make_targets() -> list[CoderTarget]:
    """Create a small, deterministic pool of coder targets for tests."""
    return [
        CoderTarget(name="alpha", host="http://alpha:11434", model="qwen:7b"),
        CoderTarget(name="beta", host="http://beta:11434", model="qwen:14b"),
    ]


def test_target_pool_empty():
    """Acquire on an empty pool returns None."""
    pool = TargetPool([])

    assert pool.acquire() is None


def test_target_pool_acquire_until_all_busy():
    """Acquire hands out targets in configured order, then returns None."""
    pool = TargetPool(_make_targets())

    first = pool.acquire()
    second = pool.acquire()

    assert first is not None and first.name == "alpha"
    assert second is not None and second.name == "beta"
    assert pool.acquire() is None


def test_target_pool_mark_unavailable_then_acquire():
    """An unavailable target is not returned until rechecked."""
    pool = TargetPool(_make_targets())

    acquired = pool.acquire()
    assert acquired is not None
    pool.mark_unavailable(acquired)

    # The unavailable target is skipped; the remaining free one is handed out.
    assert pool.acquire() is not None
    # Everything is now busy or unavailable.
    assert pool.acquire() is None


def test_target_pool_recheck_then_acquire():
    """recheck_unavailable returns unavailable targets to the pool."""
    pool = TargetPool(_make_targets())

    acquired = pool.acquire()
    assert acquired is not None
    pool.mark_unavailable(acquired)
    pool.recheck_unavailable()

    retried = pool.acquire()
    assert retried is not None
    assert retried.name == "alpha"


def test_target_pool_release_then_reacquire():
    """Release makes a busy target acquirable again."""
    pool = TargetPool(_make_targets())

    first = pool.acquire()
    second = pool.acquire()
    assert first is not None and second is not None
    assert pool.acquire() is None

    pool.release(first)

    reacquired = pool.acquire()
    assert reacquired is not None
    assert reacquired.name == first.name


def test_target_env_vars():
    """target_env_vars returns the endpoint/model pair for a target."""
    target = CoderTarget(name="alpha", host="http://alpha:11434", model="qwen:7b")

    env_vars = target_env_vars(target)

    assert env_vars == {
        "OLLAMA_ENDPOINT": "http://alpha:11434",
        "OLLAMA_MODEL": "qwen:7b",
    }


def test_target_env_vars_adds_scheme_to_bare_host():
    """A bare "host:port" (the .env.example format) isn't a valid URL on
    its own - ollama_common builds f"{endpoint}/api/tags" directly, so a
    scheme must be added here rather than left for the caller to notice.
    """
    target = CoderTarget(name="desk", host="192.168.1.20:11434", model="qwen2.5-coder")

    env_vars = target_env_vars(target)

    assert env_vars["OLLAMA_ENDPOINT"] == "http://192.168.1.20:11434"


def test_target_env_vars_preserves_model_tag():
    """The model field is passed through untouched, tag and all."""
    target = CoderTarget(name="desk", host="localhost:11434", model="qwen2.5-coder:7b")

    env_vars = target_env_vars(target)

    assert env_vars["OLLAMA_MODEL"] == "qwen2.5-coder:7b"


def test_parse_coder_targets_host_and_port():
    """The documented CODER_TARGETS format is name:host:port:model - port
    is a separate field, not folded into a free-form host, precisely so
    it can't collide with a tag inside the model name."""
    targets = _parse_coder_targets("desk:192.168.1.20:11434:qwen2.5-coder")

    assert targets == [
        CoderTarget(name="desk", host="192.168.1.20:11434", model="qwen2.5-coder")
    ]


def test_parse_coder_targets_multiple_targets():
    """Comma-separated targets each keep their own host:port intact."""
    targets = _parse_coder_targets(
        "desk:192.168.1.20:11434:qwen2.5-coder,gpu-box:192.168.1.50:11434:qwen2.5-coder"
    )

    assert [t.name for t in targets] == ["desk", "gpu-box"]
    assert [t.host for t in targets] == ["192.168.1.20:11434", "192.168.1.50:11434"]


def test_parse_coder_targets_model_with_tag():
    """An Ollama model name can itself contain a ":tag" (e.g. "...:7b") -
    without a fixed host:port split point, this would be misread as the
    port/model boundary instead of part of the model name."""
    targets = _parse_coder_targets("desk:localhost:11434:qwen2.5-coder:7b")

    assert targets == [
        CoderTarget(name="desk", host="localhost:11434", model="qwen2.5-coder:7b")
    ]


def test_parse_coder_targets_missing_port_skipped():
    """The port is mandatory (disambiguates host from a tagged model
    name), so a spec without one is dropped rather than misparsed."""
    targets = _parse_coder_targets("desk:localhost:qwen2.5-coder")

    assert targets == []


def test_parse_coder_targets_malformed_spec_skipped():
    """A spec with too few colons has no way to separate
    name/host/port/model and is dropped rather than raising."""
    targets = _parse_coder_targets("just-a-name")

    assert targets == []


def test_load_config_loads_dotenv_from_working_directory(tmp_path, monkeypatch):
    """`.env` is resolved from the process cwd, not from config.py's dir.

    A pip-installed CLI lives outside the user's project, so the default
    search (from the calling module) would miss the workspace `.env`.
    """
    (tmp_path / ".env").write_text(
        "CODER_MODEL_SOURCE=cloud\nDEEPSEEK_API_KEY=sk-from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CODER_MODEL_SOURCE", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    config = load_config()

    assert config["coder_config"].model_source == "cloud"
    assert os.environ["DEEPSEEK_API_KEY"] == "sk-from-dotenv"


def test_load_config_real_env_vars_win_over_dotenv(tmp_path, monkeypatch):
    """Real environment variables are not overridden by `.env` values."""
    (tmp_path / ".env").write_text(
        "CODER_MODEL_SOURCE=cloud\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODER_MODEL_SOURCE", "claude")

    config = load_config()

    assert config["coder_config"].model_source == "claude"


# --- MAX_CONCURRENT_ISSUES (parallel dispatch, #159) ------------------------


def test_load_config_defaults_to_sequential_dispatch():
    """MAX_CONCURRENT_ISSUES defaults to 1 - today's sequential behaviour."""
    assert load_config()["max_concurrent_issues"] == 1


def test_load_config_max_concurrent_issues_from_env(monkeypatch):
    """A positive MAX_CONCURRENT_ISSUES is parsed as the concurrency."""
    monkeypatch.setenv("MAX_CONCURRENT_ISSUES", "3")

    assert load_config()["max_concurrent_issues"] == 3


def test_load_config_clamps_max_concurrent_issues_below_one(monkeypatch, caplog):
    """Values below 1 must not brick the scheduler - clamped to 1."""
    monkeypatch.setenv("MAX_CONCURRENT_ISSUES", "0")

    assert load_config()["max_concurrent_issues"] == 1
    assert "clamping to 1" in caplog.text


def test_load_config_rejects_non_integer_max_concurrent_issues(monkeypatch, caplog):
    """A non-integer MAX_CONCURRENT_ISSUES falls back to 1 with a warning."""
    monkeypatch.setenv("MAX_CONCURRENT_ISSUES", "lots")

    assert load_config()["max_concurrent_issues"] == 1
    assert "not an integer" in caplog.text


# --- TargetPool thread safety (parallel dispatch, #159) ---------------------


def test_target_pool_hands_out_each_target_once_under_concurrent_acquire():
    """Concurrent acquire from parallel workers must never hand out the
    same target twice or exceed the pool size."""
    import threading

    pool = TargetPool(_make_targets())
    results = []
    results_lock = threading.Lock()

    def _acquire():
        target = pool.acquire()
        with results_lock:
            results.append(target.name if target else None)

    threads = [threading.Thread(target=_acquire) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    handed_out = [name for name in results if name is not None]
    assert len(handed_out) == 2  # pool of two: exactly two handed out
    assert len(set(handed_out)) == 2  # never the same target twice
    assert results.count(None) == 6


def test_target_pool_concurrent_release_allows_reacquire():
    """A target released by one worker is acquirable by another, without
    ever being handed to two workers at once (#159)."""
    import threading

    pool = TargetPool(_make_targets())
    acquired = []
    acquired_lock = threading.Lock()
    phase_one = threading.Barrier(4)
    phase_two = threading.Barrier(4)

    def _worker():
        target = pool.acquire()
        with acquired_lock:
            acquired.append(target.name if target else None)
        phase_one.wait()  # all four have tried to acquire
        if target is not None:
            pool.release(target)
        phase_two.wait()  # releases are done before the re-acquire round
        if target is None:
            retry = pool.acquire()
            with acquired_lock:
                acquired.append(retry.name if retry else None)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    first_round = [name for name in acquired[:4] if name is not None]
    second_round = acquired[4:]
    # Round one: exactly the pool's two targets, each once.
    assert len(first_round) == 2
    assert len(set(first_round)) == 2
    # Round two: the two workers that missed round one each got a freed
    # target back, and never the same one twice.
    assert sorted(second_round) == sorted(first_round)
