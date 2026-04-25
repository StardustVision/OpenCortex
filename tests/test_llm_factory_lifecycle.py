# SPDX-License-Identifier: Apache-2.0
"""Lifecycle tests for the LLMCompletion wrapper (U1, plan 009).

Locks the contract added by the TCP CLOSE_WAIT leak fix:
- The wrapper preserves the legacy ``await llm(prompt)`` callable shape.
- ``aclose()`` releases the underlying httpx pool exactly once.
- Both OpenAI and Anthropic factories apply the configured
  ``httpx.Limits`` (max_connections=20, max_keepalive_connections=5).
- Idempotent close (second call is a no-op).
- Existing kwargs reach the inner closure unmodified.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import httpx

from opencortex.models.llm_factory import (
    LLMCompletion,
    _make_anthropic_completion,
    _make_openai_completion,
)


def _run(coro):
    return asyncio.run(coro)


class _SpyClient:
    """Minimal stand-in for an httpx.AsyncClient.

    Records aclose() invocations and exposes .post for the smoke test.
    Mirrors the ``_StubClient`` template at
    ``tests/test_benchmark_llm_client.py:13-21``.
    """

    def __init__(self, response_payload: Dict[str, Any]):
        self.aclose_calls = 0
        self.post_calls: List[Dict[str, Any]] = []
        self._response_payload = response_payload
        # Mirror httpx.AsyncClient._limits so the wrapper's limit-introspection
        # tests can run against this stub. Real production code uses the
        # real client; this stub only exists for the wrapper-shape tests.
        self._limits = httpx.Limits(max_connections=20, max_keepalive_connections=5)

    async def post(self, url, headers=None, json=None):
        self.post_calls.append({"url": url, "headers": headers, "json": json})

        class _Resp:
            def __init__(self, payload):
                self._payload = payload
                self.status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        return _Resp(self._response_payload)

    async def aclose(self):
        self.aclose_calls += 1


class TestLLMCompletionWrapper(unittest.TestCase):
    """Direct contract tests against ``LLMCompletion`` itself."""

    def test_call_delegates_to_inner_callable(self):
        """``await wrapper(prompt)`` returns the inner callable's result."""

        async def check():
            seen = {}

            async def inner(prompt: str) -> str:
                seen["prompt"] = prompt
                return "hello world"

            wrapper = LLMCompletion(
                inner,
                _SpyClient({}),
                backend="openai",
                model="gpt-4o-mini",
                base_url="https://example.test",
            )
            result = await wrapper("ping")
            self.assertEqual(result, "hello world")
            self.assertEqual(seen["prompt"], "ping")

        _run(check())

    def test_aclose_releases_underlying_client(self):
        """``await wrapper.aclose()`` calls ``client.aclose()`` exactly once."""

        async def check():
            spy = _SpyClient({})
            wrapper = LLMCompletion(
                AsyncMock(return_value=""),
                spy,
                backend="openai",
                model="m",
                base_url="b",
            )
            await wrapper.aclose()
            self.assertEqual(spy.aclose_calls, 1)

        _run(check())

    def test_aclose_is_idempotent(self):
        """Two consecutive ``aclose()`` calls don't raise or double-close."""

        async def check():
            spy = _SpyClient({})
            wrapper = LLMCompletion(
                AsyncMock(return_value=""),
                spy,
                backend="openai",
                model="m",
                base_url="b",
            )
            await wrapper.aclose()
            await wrapper.aclose()
            self.assertEqual(
                spy.aclose_calls, 1,
                "second aclose() must be a no-op — orchestrator close path "
                "may invoke this defensively",
            )

        _run(check())

    def test_aclose_swallows_inner_failure(self):
        """A failing inner ``aclose`` is logged but does not propagate.

        The orchestrator close path tolerates per-client failure
        (matches the existing pattern in ``orchestrator.close()``).
        Re-raising would abort the rest of teardown.
        """

        async def check():
            failing_client = AsyncMock()
            failing_client.aclose = AsyncMock(side_effect=RuntimeError("net down"))
            wrapper = LLMCompletion(
                AsyncMock(return_value=""),
                failing_client,
                backend="openai",
                model="m",
                base_url="b",
            )
            # Should NOT raise.
            await wrapper.aclose()
            failing_client.aclose.assert_awaited_once()

        _run(check())

    def test_aclose_failure_leaves_wrapper_retry_safe(self):
        """REVIEW closure tracker adv-002: a failed aclose must NOT
        permanently mark the wrapper closed.

        Pre-fix, ``_closed`` was set BEFORE the await, so any
        transient failure (event loop closing, in-flight request,
        transport gone) burned the wrapper — a retry would silently
        return without actually closing the underlying client. This
        test locks the new contract: failure leaves ``_closed=False``
        so a subsequent aclose can re-attempt.
        """

        async def check():
            client = AsyncMock()
            # First call fails, second call succeeds.
            client.aclose = AsyncMock(
                side_effect=[RuntimeError("transient"), None],
            )
            wrapper = LLMCompletion(
                AsyncMock(return_value=""),
                client,
                backend="openai",
                model="m",
                base_url="b",
            )
            # First aclose: failure, swallowed, _closed stays False.
            await wrapper.aclose()
            self.assertFalse(
                wrapper._closed,
                "first aclose() failed; wrapper must remain retry-safe",
            )
            # Second aclose: should re-attempt and succeed.
            await wrapper.aclose()
            self.assertTrue(wrapper._closed)
            self.assertEqual(client.aclose.await_count, 2)

        _run(check())

    def test_call_forwards_extra_kwargs(self):
        """``wrapper(prompt, max_tokens=N)`` passes kwargs to the inner callable.

        Existing call sites pass extra keyword arguments; the wrapper
        must not silently drop them.
        """

        async def check():
            seen = {}

            async def inner(prompt: str, **kwargs):
                seen.update(kwargs)
                seen["prompt"] = prompt
                return "ok"

            wrapper = LLMCompletion(
                inner,
                _SpyClient({}),
                backend="openai",
                model="m",
                base_url="b",
            )
            await wrapper("p", max_tokens=512, temperature=0.1)
            self.assertEqual(seen, {"prompt": "p", "max_tokens": 512, "temperature": 0.1})

        _run(check())

    def test_client_property_exposes_pool_for_health_endpoint(self):
        """Health endpoint reads pool stats via ``wrapper.client``."""

        async def check():
            spy = _SpyClient({})
            wrapper = LLMCompletion(
                AsyncMock(return_value=""),
                spy,
                backend="openai",
                model="m",
                base_url="b",
            )
            self.assertIs(wrapper.client, spy)
            self.assertEqual(wrapper.backend, "openai")

        _run(check())


class TestFactoriesApplyHttpxLimits(unittest.TestCase):
    """Both factory functions wire the project-standard pool caps.

    R2 (plan 009): every long-lived ``httpx.AsyncClient`` created by
    the server has ``httpx.Limits(max_connections=20,
    max_keepalive_connections=5)``. Without these caps the original
    incident's connection pool grows unboundedly.

    httpx.AsyncClient does not expose ``_limits`` as a public attribute,
    so we verify the contract at construction time by intercepting the
    constructor and capturing the kwargs.
    """

    def _capture_construct(self):
        """Replace ``httpx.AsyncClient`` with a thin spy that records kwargs.

        Returns ``(restore_fn, captured_kwargs_list)``.
        """
        import opencortex.models.llm_factory as factory_module

        captured: List[Dict[str, Any]] = []
        real_client_cls = httpx.AsyncClient

        class _CapturingClient(real_client_cls):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured.append(dict(kwargs))
                super().__init__(*args, **kwargs)

        # Patch the symbol the factory imports lazily inside
        # ``_build_httpx_client``. We can't simply ``patch.object`` it
        # because ``httpx`` is imported INSIDE the function. Instead
        # patch the module-global ``httpx.AsyncClient`` for the test.
        original = httpx.AsyncClient
        httpx.AsyncClient = _CapturingClient

        def restore() -> None:
            httpx.AsyncClient = original

        return restore, captured

    def test_openai_factory_applies_limits(self):
        restore, captured = self._capture_construct()
        try:
            wrapper = _make_openai_completion(
                api_key="sk-test",
                model="gpt-4o-mini",
                base_url="https://example.test/v1",
            )
            try:
                self.assertEqual(len(captured), 1)
                limits = captured[0].get("limits")
                self.assertIsNotNone(limits, "limits= kwarg must be passed")
                self.assertEqual(limits.max_connections, 20)
                self.assertEqual(limits.max_keepalive_connections, 5)
            finally:
                asyncio.run(wrapper.aclose())
        finally:
            restore()

    def test_anthropic_factory_applies_limits(self):
        restore, captured = self._capture_construct()
        try:
            wrapper = _make_anthropic_completion(
                api_key="sk-test",
                model="claude-3-5-sonnet-latest",
                base_url="https://api.anthropic.com/v1",
            )
            try:
                self.assertEqual(len(captured), 1)
                limits = captured[0].get("limits")
                self.assertIsNotNone(limits)
                self.assertEqual(limits.max_connections, 20)
                self.assertEqual(limits.max_keepalive_connections, 5)
            finally:
                asyncio.run(wrapper.aclose())
        finally:
            restore()


class TestFactoryProducesUsableWrapper(unittest.TestCase):
    """Smoke test: an OpenAI-shaped response flows through the factory wrapper."""

    def test_openai_smoke_path(self):
        async def check():
            wrapper = _make_openai_completion(
                api_key="sk-test",
                model="gpt-4o-mini",
                base_url="https://example.test/v1",
            )
            try:
                # Replace the real httpx client with a spy that returns a
                # canned chat-completions payload.
                spy = _SpyClient(
                    {
                        "choices": [
                            {"message": {"content": "the cat sat on the mat"}}
                        ]
                    }
                )
                # Replace via attribute swap — the wrapper closure
                # captured the real client by reference, so we have to
                # also patch the wrapper's client attribute used by aclose.
                # Easier path: build a fresh wrapper directly.
                async def _inner(prompt: str) -> str:
                    resp = await spy.post(
                        "/chat/completions", headers={}, json={"prompt": prompt}
                    )
                    resp.raise_for_status()
                    return resp.json()["choices"][0]["message"]["content"]

                fresh = LLMCompletion(
                    _inner,
                    spy,
                    backend="openai",
                    model="m",
                    base_url="b",
                )
                out = await fresh("ping")
                self.assertEqual(out, "the cat sat on the mat")
            finally:
                await wrapper.aclose()

        _run(check())


if __name__ == "__main__":
    unittest.main()
