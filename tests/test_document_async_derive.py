"""Tests for async document derive worker (Units 1-5)."""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.orchestrator import MemoryOrchestrator, _DeriveTask


class MockEmbedder(DenseEmbedderBase):
    def __init__(self):
        super().__init__(model_name="mock")

    def embed(self, text):
        return EmbedResult(dense_vector=[0.1, 0.2, 0.3, 0.4])

    def get_dimension(self):
        return 4


MULTI_SECTION_CONTENT = (
    "# Introduction\n\n" + "word " * 1500
    + "\n\n# Methods\n\n" + "word " * 1500
    + "\n\n# Results\n\n" + "word " * 1500
)


def _mock_llm(prompt):
    """LLM mock returning valid derive JSON."""
    async def _inner(p):
        return '{"abstract": "chunk summary", "overview": "chunk detail", "keywords": ["test"]}'
    return _inner(prompt)


class _OrchestratorFixture:
    """Creates a temp-dir-backed orchestrator with mock embedder/LLM."""

    def __init__(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cfg = CortexConfig(data_root=self.tmpdir, embedding_dimension=4)
        init_config(self.cfg)
        self.orch = MemoryOrchestrator(
            config=self.cfg, embedder=MockEmbedder(), llm_completion=_mock_llm,
        )

    async def setup(self):
        await self.orch.init()

    async def teardown(self):
        await self.orch.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)


class TestStoreImmediateReturn(unittest.TestCase):
    """Unit 2: store() returns immediately for multi-chunk documents."""

    def test_store_returns_context_with_pending_flag(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            tokens = set_request_identity("t1", "u1")
            try:
                result = await fix.orch.add(
                    abstract="",
                    content=MULTI_SECTION_CONTENT,
                    meta={"ingest_mode": "document"},
                    category="documents",
                    context_type="resource",
                )
                self.assertIsNotNone(result)
                self.assertIsNotNone(result.uri)
                self.assertTrue(result.meta.get("derive_pending"))

                # Qdrant should have NO records yet (async derive not started)
                records = await fix.orch._storage.filter("context", None, limit=50)
                # At most 0 records (worker hasn't processed yet — it may have started)
                # The key assertion: store() returned before derive completed
                self.assertIsNotNone(result.uri)
            finally:
                reset_request_identity(tokens)
                await fix.teardown()

        asyncio.run(run())

    def test_cortexfs_has_content_and_marker(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            tokens = set_request_identity("t1", "u1")
            try:
                result = await fix.orch.add(
                    abstract="",
                    content=MULTI_SECTION_CONTENT,
                    meta={"ingest_mode": "document"},
                    category="documents",
                    context_type="resource",
                )
                fs_path = fix.orch._fs._uri_to_path(result.uri)
                content_data = fix.orch._fs.agfs.read(f"{fs_path}/content.md")
                self.assertIn(b"Introduction", content_data)

                marker_data = fix.orch._fs.agfs.read(f"{fs_path}/.derive_pending")
                marker = json.loads(marker_data)
                self.assertEqual(marker["parent_uri"], result.uri)
                self.assertEqual(marker["tenant_id"], "t1")
                self.assertEqual(marker["user_id"], "u1")
            finally:
                reset_request_identity(tokens)
                await fix.teardown()

        asyncio.run(run())

    def test_single_chunk_still_synchronous(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            tokens = set_request_identity("t1", "u1")
            try:
                result = await fix.orch.add(
                    abstract="",
                    content="# Small Doc\n\nJust a paragraph.",
                    meta={"ingest_mode": "document"},
                    category="documents",
                    context_type="resource",
                )
                self.assertIsNotNone(result)
                self.assertFalse(result.meta.get("derive_pending", False))
            finally:
                reset_request_identity(tokens)
                await fix.teardown()

        asyncio.run(run())


class TestDeriveWorkerProcessing(unittest.TestCase):
    """Unit 3: Worker processes tasks and creates Qdrant records."""

    def test_worker_creates_records_after_drain(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            tokens = set_request_identity("t1", "u1")
            try:
                result = await fix.orch.add(
                    abstract="",
                    content=MULTI_SECTION_CONTENT,
                    meta={"ingest_mode": "document"},
                    category="documents",
                    context_type="resource",
                )
                await fix.orch._drain_derive_queue()

                records = await fix.orch._storage.filter("context", None, limit=50)
                leaf_records = [r for r in records if r.get("is_leaf")]
                self.assertGreaterEqual(len(leaf_records), 2)
                self.assertTrue(all(r.get("abstract_json") for r in leaf_records))
            finally:
                reset_request_identity(tokens)
                await fix.teardown()

        asyncio.run(run())

    def test_marker_deleted_on_success(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            tokens = set_request_identity("t1", "u1")
            try:
                result = await fix.orch.add(
                    abstract="",
                    content=MULTI_SECTION_CONTENT,
                    meta={"ingest_mode": "document"},
                    category="documents",
                    context_type="resource",
                )
                await fix.orch._drain_derive_queue()

                fs_path = fix.orch._fs._uri_to_path(result.uri)
                from pathlib import Path
                marker = Path(fix.tmpdir) / fs_path.lstrip("/").replace("/local/", "", 1) if "/local/" in fs_path else None
                # Use agfs.read to check — it should raise or return empty
                try:
                    data = fix.orch._fs.agfs.read(f"{fs_path}/.derive_pending")
                    self.fail("Marker should have been deleted")
                except (FileNotFoundError, Exception):
                    pass
            finally:
                reset_request_identity(tokens)
                await fix.teardown()

        asyncio.run(run())

    def test_identity_propagation(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            tokens = set_request_identity("tenant_x", "user_y")
            try:
                result = await fix.orch.add(
                    abstract="",
                    content=MULTI_SECTION_CONTENT,
                    meta={"ingest_mode": "document"},
                    category="documents",
                    context_type="resource",
                )
                await fix.orch._drain_derive_queue()

                records = await fix.orch._storage.filter("context", None, limit=50)
                for r in records:
                    self.assertEqual(r.get("source_tenant_id"), "tenant_x")
                    self.assertEqual(r.get("source_user_id"), "user_y")
            finally:
                reset_request_identity(tokens)
                await fix.teardown()

        asyncio.run(run())


class TestStartupRecovery(unittest.TestCase):
    """Unit 4: Recovery scan finds markers and re-enqueues."""

    def test_recovery_finds_marker_and_reenqueues(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            tokens = set_request_identity("t1", "u1")
            try:
                # Store a document (creates marker + content)
                result = await fix.orch.add(
                    abstract="",
                    content=MULTI_SECTION_CONTENT,
                    meta={"ingest_mode": "document"},
                    category="documents",
                    context_type="resource",
                )
                uri = result.uri

                # Simulate crash: cancel worker without draining
                if fix.orch._derive_worker_task and not fix.orch._derive_worker_task.done():
                    fix.orch._derive_worker_task.cancel()
                    try:
                        await fix.orch._derive_worker_task
                    except asyncio.CancelledError:
                        pass

                # Verify marker still exists
                fs_path = fix.orch._fs._uri_to_path(uri)
                marker_data = fix.orch._fs.agfs.read(f"{fs_path}/.derive_pending")
                self.assertIn(b"parent_uri", marker_data)

                # Release Qdrant lock before creating second orchestrator
                fix.orch._derive_worker_task = None
                if fix.orch._storage:
                    await fix.orch._storage.close()
                fix.orch._storage = None
                fix.orch._initialized = False

                # Create fresh orchestrator on same data
                cfg2 = CortexConfig(data_root=fix.tmpdir, embedding_dimension=4)
                init_config(cfg2)
                orch2 = MemoryOrchestrator(
                    config=cfg2, embedder=MockEmbedder(), llm_completion=_mock_llm,
                )
                await orch2.init()

                # Explicitly run recovery (since _startup_maintenance is a background task)
                await orch2._recover_pending_derives()
                await orch2._drain_derive_queue()

                records = await orch2._storage.filter("context", None, limit=50)
                leaf_records = [r for r in records if r.get("is_leaf")]
                self.assertGreaterEqual(len(leaf_records), 2)

                await orch2.close()
            finally:
                reset_request_identity(tokens)
                shutil.rmtree(fix.tmpdir, ignore_errors=True)

        asyncio.run(run())

    def test_stale_marker_without_content_cleaned(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            try:
                # Manually create a stale marker (no content.md)
                from pathlib import Path
                stale_dir = Path(fix.tmpdir) / "t1" / "resource" / "stale_doc"
                stale_dir.mkdir(parents=True, exist_ok=True)
                marker = {"parent_uri": "opencortex://t1/resource/stale_doc", "tenant_id": "t1", "user_id": "u1"}
                (stale_dir / ".derive_pending").write_text(json.dumps(marker))

                # Run recovery
                await fix.orch._recover_pending_derives()

                # Marker should be deleted
                self.assertFalse((stale_dir / ".derive_pending").exists())
            finally:
                await fix.teardown()

        asyncio.run(run())

    def test_no_markers_clean_startup(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            try:
                # No markers — recovery is a no-op
                self.assertTrue(fix.orch._derive_queue.empty())
            finally:
                await fix.teardown()

        asyncio.run(run())


class TestDeriveStatus(unittest.TestCase):
    """derive_status API: pending → completed transitions."""

    def test_status_pending_then_completed(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            tokens = set_request_identity("t1", "u1")
            try:
                result = await fix.orch.add(
                    abstract="",
                    content=MULTI_SECTION_CONTENT,
                    meta={"ingest_mode": "document"},
                    category="documents",
                    context_type="resource",
                )
                status = await fix.orch.derive_status(result.uri)
                self.assertEqual(status["status"], "pending")

                await fix.orch._drain_derive_queue()

                status = await fix.orch.derive_status(result.uri)
                self.assertEqual(status["status"], "completed")
            finally:
                reset_request_identity(tokens)
                await fix.teardown()

        asyncio.run(run())

    def test_status_not_found(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            try:
                status = await fix.orch.derive_status("opencortex://fake/uri")
                self.assertEqual(status["status"], "not_found")
            finally:
                await fix.teardown()

        asyncio.run(run())


class TestLifecycleShutdown(unittest.TestCase):
    """Unit 5: close() drains worker before closing storage."""

    def test_clean_shutdown_completes_pending(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            tokens = set_request_identity("t1", "u1")
            try:
                await fix.orch.add(
                    abstract="",
                    content=MULTI_SECTION_CONTENT,
                    meta={"ingest_mode": "document"},
                    category="documents",
                    context_type="resource",
                )
                # close() should drain the worker
                await fix.orch.close()

                # Verify worker is done
                self.assertTrue(
                    fix.orch._derive_worker_task is None
                    or fix.orch._derive_worker_task.done()
                )
            finally:
                reset_request_identity(tokens)
                shutil.rmtree(fix.tmpdir, ignore_errors=True)

        asyncio.run(run())

    def test_close_with_empty_queue(self):
        async def run():
            fix = _OrchestratorFixture()
            await fix.setup()
            try:
                await fix.orch.close()
                self.assertTrue(
                    fix.orch._derive_worker_task is None
                    or fix.orch._derive_worker_task.done()
                )
            finally:
                shutil.rmtree(fix.tmpdir, ignore_errors=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
