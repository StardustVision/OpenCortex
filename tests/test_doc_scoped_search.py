import asyncio
import unittest


class TestPayloadFlattening(unittest.TestCase):
    def test_new_fields_in_context_schema(self):
        from opencortex.storage.collection_schemas import CollectionSchemas
        schema = CollectionSchemas.context_collection("test", 1024)
        field_names = [f["FieldName"] for f in schema["Fields"]]
        required = ["source_doc_id", "source_doc_title", "source_section_path",
                     "chunk_role", "speaker", "event_date"]
        for name in required:
            self.assertIn(name, field_names, f"Missing field {name} in context schema Fields")

    def test_new_fields_have_scalar_index(self):
        from opencortex.storage.collection_schemas import CollectionSchemas
        schema = CollectionSchemas.context_collection("test", 1024)
        indexed = schema["ScalarIndex"]
        for name in ["source_doc_id", "source_doc_title", "source_section_path",
                      "chunk_role", "speaker", "event_date"]:
            self.assertIn(name, indexed, f"Missing ScalarIndex for {name}")


class TestSectionPath(unittest.TestCase):
    def test_markdown_parser_adds_section_path(self):
        from opencortex.parse.parsers.markdown import MarkdownParser
        parser = MarkdownParser()
        content = "# Chapter 1\n\n## Section A\n\nSome text here.\n\n## Section B\n\nMore text."
        chunks = asyncio.run(parser.parse_content(content, source_path="test.md"))
        paths = [c.meta.get("section_path", "") for c in chunks if c.meta.get("section_path")]
        self.assertTrue(len(paths) > 0, "No chunks have section_path in meta")

    def test_section_path_reflects_hierarchy(self):
        """section_path for a nested heading should include parent heading name."""
        from opencortex.parse.parsers.markdown import MarkdownParser
        from opencortex.parse.base import ParserConfig
        # Use a small max_section_size so sections don't get merged into one chunk
        parser = MarkdownParser(config=ParserConfig(max_section_size=50, min_section_tokens=10))
        content = (
            "# Chapter 1\n\n" + "x " * 200 + "\n\n"
            "## Section A\n\n" + "y " * 200 + "\n\n"
            "## Section B\n\n" + "z " * 200 + "\n\n"
        )
        chunks = asyncio.run(parser.parse_content(content, source_path="doc.md"))
        paths = [c.meta.get("section_path", "") for c in chunks if c.meta.get("section_path")]
        self.assertTrue(any("Chapter 1" in p for p in paths), f"No path contains 'Chapter 1': {paths}")
        # Nested sections should carry parent in path
        nested = [p for p in paths if ">" in p]
        self.assertTrue(len(nested) > 0, f"No nested section paths found: {paths}")

    def test_source_doc_id_in_chunk_meta(self):
        """_add_document should inject source_doc_id into each chunk's meta."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        # Build a minimal orchestrator with mocked add()
        added_metas = []

        async def fake_add(**kwargs):
            added_metas.append(kwargs.get("meta", {}))
            ctx = MagicMock()
            ctx.uri = f"opencortex://team/user/memories/doc/{len(added_metas)}"
            ctx.is_leaf = kwargs.get("is_leaf", True)
            return ctx

        from opencortex.orchestrator import MemoryOrchestrator
        orch = object.__new__(MemoryOrchestrator)
        orch._parser_registry = None
        orch.add = fake_add

        # Large enough content to produce multiple chunks
        content = "\n\n".join(
            f"# Section {i}\n\n" + ("word " * 300)
            for i in range(5)
        )
        asyncio.run(orch._add_document(
            content=content,
            abstract="Test doc",
            overview="",
            category="document",
            parent_uri=None,
            context_type=None,
            meta={"source_path": "reports/annual.md"},
            session_id=None,
            source_path="reports/annual.md",
        ))
        # All chunk metas (except possibly the parent doc meta) should have source_doc_id
        chunk_metas = [m for m in added_metas if m.get("chunk_index") is not None]
        self.assertTrue(len(chunk_metas) > 0, "No chunk metas found")
        for m in chunk_metas:
            self.assertIn("source_doc_id", m, f"source_doc_id missing from chunk meta: {m}")
            self.assertIn("chunk_role", m, f"chunk_role missing from chunk meta: {m}")
            self.assertIn("source_section_path", m, f"source_section_path missing from chunk meta: {m}")

    def test_doc_filter_injected_when_target_doc_id_set(self):
        """retrieve() should inject a source_doc_id filter when query.target_doc_id is set."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from opencortex.retrieve.hierarchical_retriever import HierarchicalRetriever
        from opencortex.retrieve.types import ContextType, DetailLevel, TypedQuery

        # Mock storage
        storage = MagicMock()
        storage.collection_exists = AsyncMock(return_value=True)
        # Return empty results to avoid complex setup
        storage.search = AsyncMock(return_value=[])

        retriever = HierarchicalRetriever(
            storage=storage,
            embedder=None,  # no embedder → fallback path
        )

        query = TypedQuery(
            query="what happened in Q3",
            context_type=ContextType.MEMORY,
            intent="lookup",
            detail_level=DetailLevel.L1,
            target_doc_id="abc123def456",
        )

        asyncio.run(retriever.retrieve(query, limit=5))

        # storage.search should have been called with a filter containing source_doc_id
        calls = storage.search.call_args_list
        self.assertTrue(len(calls) > 0, "storage.search was never called")
        # Extract filter from first call
        first_call_kwargs = calls[0].kwargs
        filter_used = first_call_kwargs.get("filter", {})
        filter_str = str(filter_used)
        self.assertIn("source_doc_id", filter_str,
                      f"source_doc_id filter not injected. Filter used: {filter_str}")
