# SPDX-License-Identifier: Apache-2.0
"""Tests for recomposition input and record assembly."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

from opencortex.context.manager import ConversationBuffer
from opencortex.context.recomposition_input import RecompositionInputService


def _record(
    uri: str,
    msg_range: List[int],
    *,
    overview: str = "",
    meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    merged_meta = {"msg_range": msg_range}
    merged_meta.update(meta or {})
    return {
        "uri": uri,
        "overview": overview,
        "meta": merged_meta,
        "keywords": "",
        "entities": [],
    }


class TestRecompositionInputService(unittest.IsolatedAsyncioTestCase):
    """Input service behavior for recomposition records and entries."""

    def _service(self, *, fs: Any = None) -> RecompositionInputService:
        session_records = MagicMock()
        storage = MagicMock()
        storage.filter = AsyncMock(return_value=[])
        orchestrator = MagicMock()
        orchestrator._fs = fs
        orchestrator._storage = storage
        orchestrator._get_collection.return_value = "context"
        manager = SimpleNamespace(
            _session_records=session_records,
            _orchestrator=orchestrator,
            _estimate_tokens=lambda text: len(str(text).split()) or 1,
        )
        return RecompositionInputService(manager)  # type: ignore[arg-type]

    async def test_tail_selection_respects_leaf_and_message_caps(self) -> None:
        """Tail selection keeps the newest records within configured caps."""
        service = self._service()
        records = [
            _record(f"uri-{idx}", [idx * 4, idx * 4 + 3], overview=f"r{idx}")
            for idx in range(10)
        ]
        service._manager._session_records.load_merged = AsyncMock(return_value=records)

        selected = await service.select_tail_merged_records(
            session_id="session",
            source_uri="source",
        )

        self.assertEqual(
            [record["uri"] for record in selected], [f"uri-{i}" for i in range(4, 10)]
        )

    async def test_load_immediate_records_preserves_uri_order(self) -> None:
        """Immediate record loading returns records in requested URI order."""
        service = self._service()
        storage_records = [
            {"uri": "imm-2", "abstract": "second"},
            {"uri": "imm-1", "abstract": "first"},
        ]
        service._manager._orchestrator._storage.filter = AsyncMock(
            return_value=storage_records
        )

        records = await service.load_immediate_records(["imm-1", "imm-2"])

        self.assertEqual([record["uri"] for record in records], ["imm-1", "imm-2"])

    async def test_aggregate_records_metadata_merges_stable_fields(self) -> None:
        """Metadata aggregation preserves stable unique values and first date."""
        service = self._service()

        meta = await service.aggregate_records_metadata(
            [
                {
                    "entities": ["Alice"],
                    "keywords": "billing,renewal",
                    "event_date": "2026-04-28",
                    "meta": {
                        "entities": ["Bob"],
                        "topics": ["customer"],
                        "time_refs": ["2026-04-28"],
                    },
                    "abstract_json": {
                        "slots": {
                            "entities": ["Alice", "Carol"],
                            "topics": "account",
                            "time_refs": ["Tuesday"],
                        }
                    },
                },
                {
                    "entities": ["Dana"],
                    "keywords": "renewal",
                    "event_date": "2026-04-29",
                    "meta": {"time_refs": ["2026-04-29"]},
                },
            ]
        )

        self.assertEqual(meta["entities"], ["Alice", "Bob", "Carol", "Dana"])
        self.assertEqual(meta["time_refs"], ["2026-04-28", "Tuesday", "2026-04-29"])
        self.assertEqual(meta["topics"], ["billing", "renewal", "customer", "account"])
        self.assertEqual(meta["event_date"], "2026-04-28")

    async def test_build_entries_hydrates_tail_l2_and_snapshot_messages(self) -> None:
        """Entry building reads tail L2 content but keeps immediate raw text."""
        fs = MagicMock()
        fs.read_file = AsyncMock(return_value="tail l2 content")
        service = self._service(fs=fs)
        snapshot = ConversationBuffer(
            messages=["raw immediate"],
            token_count=2,
            start_msg_index=10,
            immediate_uris=["imm-1"],
        )
        tail_records = [
            _record(
                "tail-1", [0, 3], overview="tail overview", meta={"entities": ["A"]}
            )
        ]
        immediate_records = [
            {
                "uri": "imm-1",
                "abstract": "ignored immediate abstract",
                "meta": {"msg_index": 10},
                "keywords": "",
                "entities": [],
            }
        ]

        entries = await service.build_recomposition_entries(
            snapshot=snapshot,
            immediate_records=immediate_records,
            tail_records=tail_records,
        )

        self.assertEqual([entry["msg_start"] for entry in entries], [0, 10])
        self.assertEqual(entries[0]["text"], "tail l2 content")
        self.assertEqual(entries[1]["text"], "raw immediate")
        self.assertEqual(entries[0]["superseded_merged_uris"], ["tail-1"])
        self.assertEqual(entries[1]["immediate_uris"], ["imm-1"])


if __name__ == "__main__":
    unittest.main()
