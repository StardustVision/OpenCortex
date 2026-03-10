import unittest
from opencortex.alpha.types import Turn, Trace, TraceOutcome, TurnStatus
from opencortex.alpha.types import (
    Knowledge, KnowledgeType, KnowledgeStatus, KnowledgeScope,
    SEARCHABLE_STATUSES,
)


class TestTraceTypes(unittest.TestCase):

    def test_turn_minimal(self):
        """Turn with only required fields."""
        t = Turn(turn_id="t1")
        self.assertEqual(t.turn_id, "t1")
        self.assertIsNone(t.prompt_text)
        self.assertIsNone(t.final_text)
        self.assertEqual(t.turn_status, TurnStatus.COMPLETE)
        self.assertEqual(t.tool_calls, [])

    def test_turn_full(self):
        """Turn with all fields populated."""
        t = Turn(
            turn_id="t1",
            prompt_text="fix the bug",
            thought_text="let me check",
            tool_calls=[{"tool_name": "read", "tool_args": "f.py", "tool_result": "ok"}],
            final_text="done",
            turn_status=TurnStatus.COMPLETE,
            latency_ms=120,
            token_count=500,
        )
        self.assertEqual(t.prompt_text, "fix the bug")
        self.assertEqual(len(t.tool_calls), 1)

    def test_trace_minimal(self):
        """Trace with only required fields."""
        tr = Trace(
            trace_id="tr1",
            session_id="s1",
            tenant_id="team",
            user_id="hugo",
            source="claude_code",
            turns=[Turn(turn_id="t1")],
        )
        self.assertEqual(tr.trace_id, "tr1")
        self.assertIsNone(tr.task_type)
        self.assertIsNone(tr.outcome)
        self.assertFalse(tr.training_ready)

    def test_trace_outcome_enum(self):
        self.assertEqual(TraceOutcome.SUCCESS, "success")
        self.assertEqual(TraceOutcome.FAILURE, "failure")
        self.assertEqual(TraceOutcome.TIMEOUT, "timeout")
        self.assertEqual(TraceOutcome.CANCELLED, "cancelled")

    def test_trace_to_dict_roundtrip(self):
        tr = Trace(
            trace_id="tr1", session_id="s1", tenant_id="t",
            user_id="u", source="agno",
            turns=[Turn(turn_id="t1", prompt_text="hello")],
            outcome=TraceOutcome.SUCCESS,
        )
        d = tr.to_dict()
        self.assertEqual(d["trace_id"], "tr1")
        self.assertEqual(d["turns"][0]["prompt_text"], "hello")
        self.assertIsInstance(d["created_at"], str)


class TestKnowledgeTypes(unittest.TestCase):

    def test_belief(self):
        k = Knowledge(
            knowledge_id="k1",
            knowledge_type=KnowledgeType.BELIEF,
            tenant_id="team",
            user_id="hugo",
            scope=KnowledgeScope.USER,
            statement="Always check spelling before installing packages",
            objective="import error handling",
        )
        self.assertEqual(k.status, KnowledgeStatus.CANDIDATE)
        self.assertIsNone(k.confidence)

    def test_sop(self):
        k = Knowledge(
            knowledge_id="k2",
            knowledge_type=KnowledgeType.SOP,
            tenant_id="team",
            user_id="hugo",
            scope=KnowledgeScope.TENANT,
            objective="Fix import errors",
            action_steps=["Check spelling", "Check venv", "pip install", "Verify"],
            trigger_keywords=["import", "ModuleNotFoundError"],
        )
        self.assertEqual(len(k.action_steps), 4)
        self.assertEqual(len(k.trigger_keywords), 2)

    def test_negative_rule(self):
        k = Knowledge(
            knowledge_id="k3",
            knowledge_type=KnowledgeType.NEGATIVE_RULE,
            tenant_id="team",
            user_id="hugo",
            scope=KnowledgeScope.USER,
            statement="Never blindly pip install without checking spelling first",
            severity="high",
        )
        self.assertEqual(k.severity, "high")

    def test_root_cause(self):
        k = Knowledge(
            knowledge_id="k4",
            knowledge_type=KnowledgeType.ROOT_CAUSE,
            tenant_id="team",
            user_id="hugo",
            scope=KnowledgeScope.USER,
            error_pattern="ModuleNotFoundError: No module named 'foo'",
            cause="Virtual environment not activated",
            fix_suggestion="Run 'source .venv/bin/activate'",
        )
        self.assertEqual(k.cause, "Virtual environment not activated")

    def test_knowledge_to_dict(self):
        k = Knowledge(
            knowledge_id="k1",
            knowledge_type=KnowledgeType.BELIEF,
            tenant_id="t", user_id="u",
            scope=KnowledgeScope.USER,
            statement="test",
        )
        d = k.to_dict()
        self.assertEqual(d["knowledge_type"], "belief")
        self.assertEqual(d["status"], "candidate")
        self.assertNotIn("action_steps", d)  # None fields excluded

    def test_only_active_searchable(self):
        """Verify SEARCHABLE_STATUSES constant."""
        self.assertEqual(SEARCHABLE_STATUSES, {KnowledgeStatus.ACTIVE})


if __name__ == "__main__":
    unittest.main()
