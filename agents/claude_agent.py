"""
Агент: Anthropic Claude
env: ANTHROPIC_API_KEY
"""

import base64
import os

import anthropic

from agents.base import BaseAgent, Decision


class ClaudeAgent(BaseAgent):
    name = "claude"

    def __init__(self, api_key: str | None = None, model: str = "claude-opus-4-5"):
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self.model = model
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY не задан")

    def analyze(self, screenshots: list[bytes], bio: str) -> Decision:
        client = anthropic.Anthropic(api_key=self.api_key)
        content = []

        for img in screenshots[:3]:
            b64 = base64.standard_b64encode(img).decode()
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": b64,
                    },
                }
            )
        content.append({"type": "text", "text": f"Bio:\n{bio.strip() or '(пусто)'}"})

        resp = client.messages.create(
            model=self.model,
            max_tokens=200,
            system=self.SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        return self._parse_json(resp.content[0].text)
