"""
Агент: OpenAI GPT-4o / GPT-4o-mini
env: OPENAI_API_KEY
pip install openai
"""

import base64
import os

from agents.base import BaseAgent, Decision


class OpenAIAgent(BaseAgent):
    name = "openai"

    def __init__(self, api_key: str | None = None, model: str = "gpt-4o"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model   = model
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY не задан")

    def analyze(self, screenshots: list[bytes], bio: str) -> Decision:
        from openai import OpenAI
        client  = OpenAI(api_key=self.api_key)
        content = []

        for img in screenshots[:3]:
            b64 = base64.standard_b64encode(img).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"},
            })
        content.append({"type": "text", "text": f"Bio:\n{bio.strip() or '(пусто)'}"})

        resp = client.chat.completions.create(
            model=self.model,
            max_tokens=200,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user",   "content": content},
            ],
        )
        return self._parse_json(resp.choices[0].message.content)
