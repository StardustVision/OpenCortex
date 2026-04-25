# SPDX-License-Identifier: Apache-2.0
"""
LLM completion callable factory for OpenCortex.

Produces an `LLMCompletion` instance — a small wrapper class exposing
`async __call__(prompt) -> str` (the existing call shape used by
IntentAnalyzer + LLM-mode rerank) AND `async aclose()` so the
orchestrator can release the underlying httpx connection pool on
shutdown.

PRE-PR #15 the closures returned here owned their httpx clients with
no aclose path. Production servers accumulated CLOSE_WAIT TCP sockets
until the asyncio event loop blocked (project memory:
`project_connection_pool_leak.md`). The wrapper class fixes that
without changing any call site — `await self._llm_completion(prompt)`
still works because `LLMCompletion.__call__` is `async def`.

Supports two API formats:
- "openai" (default): OpenAI-compatible chat completions via /chat/completions
- "anthropic": Anthropic Messages API via /messages

All library imports are lazy so the module can be imported without any
optional dependency installed.
"""

import logging
import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from opencortex.observability.pool_stats import (
    HTTPX_MAX_CONNECTIONS,
    HTTPX_MAX_KEEPALIVE_CONNECTIONS,
)

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class LLMCompletion:
    """Async-callable wrapper around an httpx-backed LLM completion fn.

    Preserves the legacy callable contract (``await llm(prompt)``) while
    adding a lifecycle hook so ``MemoryOrchestrator.close()`` can release
    the underlying connection pool. Also exposes ``client`` so the admin
    health endpoint can read live pool stats.
    """

    def __init__(
        self,
        callable_: Callable[..., Awaitable[str]],
        client: "httpx.AsyncClient",
        *,
        backend: str,
        model: str,
        base_url: str,
    ) -> None:
        self._callable = callable_
        self._client = client
        self._backend = backend
        self._model = model
        self._base_url = base_url
        self._closed = False

    async def __call__(self, *args: Any, **kwargs: Any) -> str:
        """Delegate to the wrapped completion callable.

        Accepts ``*args, **kwargs`` so existing call sites that pass
        ``(prompt, max_tokens=N)`` keep working — the inner closure
        decides which kwargs it actually uses.
        """
        return await self._callable(*args, **kwargs)

    async def aclose(self) -> None:
        """Release the underlying httpx connection pool. Idempotent.

        REVIEW closure tracker adv-002 (plan 009 review): ``_closed``
        is set ONLY AFTER a successful ``aclose()`` so a transient
        failure (event loop closing, in-flight request, transport
        gone) doesn't permanently mark the wrapper "closed" and
        silently leak the pool when retried.
        """
        if self._closed:
            return
        try:
            await self._client.aclose()
        except Exception as exc:
            # Non-fatal — orchestrator close path tolerates per-client
            # failure (matches existing close pattern in
            # ``orchestrator.close()``). Log so an operator can still
            # see degraded shutdown. Crucially, do NOT set
            # ``_closed=True`` here: a retry must be allowed to
            # actually close the underlying client.
            logger.warning(
                "[llm_factory] LLMCompletion.aclose for backend=%s "
                "failed: %s (degraded shutdown — retry-safe; the "
                "wrapper is NOT marked closed so a subsequent aclose "
                "can re-attempt)",
                self._backend, exc,
            )
            return
        self._closed = True

    @property
    def client(self) -> "httpx.AsyncClient":
        """Underlying httpx.AsyncClient for read-only stat extraction."""
        return self._client

    @property
    def backend(self) -> str:
        return self._backend


def create_llm_completion(config) -> Optional[LLMCompletion]:
    """Create an LLM completion wrapper from CortexConfig.

    Returns:
        An ``LLMCompletion`` instance (callable + ``aclose``), or
        ``None`` when no backend is configured.
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
        wrapper = _make_anthropic_completion(
            api_key=openai_api_key, model=model, base_url=base_url,
        )
        logger.info(
            "[llm_factory] Using Anthropic Messages backend (model=%s, base=%s)",
            model, base_url,
        )
    else:
        wrapper = _make_openai_completion(
            api_key=openai_api_key, model=model, base_url=base_url,
        )
        logger.info(
            "[llm_factory] Using OpenAI-compatible backend (model=%s, base=%s)",
            model, base_url,
        )

    return wrapper


def _build_httpx_client(timeout: float) -> Any:
    """Construct an ``httpx.AsyncClient`` with the project's standard caps."""
    import httpx

    return httpx.AsyncClient(
        timeout=timeout,
        limits=httpx.Limits(
            max_connections=HTTPX_MAX_CONNECTIONS,
            max_keepalive_connections=HTTPX_MAX_KEEPALIVE_CONNECTIONS,
        ),
    )


def _make_openai_completion(api_key: str, model: str, base_url: str) -> LLMCompletion:
    """Build the OpenAI-compatible completion wrapper."""
    import httpx

    client = _build_httpx_client(timeout=60.0)
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async def _openai_completion(prompt: str) -> str:
        try:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
            }
            resp = await client.post(url, headers=headers, json=payload)
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

    return LLMCompletion(
        _openai_completion,
        client,
        backend="openai",
        model=model,
        base_url=base_url,
    )


def _make_anthropic_completion(api_key: str, model: str, base_url: str) -> LLMCompletion:
    """Build the Anthropic Messages completion wrapper."""
    import httpx

    client = _build_httpx_client(timeout=60.0)
    url = base_url.rstrip("/") + "/messages"
    headers = {
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
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
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

    return LLMCompletion(
        _anthropic_completion,
        client,
        backend="anthropic",
        model=model,
        base_url=base_url,
    )
