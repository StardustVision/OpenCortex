# SPDX-License-Identifier: Apache-2.0
"""Shutdown ordering and idempotency tests for MemoryOrchestrator.close().

Locks the contract added by U3 (plan 009): the existing close()
sequence (autophagy tasks -> recall tasks -> derive worker ->
context_manager) is extended with the new pooled-client teardown
steps for ``_llm_completion`` and ``_rerank_client`` between
``context_manager.close()`` and ``immediate_fallback_embedder.close()``.

Tests use ``MemoryOrchestrator.__new__(MemoryOrchestrator)`` to skip
the expensive ``__init__`` + ``init()`` paths (which spin up
fastembed, autophagy sweeper, etc.). The defensive ``getattr``
pattern in ``close()`` is what makes this possible — the same pattern
that guards real partial-construction crashes from production.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import List
from unittest.mock import AsyncMock

from opencortex.orchestrator import MemoryOrchestrator


def _run(coro):
    return asyncio.run(coro)


def _make_bare_orchestrator() -> MemoryOrchestrator:
    """Construct an orchestrator without going through __init__/init.

    Mirrors the pattern at ``tests/test_perf_fixes.py`` (per the
    docstring inside ``MemoryOrchestrator.close()``). Every attribute
    is then set by hand only when the test actually needs it.
    """
    return MemoryOrchestrator.__new__(MemoryOrchestrator)


class TestOrchestratorCloseOrdering(unittest.TestCase):
    """Lock the U3 close-call sequence."""

    def test_close_awaits_llm_completion_then_rerank(self):
        """``close()`` calls ``_llm_completion.aclose`` before ``_rerank_client.aclose``.

        The order matters less than the fact that BOTH get awaited —
        but pinning order makes future regressions visible (someone
        reordering the block silently could leave a dangling close).
        """

        async def check():
            order: List[str] = []

            llm = AsyncMock()
            llm.aclose = AsyncMock(side_effect=lambda: order.append("llm"))

            rerank = AsyncMock()
            rerank.aclose = AsyncMock(side_effect=lambda: order.append("rerank"))

            orch = _make_bare_orchestrator()
            orch._llm_completion = llm
            orch._rerank_client = rerank
            orch._initialized = True
            await orch.close()

            self.assertEqual(order, ["llm", "rerank"])
            llm.aclose.assert_awaited_once()
            rerank.aclose.assert_awaited_once()

        _run(check())

    def test_close_skips_aclose_when_attribute_missing(self):
        """Bare orchestrator with no _llm_completion / _rerank_client doesn't crash."""

        async def check():
            orch = _make_bare_orchestrator()
            orch._initialized = True
            # No attributes set at all — close() must not raise.
            await orch.close()

        _run(check())

    def test_close_handles_callable_llm_without_aclose(self):
        """Legacy ``llm_completion`` may be a bare callable, not LLMCompletion.

        Old tests inject ``llm_completion=async def(prompt): ...`` directly.
        That has no ``aclose`` attribute. ``close()`` must skip it
        rather than raise ``AttributeError``.
        """

        async def check():
            async def bare_callable(prompt):
                return ""

            orch = _make_bare_orchestrator()
            orch._llm_completion = bare_callable
            orch._initialized = True
            # Should NOT raise.
            await orch.close()

        _run(check())

    def test_close_continues_when_one_aclose_fails(self):
        """A failing ``_llm_completion.aclose()`` does not abort rerank close.

        Matches the existing defensive pattern (autophagy task
        cancellation also tolerates per-task failure). Without this
        guarantee a flaky LLM-side close could leak the rerank socket
        on every restart.
        """

        async def check():
            llm = AsyncMock()
            llm.aclose = AsyncMock(side_effect=RuntimeError("net down"))

            rerank = AsyncMock()
            rerank.aclose = AsyncMock()

            orch = _make_bare_orchestrator()
            orch._llm_completion = llm
            orch._rerank_client = rerank
            orch._initialized = True
            await orch.close()

            llm.aclose.assert_awaited_once()
            rerank.aclose.assert_awaited_once()

        _run(check())

    def test_close_is_idempotent(self):
        """``close()`` called twice doesn't crash (nor double-close)."""

        async def check():
            llm = AsyncMock()
            llm.aclose = AsyncMock()

            rerank = AsyncMock()
            rerank.aclose = AsyncMock()

            orch = _make_bare_orchestrator()
            orch._llm_completion = llm
            orch._rerank_client = rerank
            orch._initialized = True
            await orch.close()
            await orch.close()

            # Two awaits each. The wrappers themselves enforce
            # idempotency internally (LLMCompletion has _closed flag,
            # RerankClient nulls _http_client). The orchestrator just
            # invokes them — second invocation is a no-op INSIDE the
            # wrapper, but the orchestrator does call again.
            self.assertEqual(llm.aclose.await_count, 2)
            self.assertEqual(rerank.aclose.await_count, 2)

        _run(check())


if __name__ == "__main__":
    unittest.main()
