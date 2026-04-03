import unittest
from opencortex.storage.qdrant.filter_translator import translate_filter
from opencortex.alpha.types import KnowledgeStatus, KnowledgeScope, SEARCHABLE_STATUSES


class TestKnowledgeStoreFilters(unittest.TestCase):
    """Verify knowledge_store filter expressions produce valid Qdrant filters."""

    def _build_search_filter(self, tenant_id, user_id, types=None):
        """Reproduce the filter logic from KnowledgeStore.search()."""
        must_conds = [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "status", "conds": [s.value for s in SEARCHABLE_STATUSES]},
        ]
        if types:
            must_conds.append({"op": "must", "field": "knowledge_type", "conds": types})

        scope_filter = {"op": "or", "conds": [
            {"op": "must", "field": "scope", "conds": [
                KnowledgeScope.TENANT.value,
                KnowledgeScope.GLOBAL.value,
            ]},
            {"op": "and", "conds": [
                {"op": "must", "field": "scope", "conds": [KnowledgeScope.USER.value]},
                {"op": "must", "field": "user_id", "conds": [user_id]},
            ]},
        ]}
        must_conds.append(scope_filter)

        return {"op": "and", "conds": must_conds}

    def _build_candidates_filter(self, tenant_id):
        """Reproduce the filter logic from KnowledgeStore.list_candidates()."""
        return {"op": "and", "conds": [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "status", "conds": [
                KnowledgeStatus.CANDIDATE.value,
                KnowledgeStatus.VERIFIED.value,
            ]},
        ]}

    def test_search_filter_translates_without_error(self):
        """search() filter produces a valid non-empty Qdrant filter."""
        f = translate_filter(self._build_search_filter("team1", "userA"))
        self.assertTrue(f.must, "Top-level must should be non-empty")

    def test_search_filter_with_types(self):
        """search() with types filter adds knowledge_type condition."""
        f = translate_filter(self._build_search_filter("team1", "userA", types=["sop", "belief"]))
        self.assertTrue(f.must)
        self.assertGreaterEqual(len(f.must), 4)

    def test_search_filter_includes_scope_or_group(self):
        """search() filter includes OR group for scope visibility."""
        f = translate_filter(self._build_search_filter("team1", "userA"))
        has_should = any(
            hasattr(c, "should") and c.should for c in f.must
        )
        self.assertTrue(has_should, "Filter must contain scope OR group")

    def test_candidates_filter_translates_without_error(self):
        """list_candidates() filter produces a valid non-empty Qdrant filter."""
        f = translate_filter(self._build_candidates_filter("team1"))
        self.assertTrue(f.must, "Top-level must should be non-empty")
        self.assertEqual(len(f.must), 2)

    def test_old_dsl_format_produces_empty_filter(self):
        """Demonstrate the OLD broken format produces an empty (match-all) filter."""
        old_format = {"op": "and", "conditions": [
            {"field": "tenant_id", "op": "=", "value": "team1"},
        ]}
        f = translate_filter(old_format)
        self.assertFalse(f.must, "Old format should produce empty filter (no must)")
        self.assertFalse(f.should, "Old format should produce empty filter (no should)")


if __name__ == "__main__":
    unittest.main()
