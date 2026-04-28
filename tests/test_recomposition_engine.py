# SPDX-License-Identifier: Apache-2.0
"""Tests for ``SessionRecompositionEngine`` (Phase 7 of plan 017)."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from opencortex.context.recomposition_engine import SessionRecompositionEngine
from opencortex.context.recomposition_segmentation import (
    RecompositionSegmentationService,
)


class TestRecompositionEngineConstruction(unittest.TestCase):
    """Construction behavior for the recomposition engine."""

    def test_construct_with_mock_manager(self) -> None:
        """Engine stores manager and creates the segmentation collaborator."""
        mock_mgr = MagicMock()
        engine = SessionRecompositionEngine(mock_mgr)
        self.assertIs(engine._mgr, mock_mgr)
        self.assertIsInstance(
            engine._segmentation,
            RecompositionSegmentationService,
        )

    def test_construct_with_none(self) -> None:
        """Construction remains tolerant of None for legacy tests."""
        engine = SessionRecompositionEngine(None)  # type: ignore[arg-type]
        self.assertIsNone(engine._mgr)


class TestManagerLazyProperty(unittest.TestCase):
    """ContextManager lazy engine property behavior."""

    def test_lazy_property_works(self) -> None:
        """Lazy property creates an engine instance."""
        from opencortex.context.manager import ContextManager

        mgr = ContextManager.__new__(ContextManager)
        engine = mgr._recomposition_engine
        self.assertIsNotNone(engine)
        self.assertIsInstance(engine, SessionRecompositionEngine)

    def test_lazy_property_caches(self) -> None:
        """Lazy property caches the engine instance."""
        from opencortex.context.manager import ContextManager

        mgr = ContextManager.__new__(ContextManager)
        a = mgr._recomposition_engine
        b = mgr._recomposition_engine
        self.assertIs(a, b)

    def test_back_reference(self) -> None:
        """Created engine points back to the owning manager."""
        from opencortex.context.manager import ContextManager

        mgr = ContextManager.__new__(ContextManager)
        engine = mgr._recomposition_engine
        self.assertIs(engine._mgr, mgr)


if __name__ == "__main__":
    unittest.main()
