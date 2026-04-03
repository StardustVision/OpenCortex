import unittest
import asyncio
from opencortex.alpha.types import (
    Knowledge, KnowledgeType, KnowledgeStatus, KnowledgeScope,
)
from opencortex.alpha.sandbox import stat_gate, evaluate, GateResult, EvalResult


class TestSandboxStatGate(unittest.TestCase):
    """Verify stat_gate with realistic knowledge + trace data."""

    def _make_knowledge_dict(self, scope="user"):
        return Knowledge(
            knowledge_id="k1",
            knowledge_type=KnowledgeType.SOP,
            tenant_id="team",
            user_id="hugo",
            scope=KnowledgeScope(scope),
            statement="Always run tests before deploy",
            source_trace_ids=["tr1", "tr2", "tr3"],
        ).to_dict()

    def _make_traces(self, count=3, success_count=3, users=None):
        users = users or ["hugo"]
        traces = []
        for i in range(count):
            traces.append({
                "trace_id": f"tr{i+1}",
                "outcome": "success" if i < success_count else "failure",
                "user_id": users[i % len(users)],
            })
        return traces

    def test_stat_gate_passes_with_sufficient_evidence(self):
        """3 traces, 100% success, 1 user (user scope) -> PASS."""
        k = self._make_knowledge_dict(scope="user")
        traces = self._make_traces(count=3, success_count=3)
        result = stat_gate(k, traces, min_source_users_private=1)
        self.assertTrue(result.passed)
        self.assertEqual(result.trace_count, 3)
        self.assertEqual(result.success_rate, 1.0)

    def test_stat_gate_fails_insufficient_traces(self):
        """2 traces < min_traces=3 -> FAIL."""
        k = self._make_knowledge_dict()
        traces = self._make_traces(count=2)
        result = stat_gate(k, traces)
        self.assertFalse(result.passed)
        self.assertIn("Insufficient traces", result.reason)

    def test_stat_gate_fails_low_success_rate(self):
        """1/3 success = 33% < 70% -> FAIL."""
        k = self._make_knowledge_dict()
        traces = self._make_traces(count=3, success_count=1)
        result = stat_gate(k, traces)
        self.assertFalse(result.passed)
        self.assertIn("Low success rate", result.reason)

    def test_stat_gate_fails_user_diversity_for_tenant_scope(self):
        """Tenant scope requires 2 users, only 1 -> FAIL."""
        k = self._make_knowledge_dict(scope="tenant")
        traces = self._make_traces(count=3, success_count=3, users=["hugo"])
        result = stat_gate(k, traces, min_source_users=2)
        self.assertFalse(result.passed)
        self.assertIn("Insufficient user diversity", result.reason)


class TestSandboxEvaluate(unittest.TestCase):
    """Verify full evaluate() pipeline end-to-end."""

    def _make_knowledge_dict(self):
        return Knowledge(
            knowledge_id="k1",
            knowledge_type=KnowledgeType.SOP,
            tenant_id="team", user_id="hugo",
            scope=KnowledgeScope.USER,
            statement="Always run tests",
            confidence=0.96,
            source_trace_ids=["tr1", "tr2", "tr3"],
        ).to_dict()

    def _make_traces(self):
        return [
            {"trace_id": f"tr{i+1}", "outcome": "success",
             "user_id": "hugo", "abstract": f"Task {i+1}"}
            for i in range(3)
        ]

    def test_evaluate_auto_approve_high_confidence_user_scope(self):
        """user scope + confidence >= 0.95 -> active (auto-approve)."""
        async def _run():
            async def mock_llm(prompt):
                return '{"improved": true, "reason": "yes"}'
            result = await evaluate(
                self._make_knowledge_dict(),
                self._make_traces(),
                llm_fn=mock_llm,
                min_traces=3,
                min_source_users_private=1,
                user_auto_approve_confidence=0.95,
            )
            self.assertEqual(result.status, "active")
        asyncio.get_event_loop().run_until_complete(_run())

    def test_evaluate_needs_more_traces(self):
        """Insufficient traces -> needs_more_traces."""
        async def _run():
            result = await evaluate(
                self._make_knowledge_dict(),
                [{"trace_id": "tr1", "outcome": "success", "user_id": "hugo"}],
                min_traces=3,
            )
            self.assertEqual(result.status, "needs_more_traces")
        asyncio.get_event_loop().run_until_complete(_run())

    def test_evaluate_needs_improvement_low_llm_pass_rate(self):
        """LLM says not improved -> needs_improvement."""
        async def _run():
            async def mock_llm(prompt):
                return '{"improved": false, "reason": "no help"}'
            result = await evaluate(
                self._make_knowledge_dict(),
                self._make_traces(),
                llm_fn=mock_llm,
                min_traces=3,
                min_source_users_private=1,
                llm_min_pass_rate=0.6,
            )
            self.assertEqual(result.status, "needs_improvement")
        asyncio.get_event_loop().run_until_complete(_run())

    def test_evaluate_verified_when_human_approval_required(self):
        """Passes gates but human approval required -> verified."""
        async def _run():
            async def mock_llm(prompt):
                return '{"improved": true, "reason": "yes"}'
            kd = self._make_knowledge_dict()
            kd["confidence"] = 0.5  # Below auto-approve threshold
            result = await evaluate(
                kd,
                self._make_traces(),
                llm_fn=mock_llm,
                min_traces=3,
                min_source_users_private=1,
                require_human_approval=True,
                user_auto_approve_confidence=0.95,
            )
            self.assertEqual(result.status, "verified")
        asyncio.get_event_loop().run_until_complete(_run())


if __name__ == "__main__":
    unittest.main()
