"""
Агент: Google Gemini
env: GEMINI_API_KEY
pip install google-genai
"""

import base64
import os

from agents.base import BaseAgent, Decision


class GeminiAgent(BaseAgent):
    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str = "gemini-2.0-flash"):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model   = model
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY не задан")

    def analyze(self, screenshots: list[bytes], bio: str) -> Decision:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.api_key)
        parts  = []

        for img in screenshots[:3]:
            parts.append(types.Part.from_bytes(data=img, mime_type="image/png"))
        parts.append(types.Part.from_text(text=f"Bio:\n{bio.strip() or '(пусто)'}"))

        resp = client.models.generate_content(
            model=self.model,
            contents=[types.Content(role="user", parts=parts)],
            config=types.GenerateContentConfig(
                system_instruction=self.SYSTEM_PROMPT,
                max_output_tokens=200,
            ),
        )
        return self._parse_json(resp.text)
