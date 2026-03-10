import unittest
from opencortex.alpha.sandbox import stat_gate, GateResult


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


if __name__ == "__main__":
    unittest.main()
