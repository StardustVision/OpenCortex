# SPDX-License-Identifier: Apache-2.0
"""
LLM completion callable factory for OpenCortex.

Produces an `async def(prompt: str) -> str` callable for use with IntentAnalyzer.

Supports two API formats:
- "openai" (default): OpenAI-compatible chat completions via /chat/completions
- "anthropic": Anthropic Messages API via /messages

All library imports are lazy so the module can be imported without any
optional dependency installed.
"""

import logging
import os
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def create_llm_completion(config) -> Optional[Callable[[str], Awaitable[str]]]:
    """Create an LLM completion callable from CortexConfig.

    Args:
        config: CortexConfig instance.

    Returns:
        An async callable ``async def(prompt: str) -> str``, or None if no
        backend is available.
    """
    effective_api_key = (
        config.llm_api_key
        or os.environ.get("OPENCORTEX_LLM_API_KEY", "")
        or config.embedding_api_key
        or ""
    ).strip()
    effective_model = (config.llm_model or "").strip()
    effective_base = (config.llm_api_base or "").strip()
    effective_format = (
        getattr(config, "llm_api_format", "")
        or os.environ.get("OPENCORTEX_LLM_API_FORMAT", "")
        or "openai"
    ).strip().lower()

    openai_api_key = (
        effective_api_key
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if not openai_api_key:
        logger.debug(
            "[llm_factory] No LLM backend available; IntentAnalyzer will be disabled"
        )
        return None

    try:
        import httpx  # noqa: F401
    except ImportError:
        logger.debug("[llm_factory] httpx not installed; skipping LLM backend")
        return None

    model = effective_model or _DEFAULT_OPENAI_MODEL
    base_url = effective_base or _DEFAULT_OPENAI_BASE_URL

    if effective_format == "anthropic":
        callable_ = _make_anthropic_callable(
            api_key=openai_api_key, model=model, base_url=base_url,
        )
        logger.info(
            "[llm_factory] Using Anthropic Messages backend (model=%s, base=%s)",
            model, base_url,
        )
    else:
        callable_ = _make_openai_callable(
            api_key=openai_api_key, model=model, base_url=base_url,
        )
        logger.info(
            "[llm_factory] Using OpenAI-compatible backend (model=%s, base=%s)",
            model, base_url,
        )

    return callable_


def _make_openai_callable(api_key: str, model: str, base_url: str) -> Callable[[str], Awaitable[str]]:
    """Return an async callable that calls an OpenAI-compatible chat endpoint."""
    import httpx

    _client = httpx.AsyncClient(timeout=60.0)
    _url = base_url.rstrip("/") + "/chat/completions"
    _headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async def _openai_completion(prompt: str) -> str:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = await _client.post(_url, headers=_headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "[llm_factory] OpenAI completion HTTP %s: %s | body: %s",
                exc.response.status_code, exc, exc.response.text[:500],
            )
            raise
        except Exception as exc:
            logger.warning("[llm_factory] OpenAI completion error: %r", exc)
            raise

    return _openai_completion


def _make_anthropic_callable(api_key: str, model: str, base_url: str) -> Callable[[str], Awaitable[str]]:
    """Return an async callable that calls an Anthropic Messages endpoint."""
    import httpx

    _client = httpx.AsyncClient(timeout=60.0)
    _url = base_url.rstrip("/") + "/messages"
    _headers = {
        "Authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    async def _anthropic_completion(prompt: str) -> str:
        try:
            payload = {
                "model": model,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = await _client.post(_url, headers=_headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            # Anthropic returns content as array of blocks
            content_blocks = data.get("content", [])
            texts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
            return "".join(texts)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "[llm_factory] Anthropic completion HTTP %s: %s | body: %s",
                exc.response.status_code, exc, exc.response.text[:500],
            )
            raise
        except Exception as exc:
            logger.warning("[llm_factory] Anthropic completion error: %r", exc)
            raise

    return _anthropic_completion
