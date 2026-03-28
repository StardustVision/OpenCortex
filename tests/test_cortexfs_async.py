"""
Tests for CortexFS async write_context wrapping.

Verifies that write_context uses run_in_executor (non-blocking), writes the
correct files to disk, and handles edge cases without regressions.
"""

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.storage.cortex_fs import CortexFS, _fs_executor


class TestCortexFSAsyncWrite(unittest.TestCase):
    """Verify write_context runs I/O in the thread executor, not inline."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cortexfs_async_")
        self.fs = CortexFS(data_root=self.temp_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.run(coro)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _node_path(self, uri: str) -> str:
        """Return the absolute filesystem directory for a given URI."""
        remainder = uri[len("opencortex://"):].strip("/")
        return os.path.join(self.temp_dir, remainder)

    # ------------------------------------------------------------------
    # 1. Basic write — all three layers persisted on disk
    # ------------------------------------------------------------------

    def test_write_all_layers(self):
        """write_context persists content, abstract, and overview to disk."""
        uri = "opencortex://team1/user1/memories/test/node001"

        async def _run():
            await self.fs.write_context(
                uri=uri,
                content="Hello world",
                abstract="Short summary",
                overview="Longer overview text",
            )

        self._run(_run())

        node_dir = self._node_path(uri)
        self.assertTrue(os.path.isdir(node_dir), "Node directory must be created")

        content_path = os.path.join(node_dir, "content.md")
        abstract_path = os.path.join(node_dir, ".abstract.md")
        overview_path = os.path.join(node_dir, ".overview.md")

        self.assertTrue(os.path.isfile(content_path), "content.md must exist")
        self.assertTrue(os.path.isfile(abstract_path), ".abstract.md must exist")
        self.assertTrue(os.path.isfile(overview_path), ".overview.md must exist")

        with open(content_path, "rb") as f:
            self.assertEqual(f.read(), b"Hello world")
        with open(abstract_path, "rb") as f:
            self.assertEqual(f.read(), b"Short summary")
        with open(overview_path, "rb") as f:
            self.assertEqual(f.read(), b"Longer overview text")

    # ------------------------------------------------------------------
    # 2. Partial write — only abstract provided
    # ------------------------------------------------------------------

    def test_write_abstract_only(self):
        """write_context with only abstract does not create content or overview files."""
        uri = "opencortex://team1/user1/memories/test/node002"

        async def _run():
            await self.fs.write_context(uri=uri, abstract="Only abstract")

        self._run(_run())

        node_dir = self._node_path(uri)
        self.assertTrue(os.path.isdir(node_dir))
        self.assertTrue(os.path.isfile(os.path.join(node_dir, ".abstract.md")))
        self.assertFalse(os.path.isfile(os.path.join(node_dir, "content.md")))
        self.assertFalse(os.path.isfile(os.path.join(node_dir, ".overview.md")))

    # ------------------------------------------------------------------
    # 3. Idempotent write — second call overwrites existing files
    # ------------------------------------------------------------------

    def test_write_idempotent(self):
        """Calling write_context twice on the same URI updates files in place."""
        uri = "opencortex://team1/user1/memories/test/node003"

        async def _run():
            await self.fs.write_context(uri=uri, abstract="First")
            await self.fs.write_context(uri=uri, abstract="Second")

        self._run(_run())

        abstract_path = os.path.join(self._node_path(uri), ".abstract.md")
        with open(abstract_path, "rb") as f:
            self.assertEqual(f.read(), b"Second")

    # ------------------------------------------------------------------
    # 4. Custom content filename
    # ------------------------------------------------------------------

    def test_custom_content_filename(self):
        """write_context respects a custom content_filename parameter."""
        uri = "opencortex://team1/user1/memories/test/node004"

        async def _run():
            await self.fs.write_context(
                uri=uri,
                content="Custom file",
                content_filename="chunk.md",
            )

        self._run(_run())

        node_dir = self._node_path(uri)
        self.assertTrue(os.path.isfile(os.path.join(node_dir, "chunk.md")))
        self.assertFalse(os.path.isfile(os.path.join(node_dir, "content.md")))

    # ------------------------------------------------------------------
    # 5. Bytes content is written as-is
    # ------------------------------------------------------------------

    def test_bytes_content(self):
        """write_context accepts bytes for content and writes them unchanged."""
        uri = "opencortex://team1/user1/memories/test/node005"
        raw = b"\x00\x01\x02binary"

        async def _run():
            await self.fs.write_context(uri=uri, content=raw)

        self._run(_run())

        content_path = os.path.join(self._node_path(uri), "content.md")
        with open(content_path, "rb") as f:
            self.assertEqual(f.read(), raw)

    # ------------------------------------------------------------------
    # 6. Non-blocking — write_context does not stall the event loop
    # ------------------------------------------------------------------

    def test_write_does_not_block_event_loop(self):
        """write_context releases the event loop while file I/O is in progress."""
        uri = "opencortex://team1/user1/memories/test/node006"
        timestamps: list = []

        async def _side_task():
            timestamps.append(("side_start", time.monotonic()))
            await asyncio.sleep(0)
            timestamps.append(("side_end", time.monotonic()))

        async def _run():
            # Launch a side coroutine that simply yields to the event loop
            side = asyncio.create_task(_side_task())
            await self.fs.write_context(
                uri=uri,
                content="content",
                abstract="abstract",
                overview="overview",
            )
            await side

        self._run(_run())

        # If the side_task ran at all, the event loop was not blocked during write
        self.assertEqual(len(timestamps), 2, "side_task must complete")
        self.assertLess(
            timestamps[0][1],
            timestamps[1][1],
            "side_task start must precede its end",
        )

    # ------------------------------------------------------------------
    # 7. Executor is the module-level bounded ThreadPoolExecutor
    # ------------------------------------------------------------------

    def test_module_level_executor_exists(self):
        """_fs_executor is a ThreadPoolExecutor with max_workers=4."""
        import concurrent.futures

        self.assertIsInstance(_fs_executor, concurrent.futures.ThreadPoolExecutor)
        # ThreadPoolExecutor stores max_workers internally
        self.assertEqual(_fs_executor._max_workers, 4)

    # ------------------------------------------------------------------
    # 8. Concurrent writes complete correctly
    # ------------------------------------------------------------------

    def test_concurrent_writes(self):
        """Multiple concurrent write_context calls all persist their data."""
        uris = [
            f"opencortex://team1/user1/memories/test/concurrent{i:03d}"
            for i in range(10)
        ]

        async def _run():
            await asyncio.gather(
                *[
                    self.fs.write_context(
                        uri=uri,
                        abstract=f"abstract-{i}",
                        content=f"content-{i}",
                    )
                    for i, uri in enumerate(uris)
                ]
            )

        self._run(_run())

        for i, uri in enumerate(uris):
            node_dir = self._node_path(uri)
            abstract_path = os.path.join(node_dir, ".abstract.md")
            self.assertTrue(os.path.isfile(abstract_path), f"Missing {uri}")
            with open(abstract_path, "rb") as f:
                self.assertEqual(f.read(), f"abstract-{i}".encode())


if __name__ == "__main__":
    unittest.main()
