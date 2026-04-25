# SPDX-License-Identifier: Apache-2.0
"""Tests for ``MemoryService`` (Phase 1 of plan 010).

Per-unit additions:
- U1 (this file's initial state): smoke tests on construction.
- U2: CRUD method tests (add/update/remove/batch_add) with stub orchestrator.
- U3: query method tests (search/list_memories/memory_index/list_memories_admin).
- U4: scoring method tests (feedback/feedback_batch/decay/...).

Integration coverage of the moved methods continues to live in the
existing test suites (``tests/test_e2e_phase1.py``,
``tests/test_write_dedup.py``, ``tests/test_http_server.py``,
``tests/test_ingestion_e2e.py``) — those exercise the methods through
the orchestrator's public surface, which is preserved by Phase 1's
delegate pattern. This file adds **boundary** tests for the new
service module: that the class exists, can be constructed, and that
each method correctly forwards to / interacts with the orchestrator's
subsystems.

Note for ``__new__``-bypass test fixtures elsewhere in the suite: if
a future test constructs ``MemoryOrchestrator`` via
``MemoryOrchestrator.__new__(MemoryOrchestrator)`` and then calls a
delegated method (``add``, ``search``, ``feedback``, ...), it must
also set ``orch._memory_service = MemoryService(orch)`` to avoid an
``AttributeError``. The eager-init pattern in ``__init__`` covers all
normal construction paths.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from opencortex.services.memory_service import MemoryService


class TestMemoryServiceConstruction(unittest.TestCase):
    """U1 smoke tests — class can be constructed safely."""

    def test_construct_with_mock_orchestrator(self) -> None:
        """MemoryService(orch) succeeds and stores the back-reference."""
        mock_orch = MagicMock()
        service = MemoryService(mock_orch)
        self.assertIs(service._orch, mock_orch)

    def test_construct_with_none_orchestrator_does_not_validate(self) -> None:
        """Constructor stores the back-reference verbatim — no None guard.

        If we ever want stricter validation, that's a separate
        decision (see plan 010 / Key Technical Decisions).
        """
        service = MemoryService(None)  # type: ignore[arg-type]
        self.assertIsNone(service._orch)


if __name__ == "__main__":
    unittest.main()
