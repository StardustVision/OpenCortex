import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestMemoryStoreLayers(unittest.IsolatedAsyncioTestCase):
    async def test_write_context_persists_abstract_json(self):
        try:
            from opencortex.storage.cortex_fs import init_cortex_fs
        except ModuleNotFoundError as exc:
            if exc.name == "orjson":
                self.skipTest("orjson is not installed in the local test environment")
            raise

        with tempfile.TemporaryDirectory() as tmpdir:
            fs = init_cortex_fs(data_root=tmpdir)
            uri = "opencortex://tenant/user/memories/events/test-entry"
            abstract_json = {
                "uri": uri,
                "memory_kind": "event",
                "context_type": "memory",
                "category": "event",
                "summary": "Reviewed launch checklist.",
                "anchors": [
                    {
                        "anchor_type": "topic",
                        "value": "launch",
                        "text": "launch",
                    }
                ],
                "slots": {
                    "entities": [],
                    "time_refs": [],
                    "topics": ["launch"],
                    "preferences": [],
                    "constraints": [],
                    "relations": [],
                    "document_refs": [],
                    "summary_refs": [],
                },
                "lineage": {
                    "parent_uri": "",
                    "session_id": "",
                    "source_doc_id": "",
                    "source_doc_title": "",
                    "section_path": [],
                    "chunk_index": None,
                },
                "source": {
                    "context_type": "memory",
                    "category": "event",
                    "source_path": "",
                },
                "quality": {
                    "anchor_count": 1,
                    "entity_count": 0,
                    "keyword_count": 1,
                },
            }

            await fs.write_context(
                uri=uri,
                content="full content",
                abstract="Reviewed launch checklist.",
                abstract_json=abstract_json,
                overview="launch checklist detail",
            )

            self.assertEqual(await fs.abstract(uri), "Reviewed launch checklist.")
            self.assertEqual(await fs.overview(uri), "launch checklist detail")
            self.assertEqual(await fs.abstract_json(uri), abstract_json)
