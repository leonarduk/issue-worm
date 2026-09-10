"""Coders for the free-tier `build` flow.

`issue-worm-pro`'s `NativeCoder` (`agents/coder.py`) calls
`cicaid_bridge.fetch_review`, which lives in the private `cicaid-pro`
package. This shell has no such bridge, so its coders talk straight to
an HTTP endpoint and emit the same
`=== FILE: ... === / === MODE: ... === / === END FILE ===` format
`workspace.parse_coder_output` already parses.

Two coders live here, both satisfying the same informal Coder protocol
(a constructor that accepts optional `endpoint`/`model`, and a
`propose(workspace_dir, task, files) -> str` that never raises — any
failure is logged and reported back as `""`):

- `LocalOllamaCoder` — talks to a local/self-hosted Ollama's
  `/api/generate` (`CODER_MODEL_SOURCE=local`).
- `RemoteOpenAICoder` — talks to any OpenAI-compatible
  `/v1/chat/completions` endpoint (`CODER_MODEL_SOURCE=remote`, or
  `=cloud` for the DeepSeek default — see `build_coder`).

`build_coder` is the factory that picks between them (and the
not-yet-implemented `claude` source) based on a role's `model_source`,
reading `REMOTE_LLM_ENDPOINT` / `REMOTE_LLM_MODEL` / `REMOTE_LLM_API_KEY`
or `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL` from the environment — see
`.env-example-openai` / `.env-example-deepseek` in issue-worm-pro for the
env vars these are meant to match.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath
from typing import Protocol

import requests

from workspace import MODE_FULL, sanitize_file_path

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder"
# Overridable per invocation via CODER_REQUEST_TIMEOUT_SECONDS (read lazily
# in build_coder, like every other env-sourced setting here) - this is only
# the fallback for direct construction (tests, or a Coder built by hand).
REQUEST_TIMEOUT_SECONDS = 300

# DeepSeek's API is itself OpenAI-compatible (see .env-example-deepseek in
# issue-worm-pro), so CODER_MODEL_SOURCE=cloud reuses RemoteOpenAICoder with
# these as its provider default rather than needing a separate coder class.
# Both are overridable via DEEPSEEK_ENDPOINT / DEEPSEEK_MODEL (build_coder's
# `cloud` branch) - these are only the fallback when unset.
DEFAULT_DEEPSEEK_ENDPOINT = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"


class LocalOllamaCoder:
    """Proposes file changes for one issue via a local Ollama instance."""

    def __init__(
        self,
        endpoint: str | None = None,
        model: str | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ):
        self.endpoint = (endpoint or DEFAULT_OLLAMA_ENDPOINT).rstrip("/")
        self.model = model or DEFAULT_OLLAMA_MODEL
        self.timeout = timeout

    def propose(self, workspace_dir: str, task: str, files: list[str]) -> str:
        """Return raw Coder-formatted output, or "" on any failure — never
        raises out of this method (matches the Coder protocol).
        """
        try:
            prompt = _build_prompt(workspace_dir, task, files)
        except Exception:  # noqa: BLE001 - _build_prompt is local/pure; any failure here is "cannot propose", not a bug to propagate
            logger.warning("Failed to build prompt for %s", workspace_dir, exc_info=True)
            return ""
        try:
            response = requests.post(
                f"{self.endpoint}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json().get("response") or ""
        except (requests.RequestException, ValueError):
            logger.warning(
                "Ollama request to %s (model %s) failed",
                self.endpoint,
                self.model,
                exc_info=True,
            )
            return ""


class RemoteOpenAICoder:
    """Proposes file changes via an OpenAI-compatible `/v1/chat/completions`
    endpoint — any provider that speaks that shape (OpenAI itself, a
    self-hosted vLLM/SGLang/Ollama-serving-OpenAI-API host, or DeepSeek,
    whose API is OpenAI-compatible — see `build_coder`'s `cloud` branch).
    """

    def __init__(
        self,
        endpoint: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ):
        # No fallback default endpoint/model here (unlike LocalOllamaCoder):
        # there is no sensible generic default for an arbitrary OpenAI-
        # compatible provider, so build_coder is what supplies one
        # (required REMOTE_LLM_ENDPOINT for `remote`, DeepSeek's fixed
        # endpoint for `cloud`) before ever constructing this class.
        self.endpoint = (endpoint or "").rstrip("/")
        self.model = model or ""
        self.api_key = api_key
        self.timeout = timeout

    def propose(self, workspace_dir: str, task: str, files: list[str]) -> str:
        """Return raw Coder-formatted output, or "" on any failure — never
        raises out of this method (matches the Coder protocol).
        """
        try:
            prompt = _build_prompt(workspace_dir, task, files)
        except Exception:  # noqa: BLE001 - _build_prompt is local/pure; any failure here is "cannot propose", not a bug to propagate
            logger.warning("Failed to build prompt for %s", workspace_dir, exc_info=True)
            return ""
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = requests.post(
                f"{self.endpoint}/v1/chat/completions",
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            choices = response.json().get("choices") or []
            if not choices:
                return ""
            return choices[0].get("message", {}).get("content") or ""
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            logger.warning(
                "Remote LLM request to %s (model %s) failed",
                self.endpoint,
                self.model,
                exc_info=True,
            )
            return ""


class Coder(Protocol):
    """Informal protocol both coder classes satisfy — see module docstring."""

    def propose(self, workspace_dir: str, task: str, files: list[str]) -> str: ...


class CoderConfigError(ValueError):
    """A role's model_source is unknown, or is missing config it needs.

    Distinct from a `propose()`-time failure (network error, bad
    response): this is a startup-time configuration problem — the build
    can never succeed with it, so it must stop the run with an actionable
    message rather than construct a coder that will only ever talk to an
    endpoint that isn't there (issue-worm-pro#590).
    """


def build_coder(role_config) -> Coder:
    """Construct the right Coder for a role's configured model_source.

    Args:
        role_config: a `config.RoleConfig` (or anything with the same
            `model_source` / `ollama_endpoint` / `ollama_model` attributes).

    Returns:
        A Coder ready to call `.propose(...)`.

    Raises:
        CoderConfigError: `model_source` is unknown, or is `remote`/`cloud`
            without the environment variables it needs (see
            `.env-example-openai` / `.env-example-deepseek` in
            issue-worm-pro) — or is `claude`, not implemented here yet.
    """
    model_source = getattr(role_config, "model_source", "local")
    # Global, not role-prefixed - one HTTP timeout for whichever coder this
    # factory returns, same as REMOTE_LLM_*/DEEPSEEK_* below.
    timeout = _env_int("CODER_REQUEST_TIMEOUT_SECONDS", REQUEST_TIMEOUT_SECONDS)

    if model_source == "local":
        # Endpoint/model here are unchanged from before this factory
        # existed — local's exact behaviour is compatibility-critical
        # (issue-worm-pro#590).
        return LocalOllamaCoder(
            endpoint=getattr(role_config, "ollama_endpoint", None),
            model=getattr(role_config, "ollama_model", None),
            timeout=timeout,
        )

    if model_source == "remote":
        # REMOTE_LLM_* are global, not role-prefixed (like config.py's
        # MCP_* vars) — a generic OpenAI-compatible endpoint isn't a
        # per-role concept the way an Ollama host pool is.
        endpoint = os.getenv("REMOTE_LLM_ENDPOINT")
        if not endpoint:
            raise CoderConfigError(
                "CODER_MODEL_SOURCE=remote requires REMOTE_LLM_ENDPOINT to "
                "be set (e.g. https://api.openai.com) — see "
                "issue-worm-pro's .env-example-openai."
            )
        api_key = os.getenv("REMOTE_LLM_API_KEY")
        if not api_key:
            raise CoderConfigError(
                "CODER_MODEL_SOURCE=remote requires REMOTE_LLM_API_KEY to "
                "be set — see issue-worm-pro's .env-example-openai."
            )
        model = os.getenv("REMOTE_LLM_MODEL")
        if not model:
            raise CoderConfigError(
                "CODER_MODEL_SOURCE=remote requires REMOTE_LLM_MODEL to be "
                "set — see issue-worm-pro's .env-example-openai."
            )
        return RemoteOpenAICoder(endpoint=endpoint, model=model, api_key=api_key, timeout=timeout)

    if model_source == "cloud":
        # DeepSeek is the only `cloud` provider implemented today (its API
        # is itself OpenAI-compatible, so it reuses RemoteOpenAICoder with
        # a provider default endpoint/model instead of a bespoke class —
        # see the module docstring and issue-worm-pro#590's report).
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise CoderConfigError(
                "CODER_MODEL_SOURCE=cloud requires DEEPSEEK_API_KEY to be "
                "set — see issue-worm-pro's .env-example-deepseek. (cloud "
                "currently only supports DeepSeek.)"
            )
        endpoint = os.getenv("DEEPSEEK_ENDPOINT") or DEFAULT_DEEPSEEK_ENDPOINT
        model = os.getenv("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL
        return RemoteOpenAICoder(
            endpoint=endpoint, model=model, api_key=api_key, timeout=timeout
        )

    if model_source == "claude":
        raise CoderConfigError(
            "CODER_MODEL_SOURCE=claude is not implemented by the free "
            "engine's build coder (issue-worm-pro#590) — use 'local', "
            "'remote', or 'cloud', or run this issue through issue-worm-pro."
        )

    raise CoderConfigError(
        f"CODER_MODEL_SOURCE={model_source!r} is not a supported coder "
        "target; expected one of 'local', 'remote', 'cloud', or 'claude'."
    )


def _env_int(name: str, default: int) -> int:
    """Parse an int env var, falling back to ``default`` on invalid values.

    Mirrors config.py's own `_env_int` - duplicated rather than imported
    since coder.py otherwise has no dependency on config.py beyond the
    `RoleConfig`-shaped duck type `build_coder` already documents.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


def _build_prompt(workspace_dir: str, task: str, files: list[str]) -> str:
    file_sections = "\n".join(_format_file_section(workspace_dir, f) for f in files)
    return (
        "You are modifying a local git checkout to satisfy the task below.\n\n"
        f"Task:\n{task}\n\n"
        "Current contents of the declared files (a file that doesn't exist "
        "yet is shown as empty — the task may be asking you to create it):\n"
        f"{file_sections}\n\n"
        f"{_build_format_instructions(files)}"
    )


def _format_file_section(workspace_dir: str, path: str) -> str:
    safe_path = _safe_relative_path(path)
    if safe_path is None:
        return f"--- {path} ---\n(invalid or unsafe path — not read)\n"
    try:
        content = (Path(workspace_dir) / safe_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        content = "(file does not exist yet, or is not readable as text)"
    return f"--- {path} ---\n{content}\n"


def _safe_relative_path(path: str) -> str | None:
    """Reject anything that isn't a plain path inside the workspace —
    mirrors workspace.py's own write-side guard (`_escapes_repo`) so a
    `FILES:` entry like `../../../.env` can't be read off disk and fed
    into the prompt sent to the (possibly remote) Ollama endpoint.
    """
    normalized = sanitize_file_path(path)
    if normalized is None:
        return None
    if ".." in PurePosixPath(normalized).parts:
        return None
    return normalized


def _build_format_instructions(files: list[str]) -> str:
    return (
        "Respond with one section per changed file, in exactly this format "
        "and nothing else:\n\n"
        "=== FILE: <path> ===\n"
        f"=== MODE: {MODE_FULL} ===\n"
        "<the complete new file content>\n"
        "=== END FILE ===\n\n"
        f"Only touch these declared files: {', '.join(files)}. Always use "
        f"MODE: {MODE_FULL} (a complete file rewrite), not a diff."
    )
