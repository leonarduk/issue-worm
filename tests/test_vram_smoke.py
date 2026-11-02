"""Smoke test for the ``vram`` extra on a real NVIDIA host (#506).

Needs the ``vram`` extra installed (``pip install -e ".[vram]"``), an NVIDIA
GPU that ``nvidia-smi`` reports, and Ollama running. Anywhere else - CI
included - every test here skips rather than fails. Run it by hand after
bumping the ``ollama-tools`` pin:

    python -m pytest tests/test_vram_smoke.py -rs

It goes through issue-worm's own call site, ``coder._default_ollama_model``,
and asserts invariants of the pick against the same live readings
``get_coder_model`` takes (GPUs, VRAM Ollama has resident, pulled models),
never a model name or a VRAM size, so it holds on any host.
"""

from __future__ import annotations

import pytest

import coder

ollama_tools = pytest.importorskip("ollama_tools", reason="the vram extra is not installed")
from ollama_tools import coder_model, gpu, live  # noqa: E402


@pytest.fixture
def host():
    try:
        gpus = gpu.query_gpus()
    except gpu.GpuUnavailable as exc:
        pytest.skip(f"no NVIDIA GPU: {exc}")
    if not gpus:
        pytest.skip("nvidia-smi reports no GPUs")
    installed = live.installed_models()
    if installed is None:
        pytest.skip("Ollama is not reachable")
    return gpus, live.resident_vram_bytes(), installed


def test_default_ollama_model_picks_a_tier_that_fits_and_is_pulled(host, monkeypatch):
    gpus, resident, installed = host
    monkeypatch.delenv("CODER_OLLAMA_MODEL", raising=False)
    strategy = coder._gpu_strategy()

    model = coder._default_ollama_model()

    tiers = dict((name, minimum) for minimum, name in coder_model.CODER_TIERS)
    assert model in tiers or model == coder_model.CODER_FALLBACK, model
    if model == coder_model.CODER_FALLBACK:
        return
    # Resident VRAM counts as available (ollama-tools v0.1.1+): Ollama
    # evicts it to load the pick.
    budget = gpu.budget_bytes(live.reclaim_resident(gpus, resident), strategy)
    assert budget >= tiers[model], (
        f"{model} needs {tiers[model]} bytes but the {strategy} budget is {budget}"
    )
    assert live.normalise_model_name(model) in installed, f"{model} is not pulled"

