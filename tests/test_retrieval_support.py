import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.intent.retrieval_support import query_anchor_groups
from opencortex.intent.types import (
    MemoryQueryPlan,
    MemorySearchProfile,
    ProbeScopeSource,
    QueryAnchor,
    QueryAnchorKind,
    QueryRewriteMode,
    RetrievalDepth,
    RetrievalPlan,
    ScopeLevel,
    SearchResult,
    StartingPoint,
)
from opencortex.memory import MemoryKind


class TestQueryAnchorGroups(unittest.TestCase):
    def test_query_anchor_groups_ignore_probe_candidate_metadata(self):
        plan = RetrievalPlan(
            target_memory_kinds=[MemoryKind.EVENT],
            query_plan=MemoryQueryPlan(
                anchors=[
                    QueryAnchor(
                        kind=QueryAnchorKind.TOPIC,
                        value="support group",
                    ),
                ],
                rewrite_mode=QueryRewriteMode.NONE,
            ),
            search_profile=MemorySearchProfile(
                recall_budget=0.4,
                association_budget=0.0,
                rerank=True,
            ),
            retrieval_depth=RetrievalDepth.L1,
        )
        probe_result = SearchResult(
            should_recall=True,
            query_entities=["Caroline"],
            anchor_hits=["wrong topic"],
            starting_point_anchors=["wrong anchor"],
            starting_points=[
                StartingPoint(
                    uri="opencortex://memory/events/1",
                    session_id="sess-1",
                    entities=["Melanie"],
                    time_refs=["2023-06-09"],
                )
            ],
            scope_level=ScopeLevel.SESSION_ONLY,
            scope_source=ProbeScopeSource.SESSION_ID,
        )

        groups = query_anchor_groups(plan, probe_result)

        self.assertEqual(groups[QueryAnchorKind.TOPIC.value], {"support group"})
        self.assertEqual(groups[QueryAnchorKind.ENTITY.value], {"caroline"})
        self.assertNotIn(QueryAnchorKind.TIME.value, groups)
        self.assertNotIn("wrong topic", groups[QueryAnchorKind.TOPIC.value])
        self.assertNotIn("wrong anchor", groups[QueryAnchorKind.TOPIC.value])
        self.assertNotIn("melanie", groups[QueryAnchorKind.ENTITY.value])

