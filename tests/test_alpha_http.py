"""Tests for Cortex Alpha HTTP endpoints — model validation only."""
import unittest
from opencortex.http.models import (
    SessionMessagesRequest,
    KnowledgeSearchRequest,
    KnowledgeApproveRequest,
    KnowledgeRejectRequest,
)


class TestAlphaModels(unittest.TestCase):

    def test_session_messages_request(self):
        req = SessionMessagesRequest(
            session_id="s1",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )
        self.assertEqual(req.session_id, "s1")
        self.assertEqual(len(req.messages), 2)

    def test_knowledge_search_request(self):
        req = KnowledgeSearchRequest(query="import error")
        self.assertEqual(req.query, "import error")
        self.assertEqual(req.limit, 10)
        self.assertIsNone(req.types)

    def test_knowledge_search_with_types(self):
        req = KnowledgeSearchRequest(
            query="fix", types=["belief", "sop"], limit=5,
        )
        self.assertEqual(req.types, ["belief", "sop"])
        self.assertEqual(req.limit, 5)

    def test_knowledge_approve_request(self):
        req = KnowledgeApproveRequest(knowledge_id="k1")
        self.assertEqual(req.knowledge_id, "k1")

    def test_knowledge_reject_request(self):
        req = KnowledgeRejectRequest(knowledge_id="k2")
        self.assertEqual(req.knowledge_id, "k2")


if __name__ == "__main__":
    unittest.main()
