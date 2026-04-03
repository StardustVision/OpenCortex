import hashlib
import unittest
from opencortex.skill_engine.types import (
    SkillOrigin, SkillCategory, SkillVisibility, SkillStatus,
    SkillLineage, SkillRecord, EvolutionSuggestion,
    make_skill_uri, make_source_fingerprint,
)


class TestSkillEnums(unittest.TestCase):

    def test_skill_origin_values(self):
        self.assertEqual(SkillOrigin.IMPORTED, "imported")
        self.assertEqual(SkillOrigin.CAPTURED, "captured")
        self.assertEqual(SkillOrigin.DERIVED, "derived")
        self.assertEqual(SkillOrigin.FIXED, "fixed")

    def test_skill_category_values(self):
        self.assertEqual(SkillCategory.WORKFLOW, "workflow")
        self.assertEqual(SkillCategory.TOOL_GUIDE, "tool_guide")
        self.assertEqual(SkillCategory.PATTERN, "pattern")

    def test_skill_visibility_values(self):
        self.assertEqual(SkillVisibility.PRIVATE, "private")
        self.assertEqual(SkillVisibility.SHARED, "shared")

    def test_skill_status_values(self):
        self.assertEqual(SkillStatus.CANDIDATE, "candidate")
        self.assertEqual(SkillStatus.ACTIVE, "active")
        self.assertEqual(SkillStatus.DEPRECATED, "deprecated")


class TestSkillRecord(unittest.TestCase):

    def test_minimal_record(self):
        r = SkillRecord(
            skill_id="sk-001", name="deploy-flow",
            description="Standard deployment workflow",
            content="# Deploy\n1. Build\n2. Test\n3. Deploy",
            category=SkillCategory.WORKFLOW,
            tenant_id="team1", user_id="hugo",
        )
        self.assertEqual(r.status, SkillStatus.CANDIDATE)
        self.assertEqual(r.visibility, SkillVisibility.PRIVATE)
        self.assertEqual(r.total_selections, 0)
        self.assertEqual(r.uri, "")

    def test_to_dict_excludes_none_lists(self):
        r = SkillRecord(
            skill_id="sk-001", name="test", description="d",
            content="c", category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u",
        )
        d = r.to_dict()
        self.assertEqual(d["skill_id"], "sk-001")
        self.assertEqual(d["status"], "candidate")
        self.assertEqual(d["visibility"], "private")
        self.assertIn("lineage", d)

    def test_to_dict_roundtrip_preserves_fields(self):
        r = SkillRecord(
            skill_id="sk-002", name="debug-flow",
            description="Debug workflow", content="# Debug",
            category=SkillCategory.PATTERN,
            tenant_id="t", user_id="u",
            uri="opencortex://t/u/skills/sk-002",
            tags=["debug", "workflow"],
            source_fingerprint="abc123",
        )
        d = r.to_dict()
        self.assertEqual(d["uri"], "opencortex://t/u/skills/sk-002")
        self.assertEqual(d["tags"], ["debug", "workflow"])
        self.assertEqual(d["source_fingerprint"], "abc123")


class TestSkillLineage(unittest.TestCase):

    def test_default_lineage(self):
        l = SkillLineage()
        self.assertEqual(l.generation, 0)
        self.assertEqual(l.parent_skill_ids, [])
        self.assertEqual(l.source_memory_ids, [])

    def test_captured_lineage(self):
        l = SkillLineage(
            origin=SkillOrigin.CAPTURED,
            source_memory_ids=["m1", "m2", "m3"],
            created_by="claude-opus-4",
        )
        self.assertEqual(l.origin, SkillOrigin.CAPTURED)
        self.assertEqual(len(l.source_memory_ids), 3)


class TestEvolutionSuggestion(unittest.TestCase):

    def test_captured_suggestion(self):
        s = EvolutionSuggestion(
            evolution_type=SkillOrigin.CAPTURED,
            target_skill_ids=[],
            category=SkillCategory.WORKFLOW,
            direction="Extract deploy workflow from memory cluster",
            confidence=0.85,
            source_memory_ids=["m1", "m2"],
        )
        self.assertEqual(s.evolution_type, SkillOrigin.CAPTURED)
        self.assertEqual(len(s.target_skill_ids), 0)


class TestHelperFunctions(unittest.TestCase):

    def test_make_skill_uri_private(self):
        uri = make_skill_uri("team1", "hugo", "sk-001")
        self.assertEqual(uri, "opencortex://team1/hugo/skills/general/sk-001")

    def test_make_skill_uri_shared(self):
        uri = make_skill_uri("team1", "hugo", "sk-001", visibility="shared", category="workflow")
        self.assertEqual(uri, "opencortex://team1/shared/skills/workflow/sk-001")

    def test_make_skill_uri_private_with_category(self):
        uri = make_skill_uri("team1", "hugo", "sk-001", visibility="private", category="pattern")
        self.assertEqual(uri, "opencortex://team1/hugo/skills/pattern/sk-001")

    def test_make_source_fingerprint(self):
        fp = make_source_fingerprint(["m3", "m1", "m2"])
        fp2 = make_source_fingerprint(["m1", "m2", "m3"])
        self.assertEqual(fp, fp2)
        self.assertEqual(len(fp), 16)

    def test_make_source_fingerprint_empty(self):
        fp = make_source_fingerprint([])
        self.assertEqual(len(fp), 16)


if __name__ == "__main__":
    unittest.main()
