"""
LLM client for evaluation (OpenAI/Anthropic-compatible).

Extracted from benchmarks/locomo_eval.py preserving all existing logic:
- _strip_thinking() for reasoning models
- _resolve_api_style() auto-detection
- Retry with exponential backoff on 429/5xx
"""

import asyncio
import re
from typing import Any, Dict
from urllib.parse import urlparse

import httpx


class LLMClient:
    def __init__(
        self,
        base: str,
        key: str,
        model: str,
        timeout: float = 60.0,
        api_style: str = "auto",
        no_thinking: bool = False,
    ):
        self._base = base.rstrip("/")
        self._key = key
        self._model = model
        self._api_style = self._resolve_api_style(api_style)
        self._no_thinking = no_thinking
        self._client = httpx.AsyncClient(timeout=timeout)

    async def complete(self, prompt: str, max_tokens: int = 512, retries: int = 3) -> str:
        """Send completion request with retry on transient errors."""
        url = self._build_request_url()
        payload = self._build_payload(prompt, max_tokens)
        for attempt in range(1, retries + 1):
            try:
                resp = await self._client.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._key}"},
                )
                resp.raise_for_status()
                data = resp.json()
                return self._extract_text(data)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= retries:
                    raise
                await asyncio.sleep(2 * attempt)
            except httpx.HTTPStatusError as e:
                if attempt >= retries or e.response.status_code not in (429, 500, 502, 503):
                    raise
                await asyncio.sleep(3 * attempt)
        return ""

    async def close(self):
        await self._client.aclose()

    def _resolve_api_style(self, api_style: str) -> str:
        if api_style in {"openai", "anthropic"}:
            return api_style
        host = urlparse(self._base).netloc.lower()
        if "anthropic" in host:
            return "anthropic"
        return "openai"

    def _build_request_url(self) -> str:
        if self._api_style == "anthropic":
            return f"{self._base}/messages"
        return f"{self._base}/chat/completions"

    def _build_payload(self, prompt: str, max_tokens: int) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
        if self._no_thinking:
            payload["thinking"] = {"type": "disabled"}
            payload["temperature"] = 0.7
        return payload

    @staticmethod
    def _strip_thinking(text: str) -> str:
        """Remove <think>...</think> reasoning blocks, return only the final answer."""
        stripped = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
        if stripped.strip():
            return stripped.strip()
        if "<think>" in text:
            parts = text.split("</think>")
            if len(parts) > 1:
                return parts[-1].strip()
        return text.strip()

    def _extract_text(self, data: Dict[str, Any]) -> str:
        if self._api_style == "anthropic":
            content = data.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return self._strip_thinking(str(block.get("text", "")))
                if content and isinstance(content[0], dict):
                    return self._strip_thinking(str(content[0].get("text", "")))
            raise KeyError("Anthropic response missing content text")

        choices = data.get("choices", [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            if isinstance(message, dict):
                content = message.get("content", "")
                if not content:
                    content = message.get("reasoning_content", "")
                return self._strip_thinking(str(content))
        raise KeyError("OpenAI-compatible response missing choices[0].message.content")
