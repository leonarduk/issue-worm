"""Configuration management for issue-worm.

Loads settings for model providers, coder targets, and workspace paths.
"""

import logging
import os
import threading
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


@dataclass
class CoderTarget:
    """A configured coder backend target."""
    name: str
    host: str
    model: str


# MCP doc lookup (issue #15): an optional pre-generation API-doc lookup so
# an agent can verify an API's current signature before calling it. Configured
# globally (same env vars for every role), off by default, and only the
# Analyser acts on it today - see cicaid_bridge.mcp_doc_lookup.
DEFAULT_MCP_SERVER_URL = "https://mcp.context7.com/mcp"
DEFAULT_MCP_TOOL_NAME = "get_context7_docs"
DEFAULT_MCP_TIMEOUT_SECONDS = 10
DEFAULT_MCP_MAX_DOC_CHARS = 8000


@dataclass
class RoleConfig:
    """Configuration for a specific role (coder, analyser, triage)."""
    model_source: str  # "local" | "cloud" | "remote" | "claude"
    ollama_endpoint: Optional[str] = None
    ollama_model: Optional[str] = None
    # MCP doc lookup settings (issue #15). mcp_doc_lookup_enabled gates the
    # feature; the rest are the resolved defaults so get_role_env_vars can
    # emit a self-contained env dict when enabled.
    mcp_doc_lookup_enabled: bool = False
    mcp_server_url: str = DEFAULT_MCP_SERVER_URL
    mcp_tool_name: str = DEFAULT_MCP_TOOL_NAME
    mcp_timeout_seconds: int = DEFAULT_MCP_TIMEOUT_SECONDS
    mcp_max_doc_chars: int = DEFAULT_MCP_MAX_DOC_CHARS


def load_config() -> dict:
    """Load configuration from environment or config file.

    Loads `.env` from the current directory first (real environment
    variables win over `.env` values), then reads the resulting
    environment.

    Returns a dict with:
    - coder_backend: "native" | "aider"
    - coder_targets: list of CoderTarget
    - workspace_root: path to workspace directory
    - test_command: shell command to run tests
    - coder_config: RoleConfig for the coder role
    - analyser_config: RoleConfig for the analyser role
    - triage_config: RoleConfig for the triage role
    - log_level: logging level name (default "INFO")
    - log_file: optional path mirroring log records to a file
      ("" = stderr only)
    - max_rejections: AI-review rejections allowed before an issue is
      handed off to a human instead of re-dispatched (default 3, #306)
    """
    # `dotenv_path=".env"` resolves from the process working directory,
    # not from this module's location — otherwise a pip-installed CLI
    # would search next to config.py and never see the user's `.env`.
    # override=False keeps real environment variables on top.
    load_dotenv(dotenv_path=".env", override=False)

    config = {
        "coder_backend": os.getenv("CODER_BACKEND", "native"),
        "workspace_root": os.getenv("WORKSPACE_ROOT", "."),
        "test_command": os.getenv("TEST_COMMAND", "pytest"),
        "max_concurrent_issues": _parse_max_concurrent_issues(
            os.getenv("MAX_CONCURRENT_ISSUES", "1")
        ),
        # AI-review rejections allowed before the review-feedback loop
        # hands the issue to a human (#306).
        "max_rejections": _env_int("MAX_REJECTIONS", 3),
        "coder_config": _load_role_config("CODER"),
        "analyser_config": _load_role_config("ANALYSER"),
        "triage_config": _load_role_config("TRIAGE"),
        "coder_targets": _parse_coder_targets(os.getenv("CODER_TARGETS", "")),
        "log_level": os.getenv("LOG_LEVEL", "INFO"),
        "log_file": os.getenv("LOG_FILE", ""),
    }
    return config


def _parse_max_concurrent_issues(raw: str) -> int:
    """Parse MAX_CONCURRENT_ISSUES, falling back to 1 on invalid values.

    The default of 1 is today's strictly sequential dispatch (see #159), so
    a mis-set env var must not brick the Scheduler: values below 1 and
    non-integer values are clamped to 1 with a warning rather than raising.
    """
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "MAX_CONCURRENT_ISSUES=%r is not an integer; using 1 (sequential)", raw
        )
        return 1
    if value < 1:
        logger.warning(
            "MAX_CONCURRENT_ISSUES=%s is below 1; clamping to 1 (sequential)", raw
        )
        return 1
    return value


def _load_role_config(role_prefix: str) -> RoleConfig:
    """Load configuration for a specific role from environment variables.

    Args:
        role_prefix: Prefix for environment variables (e.g., "CODER", "ANALYSER")

    Returns:
        RoleConfig with model source and Ollama settings.
    """
    model_source = os.getenv(f"{role_prefix}_MODEL_SOURCE", "local")

    ollama_endpoint = os.getenv(f"{role_prefix}_OLLAMA_ENDPOINT")
    ollama_model = os.getenv(f"{role_prefix}_OLLAMA_MODEL")

    if not ollama_endpoint:
        ollama_endpoint = os.getenv(f"{role_prefix}_ENDPOINT")

    if not ollama_model:
        ollama_model = os.getenv(f"{role_prefix}_MODEL")

    # MCP doc lookup keys are global, not role-prefixed (issue #15): every
    # role reads the same MCP_* vars, and only an agent that acts on them
    # (currently the Analyser) uses them. The Context7 API key is deliberately
    # NOT loaded here - it must stay out of role_env_vars (which AiderCoder
    # merges into the aider subprocess env) and is read from os.environ by
    # cicaid_bridge.mcp_doc_lookup instead.
    mcp_doc_lookup_enabled = _env_flag("MCP_DOC_LOOKUP_ENABLED")

    return RoleConfig(
        model_source=model_source,
        ollama_endpoint=ollama_endpoint,
        ollama_model=ollama_model,
        mcp_doc_lookup_enabled=mcp_doc_lookup_enabled,
        mcp_server_url=os.getenv("MCP_SERVER_URL") or DEFAULT_MCP_SERVER_URL,
        mcp_tool_name=os.getenv("MCP_TOOL_NAME") or DEFAULT_MCP_TOOL_NAME,
        mcp_timeout_seconds=_env_int("MCP_TIMEOUT_SECONDS", DEFAULT_MCP_TIMEOUT_SECONDS),
        mcp_max_doc_chars=_env_int("MCP_MAX_DOC_CHARS", DEFAULT_MCP_MAX_DOC_CHARS),
    )


_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    """Parse a boolean env var; anything not explicitly true is False.

    An unset or empty var is False, matching the opt-in shape of the other
    issue-worm switches (feature off unless explicitly enabled).
    """
    return os.getenv(name, "").strip().lower() in _TRUE_ENV_VALUES


def _env_int(name: str, default: int) -> int:
    """Parse an int env var, falling back to ``default`` on invalid values.

    Mirrors _parse_max_concurrent_issues' fail-safe: a mis-set value must
    not brick the feature, it falls back with a warning.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


def _parse_coder_targets(targets_str: str) -> list[CoderTarget]:
    """Parse CODER_TARGETS environment variable into a list of CoderTarget.

    Format: "name1:host1:model1,name2:host2:model2"

    Args:
        targets_str: Comma-separated list of colon-separated target specs.

    Returns:
        List of CoderTarget objects.
    """
    targets = []
    if not targets_str:
        return targets

    for target_spec in targets_str.split(","):
        parts = target_spec.strip().split(":")
        if len(parts) == 3:
            targets.append(CoderTarget(name=parts[0], host=parts[1], model=parts[2]))

    return targets


class TargetPool:
    """Tracks which coder targets are free, busy, or known-unreachable.

    Wraps the list of CoderTarget produced by _parse_coder_targets and keeps
    in-memory availability state so the Scheduler can hand out targets
    without handing out the same one twice, and can retry unreachable hosts
    on a later polling pass. All state transitions are guarded by a lock:
    with parallel dispatch (#159) several workers acquire/release
    concurrently, and the check-then-add in acquire() must be atomic or the
    same target could be handed to two workers.
    """

    def __init__(self, targets: list[CoderTarget]):
        self._targets = list(targets)
        self._busy: set[str] = set()
        self._unavailable: set[str] = set()
        self._lock = threading.Lock()

    def acquire(self) -> Optional[CoderTarget]:
        """Return the first free, available target and mark it busy.

        Iterates the configured order so acquisition is deterministic.

        Returns:
            A free, available CoderTarget, or None if all targets are busy
            or unavailable.
        """
        with self._lock:
            for target in self._targets:
                if (
                    target.name not in self._busy
                    and target.name not in self._unavailable
                ):
                    self._busy.add(target.name)
                    return target
        return None

    def release(self, target: CoderTarget) -> None:
        """Mark a busy target free again so it can be acquired."""
        with self._lock:
            self._busy.discard(target.name)

    def mark_unavailable(self, target: CoderTarget) -> None:
        """Mark a target known-unreachable and free it.

        The target will not be returned by acquire() until
        recheck_unavailable() is called.
        """
        with self._lock:
            self._busy.discard(target.name)
            self._unavailable.add(target.name)

    def recheck_unavailable(self) -> None:
        """Return all unavailable targets to the available set.

        The Scheduler calls this at the start of a later polling pass so an
        unreachable host is retried rather than stalling the queue.
        """
        with self._lock:
            self._unavailable.clear()


def get_role_env_vars(role_config: RoleConfig) -> dict[str, str]:
    """Convert a RoleConfig to environment variables for cicaid's fetch_review.

    Args:
        role_config: The RoleConfig to convert.

    Returns:
        Dict of environment variables to set before calling fetch_review.
    """
    env_vars = {}

    if role_config.model_source:
        env_vars["MODEL_SOURCE"] = role_config.model_source

    if role_config.ollama_endpoint:
        env_vars["OLLAMA_ENDPOINT"] = role_config.ollama_endpoint

    if role_config.ollama_model:
        env_vars["OLLAMA_MODEL"] = role_config.ollama_model

    if role_config.mcp_doc_lookup_enabled:
        # Self-contained when enabled: cicaid_bridge.mcp_doc_lookup can rely
        # on these keys being present (with the resolved defaults filled in).
        # The Context7 API key is intentionally not emitted here - see
        # _load_role_config; the bridge reads it from os.environ.
        env_vars["MCP_DOC_LOOKUP_ENABLED"] = "1"
        env_vars["MCP_SERVER_URL"] = role_config.mcp_server_url or DEFAULT_MCP_SERVER_URL
        env_vars["MCP_TOOL_NAME"] = role_config.mcp_tool_name or DEFAULT_MCP_TOOL_NAME
        env_vars["MCP_TIMEOUT_SECONDS"] = str(role_config.mcp_timeout_seconds)
        env_vars["MCP_MAX_DOC_CHARS"] = str(role_config.mcp_max_doc_chars)

    return env_vars


def target_env_vars(target: CoderTarget) -> dict[str, str]:
    """Convert a CoderTarget to Ollama environment variables.

    Args:
        target: The CoderTarget to convert.

    Returns:
        Dict of environment variables to set when dispatching to the target.
    """
    return {"OLLAMA_ENDPOINT": target.host, "OLLAMA_MODEL": target.model}
