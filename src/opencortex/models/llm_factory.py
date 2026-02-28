# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""
LLM completion callable factory for OpenCortex.

Produces an `async def(prompt: str) -> str` callable for use with IntentAnalyzer.

Backend selection priority:
1. Volcengine Ark SDK  — if volcenginesdkarkruntime is installed AND the
   effective base URL contains "volces.com".
2. OpenAI-compatible   — if an API key is available from config (llm_api_key /
   embedding_api_key) or the OPENAI_API_KEY env var.
3. None                — if no backend is available (IntentAnalyzer won't run).

All SDK/library imports are lazy so the module can be imported without any
optional dependency installed.
"""

import logging
import os
from typing import Callable, Awaitable, Optional

logger = logging.getLogger(__name__)

# Default Ark model for intent analysis
_DEFAULT_ARK_MODEL = "doubao-seed-1-8-251228"
# Default Ark base URL
_DEFAULT_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
# Default OpenAI chat completions endpoint
_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


def create_llm_completion(config) -> Optional[Callable[[str], Awaitable[str]]]:
    """Create an LLM completion callable from CortexConfig.

    Selects the best available backend in priority order:
      1. Volcengine Ark SDK (if installed and base URL is volces.com)
      2. OpenAI-compatible via httpx (config key or OPENAI_API_KEY env var)
      3. None (no backend available)

    Args:
        config: CortexConfig instance.

    Returns:
        An async callable ``async def(prompt: str) -> str``, or None if no
        backend is available.
    """
    # Resolve the effective API key: prefer llm_api_key, fall back to
    # OPENCORTEX_LLM_API_KEY env var, then embedding_api_key (reuse the
    # Ark key for chat completions).
    effective_api_key = (
        config.llm_api_key
        or os.environ.get("OPENCORTEX_LLM_API_KEY", "")
        or config.embedding_api_key
        or ""
    ).strip()
    effective_model = (config.llm_model or "").strip()
    effective_base = (config.llm_api_base or "").strip()

    # ------------------------------------------------------------------
    # Backend 1: Volcengine Ark SDK
    # Only when the SDK is installed AND the base URL points to Volcengine.
    # ------------------------------------------------------------------
    _is_volcengine = "volces.com" in (effective_base or _DEFAULT_ARK_BASE_URL)
    if effective_api_key and _is_volcengine:
        try:
            import volcenginesdkarkruntime  # noqa: F401 — lazy availability check
            callable_ = _make_ark_callable(
                api_key=effective_api_key,
                model=effective_model or _DEFAULT_ARK_MODEL,
                base_url=effective_base or _DEFAULT_ARK_BASE_URL,
            )
            logger.info(
                "[llm_factory] Using Volcengine Ark SDK backend "
                "(model=%s, base=%s)",
                effective_model or _DEFAULT_ARK_MODEL,
                effective_base or _DEFAULT_ARK_BASE_URL,
            )
            return callable_
        except ImportError:
            logger.debug(
                "[llm_factory] volcenginesdkarkruntime not installed; "
                "skipping Ark backend"
            )

    # ------------------------------------------------------------------
    # Backend 2: OpenAI-compatible via httpx (config key or env var)
    # ------------------------------------------------------------------
    openai_api_key = (
        effective_api_key
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )
    if openai_api_key:
        try:
            import httpx  # noqa: F401 — lazy availability check
            callable_ = _make_openai_callable(
                api_key=openai_api_key,
                model=effective_model or _DEFAULT_OPENAI_MODEL,
                base_url=effective_base or _DEFAULT_OPENAI_BASE_URL,
            )
            logger.info(
                "[llm_factory] Using OpenAI-compatible backend "
                "(model=%s, base=%s)",
                effective_model or _DEFAULT_OPENAI_MODEL,
                effective_base or _DEFAULT_OPENAI_BASE_URL,
            )
            return callable_
        except ImportError:
            logger.debug(
                "[llm_factory] httpx not installed; skipping OpenAI backend"
            )

    # ------------------------------------------------------------------
    # No backend available
    # ------------------------------------------------------------------
    logger.debug(
        "[llm_factory] No LLM backend available; IntentAnalyzer will be disabled"
    )
    return None


def _make_ark_callable(api_key: str, model: str, base_url: str) -> Callable[[str], Awaitable[str]]:
    """Return an async callable backed by the Volcengine Ark SDK."""
    import volcenginesdkarkruntime

    # Create the client once; reuse across all invocations to avoid
    # repeated connection setup and potential resource leaks.
    client = volcenginesdkarkruntime.AsyncArk(
        api_key=api_key,
        base_url=base_url,
    )

    async def _ark_completion(prompt: str) -> str:
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            logger.warning("[llm_factory] Ark completion error: %s", exc)
            raise

    return _ark_completion


def _make_openai_callable(api_key: str, model: str, base_url: str) -> Callable[[str], Awaitable[str]]:
    """Return an async callable that calls an OpenAI-compatible chat endpoint."""
    import httpx

    # Create the client once; reuse across all invocations for connection pooling.
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
        except Exception as exc:
            logger.warning("[llm_factory] OpenAI completion error: %s", exc)
            raise

    return _openai_completion
