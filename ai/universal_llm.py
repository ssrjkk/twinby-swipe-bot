import asyncio
import base64
import json
import logging
import re
from typing import Any

import httpx

from config import settings

logger = logging.getLogger(__name__)

PROVIDER_PRESETS: dict[str, tuple[str, str]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "anthropic": ("https://api.anthropic.com/v1", "claude-3-5-sonnet-latest"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "mistral": ("https://api.mistral.ai/v1", "mistral-small-latest"),
    "together": ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "xai": ("https://api.x.ai/v1", "grok-2-latest"),
    "ollama": ("http://localhost:11434/v1", "llama3.2-vision"),
    "vllm": ("http://localhost:8000/v1", "Qwen2.5-VL-7B-Instruct"),
    "lmstudio": ("http://localhost:1234/v1", "local-model"),
    "custom": ("http://localhost:8000/v1", "local-model"),
}

# Провайдеры, которые штатно работают без ключа вообще.
KEYLESS_PROVIDERS: tuple[str, ...] = ("ollama", "vllm", "lmstudio")

# Если ключ уже содержит схему, вторую не добавляем никогда.
AUTH_SCHEMES: tuple[str, ...] = ("bearer ", "token ", "api-key ", "apikey ", "basic ", "key ")


def mask_api_key(value: str) -> str:
    """Внешне безопасное представление секрета: виден только хвост, сырой ключ не уходит наружу."""
    raw = (value or "").strip()
    if not raw or raw == "sk-dummy":
        return ""
    if len(raw) <= 8:
        return "*" * len(raw)
    return f"{'*' * (len(raw) - 7)}{raw[-7:]}"


def list_provider_presets() -> list[dict[str, Any]]:
    """Пресеты для мастера, панели и `heart3 setup`: (URL, модель) на каждого провайдера."""
    return [
        {"provider": name, "base_url": url, "model": model, "requires_key": name not in KEYLESS_PROVIDERS}
        for name, (url, model) in PROVIDER_PRESETS.items()
    ]


def extract_json_object(raw_text: str) -> dict[str, Any]:
    """Надёжно извлекает первый валидный JSON-объект из ответа LLM
    (включая ответы в ```json ... ``` или с пояснениями до/после JSON).
    """
    cleaned = raw_text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1).strip())

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(cleaned[idx:])
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

    raise ValueError(f"Не удалось извлечь JSON-объект из ответа модели: {cleaned[:160]!r}")


class UniversalAIGateway:
    """Универсальный мультимодальный шлюз для любого провайдера ИИ:
    - OpenAI / OpenRouter / DeepSeek / Ollama / vLLM / Custom (OpenAI-compatible)
    - Anthropic Claude (native /messages + base64 images)
    - Google Gemini (нативный REST API :generateContent и OpenAI-compatible endpoint)
    Поддерживает любые форматы ключей (Bearer, x-api-key, x-goog-api-key, custom_auth_header).
    """

    def __init__(
        self,
        provider: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        custom_header: str | None = None,
        max_retries: int = 3,
        auth_prefix: str | None = None,
        api_key_query_param: str | None = None,
    ) -> None:
        self.provider = (provider or settings.ai_provider).lower().strip()
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = (api_key if api_key is not None else settings.llm_api_key.get_secret_value()).strip()
        self.model = (model or settings.llm_model).strip()
        self.custom_header = custom_header if custom_header is not None else settings.custom_auth_header
        self.max_retries = max_retries
        self.auth_prefix = auth_prefix if auth_prefix is not None else settings.llm_auth_prefix
        self.api_key_query_param = (
            api_key_query_param if api_key_query_param is not None else settings.llm_api_key_query_param
        )

    def _is_native_gemini(self) -> bool:
        return self.provider == "gemini" and "/openai" not in self.base_url

    def _has_key(self) -> bool:
        return bool(self.api_key) and self.api_key != "sk-dummy"

    def _uses_query_key(self) -> bool:
        return bool(self.api_key_query_param)

    def _key_carries_scheme(self) -> bool:
        return self.api_key.lower().startswith(AUTH_SCHEMES)

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not self._has_key():
            return headers

        if self.custom_header:
            headers[self.custom_header] = self.api_key
        elif self.provider == "anthropic":
            headers["x-api-key"] = self.api_key
            headers["anthropic-version"] = "2023-06-01"
        elif self._uses_query_key():
            pass
        elif self.provider == "gemini":
            headers["x-goog-api-key"] = self.api_key
        elif self._key_carries_scheme():
            headers["Authorization"] = self.api_key
        else:
            headers["Authorization"] = f"{self.auth_prefix if self.auth_prefix is not None else 'Bearer '}{self.api_key}"

        return headers

    def _with_query_key(self, url: str) -> str:
        if not self._uses_query_key() or not self._has_key():
            return url
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{self.api_key_query_param}={self.api_key}"

    def auth_scheme(self) -> str:
        """Как именно ключ уйдёт провайдеру — для отображения в панели (без самого ключа)."""
        if not self._has_key():
            return "none"
        if self.custom_header:
            return f"header: {self.custom_header}"
        if self._uses_query_key():
            return f"query: {self.api_key_query_param}"
        if self.provider == "anthropic":
            return "x-api-key"
        if self.provider == "gemini":
            return "x-goog-api-key"
        if self._key_carries_scheme():
            return "как задан в ключе"
        if self.auth_prefix == "":
            return "Authorization (raw)"
        if self.auth_prefix:
            return f"Authorization: {self.auth_prefix.strip()}"
        return "Authorization: Bearer"

    def describe(self) -> dict[str, Any]:
        """Состояние шлюза для панели: секрета здесь нет — только маска ключа и способ авторизации."""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "auth": self.auth_scheme(),
            "key_configured": self._has_key(),
            "api_key_masked": mask_api_key(self.api_key),
            "keyless_ok": self.provider in KEYLESS_PROVIDERS,
            "custom_auth_header": self.custom_header or "",
            "auth_prefix": self.auth_prefix if self.auth_prefix is not None else "Bearer ",
            "api_key_query_param": self.api_key_query_param or "",
        }

    def _build_anthropic_payload(
        self,
        messages: list[dict[str, Any]],
        temperature: float,
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        system_parts: list[str] = []
        anth_messages: list[dict[str, Any]] = []

        for msg in messages:
            role = msg["role"]
            content = msg["content"]
            if role == "system":
                system_parts.append(str(content))
            else:
                anth_messages.append({"role": role, "content": content})

        valid_images = [raw for raw in (images or []) if raw]
        if valid_images and anth_messages:
            last = anth_messages[-1]
            blocks: list[dict[str, Any]] = []
            for raw in valid_images:
                media = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media,
                            "data": base64.b64encode(raw).decode("ascii"),
                        },
                    }
                )
            blocks.append({"type": "text", "text": str(last["content"])})
            last["content"] = blocks

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1024,
            "temperature": temperature,
            "messages": anth_messages,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        return payload

    def _build_native_gemini_payload(
        self,
        messages: list[dict[str, Any]],
        temperature: float,
        json_mode: bool,
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        system_parts: list[str] = []
        contents: list[dict[str, Any]] = []

        for msg in messages:
            role = msg["role"]
            text = str(msg["content"])
            if role == "system":
                system_parts.append(text)
            else:
                gem_role = "model" if role == "assistant" else "user"
                contents.append({"role": gem_role, "parts": [{"text": text}]})

        valid_images = [raw for raw in (images or []) if raw]
        if valid_images and contents:
            last_parts = contents[-1]["parts"]
            for raw in valid_images:
                mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
                last_parts.append(
                    {
                        "inline_data": {
                            "mime_type": mime,
                            "data": base64.b64encode(raw).decode("ascii"),
                        }
                    }
                )

        gen_cfg: dict[str, Any] = {"temperature": temperature}
        if json_mode:
            gen_cfg["responseMimeType"] = "application/json"

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": gen_cfg,
        }
        if system_parts:
            payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
        return payload

    def _build_openai_payload(
        self,
        messages: list[dict[str, Any]],
        temperature: float,
        json_mode: bool,
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        formatted: list[dict[str, Any]] = [dict(m) for m in messages]

        valid_images = [raw for raw in (images or []) if raw]
        if valid_images and formatted:
            last = formatted[-1]
            content_blocks: list[dict[str, Any]] = [{"type": "text", "text": str(last["content"])}]
            for raw in valid_images:
                mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
                b64 = base64.b64encode(raw).decode("ascii")
                content_blocks.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"},
                    }
                )
            last["content"] = content_blocks

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": formatted,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _resolve_endpoint_and_payload(
        self,
        messages: list[dict[str, Any]],
        temperature: float,
        json_mode: bool,
        images: list[bytes] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if self.provider == "anthropic":
            if self.base_url.endswith("/messages"):
                url = self.base_url
            else:
                base = self.base_url if self.base_url.endswith("/v1") else f"{self.base_url}/v1"
                url = f"{base}/messages"
            return url, self._build_anthropic_payload(messages, temperature, images=images)

        if self._is_native_gemini():
            if ":generateContent" in self.base_url:
                url = self.base_url
            else:
                base = self.base_url
                if not base.endswith("/v1beta") and not base.endswith("/v1"):
                    base = f"{base}/v1beta"
                url = f"{base}/models/{self.model}:generateContent"
            return url, self._build_native_gemini_payload(messages, temperature, json_mode=json_mode, images=images)

        url = self.base_url if self.base_url.endswith("/chat/completions") else f"{self.base_url}/chat/completions"
        return url, self._build_openai_payload(messages, temperature, json_mode=json_mode, images=images)

    def _extract_response_text(self, data: dict[str, Any]) -> str:
        if self.provider == "anthropic":
            return str(data["content"][0]["text"])
        if self._is_native_gemini():
            parts = data["candidates"][0]["content"]["parts"]
            return "".join(str(p.get("text", "")) for p in parts)
        return str(data["choices"][0]["message"]["content"])

    async def complete_chat(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.7,
        json_mode: bool = True,
        images: list[bytes] | None = None,
    ) -> str:
        headers = self._build_headers()
        url, payload = self._resolve_endpoint_and_payload(messages, temperature, json_mode, images=images)
        url = self._with_query_key(url)

        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=45.0) as client:
            for attempt in range(1, self.max_retries + 1):
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code == 400 and "response_format" in payload:
                        payload.pop("response_format", None)
                        resp = await client.post(url, headers=headers, json=payload)

                    if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                        wait_s = min(2 ** attempt, 12)
                        logger.warning("LLM HTTP %d (попытка %d/%d), повтор через %d сек", resp.status_code, attempt, self.max_retries, wait_s)
                        await asyncio.sleep(wait_s)
                        continue

                    if resp.status_code >= 400:
                        body_preview = getattr(resp, "text", "")[:300]
                        logger.error("LLM %s/%s HTTP %d: %s", self.provider, self.model, resp.status_code, body_preview)
                    resp.raise_for_status()
                    return self._extract_response_text(resp.json())
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_exc = exc
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(2 ** attempt, 10))
                        continue
                    raise
        assert last_exc is not None
        raise last_exc

    async def complete_json(
        self,
        messages: list[dict[str, Any]],
        temperature: float = 0.3,
        images: list[bytes] | None = None,
    ) -> dict[str, Any]:
        raw = await self.complete_chat(messages=messages, temperature=temperature, json_mode=True, images=images)
        return extract_json_object(raw)

    async def health_check(self) -> tuple[bool, str]:
        try:
            reply = await self.complete_chat(
                messages=[{"role": "user", "content": "Ответь ровно одним словом: OK"}],
                temperature=0.0,
                json_mode=False,
            )
            return True, f"{self.provider}/{self.model} ответил: {reply.strip()[:60]}"
        except Exception as exc:
            return False, f"Ошибка подключения к {self.provider}/{self.model}: {exc}"
