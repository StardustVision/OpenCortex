# SPDX-License-Identifier: Apache-2.0
"""Tests for ``MemoryDocumentWriteService`` helper behavior."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import MagicMock

from opencortex.services.memory_document_write_service import MemoryDocumentWriteService


class TestGenerateAbstractOverview(unittest.TestCase):
    """Verify document abstract/overview generation fallback behavior."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_no_llm_returns_file_path_and_truncated_overview(self) -> None:
        """No LLM preserves the legacy deterministic fallback."""
        mock_memory_service = MagicMock()
        mock_memory_service._orch._llm_completion = None
        service = MemoryDocumentWriteService(mock_memory_service)
        content = "x" * 800

        abstract, overview = self._run(
            service._generate_abstract_overview(content, "docs/example.md")
        )

        self.assertEqual(abstract, "docs/example.md")
        self.assertLessEqual(len(overview), 500)
        self.assertTrue(overview)
