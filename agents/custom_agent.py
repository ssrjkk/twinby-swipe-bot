"""
Шаблон для добавления своего агента.

Инструкция:
  1. Скопируй этот файл в agents/my_agent.py
  2. Переименуй класс и установи name
  3. Реализуй analyze()
  4. Зарегистрируй в agent_factory.py (добавь в REGISTRY)
"""

from agents.base import BaseAgent, Decision


class CustomAgent(BaseAgent):
    name = "custom"  # <-- задай уникальное имя

    def __init__(self, api_key: str | None = None, **kwargs):
        self.api_key = api_key
        # инициализируй свой клиент здесь

    def analyze(self, screenshots: list[bytes], bio: str) -> Decision:
        """
        Отправь screenshots и bio в свой AI,
        верни Decision(decision, confidence, reason).
        """
        # пример заглушки:
        return Decision(decision="skip", confidence=50, reason="агент не настроен")
