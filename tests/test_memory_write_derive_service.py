# SPDX-License-Identifier: Apache-2.0
"""Tests for write-path derive coordination."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from opencortex.services.memory_write_derive_service import MemoryWriteDeriveService


class TestMemoryWriteDeriveService(unittest.IsolatedAsyncioTestCase):
    """Verify derive/fallback behavior extracted from MemoryWriteService.add."""

    def _build_service(self) -> tuple[MemoryWriteDeriveService, SimpleNamespace]:
        write_service = SimpleNamespace(
            _derive_layers=AsyncMock(
                return_value={
                    "abstract": "derived abstract",
                    "overview": "derived overview",
                    "keywords": "alpha, beta",
                    "fact_points": ["derived fact"],
                }
            ),
            _fallback_overview_from_content=MagicMock(return_value="fallback overview"),
            _derive_abstract_from_overview=MagicMock(return_value="fallback abstract"),
        )
        return MemoryWriteDeriveService(write_service), write_service

    async def test_content_leaf_derives_missing_abstract_and_overview(self) -> None:
        """Non-deferred content leaf writes call _derive_layers and fill blanks."""
        service, orch = self._build_service()

        result = await service.derive_for_write(
            abstract="",
            overview="",
            content="full content",
            is_leaf=True,
            defer_derive=False,
        )

        orch._derive_layers.assert_awaited_once_with(
            user_abstract="",
            content="full content",
            user_overview="",
        )
        self.assertEqual(result.abstract, "derived abstract")
        self.assertEqual(result.overview, "derived overview")
        self.assertEqual(result.layers["keywords"], "alpha, beta")
        self.assertGreaterEqual(result.derive_layers_ms, 0)
        orch._fallback_overview_from_content.assert_not_called()
        orch._derive_abstract_from_overview.assert_not_called()

    async def test_content_leaf_preserves_user_abstract_and_overview(self) -> None:
        """User-supplied abstract and overview win over derived values."""
        service, _orch = self._build_service()

        result = await service.derive_for_write(
            abstract="user abstract",
            overview="user overview",
            content="full content",
            is_leaf=True,
            defer_derive=False,
        )

        self.assertEqual(result.abstract, "user abstract")
        self.assertEqual(result.overview, "user overview")
        self.assertEqual(result.layers["abstract"], "derived abstract")

    async def test_deferred_content_leaf_uses_fallbacks_without_layers(self) -> None:
        """Deferred derive fills deterministic placeholders and skips LLM derive."""
        service, orch = self._build_service()

        result = await service.derive_for_write(
            abstract="",
            overview="",
            content="full content",
            is_leaf=True,
            defer_derive=True,
        )

        orch._derive_layers.assert_not_awaited()
        orch._fallback_overview_from_content.assert_called_once_with(
            user_overview="",
            content="full content",
        )
        orch._derive_abstract_from_overview.assert_called_once_with(
            user_abstract="",
            overview="fallback overview",
            content="full content",
        )
        self.assertEqual(result.abstract, "fallback abstract")
        self.assertEqual(result.overview, "fallback overview")
        self.assertEqual(result.layers, {})
        self.assertEqual(result.derive_layers_ms, 0)

    async def test_deferred_content_leaf_preserves_user_values(self) -> None:
        """Deferred derive does not overwrite supplied abstract or overview."""
        service, orch = self._build_service()

        result = await service.derive_for_write(
            abstract="user abstract",
            overview="user overview",
            content="full content",
            is_leaf=True,
            defer_derive=True,
        )

        orch._derive_layers.assert_not_awaited()
        orch._fallback_overview_from_content.assert_not_called()
        orch._derive_abstract_from_overview.assert_not_called()
        self.assertEqual(result.abstract, "user abstract")
        self.assertEqual(result.overview, "user overview")

    async def test_non_leaf_or_empty_content_skips_derive(self) -> None:
        """Non-leaf and empty-content writes keep original fields."""
        service, orch = self._build_service()

        result = await service.derive_for_write(
            abstract="original abstract",
            overview="original overview",
            content="full content",
            is_leaf=False,
            defer_derive=False,
        )
        empty_result = await service.derive_for_write(
            abstract="empty abstract",
            overview="empty overview",
            content="",
            is_leaf=True,
            defer_derive=False,
        )

        orch._derive_layers.assert_not_awaited()
        orch._fallback_overview_from_content.assert_not_called()
        orch._derive_abstract_from_overview.assert_not_called()
        self.assertEqual(result.abstract, "original abstract")
        self.assertEqual(result.overview, "original overview")
        self.assertEqual(result.layers, {})
        self.assertEqual(empty_result.abstract, "empty abstract")
        self.assertEqual(empty_result.overview, "empty overview")
