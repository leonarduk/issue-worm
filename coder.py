"""Local-Ollama coder for the free-tier `build` flow.

`issue-worm-core`'s `NativeCoder` (`agents/coder.py`) calls
`cicaid_bridge.fetch_review`, which lives in the private `cicaid-core`
package. This shell has no such bridge, so `LocalOllamaCoder` talks
straight to a local Ollama HTTP endpoint's `/api/generate` and emits the
same `=== FILE: ... === / === MODE: ... === / === END FILE ===` format
`workspace.parse_coder_output` already parses.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from workspace import MODE_FULL

logger = logging.getLogger(__name__)

DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder"
REQUEST_TIMEOUT_SECONDS = 300


class LocalOllamaCoder:
    """Proposes file changes for one issue via a local Ollama instance."""

    def __init__(self, endpoint: str | None = None, model: str | None = None):
        self.endpoint = (endpoint or DEFAULT_OLLAMA_ENDPOINT).rstrip("/")
        self.model = model or DEFAULT_OLLAMA_MODEL

    def propose(self, workspace_dir: str, task: str, files: list[str]) -> str:
        """Return raw Coder-formatted output, or "" on any failure — never
        raises out of this method (matches the Coder protocol).
        """
        prompt = _build_prompt(workspace_dir, task, files)
        try:
            response = requests.post(
                f"{self.endpoint}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
                timeout=REQUEST_TIMEOUT_SECONDS,
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
    try:
        content = (Path(workspace_dir) / path).read_text(encoding="utf-8")
    except OSError:
        content = "(file does not exist yet)"
    return f"--- {path} ---\n{content}\n"


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
