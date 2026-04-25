"""Test conversation merge layer: buffer accumulation + recomposition entries."""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestConversationBuffer(unittest.TestCase):
    def test_buffer_dataclass(self):
        from opencortex.context.manager import ConversationBuffer
        buf = ConversationBuffer()
        self.assertEqual(buf.messages, [])
        self.assertEqual(buf.token_count, 0)
        self.assertEqual(buf.start_msg_index, 0)
        self.assertEqual(buf.immediate_uris, [])

    def test_buffer_accumulates(self):
        from opencortex.context.manager import ConversationBuffer
        buf = ConversationBuffer()
        buf.messages.append("Hello world")
        buf.token_count += 100
        buf.immediate_uris.append("opencortex://test/uri")
        self.assertEqual(len(buf.messages), 1)
        self.assertEqual(buf.token_count, 100)


def _make_context_manager(fs=None):
    """Create a minimal ContextManager with mock orchestrator."""
    from opencortex.context.manager import ContextManager

    orch = MagicMock()
    orch._fs = fs
    observer = MagicMock()
    cm = ContextManager(orch, observer)
    return cm


class TestBuildRecompositionEntries(unittest.TestCase):
    """Test _build_recomposition_entries reads CortexFS L2 for tail records."""

    def test_tail_records_use_cortexfs_l2(self):
        """Tail records should read L2 content from CortexFS, not Qdrant overview."""
        from opencortex.context.manager import ConversationBuffer

        raw_content = "[7:55 pm] [Alice]: Hello from raw content"

        fs = MagicMock()
        fs.read_file = AsyncMock(return_value=raw_content)
        cm = _make_context_manager(fs=fs)

        snapshot = ConversationBuffer(
            messages=["[8:00 pm] [Bob]: New message"],
            token_count=10,
            start_msg_index=10,
            immediate_uris=["opencortex://t/u/memories/events/imm-001"],
        )
        tail_records = [
            {
                "uri": "opencortex://t/u/memories/events/conv-000000-000005",
                "overview": "LLM summary that should not be used",
                "abstract": "LLM abstract",
                "meta": {"msg_range": [0, 5], "entities": ["Alice"]},
                "keywords": "greeting",
            }
        ]
        immediate_records = [
            {
                "uri": "opencortex://t/u/memories/events/imm-001",
                "abstract": "[8:00 pm] [Bob]: New message",
                "meta": {"msg_index": 10},
                "keywords": "",
                "entities": [],
            }
        ]

        entries = asyncio.run(cm._build_recomposition_entries(
            snapshot=snapshot,
            immediate_records=immediate_records,
            tail_records=tail_records,
        ))

        tail_entry = next(e for e in entries if e["msg_start"] == 0)
        self.assertEqual(tail_entry["text"], raw_content)
        fs.read_file.assert_called_once_with(
            "opencortex://t/u/memories/events/conv-000000-000005/content.md"
        )

    def test_tail_records_fallback_on_cortexfs_failure(self):
        """When CortexFS read fails, fall back to Qdrant overview."""
        from opencortex.context.manager import ConversationBuffer

        fs = MagicMock()
        fs.read_file = AsyncMock(side_effect=Exception("CortexFS unavailable"))
        cm = _make_context_manager(fs=fs)

        snapshot = ConversationBuffer(
            messages=[],
            token_count=0,
            start_msg_index=10,
            immediate_uris=[],
        )
        tail_records = [
            {
                "uri": "opencortex://t/u/memories/events/conv-000000-000005",
                "overview": "LLM overview fallback",
                "meta": {"msg_range": [0, 5]},
                "keywords": "",
            }
        ]

        entries = asyncio.run(cm._build_recomposition_entries(
            snapshot=snapshot,
            immediate_records=[],
            tail_records=tail_records,
        ))

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "LLM overview fallback")

    def test_tail_records_fallback_no_fs(self):
        """When no CortexFS instance exists, use Qdrant text."""
        from opencortex.context.manager import ConversationBuffer

        cm = _make_context_manager(fs=None)

        snapshot = ConversationBuffer(
            messages=[],
            token_count=0,
            start_msg_index=10,
            immediate_uris=[],
        )
        tail_records = [
            {
                "uri": "opencortex://t/u/memories/events/conv-000000-000005",
                "overview": "Qdrant overview",
                "meta": {"msg_range": [0, 5]},
                "keywords": "",
            }
        ]

        entries = asyncio.run(cm._build_recomposition_entries(
            snapshot=snapshot,
            immediate_records=[],
            tail_records=tail_records,
        ))

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "Qdrant overview")

    def test_immediate_records_use_snapshot_messages(self):
        """Immediate records should always use snapshot.messages raw text."""
        from opencortex.context.manager import ConversationBuffer

        fs = MagicMock()
        fs.read_file = AsyncMock(return_value="should not affect immediates")
        cm = _make_context_manager(fs=fs)

        raw_msg = "[8:00 pm] [Bob]: Raw immediate message"
        snapshot = ConversationBuffer(
            messages=[raw_msg],
            token_count=10,
            start_msg_index=10,
            immediate_uris=["opencortex://t/u/memories/events/imm-001"],
        )
        immediate_records = [
            {
                "uri": "opencortex://t/u/memories/events/imm-001",
                "abstract": "LLM abstract of immediate",
                "meta": {"msg_index": 10},
                "keywords": "",
                "entities": [],
            }
        ]

        entries = asyncio.run(cm._build_recomposition_entries(
            snapshot=snapshot,
            immediate_records=immediate_records,
            tail_records=[],
        ))

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], raw_msg)


def _make_entry(msg_start, msg_end, anchors=None, time_refs=None, tokens=100,
                 uri="", text="msg"):
    """Helper to build a recomposition entry for segment tests."""
    return {
        "text": text,
        "uri": uri or f"opencortex://t/u/memories/events/e-{msg_start:06d}-{msg_end:06d}",
        "msg_start": msg_start,
        "msg_end": msg_end,
        "token_count": tokens,
        "anchor_terms": set(anchors or []),
        "time_refs": set(time_refs or []),
        "source_record": {"uri": uri, "meta": {"msg_range": [msg_start, msg_end]}},
        "immediate_uris": [],
        "superseded_merged_uris": [uri] if uri else [],
    }


class TestBuildAnchorClusteredSegments(unittest.TestCase):
    """Test _build_anchor_clustered_segments Jaccard clustering."""

    def test_shared_anchors_same_segment(self):
        """Entries sharing anchors cluster together."""
        cm = _make_context_manager()
        entries = [
            _make_entry(0, 5, anchors=["Alice", "Hangzhou"]),
            _make_entry(6, 10, anchors=["Alice", "Hangzhou", "tea"]),
        ]
        segments = cm._build_anchor_clustered_segments(entries)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["msg_range"], [0, 10])

    def test_different_anchors_split(self):
        """Entries with no shared anchors split into separate segments."""
        cm = _make_context_manager()
        entries = [
            _make_entry(0, 5, anchors=["Alice", "Hangzhou"]),
            _make_entry(6, 10, anchors=["Alice", "Hangzhou"]),
            _make_entry(11, 15, anchors=["Bob", "Shanghai"]),
        ]
        segments = cm._build_anchor_clustered_segments(entries)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["msg_range"], [0, 10])
        self.assertEqual(segments[1]["msg_range"], [11, 15])

    def test_empty_anchors_join_previous(self):
        """Entries with empty anchors join the current group."""
        cm = _make_context_manager()
        entries = [
            _make_entry(0, 5, anchors=["Alice"]),
            _make_entry(6, 10, anchors=[]),
            _make_entry(11, 15, anchors=["Bob"]),
        ]
        segments = cm._build_anchor_clustered_segments(entries)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["msg_range"], [0, 10])
        self.assertEqual(segments[1]["msg_range"], [11, 15])

    def test_token_hard_cap_forces_split(self):
        """Even with shared anchors, token cap forces a split."""
        from opencortex.context.manager import _RECOMPOSE_CLUSTER_MAX_TOKENS
        cm = _make_context_manager()
        entries = [
            _make_entry(0, 5, anchors=["Alice"], tokens=_RECOMPOSE_CLUSTER_MAX_TOKENS - 100),
            _make_entry(6, 10, anchors=["Alice"], tokens=200),
        ]
        segments = cm._build_anchor_clustered_segments(entries)
        self.assertEqual(len(segments), 2)

    def test_all_shared_single_segment(self):
        """All entries sharing anchors → one segment."""
        cm = _make_context_manager()
        entries = [
            _make_entry(0, 2, anchors=["X", "Y"]),
            _make_entry(3, 5, anchors=["X", "Y"]),
            _make_entry(6, 8, anchors=["X", "Y"]),
        ]
        segments = cm._build_anchor_clustered_segments(entries)
        self.assertEqual(len(segments), 1)

    def test_empty_entries_returns_empty(self):
        cm = _make_context_manager()
        self.assertEqual(cm._build_anchor_clustered_segments([]), [])

    def test_segment_output_format(self):
        """Output must match _finalize_recomposition_segment format."""
        cm = _make_context_manager()
        entries = [_make_entry(0, 5, anchors=["Alice"], text="hello")]
        segments = cm._build_anchor_clustered_segments(entries)
        seg = segments[0]
        self.assertIn("messages", seg)
        self.assertIn("msg_range", seg)
        self.assertIn("immediate_uris", seg)
        self.assertIn("superseded_merged_uris", seg)
        self.assertEqual(seg["messages"], ["hello"])
        self.assertEqual(seg["msg_range"], [0, 5])


class TestDeleteImmediateFamiliesCortexFS(unittest.TestCase):
    """Test _purge_records_and_fs_subtree also cleans CortexFS directories."""

    def test_deletes_both_qdrant_and_cortexfs(self):
        """Both Qdrant and CortexFS should be cleaned."""
        fs = MagicMock()
        fs.rm = AsyncMock()
        cm = _make_context_manager(fs=fs)
        cm._orchestrator._storage.remove_by_uri = AsyncMock()
        cm._orchestrator._get_collection.return_value = "context"

        asyncio.run(cm._purge_records_and_fs_subtree([
            "opencortex://t/u/memories/events/imm-001",
        ]))

        cm._orchestrator._storage.remove_by_uri.assert_called_once_with(
            "context", "opencortex://t/u/memories/events/imm-001",
        )
        fs.rm.assert_called_once_with(
            "opencortex://t/u/memories/events/imm-001", recursive=True,
        )

    def test_cortexfs_failure_does_not_block(self):
        """CortexFS rm failure should not prevent Qdrant cleanup."""
        fs = MagicMock()
        fs.rm = AsyncMock(side_effect=Exception("disk error"))
        cm = _make_context_manager(fs=fs)
        cm._orchestrator._storage.remove_by_uri = AsyncMock()
        cm._orchestrator._get_collection.return_value = "context"

        asyncio.run(cm._purge_records_and_fs_subtree([
            "opencortex://t/u/memories/events/imm-001",
            "opencortex://t/u/memories/events/imm-002",
        ]))

        self.assertEqual(cm._orchestrator._storage.remove_by_uri.call_count, 2)
        self.assertEqual(fs.rm.call_count, 2)

    def test_no_fs_graceful_skip(self):
        """Without CortexFS, only Qdrant should be cleaned."""
        cm = _make_context_manager(fs=None)
        cm._orchestrator._storage.remove_by_uri = AsyncMock()
        cm._orchestrator._get_collection.return_value = "context"

        asyncio.run(cm._purge_records_and_fs_subtree([
            "opencortex://t/u/memories/events/imm-001",
        ]))

        cm._orchestrator._storage.remove_by_uri.assert_called_once()


if __name__ == "__main__":
    unittest.main()
