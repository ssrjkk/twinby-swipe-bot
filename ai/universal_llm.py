from typing import Any
from urllib.parse import urlsplit

import httpx

from config import settings


class UniversalAIGateway:
    """Explicitly configured providers; Gemini supports only OpenAI-compatible /openai URLs."""

    def __init__(self) -> None:
        self.provider = settings.ai_provider.lower().strip()
        if self.provider not in {
            "openai",
            "anthropic",
            "gemini",
            "openrouter",
            "deepseek",
            "ollama",
            "custom",
        }:
            raise ValueError("Unsupported AI provider")
        self.base_url = settings.llm_base_url.rstrip("/")
        self.api_key = settings.llm_api_key.get_secret_value().strip()
        self.model = settings.llm_model.strip()
        self.custom_header = settings.custom_auth_header

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key and self.api_key != "sk-dummy":
            if self.custom_header:
                headers[self.custom_header] = self.api_key
            elif self.provider == "anthropic":
                headers["x-api-key"] = self.api_key
            elif self.provider == "gemini":
                headers["x-goog-api-key"] = self.api_key
            elif self.api_key.lower().startswith("bearer "):
                headers["Authorization"] = self.api_key
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"

        # Protocol negotiation is required even with custom or absent authentication.
        if self.provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return headers

    async def complete_chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        json_mode: bool = True,
    ) -> str:
        if self.provider == "gemini" and not urlsplit(self.base_url).path.endswith(
            "/openai"
        ):
            raise ValueError(
                "Gemini requires an OpenAI-compatible base URL ending in /openai"
            )
        headers = self._build_headers()

        # Корректировка эндпоинта под специфику провайдеров
        url = f"{self.base_url}/chat/completions"
        if self.provider == "anthropic":
            url = f"{self.base_url}/messages"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if self.provider == "anthropic":
            payload["max_tokens"] = 1024
            payload["messages"] = [m for m in messages if m["role"] != "system"]
            system_messages = [m["content"] for m in messages if m["role"] == "system"]
            if system_messages:
                payload["system"] = "\n\n".join(system_messages)
        elif json_mode:
            payload["response_format"] = {"type": "json_object"}

        # Do not inherit workstation proxy credentials or malformed NO_PROXY values.
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

            if self.provider == "anthropic":
                return data["content"][0]["text"]
            return data["choices"][0]["message"]["content"]
