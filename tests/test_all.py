"""
pytest tests/test_all.py -v
"""

# ruff: noqa: E402
import json
import os
import sys
import types

import pytest

# ── stub тяжёлых зависимостей до импорта ─────────────────────────────────────
for mod in [
    "uiautomator2", "appium", "appium.webdriver", "appium.options",
    "appium.webdriver.common", "appium.webdriver.common.appiumby",
    "selenium", "selenium.common", "selenium.common.exceptions",
    "selenium.webdriver", "selenium.webdriver.support",
    "selenium.webdriver.support.ui",
    "selenium.webdriver.support.expected_conditions",
    "openai", "google", "google.genai", "google.genai.types",
]:
    sys.modules.setdefault(mod, types.ModuleType(mod))

sys.modules["selenium.common.exceptions"].NoSuchElementException = Exception
sys.modules["appium.webdriver.common.appiumby"].AppiumBy = type(
    "AppiumBy", (), {"ACCESSIBILITY_ID": "id"}
)()

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.base import BaseAgent, Decision  # noqa: E402
from agent_factory import create_agent, list_agents, REGISTRY  # noqa: E402


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Decision dataclass
# ═══════════════════════════════════════════════════════════════════════════════


class TestDecision:
    def test_like_valid(self):
        d = Decision("like", 80, "хорошо")
        assert d.decision == "like"
        assert d.confidence == 80

    def test_skip_valid(self):
        d = Decision("skip", 30, "слабо")
        assert d.decision == "skip"

    def test_invalid_decision(self):
        with pytest.raises(ValueError, match="decision must be"):
            Decision("maybe", 50, "")

    def test_confidence_out_of_range_high(self):
        with pytest.raises(ValueError, match="confidence"):
            Decision("like", 101, "")

    def test_confidence_out_of_range_low(self):
        with pytest.raises(ValueError, match="confidence"):
            Decision("skip", -1, "")

    def test_confidence_boundaries(self):
        Decision("like", 0, "")
        Decision("skip", 100, "")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. BaseAgent._parse_json
# ═══════════════════════════════════════════════════════════════════════════════


class ConcreteAgent(BaseAgent):
    name = "test"

    def analyze(self, screenshots, bio):
        return Decision("skip", 0, "stub")


class TestParseJson:
    def setup_method(self):
        self.agent = ConcreteAgent()

    def test_clean_json(self):
        raw = '{"decision":"like","confidence":82,"reason":"отлично"}'
        d = self.agent._parse_json(raw)
        assert d.decision == "like"
        assert d.confidence == 82
        assert d.reason == "отлично"

    def test_markdown_wrapper(self):
        raw = '```json\n{"decision":"skip","confidence":40,"reason":"слабо"}\n```'
        d = self.agent._parse_json(raw)
        assert d.decision == "skip"
        assert d.confidence == 40

    def test_whitespace_padded(self):
        raw = '  \n{"decision":"like","confidence":90,"reason":"топ"}  \n'
        d = self.agent._parse_json(raw)
        assert d.decision == "like"

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            self.agent._parse_json("not json at all")

    def test_missing_fields_defaults(self):
        raw = '{"decision":"skip"}'
        d = self.agent._parse_json(raw)
        assert d.confidence == 0
        assert d.reason == "—"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. CustomAgent
# ═══════════════════════════════════════════════════════════════════════════════


class TestCustomAgent:
    def test_analyze_returns_decision(self):
        from agents.custom_agent import CustomAgent

        agent = CustomAgent()
        result = agent.analyze([b"fakepng"], "bio text")
        assert isinstance(result, Decision)
        assert result.decision in ("like", "skip")
        assert 0 <= result.confidence <= 100

    def test_name(self):
        from agents.custom_agent import CustomAgent

        assert CustomAgent.name == "custom"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. AgentFactory
# ═══════════════════════════════════════════════════════════════════════════════


class TestAgentFactory:
    def test_list_agents_contains_all(self):
        agents = list_agents()
        assert "claude" in agents
        assert "openai" in agents
        assert "gemini" in agents
        assert "custom" in agents

    def test_create_custom_no_key(self):
        agent = create_agent("custom")
        assert agent.name == "custom"

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError, match="не найден"):
            create_agent("nonexistent_agent_xyz")

    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("AGENT", "custom")
        agent = create_agent()
        assert agent.name == "custom"

    def test_explicit_overrides_env(self, monkeypatch):
        monkeypatch.setenv("AGENT", "openai")
        agent = create_agent("custom")
        assert agent.name == "custom"

    def test_registry_has_module_and_class(self):
        for name, (mod, cls) in REGISTRY.items():
            assert "." in mod, f"{name}: путь модуля должен содержать '.'"
            assert cls[0].isupper(), f"{name}: класс должен начинаться с заглавной"

    def test_all_agents_importable(self):
        import importlib

        for name, (mod_path, cls_name) in REGISTRY.items():
            mod = importlib.import_module(mod_path)
            cls = getattr(mod, cls_name, None)
            assert cls is not None, f"{name}: класс {cls_name} не найден в {mod_path}"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Threshold logic
# ═══════════════════════════════════════════════════════════════════════════════


class TestThreshold:
    @pytest.mark.parametrize(
        "confidence,expected",
        [
            (80, "like"),
            (75, "like"),
            (74, "skip"),
            (0, "skip"),
        ],
    )
    def test_threshold_applied(self, confidence, expected):
        threshold = 75
        result = Decision("like", confidence, "test")
        if result.decision == "like" and result.confidence < threshold:
            result.decision = "skip"
        assert result.decision == expected

    def test_skip_unaffected_by_threshold(self):
        result = Decision("skip", 90, "test")
        if result.decision == "like" and result.confidence < 75:
            result.decision = "skip"
        assert result.decision == "skip"


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Syntax check всех файлов
# ═══════════════════════════════════════════════════════════════════════════════


class TestSyntax:
    @pytest.mark.parametrize(
        "fname",
        [
            "config.py",
            "agent_factory.py",
            "run_android.py",
            "run_ios.py",
            "run_mac.py",
            "agents/base.py",
            "agents/claude_agent.py",
            "agents/openai_agent.py",
            "agents/gemini_agent.py",
            "agents/custom_agent.py",
        ],
    )
    def test_syntax(self, fname):
        import ast

        root = os.path.dirname(os.path.dirname(__file__))
        path = os.path.join(root, fname)
        with open(path) as f:
            src = f.read()
        try:
            ast.parse(src)
        except SyntaxError as e:
            pytest.fail(f"SyntaxError в {fname}: {e}")
