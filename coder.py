"""Coders for the free-tier `build` flow.

`issue-worm-pro`'s `NativeCoder` (`agents/coder.py`) calls
`cicaid_bridge.fetch_review`, which lives in the private `cicaid-pro`
package. This shell has no such bridge, so its coders talk straight to
an HTTP endpoint and emit the same
`=== FILE: ... === / === MODE: ... === / === END FILE ===` format
`workspace.parse_coder_output` already parses.

Two coders live here, both satisfying the same informal Coder protocol
(a constructor that accepts optional `endpoint`/`model`, a
`propose(workspace_dir, task, files) -> str` for the file-edit contract,
and a `complete(prompt) -> str` for a plain-text completion — both never
raise, any failure is logged and reported back as `""`):

- `LocalOllamaCoder` — talks to a local/self-hosted Ollama's
  `/api/generate` (`CODER_MODEL_SOURCE=local`).
- `RemoteOpenAICoder` — talks to any OpenAI-compatible
  `/v1/chat/completions` endpoint (`CODER_MODEL_SOURCE=remote`, or
  `=cloud` for the DeepSeek default, or `=lmstudio` for a local LM Studio
  server — see `build_coder`).

`build_coder` is the factory that picks between them (and the
not-yet-implemented `claude` source) based on a role's `model_source`,
reading `REMOTE_LLM_ENDPOINT` / `REMOTE_LLM_MODEL` / `REMOTE_LLM_API_KEY`,
`DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`, or `LMSTUDIO_ENDPOINT` /
`LMSTUDIO_MODEL` from the environment — see `.env-example-openai` /
`.env-example-deepseek` / `.env-example-lmstudio` in issue-worm-pro for the
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
# Output-token cap sent as `max_tokens` for CODER_MODEL_SOURCE=cloud. Left
# unset, the provider applies its own (much smaller) default, and a
# multi-file FULL rewrite is silently cut off mid-file - the Coder output
# then fails parse_coder_output as "missing declared file(s)". 32768 covers
# a FULL rewrite of several large files and stays well under DeepSeek
# V4's documented output ceiling. Overridable via CODER_MAX_TOKENS.
DEFAULT_DEEPSEEK_MAX_TOKENS = 32768

# LM Studio serves an OpenAI-compatible API on this machine, so
# CODER_MODEL_SOURCE=lmstudio reuses RemoteOpenAICoder with local defaults
# and no API key, for the same reason `cloud` does with DeepSeek's. The
# env var names deliberately match cicaid-pro's `lmstudio_common`, so one
# .env configures both the coder here and the reviewer there (#410).
DEFAULT_LMSTUDIO_ENDPOINT = "http://localhost:1234"
# Seconds to wait on /v1/models when LMSTUDIO_MODEL is unset. Short on
# purpose: it's a startup-time lookup against a server on localhost, and
# build_coder runs before any of the build's real work.
LMSTUDIO_MODEL_LOOKUP_TIMEOUT_SECONDS = 5


_DEFAULT_GPU_STRATEGY = "conservative"


def _gpu_strategy() -> str:
    """The VRAM-budget strategy ``get_coder_model()`` should use.

    Resolved from ``OLLAMA_GPU_STRATEGY``, validated against
    ``ollama_tools.gpu.STRATEGIES``. An unset or invalid value falls back
    to ``conservative`` -- the safe default (see laptop-egpu-llm's
    docs/model-picker.md: guessing high is what hangs the machine).
    ``proportional`` is what actually reaches the qwen3.8-216k tier on an
    asymmetric card pair, but only if the runtime is genuinely configured
    to place layers proportionally rather than evenly -- that is a fact
    about the machine, not something this function can detect, so it is
    opt-in via the env var rather than assumed.
    """
    raw = os.environ.get("OLLAMA_GPU_STRATEGY", "").strip().lower()
    if not raw:
        return _DEFAULT_GPU_STRATEGY
    try:
        from ollama_tools.gpu import STRATEGIES
    except ImportError:
        return _DEFAULT_GPU_STRATEGY
    except Exception:  # noqa: BLE001 - a broken ollama-tools install must not break coder construction
        logger.warning(
            "OLLAMA_GPU_STRATEGY=%r was set but ollama_tools.gpu failed to import; using %r",
            raw, _DEFAULT_GPU_STRATEGY,
        )
        return _DEFAULT_GPU_STRATEGY
    if raw not in STRATEGIES:
        logger.warning(
            "OLLAMA_GPU_STRATEGY=%r is not one of %s; using %r",
            raw, STRATEGIES, _DEFAULT_GPU_STRATEGY,
        )
        return _DEFAULT_GPU_STRATEGY
    return raw


def _default_ollama_model() -> str:
    """The model LocalOllamaCoder uses when CODER_OLLAMA_MODEL is unset.

    ``ollama-tools`` (leonarduk/laptop-egpu-llm) is an optional dependency
    (the ``vram`` extra) -- it targets one specific machine's NVIDIA/eGPU
    setup, so most installs of issue-worm will not have it. When it is
    importable, its ``get_coder_model()`` picks a model sized to the VRAM
    actually attached right now (it shells out to ``nvidia-smi``), under
    the strategy :func:`_gpu_strategy` resolves; otherwise, or if that
    probe fails or returns nothing usable, this falls back to
    DEFAULT_OLLAMA_MODEL, unchanged from before. Every failure mode here --
    missing package, a driver hiccup, an unexpected empty result -- must
    still let LocalOllamaCoder construct with a usable model, so nothing
    below is allowed to raise out of this function.
    """
    try:
        from ollama_tools.coder_model import get_coder_model

        model = get_coder_model(_gpu_strategy())
    except Exception:  # noqa: BLE001 - any failure here (missing package, GPU probe error) must fall back, never raise out of coder construction
        return DEFAULT_OLLAMA_MODEL
    return model or DEFAULT_OLLAMA_MODEL


class LocalOllamaCoder:
    """Proposes file changes for one issue via a local Ollama instance."""

    def __init__(
        self,
        endpoint: str | None = None,
        model: str | None = None,
        timeout: int = REQUEST_TIMEOUT_SECONDS,
    ):
        self.endpoint = (endpoint or DEFAULT_OLLAMA_ENDPOINT).rstrip("/")
        self.model = model or _default_ollama_model()
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
        return self.complete(prompt)

    def complete(self, prompt: str) -> str:
        """Return the raw text completion for an arbitrary `prompt`, or ""
        on any failure — never raises. Unlike `propose`, this skips the
        file-edit prompt/output contract, for callers that just want a
        plain-text answer back (e.g. `review.draft_implementation_notes`,
        which drafts a scope section rather than a file diff).
        """
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
        max_tokens: int | None = None,
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
        # None = don't send `max_tokens` at all (provider default). Not
        # every OpenAI-compatible provider accepts the field for every
        # model, so it is only sent when build_coder (or a caller) sets it.
        self.max_tokens = max_tokens

    def propose(self, workspace_dir: str, task: str, files: list[str]) -> str:
        """Return raw Coder-formatted output, or "" on any failure — never
        raises out of this method (matches the Coder protocol).
        """
        try:
            prompt = _build_prompt(workspace_dir, task, files)
        except Exception:  # noqa: BLE001 - _build_prompt is local/pure; any failure here is "cannot propose", not a bug to propagate
            logger.warning("Failed to build prompt for %s", workspace_dir, exc_info=True)
            return ""
        return self.complete(prompt)

    def complete(self, prompt: str) -> str:
        """Return the raw text completion for an arbitrary `prompt`, or ""
        on any failure — never raises. Unlike `propose`, this skips the
        file-edit prompt/output contract, for callers that just want a
        plain-text answer back (e.g. `review.draft_implementation_notes`,
        which drafts a scope section rather than a file diff).
        """
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        try:
            response = requests.post(
                f"{self.endpoint}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            choices = response.json().get("choices") or []
            if not choices:
                return ""
            if choices[0].get("finish_reason") == "length":
                # The provider stopped at its output-token cap, so the text
                # below is cut off. Still return it (the parser's recovery
                # may salvage it), but name the cause - otherwise it only
                # surfaces later as "missing declared file(s)".
                logger.warning(
                    "Remote LLM %s (model %s) stopped at its output-token "
                    "cap (finish_reason=length, max_tokens=%s); the response "
                    "is truncated - raise CODER_MAX_TOKENS if the provider "
                    "allows it",
                    self.endpoint,
                    self.model,
                    self.max_tokens if self.max_tokens is not None else "provider default",
                )
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
    def complete(self, prompt: str) -> str: ...


class CoderConfigError(ValueError):
    """A role's model_source is unknown, or is missing config it needs.

    Distinct from a `propose()`-time failure (network error, bad
    response): this is a startup-time configuration problem — the build
    can never succeed with it, so it must stop the run with an actionable
    message rather than construct a coder that will only ever talk to an
    endpoint that isn't there (issue-worm-pro#590).
    """


def _discover_lmstudio_model(endpoint: str) -> str:
    """Return a model id from LM Studio's `/v1/models`, or "" if the server
    is unreachable or has nothing to offer.

    Mirrors cicaid-pro's `lmstudio_common.get_lmstudio_model`, preference
    for a name containing "coder" included: both tiers must resolve the
    same server to the same model, or a build's coder and its reviewer
    silently end up on different ones.
    """
    url = f"{endpoint}/v1/models"
    try:
        response = requests.get(url, timeout=LMSTUDIO_MODEL_LOOKUP_TIMEOUT_SECONDS)
        response.raise_for_status()
        models = [
            entry["id"]
            for entry in (response.json().get("data") or [])
            if isinstance(entry, dict) and entry.get("id")
        ]
    except (requests.RequestException, ValueError, AttributeError, TypeError):
        # Caller turns an empty result into an actionable CoderConfigError
        # naming the endpoint, so this only needs to record the cause.
        logger.warning("LM Studio model lookup at %s failed", url, exc_info=True)
        return ""

    for name in models:
        if "coder" in name.lower():
            return name
    return models[0] if models else ""


def build_coder(role_config) -> Coder:
    """Construct the right Coder for a role's configured model_source.

    Args:
        role_config: a `config.RoleConfig` (or anything with the same
            `model_source` / `ollama_endpoint` / `ollama_model` attributes).

    Returns:
        A Coder ready to call `.propose(...)`.

    Raises:
        CoderConfigError: `model_source` is unknown, or is
            `remote`/`cloud`/`lmstudio` without the environment variables
            (or, for `lmstudio`, the reachable server) it needs (see
            `.env-example-openai` / `.env-example-deepseek` /
            `.env-example-lmstudio` in issue-worm-pro) — or is `claude`,
            not implemented here yet.
    """
    model_source = getattr(role_config, "model_source", "local")
    # Global, not role-prefixed - one HTTP timeout for whichever coder this
    # factory returns, same as REMOTE_LLM_*/DEEPSEEK_* below.
    timeout = _env_int("CODER_REQUEST_TIMEOUT_SECONDS", REQUEST_TIMEOUT_SECONDS)
    # Global output-token cap for the OpenAI-compatible coders (remote and
    # cloud). 0 / negative / non-integer = not configured.
    max_tokens_override: int | None = _env_int("CODER_MAX_TOKENS", 0)
    if max_tokens_override is not None and max_tokens_override <= 0:
        max_tokens_override = None

    if model_source == "local":
        # Endpoint/model here are unchanged from before this factory
        # existed — local's exact behaviour is compatibility-critical
        # (issue-worm-pro#590).
        return LocalOllamaCoder(
            endpoint=getattr(role_config, "ollama_endpoint", None),
            model=getattr(role_config, "ollama_model", None),
            timeout=timeout,
        )

    if model_source == "lmstudio":
        # Unlike `remote`, the zero-config case is the normal one here: the
        # local server has a well-known port, needs no API key, and can name
        # its own model — so only an unreachable/empty server is an error.
        endpoint = (
            os.getenv("LMSTUDIO_ENDPOINT") or DEFAULT_LMSTUDIO_ENDPOINT
        ).rstrip("/")
        model = os.getenv("LMSTUDIO_MODEL") or _discover_lmstudio_model(endpoint)
        if not model:
            raise CoderConfigError(
                f"CODER_MODEL_SOURCE=lmstudio found no model at {endpoint}: "
                "LMSTUDIO_MODEL is not set and /v1/models is unreachable or "
                "empty. Start LM Studio and load a model, set LMSTUDIO_MODEL, "
                "or point LMSTUDIO_ENDPOINT at the right server (no trailing "
                "/v1) — see issue-worm-pro's .env-example-lmstudio."
            )
        # No api_key: LM Studio's local server ignores Authorization, and
        # RemoteOpenAICoder omits the header entirely when it is falsy.
        # No default max_tokens either, matching `remote` — LM Studio's own
        # default is the loaded model's context, not a small provider cap.
        return RemoteOpenAICoder(
            endpoint=endpoint,
            model=model,
            api_key=None,
            timeout=timeout,
            max_tokens=max_tokens_override,
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
        # No default cap for a generic provider: some OpenAI models reject
        # `max_tokens`, so it is only sent when CODER_MAX_TOKENS is set.
        return RemoteOpenAICoder(
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            timeout=timeout,
            max_tokens=max_tokens_override,
        )

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
            endpoint=endpoint,
            model=model,
            api_key=api_key,
            timeout=timeout,
            max_tokens=max_tokens_override or DEFAULT_DEEPSEEK_MAX_TOKENS,
        )

    if model_source == "claude":
        raise CoderConfigError(
            "CODER_MODEL_SOURCE=claude is not implemented by the free "
            "engine's build coder (issue-worm-pro#590) — use 'local', "
            "'lmstudio', 'remote', or 'cloud', or run this issue through "
            "issue-worm-pro."
        )

    raise CoderConfigError(
        f"CODER_MODEL_SOURCE={model_source!r} is not a supported coder "
        "target; expected one of 'local', 'lmstudio', 'remote', 'cloud', or "
        "'claude'."
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
