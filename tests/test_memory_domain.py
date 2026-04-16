import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.memory import (
    MemoryKind,
    infer_memory_kind,
    memory_abstract_from_record,
    memory_merge_signature_from_abstract,
    memory_object_view_from_match,
    memory_object_view_from_record,
    retrieval_hints_for_kinds,
)


class _MatchedContextStub:
    def __init__(self, **kwargs):
        self.uri = kwargs.get("uri", "")
        self.category = kwargs.get("category", "")
        self.context_type = kwargs.get("context_type", "memory")
        self.abstract = kwargs.get("abstract", "")
        self.overview = kwargs.get("overview")
        self.content = kwargs.get("content")
        self.metadata = kwargs.get("metadata", {})


class TestMemoryDomain(unittest.TestCase):
    def test_infer_memory_kind_distinguishes_preference_and_profile(self):
        self.assertEqual(
            infer_memory_kind(category="preferences", context_type="memory"),
            MemoryKind.PREFERENCE,
        )
        self.assertEqual(
            infer_memory_kind(category="profile", context_type="memory"),
            MemoryKind.PROFILE,
        )

    def test_record_projection_builds_partial_object_view(self):
        view = memory_object_view_from_record(
            {
                "uri": "opencortex://team/user/memories/preferences/coffee",
                "category": "preferences",
                "context_type": "memory",
                "abstract": "User likes pour-over coffee.",
                "metadata": {"topics": ["coffee", "brewing"]},
            }
        )

        self.assertEqual(view.memory_kind, MemoryKind.PREFERENCE)
        self.assertEqual(
            view.structured_slots.preferences,
            ["User likes pour-over coffee."],
        )
        self.assertEqual(view.structured_slots.topics, ["coffee", "brewing"])
        self.assertTrue(view.policy.mergeable)

    def test_match_projection_handles_resource_records(self):
        view = memory_object_view_from_match(
            _MatchedContextStub(
                uri="opencortex://team/user/resources/docs/plan",
                category="document",
                context_type="resource",
                abstract="Release plan chunk",
            )
        )

        self.assertEqual(view.memory_kind, MemoryKind.DOCUMENT_CHUNK)
        self.assertIn("resource", view.policy.retrieval_context_types)

    def test_retrieval_hints_merge_multiple_kinds_without_duplicates(self):
        hints = retrieval_hints_for_kinds(
            [MemoryKind.RELATION, MemoryKind.DOCUMENT_CHUNK]
        )

        self.assertIn("memory", hints.context_types)
        self.assertIn("resource", hints.context_types)
        self.assertEqual(len(hints.categories), len(set(hints.categories)))

    def test_memory_abstract_uses_fixed_shared_schema(self):
        abstract_payload = memory_abstract_from_record(
            {
                "uri": "opencortex://team/user/resources/doc/chunk-1",
                "category": "document_chunk",
                "context_type": "resource",
                "abstract": "Release plan chunk",
                "metadata": {
                    "topics": ["release"],
                    "source_doc_id": "doc-1",
                    "source_doc_title": "Release Plan",
                    "section_path": ["Intro", "Timeline"],
                    "chunk_index": 1,
                },
            }
        ).to_dict()

        self.assertEqual(abstract_payload["memory_kind"], "document_chunk")
        self.assertEqual(abstract_payload["summary"], "Release plan chunk")
        self.assertEqual(abstract_payload["lineage"]["source_doc_id"], "doc-1")
        self.assertEqual(
            abstract_payload["lineage"]["section_path"],
            ["Intro", "Timeline"],
        )
        self.assertEqual(abstract_payload["source"]["context_type"], "resource")
        self.assertIn("anchors", abstract_payload)

    def test_record_projection_prefers_abstract_json_when_present(self):
        view = memory_object_view_from_record(
            {
                "uri": "opencortex://team/user/memories/events/1",
                "abstract": "fallback abstract",
                "context_type": "memory",
                "category": "events",
                "abstract_json": {
                    "uri": "opencortex://team/user/memories/events/1",
                    "memory_kind": "event",
                    "context_type": "memory",
                    "category": "events",
                    "summary": "canonical abstract",
                    "anchors": [
                        {"anchor_type": "topic", "value": "launch", "text": "launch"},
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
                        "session_id": "sess-1",
                        "source_doc_id": "",
                        "source_doc_title": "",
                        "section_path": [],
                        "chunk_index": None,
                    },
                    "source": {
                        "context_type": "memory",
                        "category": "events",
                        "source_path": "",
                    },
                    "quality": {
                        "anchor_count": 1,
                        "entity_count": 0,
                        "keyword_count": 1,
                    },
                },
            }
        )

        self.assertEqual(view.abstract, "fallback abstract")
        self.assertEqual(view.anchor_entries[0].text, "launch")
        self.assertEqual(view.structured_slots.topics, ["launch"])
        self.assertEqual(view.lineage.session_id, "sess-1")

    def test_merge_signature_uses_canonical_abstract_payload(self):
        abstract_payload = memory_abstract_from_record(
            {
                "uri": "opencortex://team/user/memories/preferences/theme",
                "category": "preferences",
                "context_type": "memory",
                "abstract": "User prefers dark theme in editors.",
            }
        ).to_dict()

        signature = memory_merge_signature_from_abstract(abstract_payload)

        self.assertTrue(signature.startswith("preference|"))
        self.assertIn("user prefers dark theme in editors.", signature)

    def test_memory_abstract_promotes_keywords_and_anchor_handles(self):
        abstract_payload = memory_abstract_from_record(
            {
                "uri": "opencortex://team/user/resources/doc/chunk-2",
                "category": "document_chunk",
                "context_type": "resource",
                "abstract": "Dark mode rollout lives in src/main.py with 3 replicas.",
                "keywords": ["dark mode", "src/main.py"],
                "metadata": {
                    "anchor_handles": ["3 replicas", "rollout checklist"],
                },
            }
        ).to_dict()

        anchor_texts = [anchor["text"] for anchor in abstract_payload["anchors"]]

        self.assertIn("dark mode", anchor_texts)
        self.assertIn("src/main.py", anchor_texts)
        self.assertIn("3 replicas", anchor_texts)
        self.assertLessEqual(len(anchor_texts), 6)

    def test_memory_abstract_drops_generic_keyword_handles(self):
        abstract_payload = memory_abstract_from_record(
            {
                "uri": "opencortex://team/user/memories/events/2",
                "category": "events",
                "context_type": "memory",
                "abstract": "Release note",
                "keywords": ["events", "summary", "document"],
            }
        ).to_dict()

        anchor_texts = [anchor["text"] for anchor in abstract_payload["anchors"]]

        self.assertNotIn("events", anchor_texts)
        self.assertNotIn("summary", anchor_texts)
        self.assertNotIn("document", anchor_texts)


if __name__ == "__main__":
    unittest.main()
