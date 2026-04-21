"""Test immediate layer: per-message embed + write for instant searchability."""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

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


class TimeoutEmbedder(DenseEmbedderBase):
    def __init__(self):
        super().__init__(model_name="timeout")

    def embed(self, text):
        raise TimeoutError("remote embedding timed out")

    def get_dimension(self):
        return 4


class RuntimeErrorEmbedder(DenseEmbedderBase):
    def __init__(self):
        super().__init__(model_name="runtime")

    def embed(self, text):
        raise RuntimeError("OpenAI embedding API error 404: model does not exist")

    def get_dimension(self):
        return 4


class TestConversationImmediate(unittest.TestCase):
    def test_write_immediate_creates_searchable_record(self):
        """_write_immediate writes to Qdrant without LLM, making message searchable."""
        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(data_root=tmpdir, embedding_dimension=4)
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=MockEmbedder())
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    uri = await orch._write_immediate(
                        session_id="sess-1",
                        msg_index=0,
                        text="[1 May, 2023] Alice moved to Hangzhou and plans a West Lake visit.",
                        meta={
                            "speaker": "Alice",
                            "event_date": "2023-05-01T09:00:00Z",
                            "time_refs": ["1 May, 2023"],
                            "entities": ["Alice", "Hangzhou", "West Lake"],
                        },
                    )
                    self.assertTrue(uri.startswith("opencortex://"))
                    self.assertIn("events", uri)
                    records = await orch._storage.filter(
                        "context",
                        {"op": "must", "field": "uri", "conds": [uri]},
                        limit=1,
                    )
                    self.assertEqual(records[0].get("memory_kind"), "event")
                    self.assertIn("abstract_json", records[0])
                    anchor_records = await orch._storage.filter(
                        "context",
                        {"op": "prefix", "field": "uri", "prefix": f"{uri}/anchors"},
                        limit=10,
                    )
                    self.assertGreaterEqual(len(anchor_records), 1)
                    self.assertTrue(
                        all(
                            record.get("retrieval_surface") == "anchor_projection"
                            for record in anchor_records
                        )
                    )
                    self.assertTrue(all(record.get("anchor_surface") for record in anchor_records))
                finally:
                    reset_request_identity(tokens)
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    def test_write_immediate_falls_back_to_local_embedder_after_remote_timeout(self):
        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(
                    data_root=tmpdir,
                    embedding_dimension=4,
                    embedding_provider="openai",
                    embedding_model="qwen3-embedding-0.6b",
                    embedding_api_key="test-key",
                    embedding_api_base="http://127.0.0.1:19500/v1",
                )
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=TimeoutEmbedder())
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    with patch.object(
                        orch,
                        "_get_immediate_fallback_embedder",
                        return_value=MockEmbedder(),
                    ) as fallback_getter:
                        uri = await orch._write_immediate(
                            session_id="sess-fallback-1",
                            msg_index=0,
                            text="Alice moved to Hangzhou.",
                        )
                    self.assertTrue(uri.startswith("opencortex://"))
                    fallback_getter.assert_called_once()
                    records = await orch._storage.filter(
                        "context",
                        {"op": "must", "field": "uri", "conds": [uri]},
                        limit=1,
                    )
                    self.assertEqual(len(records), 1)
                finally:
                    reset_request_identity(tokens)
                    await orch.close()
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    def test_write_immediate_preserves_timeout_when_no_local_fallback_available(self):
        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(
                    data_root=tmpdir,
                    embedding_dimension=4,
                    embedding_provider="openai",
                    embedding_model="qwen3-embedding-0.6b",
                    embedding_api_key="test-key",
                    embedding_api_base="http://127.0.0.1:19500/v1",
                )
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=TimeoutEmbedder())
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    with patch.object(
                        orch,
                        "_get_immediate_fallback_embedder",
                        return_value=None,
                    ) as fallback_getter:
                        with self.assertRaises(TimeoutError):
                            await orch._write_immediate(
                                session_id="sess-fallback-2",
                                msg_index=0,
                                text="Alice moved to Hangzhou.",
                            )
                    fallback_getter.assert_called_once()
                finally:
                    reset_request_identity(tokens)
                    await orch.close()
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())

    def test_write_immediate_does_not_hide_non_retryable_remote_errors(self):
        async def run():
            tmpdir = tempfile.mkdtemp()
            try:
                cfg = CortexConfig(
                    data_root=tmpdir,
                    embedding_dimension=4,
                    embedding_provider="openai",
                    embedding_model="qwen3-embedding-0.6b",
                    embedding_api_key="test-key",
                    embedding_api_base="http://127.0.0.1:19500/v1",
                )
                init_config(cfg)
                orch = MemoryOrchestrator(config=cfg, embedder=RuntimeErrorEmbedder())
                await orch.init()
                tokens = set_request_identity("t1", "u1")
                try:
                    with patch.object(orch, "_get_immediate_fallback_embedder") as fallback_getter:
                        with self.assertRaisesRegex(RuntimeError, "404"):
                            await orch._write_immediate(
                                session_id="sess-fallback-3",
                                msg_index=0,
                                text="Alice moved to Hangzhou.",
                            )
                    fallback_getter.assert_not_called()
                finally:
                    reset_request_identity(tokens)
                    await orch.close()
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
