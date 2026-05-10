# SPDX-License-Identifier: Apache-2.0
"""Required LLM completion client for opencortex write derivation."""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, model_validator

from opencortex.prompts.write import LAYER_DERIVATION_SYSTEM_PROMPT


class LLMConfig(BaseModel):
    """Configuration for the required write-path LLM."""

    api_key: str = ""
    api_base: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"
    api_style: str = "openai"
    system_prompt: str = LAYER_DERIVATION_SYSTEM_PROMPT
    timeout_seconds: float = 60.0
    max_tokens: int = 4096

    @model_validator(mode="after")
    def validate_required_llm(self) -> "LLMConfig":
        """Require complete LLM configuration."""
        if not self.api_key.strip():
            raise ValueError("LLM api key is required")
        if not self.api_base.strip():
            raise ValueError("LLM api base is required")
        if not self.model.strip():
            raise ValueError("LLM model is required")
        if self.api_style not in {"openai", "anthropic", "auto"}:
            raise ValueError("LLM api style must be openai, anthropic, or auto")
        if not self.system_prompt.strip():
            raise ValueError("LLM system prompt is required")
        return self


class LLMCompletion:
    """Small OpenAI/Anthropic-compatible async LLM client."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.api_style = resolve_api_style(config.api_style, config.api_base)
        self.client = httpx.AsyncClient(timeout=config.timeout_seconds)

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.0,
        system_prompt: str | None = None,
    ) -> str:
        """Complete one prompt using the configured LLM."""
        url = self.request_url()
        payload = self.payload(
            prompt,
            temperature=temperature,
            system_prompt=system_prompt,
        )
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        for attempt in range(1, 4):
            try:
                response = await self.client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                return self.extract_text(response.json())
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= 3:
                    raise
                await asyncio.sleep(2**attempt)
            except httpx.HTTPStatusError as exc:
                if attempt >= 3 or exc.response.status_code not in (429, 500, 502, 503):
                    body = exc.response.text[:500]
                    raise RuntimeError(
                        f"LLM HTTP {exc.response.status_code}: {body}"
                    ) from exc
                await asyncio.sleep(2**attempt)
        return ""

    async def __call__(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
    ) -> str:
        """Compatibility callable."""
        return await self.complete(prompt, system_prompt=system_prompt)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self.client.aclose()

    def request_url(self) -> str:
        """Return the API endpoint URL."""
        if self.api_style == "anthropic":
            return f"{self.config.api_base.rstrip('/')}/messages"
        return f"{self.config.api_base.rstrip('/')}/chat/completions"

    def payload(
        self,
        prompt: str,
        *,
        temperature: float,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Build provider-specific request payload."""
        resolved_system_prompt = system_prompt or self.config.system_prompt
        if self.api_style == "anthropic":
            return {
                "model": self.config.model,
                "system": resolved_system_prompt,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": self.config.max_tokens,
                "temperature": temperature,
            }
        return {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": resolved_system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": temperature,
        }

    def extract_text(self, data: dict[str, Any]) -> str:
        """Extract assistant text from provider response JSON."""
        if self.api_style == "anthropic":
            content = data.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return strip_thinking(str(block.get("text", "")))
            raise KeyError("Anthropic response missing content text")

        choices = data.get("choices", [])
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message", {})
            if isinstance(message, dict):
                text = (
                    message.get("content", "")
                    or message.get("reasoning_content", "")
                    or message.get("reasoning", "")
                )
                return strip_thinking(str(text))
        raise KeyError("OpenAI response missing choices[0].message.content")


def resolve_api_style(api_style: str, api_base: str) -> str:
    """Resolve auto API style from the base URL."""
    style = api_style.strip().lower()
    if style in {"openai", "anthropic"}:
        return style
    host = urlparse(api_base).netloc.lower()
    return "anthropic" if "anthropic" in host else "openai"


def strip_thinking(text: str) -> str:
    """Remove reasoning tags from model output when present."""
    stripped = re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)
    if stripped.strip():
        return stripped.strip()
    if "<think>" in text and "</think>" in text:
        return text.split("</think>", maxsplit=1)[-1].strip()
    return text.strip()


def llm_api_key_from_env() -> str:
    """Return API key from supported environment variables."""
    return os.environ.get("OPENCORTEX_APP_LLM_API_KEY", "") or os.environ.get(
        "OPENAI_API_KEY",
        "",
    )
