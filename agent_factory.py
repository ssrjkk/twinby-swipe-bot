"""
Фабрика агентов.
Выбирает агента по имени из .env или аргумента командной строки.

Добавить нового агента:
  1. Реализуй класс в agents/my_agent.py (унаследуй BaseAgent)
  2. Добавь в REGISTRY ниже:
       "myagent": ("agents.my_agent", "MyAgent")
"""

import importlib
import os

from agents.base import BaseAgent

# ── Реестр агентов ────────────────────────────────────────────────────────────
# name -> (module_path, class_name)
REGISTRY: dict[str, tuple[str, str]] = {
    "claude":  ("agents.claude_agent", "ClaudeAgent"),
    "openai":  ("agents.openai_agent", "OpenAIAgent"),
    "gemini":  ("agents.gemini_agent", "GeminiAgent"),
    "custom":  ("agents.custom_agent", "CustomAgent"),
    # пример добавления своего:
    # "mistral": ("agents.mistral_agent", "MistralAgent"),
}


def list_agents() -> list[str]:
    return list(REGISTRY.keys())


def create_agent(name: str | None = None, **kwargs) -> BaseAgent:
    """
    Создаёт агента по имени.
    Если name не задан — читает AGENT из .env (default: claude).

    Примеры:
      agent = create_agent()            # из .env
      agent = create_agent("openai")    # явно
      agent = create_agent("gemini", model="gemini-2.0-flash")
    """
    name = (name or os.getenv("AGENT", "claude")).lower().strip()

    if name not in REGISTRY:
        available = ", ".join(REGISTRY)
        raise ValueError(f"Агент {name!r} не найден. Доступные: {available}")

    module_path, class_name = REGISTRY[name]
    module = importlib.import_module(module_path)
    cls    = getattr(module, class_name)
    return cls(**kwargs)
