import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.alpha.trace_store import TraceStore
from opencortex.alpha.types import Trace, Turn, TraceOutcome


class TestTraceStore(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.storage = AsyncMock()
        self.storage.collection_exists = AsyncMock(return_value=True)
        self.embedder = MagicMock()
        self.embedder.embed = MagicMock(return_value=MagicMock(dense_vector=[0.1]*4))
        self.cortex_fs = AsyncMock()
        self.store = TraceStore(
            storage=self.storage,
            embedder=self.embedder,
            cortex_fs=self.cortex_fs,
            collection_name="traces",
            embedding_dim=4,
        )

    async def test_save_trace(self):
        trace = Trace(
            trace_id="tr1", session_id="s1",
            tenant_id="team", user_id="hugo",
            source="claude_code",
            turns=[Turn(turn_id="t1", prompt_text="fix bug", final_text="done")],
            abstract="Fixed a Python import error",
            overview="## Steps\n1. Checked spelling\n2. Fixed import",
        )
        await self.store.save(trace)
        self.storage.upsert.assert_called_once()
        call_args = self.storage.upsert.call_args
        self.assertEqual(call_args[0][0], "traces")  # collection name

    async def test_save_trace_persists_explicit_project_id(self):
        trace = Trace(
            trace_id="tr-project", session_id="s1",
            tenant_id="team", user_id="hugo",
            project_id="project-42",
            source="claude_code",
            turns=[Turn(turn_id="t1", prompt_text="fix bug", final_text="done")],
            abstract="Project-scoped trace",
        )

        await self.store.save(trace)

        saved_record = self.storage.upsert.call_args[0][1]
        self.assertEqual(saved_record["project_id"], "project-42")

    async def test_save_trace_invokes_optional_callback(self):
        on_trace_saved = AsyncMock()
        self.store = TraceStore(
            storage=self.storage,
            embedder=self.embedder,
            cortex_fs=self.cortex_fs,
            collection_name="traces",
            embedding_dim=4,
            on_trace_saved=on_trace_saved,
        )
        trace = Trace(
            trace_id="tr-callback", session_id="s1",
            tenant_id="team", user_id="hugo",
            source="claude_code",
            turns=[Turn(turn_id="t1", prompt_text="fix bug", final_text="done")],
            abstract="Callback trace",
        )

        saved_id = await self.store.save(trace)

        self.assertEqual(saved_id, "tr-callback")
        on_trace_saved.assert_awaited_once_with(trace)

    async def test_save_trace_ignores_callback_failure_after_durable_save(self):
        on_trace_saved = AsyncMock(side_effect=RuntimeError("callback failed"))
        self.store = TraceStore(
            storage=self.storage,
            embedder=self.embedder,
            cortex_fs=self.cortex_fs,
            collection_name="traces",
            embedding_dim=4,
            on_trace_saved=on_trace_saved,
        )
        trace = Trace(
            trace_id="tr-safe-callback", session_id="s1",
            tenant_id="team", user_id="hugo",
            source="claude_code",
            turns=[Turn(turn_id="t1", prompt_text="fix bug", final_text="done")],
            abstract="Safe callback trace",
        )

        saved_id = await self.store.save(trace)

        self.assertEqual(saved_id, "tr-safe-callback")
        self.storage.upsert.assert_called_once()
        on_trace_saved.assert_awaited_once_with(trace)

    async def test_get_trace(self):
        self.storage.get = AsyncMock(return_value=[{
            "trace_id": "tr1", "session_id": "s1",
            "tenant_id": "team", "user_id": "hugo",
            "source": "claude_code", "abstract": "test",
        }])
        result = await self.store.get("tr1")
        self.assertIsNotNone(result)

    async def test_list_by_session(self):
        self.storage.filter = AsyncMock(return_value=[])
        result = await self.store.list_by_session("s1", "team", "hugo")
        self.storage.filter.assert_called_once()
        self.assertIsInstance(result, list)

    async def test_search(self):
        self.storage.search = AsyncMock(return_value=[])
        result = await self.store.search("import error", "team", "hugo", limit=5)
        self.storage.search.assert_called_once()


if __name__ == "__main__":
    unittest.main()
