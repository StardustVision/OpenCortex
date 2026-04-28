"""Tests for the event noise reduction pipeline (tool_calls three-way split)."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.alpha.observer import Observer
from opencortex.config import CortexConfig, init_config
from opencortex.context.manager import ContextManager, ConversationBuffer
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.orchestrator import MemoryOrchestrator
from tests.test_e2e_phase1 import MockEmbedder, InMemoryStorage


class TestObserverToolCalls(unittest.TestCase):
    """Observer.record_batch must preserve tool_calls in transcript."""

    def test_record_batch_with_tool_calls(self):
        obs = Observer()
        obs.begin_session("s1", "team", "user")
        messages = [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "Fixed the selection logic."},
        ]
        tool_calls = [
            {"name": "Read", "summary": "Memories.tsx"},
            {"name": "Edit", "summary": "modified useEffect"},
        ]
        obs.record_batch("s1", messages, "team", "user", tool_calls=tool_calls)

        transcript = obs.get_transcript("s1")
        self.assertEqual(len(transcript), 2)
        self.assertNotIn("tool_calls", transcript[0])
        self.assertIn("tool_calls", transcript[1])
        self.assertEqual(len(transcript[1]["tool_calls"]), 2)
        self.assertEqual(transcript[1]["tool_calls"][0]["name"], "Read")

    def test_record_batch_without_tool_calls_backward_compat(self):
        obs = Observer()
        obs.begin_session("s2", "team", "user")
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        obs.record_batch("s2", messages, "team", "user")

        transcript = obs.get_transcript("s2")
        self.assertEqual(len(transcript), 2)
        self.assertNotIn("tool_calls", transcript[0])
        self.assertNotIn("tool_calls", transcript[1])


class TestCommitToolCalls(unittest.TestCase):
    """Test that _commit correctly stores tool_calls in immediate records and buffer."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="noise_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(self.config)
        self._tokens = set_request_identity("testteam", "alice")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        reset_request_identity(self._tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_commit_stores_tool_calls_in_meta(self):
        """Immediate records written by _commit should carry tool_calls in meta."""
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder,
        )
        self._run(orch.init())
        cm = orch._context_manager

        # Prepare first to register session
        self._run(cm.handle(
            session_id="s1", phase="prepare",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[{"role": "user", "content": "fix the bug"}],
        ))

        tool_calls = [
            {"name": "Read", "summary": "Memories.tsx"},
            {"name": "Edit", "summary": "modified useEffect"},
        ]
        result = self._run(cm.handle(
            session_id="s1", phase="commit",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[
                {"role": "user", "content": "fix the bug"},
                {"role": "assistant", "content": "Fixed the selection logic."},
            ],
            tool_calls=tool_calls,
        ))
        self.assertTrue(result["accepted"])

        # Inspect stored records in the "context" collection
        records = self.storage._records.get("context", {})
        has_tool_calls = any(
            r.get("meta", {}).get("tool_calls")
            for r in records.values()
        )
        self.assertTrue(has_tool_calls, "At least one immediate record should have tool_calls in meta")

        # Verify user message records do NOT have tool_calls
        for r in records.values():
            meta = r.get("meta", {})
            if meta.get("layer") == "immediate":
                tc = meta.get("tool_calls", [])
                abstract = r.get("abstract", "")
                if "fix the bug" in abstract:
                    self.assertEqual(tc, [], "User message should not have tool_calls")

        self._run(orch.close())

    def test_commit_without_tool_calls_backward_compat(self):
        """Commit without tool_calls should still work (backward compatibility)."""
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder,
        )
        self._run(orch.init())
        cm = orch._context_manager

        self._run(cm.handle(
            session_id="s2", phase="prepare",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[{"role": "user", "content": "hello"}],
        ))

        result = self._run(cm.handle(
            session_id="s2", phase="commit",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        ))
        self.assertTrue(result["accepted"])
        self.assertEqual(result["write_status"], "ok")

        self._run(orch.close())

    def test_commit_parallel_writes_multiple_messages(self):
        """Multiple messages in a single commit should all be written."""
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder,
        )
        self._run(orch.init())
        cm = orch._context_manager

        self._run(cm.handle(
            session_id="s3", phase="prepare",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[{"role": "user", "content": "question one"}],
        ))

        result = self._run(cm.handle(
            session_id="s3", phase="commit",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[
                {"role": "user", "content": "question one"},
                {"role": "assistant", "content": "answer one"},
            ],
        ))
        self.assertTrue(result["accepted"])

        # Both messages should be in the buffer
        # SessionKey is now (collection, tenant_id, user_id, session_id) per Plan 005
        sk = ("context", "testteam", "alice", "s3")
        buffer = cm._conversation_buffers.get(sk)
        self.assertIsNotNone(buffer)
        self.assertEqual(len(buffer.messages), 2)
        self.assertEqual(len(buffer.immediate_uris), 2)

        self._run(orch.close())

    def test_tool_calls_aggregated_in_buffer(self):
        """tool_calls_per_turn should accumulate across multiple commits."""
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder,
        )
        self._run(orch.init())
        cm = orch._context_manager

        self._run(cm.handle(
            session_id="s4", phase="prepare",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[{"role": "user", "content": "do thing 1"}],
        ))

        tc1 = [{"name": "Read", "summary": "file1.py"}]
        self._run(cm.handle(
            session_id="s4", phase="commit",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[
                {"role": "user", "content": "do thing 1"},
                {"role": "assistant", "content": "done 1"},
            ],
            tool_calls=tc1,
        ))

        tc2 = [{"name": "Edit", "summary": "file2.py"}]
        self._run(cm.handle(
            session_id="s4", phase="commit",
            tenant_id="testteam", user_id="alice", turn_id="t2",
            messages=[
                {"role": "user", "content": "do thing 2"},
                {"role": "assistant", "content": "done 2"},
            ],
            tool_calls=tc2,
        ))

        # SessionKey is now (collection, tenant_id, user_id, session_id) per Plan 005
        sk = ("context", "testteam", "alice", "s4")
        buffer = cm._conversation_buffers.get(sk)
        self.assertIsNotNone(buffer)
        self.assertEqual(len(buffer.tool_calls_per_turn), 2)
        self.assertEqual(buffer.tool_calls_per_turn[0], tc1)
        self.assertEqual(buffer.tool_calls_per_turn[1], tc2)

        self._run(orch.close())


class TestNoiseReductionE2E(unittest.TestCase):
    """Full context commit pipeline with tool_calls storage verification."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="noise_e2e_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(self.config)
        self._tokens = set_request_identity("testteam", "alice")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        reset_request_identity(self._tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    def test_tool_calls_not_in_event_body(self):
        """tool_calls should be in meta only, not polluting event text."""
        orch = MemoryOrchestrator(config=self.config, storage=self.storage, embedder=self.embedder)
        self._run(orch.init())
        cm = orch._context_manager

        self._run(cm.handle(
            session_id="e2e", phase="prepare",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[{"role": "user", "content": "fix the right panel bug"}],
        ))

        self._run(cm.handle(
            session_id="e2e", phase="commit",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[
                {"role": "user", "content": "fix the right panel bug"},
                {"role": "assistant", "content": "Fixed stale content state with cancelled flag."},
            ],
            tool_calls=[
                {"name": "Read", "summary": "web/src/pages/Memories.tsx"},
                {"name": "Edit", "summary": "modified selectedMemory useEffect"},
            ],
        ))

        # Check records in the "context" collection
        records = list(self.storage._records.get("context", {}).values())

        # Event body should NOT contain tool names
        for r in records:
            abstract = r.get("abstract", "")
            self.assertNotIn("[tool-use]", abstract)

        # meta.tool_calls should exist on at least one record
        has_tool_calls = any(r.get("meta", {}).get("tool_calls") for r in records)
        self.assertTrue(has_tool_calls, "Should have tool_calls in meta")

        # tool_calls content check
        tc_records = [r for r in records if r.get("meta", {}).get("tool_calls")]
        tc = tc_records[0]["meta"]["tool_calls"]
        self.assertEqual(tc[0]["name"], "Read")

        self._run(orch.close())

    def test_backward_compat_no_tool_calls(self):
        """Without tool_calls, commit works exactly as before."""
        orch = MemoryOrchestrator(config=self.config, storage=self.storage, embedder=self.embedder)
        self._run(orch.init())
        cm = orch._context_manager

        self._run(cm.handle(
            session_id="bc", phase="prepare",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[{"role": "user", "content": "hello"}],
        ))

        result = self._run(cm.handle(
            session_id="bc", phase="commit",
            tenant_id="testteam", user_id="alice", turn_id="t1",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        ))
        self.assertTrue(result["accepted"])

        self._run(orch.close())


class TestConversationBufferDataclass(unittest.TestCase):
    """Test ConversationBuffer dataclass field defaults."""

    def test_tool_calls_per_turn_default(self):
        """New ConversationBuffer should have empty tool_calls_per_turn."""
        buf = ConversationBuffer()
        self.assertEqual(buf.tool_calls_per_turn, [])
        self.assertIsInstance(buf.tool_calls_per_turn, list)

    def test_independent_instances(self):
        """Each ConversationBuffer instance should have independent lists."""
        a = ConversationBuffer()
        b = ConversationBuffer()
        a.tool_calls_per_turn.append([{"name": "X"}])
        self.assertEqual(len(b.tool_calls_per_turn), 0)
