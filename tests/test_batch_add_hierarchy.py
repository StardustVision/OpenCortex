"""Test batch_add directory hierarchy building from file_path metadata."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.config import CortexConfig, init_config
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.orchestrator import MemoryOrchestrator


class MockEmbedder(DenseEmbedderBase):
    def __init__(self):
        super().__init__(model_name="mock")
    def embed(self, text):
        return EmbedResult(dense_vector=[0.1, 0.2, 0.3, 0.4])
    def get_dimension(self):
        return 4


class TestBatchAddHierarchy(unittest.TestCase):
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_batch_creates_directory_nodes(self):
        """batch_add with scan_meta creates directory nodes from file_path."""
        async def mock_llm(prompt):
            return '{"abstract": "file summary", "overview": "detail", "keywords": ["test"]}'

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir, embedding_dimension=4)
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder(), llm_completion=mock_llm)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    items = [
                        {
                            "abstract": "README file",
                            "content": "Project readme content",
                            "category": "documents",
                            "context_type": "resource",
                            "meta": {"file_path": "project/docs/README.md"},
                        },
                        {
                            "abstract": "Config file",
                            "content": "Config content",
                            "category": "documents",
                            "context_type": "resource",
                            "meta": {"file_path": "project/src/config.py"},
                        },
                    ]
                    result = await orch.batch_add(
                        items=items,
                        scan_meta={"total_files": 2, "has_git": True},
                    )
                    # Should have directory nodes + leaf items
                    # Directories: project, project/docs, project/src = 3
                    # Leaf items: 2
                    total_uris = len(result.get("uris", []))
                    self.assertEqual(total_uris, 5, f"Expected 5 URIs (3 dirs + 2 files), got {total_uris}")
                    self.assertEqual(result["imported"], 2)
                    self.assertEqual(len(result.get("errors", [])), 0)
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    def test_batch_without_scan_meta_no_dirs(self):
        """batch_add without scan_meta does not create directory nodes."""
        async def mock_llm(prompt):
            return '{"abstract": "file summary", "overview": "detail", "keywords": ["test"]}'

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir, embedding_dimension=4)
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder(), llm_completion=mock_llm)
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    items = [
                        {
                            "abstract": "README file",
                            "content": "Project readme",
                            "category": "documents",
                            "context_type": "resource",
                            "meta": {"file_path": "project/docs/README.md"},
                        },
                    ]
                    result = await orch.batch_add(items=items)
                    # No scan_meta → no directory nodes
                    total_uris = len(result.get("uris", []))
                    self.assertEqual(total_uris, 1)
                    self.assertEqual(result["imported"], 1)
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    def test_batch_parent_uri_assigned(self):
        """Leaf items get parent_uri pointing to their directory node."""
        async def mock_llm(prompt):
            return '{"abstract": "summary", "overview": "detail", "keywords": ["test"]}'

        created_records = []
        original_add = None

        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir, embedding_dimension=4)
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder(), llm_completion=mock_llm)
                await orch.init()

                # Patch add to capture parent_uri values
                original = orch.add
                async def spy_add(**kwargs):
                    result = await original(**kwargs)
                    created_records.append({
                        "abstract": kwargs.get("abstract", ""),
                        "parent_uri": kwargs.get("parent_uri"),
                        "is_leaf": kwargs.get("is_leaf", True),
                        "uri": result.uri,
                    })
                    return result
                orch.add = spy_add

                tokens = set_request_identity("t1", "u1")
                try:
                    items = [
                        {
                            "abstract": "File A",
                            "content": "Content A",
                            "category": "documents",
                            "context_type": "resource",
                            "meta": {"file_path": "root/sub/a.txt"},
                        },
                    ]
                    await orch.batch_add(
                        items=items,
                        scan_meta={"total_files": 1},
                    )

                    # Find directory nodes and leaf
                    dirs = [r for r in created_records if not r["is_leaf"]]
                    leaves = [r for r in created_records if r["is_leaf"]]

                    self.assertEqual(len(dirs), 2)  # root, root/sub
                    self.assertEqual(len(leaves), 1)

                    # Leaf's parent_uri should be the "root/sub" directory
                    sub_dir = [d for d in dirs if d["abstract"] == "sub"][0]
                    self.assertEqual(leaves[0]["parent_uri"], sub_dir["uri"])
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
