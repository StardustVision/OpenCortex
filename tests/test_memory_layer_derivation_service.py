# SPDX-License-Identifier: Apache-2.0
"""Tests for pure memory layer derivation service boundaries."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opencortex.services.memory_layer_derivation_service import (
    MemoryLayerDerivationService,
)


class TestMemoryLayerDerivationService(unittest.IsolatedAsyncioTestCase):
    """Verify pure derive behavior without deferred persistence side effects."""

    async def test_no_llm_fallback_derives_from_content(self) -> None:
        """No-LLM path derives deterministic overview and abstract from content."""
        service = MemoryLayerDerivationService(SimpleNamespace(_llm_completion=None))

        result = await service._derive_layers(
            user_abstract="",
            content="Alpha fact. Beta detail.",
        )

        self.assertEqual(result["abstract"], "Alpha fact.")
        self.assertEqual(result["overview"], "Alpha fact. Beta detail.")
        self.assertEqual(result["keywords"], "")
        self.assertEqual(result["entities"], [])
        self.assertEqual(result["anchor_handles"], [])
        self.assertEqual(result["fact_points"], [])

    async def test_orchestrator_llm_completion_override_is_honored(self) -> None:
        """Direct orchestrator override still wins over configured LLM completion."""

        async def override(prompt: str) -> str:
            return f"override:{prompt}"

        orch = SimpleNamespace(
            _llm_completion=AsyncMock(side_effect=AssertionError("should not call"))
        )
        orch._derive_layers_llm_completion = override
        service = MemoryLayerDerivationService(orch)

        result = await service._derive_layers_llm_completion("prompt")

        self.assertEqual(result, "override:prompt")
        orch._llm_completion.assert_not_awaited()

    async def test_split_field_payload_shape_is_preserved(self) -> None:
        """Split-field derive keeps keyword string and list field normalization."""

        async def override(_: str) -> str:
            return (
                '{"abstract":"A","overview":"Overview text.",'
                '"keywords":["k1","k2"],'
                '"entities":["Entity One","ENTITY TWO"],'
                '"anchor_handles":["anchor"],'
                '"fact_points":["fact"]}'
            )

        orch = SimpleNamespace(_llm_completion=object())
        orch._derive_layers_llm_completion = override
        service = MemoryLayerDerivationService(orch)

        result = await service._derive_layers(
            user_abstract="",
            content="Content text.",
        )

        self.assertEqual(result["abstract"], "A")
        self.assertEqual(result["overview"], "Overview text.")
        self.assertEqual(result["keywords"], "k1, k2")
        self.assertEqual(result["entities"], ["entity one", "entity two"])
        self.assertEqual(result["anchor_handles"], ["anchor"])
        self.assertEqual(result["fact_points"], ["fact"])
