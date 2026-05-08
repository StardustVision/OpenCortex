# SPDX-License-Identifier: Apache-2.0
"""Tests for opencortex_app CortexStorage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opencortex_app.storage.cortex_storage import CortexStorage


class TestCortexStorage(unittest.IsolatedAsyncioTestCase):
    """CortexStorage persists OpenCortex URI trees locally."""

    async def test_write_context_persists_layers(self) -> None:
        """write_context writes content, abstract, overview, and abstract JSON."""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = CortexStorage(data_root=temp_dir)
            uri = "opencortex://tenant/user/memories/events/test-entry"
            abstract_json = {
                "uri": uri,
                "context_type": "memory",
                "category": "events",
                "summary": "Reviewed launch checklist.",
            }

            await storage.write_context(
                uri=uri,
                content="full content",
                abstract="Reviewed launch checklist.",
                abstract_json=abstract_json,
                overview="launch checklist detail",
            )

            self.assertEqual(
                await storage.read_file(f"{uri}/content.md"),
                "full content",
            )
            self.assertEqual(await storage.abstract(uri), "Reviewed launch checklist.")
            self.assertEqual(await storage.overview(uri), "launch checklist detail")
            self.assertEqual(await storage.abstract_json(uri), abstract_json)

    async def test_uri_resolution_stays_inside_data_root(self) -> None:
        """URI path traversal is rejected before touching the filesystem."""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = CortexStorage(data_root=temp_dir)

            with self.assertRaises(ValueError):
                storage.uri_to_path("opencortex://tenant/../outside")

    async def test_cortex_storage_writes_files(self) -> None:
        """CortexStorage writes content files to its CFS root."""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = CortexStorage(data_root=temp_dir)
            uri = "opencortex://tenant/user/memories/events/action-entry"

            await storage.write_context(
                uri=uri,
                content="event content",
                abstract="event abstract",
                overview="event overview",
            )

            node_path = Path(temp_dir) / "tenant/user/memories/events/action-entry"
            self.assertTrue(node_path.is_dir())
            self.assertEqual((node_path / "content.md").read_text(), "event content")

    async def test_filesystem_operations(self) -> None:
        """CortexStorage supports grep, glob, move, batch read, and temp cleanup."""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = CortexStorage(data_root=temp_dir)
            uri = "opencortex://tenant/user/memories/events/fs-entry"
            moved_uri = "opencortex://tenant/user/memories/events/fs-entry-moved"

            await storage.write_context(
                uri=uri,
                content="Alpha beta\nsecond line",
                abstract="Alpha summary",
                overview="Alpha overview",
            )
            await storage.write_file(f"{uri}/notes.txt", "Find Alpha here")

            grep_result = await storage.grep(uri, "alpha", case_insensitive=True)
            glob_result = await storage.glob("*.txt", uri)
            batch = await storage.read_batch([uri], level="l0")
            await storage.move_file(f"{uri}/notes.txt", f"{uri}/notes-moved.txt")
            await storage.mv(uri, moved_uri)
            temp_uri = storage.create_temp_uri()
            temp_path = storage.uri_to_path(temp_uri)
            await storage.write_file(f"{temp_uri}/scratch.txt", "scratch")
            await storage.delete_temp(temp_uri)

            self.assertGreaterEqual(grep_result["count"], 1)
            self.assertEqual(glob_result["matches"], [f"{uri}/notes.txt"])
            self.assertEqual(batch, {uri: "Alpha summary"})
            self.assertEqual(
                await storage.read_file(f"{moved_uri}/notes-moved.txt"),
                "Find Alpha here",
            )
            self.assertFalse(temp_path.exists())

    async def test_relation_table_operations(self) -> None:
        """Relation helpers read and write only .relations.json files."""
        with tempfile.TemporaryDirectory() as temp_dir:
            storage = CortexStorage(data_root=temp_dir)
            source_uri = "opencortex://tenant/user/memories/events/source"
            target_uri = "opencortex://tenant/user/memories/events/target"

            await storage.write_context(uri=source_uri, abstract="source")
            await storage.write_context(
                uri=target_uri,
                abstract="target abstract",
                overview="target overview",
            )
            await storage.link(source_uri, target_uri, reason="related")

            self.assertEqual(
                await storage.relations(source_uri),
                [{"uri": target_uri, "reason": "related"}],
            )
            self.assertEqual(await storage.get_relations(source_uri), [target_uri])
            self.assertEqual(
                await storage.get_relations_with_content(
                    source_uri,
                    include_l1=True,
                ),
                [
                    {
                        "uri": target_uri,
                        "abstract": "target abstract",
                        "overview": "target overview",
                    }
                ],
            )

            await storage.unlink(source_uri, target_uri)

            self.assertEqual(await storage.relations(source_uri), [])
