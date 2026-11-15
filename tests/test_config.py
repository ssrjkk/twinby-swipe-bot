"""Tests for scripts.config: defaults, env overrides, threshold bounds.

The config module is critical: LIKE_THRESHOLD guards the "like only on
confident match" invariant, so its default and override behaviour are
locked here. sys.path is prepared the same way test_all.py does it.
"""

import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest  # noqa: E402  (must follow the sys.path setup above)

ENV_VARS = ("LIKE_THRESHOLD", "MAX_SWIPES", "DELAY_MIN", "DELAY_MAX", "AGENT")


@pytest.fixture
def clean_config(monkeypatch):
    """Reload scripts.config with the tune-up env vars removed."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    import scripts.config as cfg

    cfg = importlib.reload(cfg)
    yield cfg
    importlib.reload(cfg)


def test_defaults_match_documented_behaviour(clean_config):
    assert clean_config.LIKE_THRESHOLD == 75
    assert clean_config.MAX_SWIPES == 50
    assert clean_config.DELAY_MIN == 3
    assert clean_config.DELAY_MAX == 7
    assert clean_config.AGENT == "claude"


def test_threshold_override_via_env(monkeypatch, clean_config):
    monkeypatch.setenv("LIKE_THRESHOLD", "90")
    cfg = importlib.reload(clean_config)
    assert cfg.LIKE_THRESHOLD == 90


def test_agent_override_via_env(monkeypatch, clean_config):
    monkeypatch.setenv("AGENT", "gemini")
    cfg = importlib.reload(clean_config)
    assert cfg.AGENT == "gemini"


def test_threshold_falls_back_on_garbage(clean_config, monkeypatch):
    monkeypatch.setenv("LIKE_THRESHOLD", "not-a-number")
    with pytest.raises(ValueError):
        importlib.reload(clean_config)
    # a broken override must not survive into later runs
    monkeypatch.delenv("LIKE_THRESHOLD")
    importlib.reload(clean_config)
