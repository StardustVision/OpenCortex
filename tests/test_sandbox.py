import json
import unittest
from unittest.mock import AsyncMock
from opencortex.alpha.sandbox import stat_gate, llm_verify, evaluate, GateResult


class TestStatGate(unittest.TestCase):

    def _make_traces(self, count, success_count, users=None):
        """Helper to create trace dicts."""
        traces = []
        for i in range(count):
            outcome = "success" if i < success_count else "failure"
            user = users[i] if users else f"user_{i}"
            traces.append({
                "trace_id": f"t{i}",
                "outcome": outcome,
                "user_id": user,
            })
        return traces

    def test_passes_when_enough_traces(self):
        """3 traces, 80% success rate -> pass."""
        traces = self._make_traces(5, 4, users=["u1", "u2", "u1", "u2", "u1"])
        result = stat_gate(
            {"scope": "tenant"},
            traces,
            min_traces=3,
            min_success_rate=0.7,
            min_source_users=2,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.trace_count, 5)
        self.assertAlmostEqual(result.success_rate, 0.8)
        self.assertEqual(result.unique_users, 2)

    def test_fails_insufficient_traces(self):
        """1 trace -> fail."""
        traces = self._make_traces(1, 1)
        result = stat_gate(
            {"scope": "user"},
            traces,
            min_traces=3,
        )
        self.assertFalse(result.passed)
        self.assertIn("Insufficient traces", result.reason)

    def test_fails_low_success_rate(self):
        """5 traces, 20% success -> fail."""
        traces = self._make_traces(5, 1, users=["u1", "u2", "u3", "u4", "u5"])
        result = stat_gate(
            {"scope": "user"},
            traces,
            min_traces=3,
            min_success_rate=0.7,
        )
        self.assertFalse(result.passed)
        self.assertIn("Low success rate", result.reason)

    def test_user_scope_lower_min_users(self):
        """User scope needs only 1 source user."""
        traces = self._make_traces(3, 3, users=["hugo", "hugo", "hugo"])
        result = stat_gate(
            {"scope": "user"},
            traces,
            min_traces=3,
            min_success_rate=0.7,
            min_source_users=2,
            min_source_users_private=1,
        )
        self.assertTrue(result.passed)

    def test_tenant_scope_requires_multiple_users(self):
        """Tenant scope needs 2+ source users."""
        traces = self._make_traces(3, 3, users=["hugo", "hugo", "hugo"])
        result = stat_gate(
            {"scope": "tenant"},
            traces,
            min_traces=3,
            min_success_rate=0.7,
            min_source_users=2,
        )
        self.assertFalse(result.passed)
        self.assertIn("user diversity", result.reason)

    def test_empty_traces(self):
        """No traces -> fail."""
        result = stat_gate({"scope": "user"}, [], min_traces=3)
        self.assertFalse(result.passed)

    def test_gate_result_dataclass(self):
        """GateResult fields."""
        r = GateResult(passed=True, reason="ok", trace_count=5, success_rate=0.9, unique_users=3)
        self.assertTrue(r.passed)
        self.assertEqual(r.trace_count, 5)


class TestLLMVerify(unittest.IsolatedAsyncioTestCase):

    def _make_knowledge(self):
        return {
            "knowledge_type": "sop",
            "statement": "Always check spelling",
            "objective": "Fix import errors",
            "action_steps": ["Check spelling", "pip install"],
        }

    def _make_traces(self, count):
        return [{"trace_id": f"t{i}", "abstract": f"trace {i}"} for i in range(count)]

    async def test_llm_verify_passes(self):
        """LLM says all traces would improve -> pass."""
        async def mock_llm(prompt):
            return json.dumps({"improved": True, "reason": "would help"})

        result = await llm_verify(
            self._make_knowledge(),
            self._make_traces(3),
            mock_llm,
            min_pass_rate=0.6,
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.pass_rate, 1.0)
        self.assertEqual(result.sample_size, 3)

    async def test_llm_verify_fails(self):
        """LLM says no improvement -> fail."""
        async def mock_llm(prompt):
            return json.dumps({"improved": False, "reason": "no help"})

        result = await llm_verify(
            self._make_knowledge(),
            self._make_traces(3),
            mock_llm,
            min_pass_rate=0.6,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.pass_rate, 0.0)

    async def test_llm_verify_no_traces(self):
        """No traces -> fail."""
        async def mock_llm(prompt):
            return json.dumps({"improved": True, "reason": "ok"})

        result = await llm_verify(self._make_knowledge(), [], mock_llm)
        self.assertFalse(result.passed)

    async def test_llm_verify_handles_bad_json(self):
        """LLM returns invalid JSON -> handled gracefully."""
        async def mock_llm(prompt):
            return "not json"

        result = await llm_verify(
            self._make_knowledge(),
            self._make_traces(2),
            mock_llm,
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.pass_rate, 0.0)


class TestEvaluate(unittest.IsolatedAsyncioTestCase):

    def _make_traces(self, count, success_count, users=None):
        traces = []
        for i in range(count):
            outcome = "success" if i < success_count else "failure"
            user = users[i] if users else f"user_{i}"
            traces.append({
                "trace_id": f"t{i}",
                "outcome": outcome,
                "user_id": user,
                "abstract": f"trace {i}",
            })
        return traces

    async def test_evaluate_needs_more_traces(self):
        """Too few traces -> needs_more_traces."""
        result = await evaluate(
            {"scope": "user"}, [], min_traces=3,
        )
        self.assertEqual(result.status, "needs_more_traces")

    async def test_evaluate_auto_approve_user_high_confidence(self):
        """User scope + high confidence -> auto-approve."""
        traces = self._make_traces(3, 3, users=["hugo", "hugo", "hugo"])

        async def mock_llm(prompt):
            return json.dumps({"improved": True, "reason": "helps"})

        result = await evaluate(
            {"scope": "user", "confidence": 0.98},
            traces,
            llm_fn=mock_llm,
            min_traces=3,
            min_source_users_private=1,
        )
        self.assertEqual(result.status, "active")

    async def test_evaluate_verified_awaiting_human(self):
        """Tenant scope -> verified, awaiting human."""
        traces = self._make_traces(5, 4, users=["u1", "u2", "u1", "u2", "u1"])

        async def mock_llm(prompt):
            return json.dumps({"improved": True, "reason": "helps"})

        result = await evaluate(
            {"scope": "tenant", "confidence": 0.5},
            traces,
            llm_fn=mock_llm,
            min_traces=3,
            min_source_users=2,
        )
        self.assertEqual(result.status, "verified")

    async def test_evaluate_no_llm_fn(self):
        """No LLM function -> skip LLM stage."""
        traces = self._make_traces(3, 3, users=["hugo", "hugo", "hugo"])
        result = await evaluate(
            {"scope": "user", "confidence": 0.98},
            traces,
            llm_fn=None,
            min_traces=3,
            min_source_users_private=1,
        )
        self.assertEqual(result.status, "active")
        self.assertIsNone(result.llm_verify)

    async def test_evaluate_llm_fails_needs_improvement(self):
        """LLM says no improvement -> needs_improvement."""
        traces = self._make_traces(3, 3, users=["hugo", "hugo", "hugo"])

        async def mock_llm(prompt):
            return json.dumps({"improved": False, "reason": "no"})

        result = await evaluate(
            {"scope": "user"},
            traces,
            llm_fn=mock_llm,
            min_traces=3,
            min_source_users_private=1,
        )
        self.assertEqual(result.status, "needs_improvement")


if __name__ == "__main__":
    unittest.main()
