"""
ACE Phase 2 tests — Reflector, SkillManager, full learn() pipeline, trajectory_end integration.

Uses in-memory mocks (no external binary or network calls needed).
"""

import asyncio
import json
import math
import os
import shutil
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.ace.engine import ACEngine
from opencortex.ace.reflector import Reflector
from opencortex.ace.skill_manager import SkillManager
from opencortex.ace.skillbook import Skillbook
from opencortex.ace.types import (
    HooksStats,
    LearnResult,
    Learning,
    ReflectorOutput,
    Skill,
    SkillTag,
    UpdateOperation,
)
from opencortex.models.embedder.base import DenseEmbedderBase, EmbedResult
from opencortex.storage.viking_fs import VikingFS
from opencortex.storage.vikingdb_interface import (
    CollectionNotFoundError,
    VikingDBInterface,
)


# =============================================================================
# Mock Embedder (same as Phase 1)
# =============================================================================


class MockEmbedder(DenseEmbedderBase):
    DIMENSION = 4

    def __init__(self):
        super().__init__(model_name="mock-embedder-v1")

    def embed(self, text: str) -> EmbedResult:
        vec = self._text_to_vector(text)
        return EmbedResult(dense_vector=vec)

    def get_dimension(self) -> int:
        return self.DIMENSION

    @staticmethod
    def _text_to_vector(text: str) -> List[float]:
        h = hash(text) & 0xFFFFFFFF
        raw = [
            ((h >> 0) & 0xFF) / 255.0,
            ((h >> 8) & 0xFF) / 255.0,
            ((h >> 16) & 0xFF) / 255.0,
            ((h >> 24) & 0xFF) / 255.0,
        ]
        norm = math.sqrt(sum(x * x for x in raw)) or 1.0
        return [x / norm for x in raw]


# =============================================================================
# In-Memory Storage (same as Phase 1)
# =============================================================================


class InMemoryStorage(VikingDBInterface):
    def __init__(self):
        self._collections: Dict[str, Dict[str, Any]] = {}
        self._records: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._closed = False

    async def create_collection(self, name: str, schema: Dict[str, Any]) -> bool:
        if name in self._collections:
            return False
        self._collections[name] = schema
        self._records[name] = {}
        return True

    async def drop_collection(self, name: str) -> bool:
        if name not in self._collections:
            return False
        del self._collections[name]
        del self._records[name]
        return True

    async def collection_exists(self, name: str) -> bool:
        return name in self._collections

    async def list_collections(self) -> List[str]:
        return list(self._collections.keys())

    async def get_collection_info(self, name: str) -> Optional[Dict[str, Any]]:
        if name not in self._collections:
            return None
        return {
            "name": name,
            "vector_dim": self._collections[name].get("vector_dim", 4),
            "count": len(self._records.get(name, {})),
            "status": "ready",
        }

    async def insert(self, collection: str, data: Dict[str, Any]) -> str:
        self._ensure(collection)
        record_id = data.get("id", str(uuid4()))
        data["id"] = record_id
        self._records[collection][record_id] = dict(data)
        return record_id

    async def update(self, collection: str, id: str, data: Dict[str, Any]) -> bool:
        self._ensure(collection)
        if id not in self._records[collection]:
            return False
        self._records[collection][id].update(data)
        return True

    async def upsert(self, collection: str, data: Dict[str, Any]) -> str:
        self._ensure(collection)
        record_id = data.get("id", str(uuid4()))
        data["id"] = record_id
        self._records[collection][record_id] = dict(data)
        return record_id

    async def delete(self, collection: str, ids: List[str]) -> int:
        self._ensure(collection)
        count = 0
        for rid in ids:
            if rid in self._records[collection]:
                del self._records[collection][rid]
                count += 1
        return count

    async def get(self, collection: str, ids: List[str]) -> List[Dict[str, Any]]:
        self._ensure(collection)
        return [
            dict(self._records[collection][rid])
            for rid in ids
            if rid in self._records[collection]
        ]

    async def exists(self, collection: str, id: str) -> bool:
        self._ensure(collection)
        return id in self._records[collection]

    async def batch_insert(self, collection: str, data: List[Dict[str, Any]]) -> List[str]:
        return [await self.insert(collection, d) for d in data]

    async def batch_upsert(self, collection: str, data: List[Dict[str, Any]]) -> List[str]:
        return [await self.upsert(collection, d) for d in data]

    async def batch_delete(self, collection: str, filter: Dict[str, Any]) -> int:
        records = await self.filter(collection, filter, limit=100_000)
        ids = [r["id"] for r in records]
        return await self.delete(collection, ids)

    async def remove_by_uri(self, collection: str, uri: str) -> int:
        self._ensure(collection)
        to_remove = [
            rid
            for rid, rec in self._records[collection].items()
            if rec.get("uri", "").startswith(uri)
        ]
        for rid in to_remove:
            del self._records[collection][rid]
        return len(to_remove)

    async def search(
        self,
        collection: str,
        query_vector: Optional[List[float]] = None,
        sparse_query_vector: Optional[Dict[str, float]] = None,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 10,
        offset: int = 0,
        output_fields: Optional[List[str]] = None,
        with_vector: bool = False,
    ) -> List[Dict[str, Any]]:
        self._ensure(collection)
        candidates = list(self._records[collection].values())
        if filter:
            candidates = [r for r in candidates if self._eval_filter(r, filter)]
        if query_vector:
            scored = []
            for r in candidates:
                vec = r.get("vector")
                score = self._cosine_sim(query_vector, vec) if vec else 0.0
                rec = dict(r)
                rec["_score"] = score
                scored.append(rec)
            scored.sort(key=lambda x: x["_score"], reverse=True)
            candidates = scored
        return candidates[offset : offset + limit]

    async def filter(
        self,
        collection: str,
        filter: Dict[str, Any],
        limit: int = 10,
        offset: int = 0,
        output_fields: Optional[List[str]] = None,
        order_by: Optional[str] = None,
        order_desc: bool = False,
    ) -> List[Dict[str, Any]]:
        self._ensure(collection)
        candidates = [
            dict(r)
            for r in self._records[collection].values()
            if self._eval_filter(r, filter)
        ]
        if order_by:
            candidates.sort(key=lambda r: r.get(order_by, ""), reverse=order_desc)
        return candidates[offset : offset + limit]

    async def scroll(
        self,
        collection: str,
        filter: Optional[Dict[str, Any]] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        offset = int(cursor) if cursor else 0
        records = await self.filter(collection, filter or {}, limit=limit + 1, offset=offset)
        if len(records) > limit:
            return records[:limit], str(offset + limit)
        return records, None

    async def count(self, collection: str, filter: Optional[Dict[str, Any]] = None) -> int:
        self._ensure(collection)
        if filter:
            return len(await self.filter(collection, filter, limit=100_000))
        return len(self._records[collection])

    async def create_index(self, collection: str, field: str, index_type: str, **kw) -> bool:
        return True

    async def drop_index(self, collection: str, field: str) -> bool:
        return True

    async def clear(self, collection: str) -> bool:
        self._ensure(collection)
        self._records[collection].clear()
        return True

    async def optimize(self, collection: str) -> bool:
        return True

    async def close(self) -> None:
        self._closed = True

    async def health_check(self) -> bool:
        return not self._closed

    async def get_stats(self) -> Dict[str, Any]:
        total = sum(len(recs) for recs in self._records.values())
        return {
            "collections": len(self._collections),
            "total_records": total,
            "storage_size": 0,
            "backend": "in-memory",
        }

    def _ensure(self, collection: str) -> None:
        if collection not in self._collections:
            raise CollectionNotFoundError(f"Collection '{collection}' does not exist")

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _eval_filter(self, record: Dict[str, Any], filt: Dict[str, Any]) -> bool:
        if not filt:
            return True
        op = filt.get("op", "")
        if op == "must":
            field_name = filt.get("field", "")
            conds = filt.get("conds", [])
            val = record.get(field_name)
            return val in conds
        elif op == "prefix":
            field_name = filt.get("field", "")
            prefix = filt.get("prefix", "")
            val = record.get(field_name, "")
            return str(val).startswith(prefix)
        elif op == "range":
            field_name = filt.get("field", "")
            val = record.get(field_name, 0)
            if "gte" in filt and val < filt["gte"]:
                return False
            if "gt" in filt and val <= filt["gt"]:
                return False
            if "lte" in filt and val > filt["lte"]:
                return False
            if "lt" in filt and val >= filt["lt"]:
                return False
            return True
        elif op == "and":
            return all(self._eval_filter(record, c) for c in filt.get("conds", []))
        elif op == "or":
            return any(self._eval_filter(record, c) for c in filt.get("conds", []))
        return True


# =============================================================================
# Test Helpers
# =============================================================================


def _run(coro):
    return asyncio.run(coro)


# Mock LLM response builders
def _reflector_success_response():
    return json.dumps({
        "reasoning": "The agent used async/await correctly for IO operations",
        "error_identification": "none",
        "root_cause_analysis": "Good pattern matching with existing async patterns",
        "key_insight": "Async/await is the correct pattern for IO-bound operations",
        "extracted_learnings": [
            {
                "learning": "Use async/await for all IO-bound operations",
                "evidence": "Agent correctly used await for file read",
                "justification": "IO operations block the event loop without await",
            }
        ],
        "skill_tags": [
            {"skill_id": "strat-00001", "tag": "helpful"},
        ],
    })


def _reflector_failure_response():
    return json.dumps({
        "reasoning": "The agent used synchronous calls for database access",
        "error_identification": "Used blocking DB calls in async context",
        "root_cause_analysis": "WRONG_STRATEGY: should have used async DB driver",
        "key_insight": "Never use synchronous DB calls in async handlers",
        "extracted_learnings": [
            {
                "learning": "Use async database drivers in async handlers",
                "evidence": "Synchronous psycopg2 blocked the event loop for 3 seconds",
                "justification": "Blocking calls defeat the purpose of async architecture",
            }
        ],
        "skill_tags": [
            {"skill_id": "strat-00001", "tag": "harmful"},
        ],
    })


def _skill_manager_add_response():
    return json.dumps([
        {
            "type": "ADD",
            "section": "strategies",
            "content": "Use async database drivers in async handlers",
            "justification": "Prevents event loop blocking",
            "evidence": "Synchronous psycopg2 blocked the event loop",
        }
    ])


def _skill_manager_empty_response():
    return json.dumps([])


# =============================================================================
# TestReflector
# =============================================================================


class TestReflector(unittest.TestCase):
    """Test Reflector LLM analysis and parsing."""

    def test_01_reflect_success(self):
        """Reflector parses a success case with learning + skill_tags."""
        async def mock_llm(prompt: str) -> str:
            return _reflector_success_response()

        reflector = Reflector(llm_completion=mock_llm)
        output = _run(reflector.reflect(
            question="How to read a file?",
            reasoning="Use async file IO",
            answer="Used aiofiles.open()",
            feedback="Success: file read correctly",
        ))

        self.assertIsInstance(output, ReflectorOutput)
        self.assertEqual(output.error_identification, "none")
        self.assertEqual(len(output.extracted_learnings), 1)
        self.assertEqual(output.extracted_learnings[0].learning, "Use async/await for all IO-bound operations")
        self.assertEqual(len(output.skill_tags), 1)
        self.assertEqual(output.skill_tags[0].tag, "helpful")

    def test_02_reflect_failure_case(self):
        """Reflector parses WRONG_STRATEGY + harmful tag."""
        async def mock_llm(prompt: str) -> str:
            return _reflector_failure_response()

        reflector = Reflector(llm_completion=mock_llm)
        output = _run(reflector.reflect(
            question="Query the database",
            reasoning="Used psycopg2 directly",
            answer="cursor.execute(query)",
            feedback="Failure: event loop blocked",
        ))

        self.assertIn("WRONG_STRATEGY", output.root_cause_analysis)
        self.assertEqual(output.skill_tags[0].tag, "harmful")
        self.assertEqual(len(output.extracted_learnings), 1)

    def test_03_reflect_parse_error_graceful(self):
        """Unparseable response returns degraded output."""
        async def mock_llm(prompt: str) -> str:
            return "This is not valid JSON at all!!!"

        reflector = Reflector(llm_completion=mock_llm)
        output = _run(reflector.reflect(
            question="test", reasoning="test", answer="test", feedback="test",
        ))

        self.assertIsInstance(output, ReflectorOutput)
        self.assertEqual(output.reasoning, "parse_error")
        self.assertEqual(len(output.extracted_learnings), 0)
        self.assertEqual(len(output.skill_tags), 0)

    def test_04_reflect_with_skills_context(self):
        """Reflecting with skills context doesn't crash."""
        async def mock_llm(prompt: str) -> str:
            # Verify skills appear in prompt
            assert "strat-00001" in prompt
            return _reflector_success_response()

        skills = [
            Skill(id="strat-00001", section="strategies", content="Use type hints"),
        ]
        reflector = Reflector(llm_completion=mock_llm)
        output = _run(reflector.reflect(
            question="test", reasoning="", answer="", feedback="ok",
            skills=skills,
        ))

        self.assertIsInstance(output, ReflectorOutput)

    def test_05_reflect_json_in_code_block(self):
        """Handles ```json ... ``` wrapped response."""
        async def mock_llm(prompt: str) -> str:
            inner = _reflector_success_response()
            return f"Here is my analysis:\n```json\n{inner}\n```\nDone."

        reflector = Reflector(llm_completion=mock_llm)
        output = _run(reflector.reflect(
            question="test", reasoning="", answer="", feedback="ok",
        ))

        self.assertEqual(len(output.extracted_learnings), 1)
        self.assertEqual(output.error_identification, "none")

    def test_06_reflect_skips_invalid_learnings(self):
        """Skips learnings without evidence."""
        async def mock_llm(prompt: str) -> str:
            return json.dumps({
                "reasoning": "analysis",
                "error_identification": "none",
                "root_cause_analysis": "root cause",
                "key_insight": "insight",
                "extracted_learnings": [
                    {"learning": "Valid learning", "evidence": "Has evidence", "justification": "ok"},
                    {"learning": "No evidence learning", "evidence": "", "justification": "bad"},
                    {"learning": "", "evidence": "No learning text", "justification": "bad"},
                ],
                "skill_tags": [
                    {"skill_id": "strat-00001", "tag": "helpful"},
                    {"skill_id": "strat-00002", "tag": "invalid_tag"},  # invalid
                ],
            })

        reflector = Reflector(llm_completion=mock_llm)
        output = _run(reflector.reflect(
            question="test", reasoning="", answer="", feedback="ok",
        ))

        self.assertEqual(len(output.extracted_learnings), 1)
        self.assertEqual(output.extracted_learnings[0].learning, "Valid learning")
        # Only the valid tag
        self.assertEqual(len(output.skill_tags), 1)
        self.assertEqual(output.skill_tags[0].skill_id, "strat-00001")


# =============================================================================
# TestSkillManager
# =============================================================================


class TestSkillManager(unittest.TestCase):
    """Test SkillManager decision-making."""

    def test_07_decide_with_learnings(self):
        """TAG + ADD operations generated from learnings."""
        async def mock_llm(prompt: str) -> str:
            return _skill_manager_add_response()

        reflection = ReflectorOutput(
            reasoning="analysis",
            error_identification="error found",
            root_cause_analysis="wrong driver",
            key_insight="use async drivers",
            extracted_learnings=[
                Learning(
                    learning="Use async DB drivers",
                    evidence="Blocking call observed",
                    justification="Event loop blocking",
                ),
            ],
            skill_tags=[
                SkillTag(skill_id="strat-00001", tag="harmful"),
            ],
        )

        manager = SkillManager(llm_completion=mock_llm)
        ops = _run(manager.decide(
            reflection=reflection,
            skillbook_state="ID\tSection\tContent\tHelpful\tHarmful\nstrat-00001\tstrategies\tOld skill\t0\t0",
            context="Question: test\nFeedback: failure",
        ))

        # Should have TAG + ADD
        tag_ops = [op for op in ops if op.type == "TAG"]
        add_ops = [op for op in ops if op.type == "ADD"]
        self.assertGreaterEqual(len(tag_ops), 1)
        self.assertEqual(len(add_ops), 1)
        self.assertEqual(add_ops[0].content, "Use async database drivers in async handlers")

    def test_08_decide_no_learnings_tags_only(self):
        """No learnings → only TAG ops, no LLM call."""
        llm_called = []

        async def mock_llm(prompt: str) -> str:
            llm_called.append(True)
            return "[]"

        reflection = ReflectorOutput(
            reasoning="analysis",
            error_identification="none",
            root_cause_analysis="all good",
            key_insight="no issues",
            extracted_learnings=[],  # empty
            skill_tags=[
                SkillTag(skill_id="strat-00001", tag="helpful"),
            ],
        )

        manager = SkillManager(llm_completion=mock_llm)
        ops = _run(manager.decide(
            reflection=reflection,
            skillbook_state="",
            context="",
        ))

        # LLM should NOT have been called
        self.assertEqual(len(llm_called), 0)
        # Only TAG ops
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].type, "TAG")

    def test_09_decide_parse_failure_graceful(self):
        """Parse failure returns TAG ops only."""
        async def mock_llm(prompt: str) -> str:
            return "Not valid JSON response"

        reflection = ReflectorOutput(
            reasoning="analysis",
            error_identification="none",
            root_cause_analysis="ok",
            key_insight="insight",
            extracted_learnings=[
                Learning(learning="test", evidence="test", justification="test"),
            ],
            skill_tags=[
                SkillTag(skill_id="strat-00001", tag="neutral"),
            ],
        )

        manager = SkillManager(llm_completion=mock_llm)
        ops = _run(manager.decide(
            reflection=reflection,
            skillbook_state="",
            context="",
        ))

        # Should still have TAG ops
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].type, "TAG")

    def test_10_decide_validates_operations(self):
        """Skips invalid operations (ADD without content, UPDATE without skill_id)."""
        async def mock_llm(prompt: str) -> str:
            return json.dumps([
                {"type": "ADD", "section": "strategies"},  # no content
                {"type": "UPDATE", "section": "strategies", "content": "new"},  # no skill_id
                {"type": "ADD", "section": "strategies", "content": "Valid skill"},  # valid
            ])

        reflection = ReflectorOutput(
            reasoning="",
            error_identification="none",
            root_cause_analysis="",
            key_insight="",
            extracted_learnings=[
                Learning(learning="x", evidence="y", justification="z"),
            ],
        )

        manager = SkillManager(llm_completion=mock_llm)
        ops = _run(manager.decide(
            reflection=reflection,
            skillbook_state="",
            context="",
        ))

        # Only the valid ADD should pass
        add_ops = [op for op in ops if op.type == "ADD"]
        self.assertEqual(len(add_ops), 1)
        self.assertEqual(add_ops[0].content, "Valid skill")


# =============================================================================
# TestLearnPipeline
# =============================================================================


class TestLearnPipeline(unittest.TestCase):
    """Test the full learn() pipeline in ACEngine."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_learn_test_")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()
        self.fs = VikingFS(data_root=self.temp_dir, vector_store=self.storage)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_engine(self, llm_fn=None):
        engine = ACEngine(
            storage=self.storage,
            embedder=self.embedder,
            viking_fs=self.fs,
            llm_fn=llm_fn,
            tenant_id="test",
            user_id="alice",
        )
        _run(engine.init())
        return engine

    def test_11_learn_full_pipeline(self):
        """Full Reflector → SkillManager → Apply pipeline."""
        call_count = []

        async def mock_llm(prompt: str) -> str:
            call_count.append(1)
            if len(call_count) == 1:
                # Reflector call
                return _reflector_success_response()
            else:
                # SkillManager call
                return _skill_manager_add_response()

        engine = self._make_engine(llm_fn=mock_llm)

        # Add a seed skill so TAG has something to tag
        _run(engine.remember(content="Use async patterns", memory_type="strategies"))

        state = "How to read a file?|||Use async IO|||aiofiles.open()|||Success"
        result = _run(engine.learn(state=state, action="aiofiles.open()", reward=1.0))

        self.assertIsInstance(result, LearnResult)
        self.assertTrue(result.success)
        self.assertGreater(result.operations_applied, 0)
        self.assertIn("full:", result.message)
        self.assertTrue(result.reflection_key_insight)

    def test_12_learn_no_llm_degrades(self):
        """Without LLM, learn degrades to simple TAG-based learning."""
        engine = self._make_engine(llm_fn=None)

        # Add a skill to be tagged
        _run(engine.remember(content="Test skill", memory_type="general"))

        result = _run(engine.learn(state="test query", action="test", reward=1.0))

        self.assertIsInstance(result, LearnResult)
        self.assertTrue(result.success)
        self.assertIn("simple:", result.message)
        self.assertIn("helpful", result.message)

    def test_13_learn_plain_state_parsing(self):
        """Plain text state (no |||) parses correctly."""
        engine = self._make_engine(llm_fn=None)

        result = _run(engine.learn(state="plain text question", action="answer", reward=-1.0))

        self.assertTrue(result.success)
        self.assertIn("harmful", result.message)

    def test_14_learn_existing_stub_compat(self):
        """LearnResult still has success, best_action, message (backward compat)."""
        engine = self._make_engine(llm_fn=None)

        result = _run(engine.learn(state="test", action="act", reward=0.0))

        self.assertTrue(hasattr(result, "success"))
        self.assertTrue(hasattr(result, "best_action"))
        self.assertTrue(hasattr(result, "message"))
        self.assertTrue(hasattr(result, "operations_applied"))
        self.assertTrue(hasattr(result, "reflection_key_insight"))


# =============================================================================
# TestTrajectoryLearn
# =============================================================================


class TestTrajectoryLearn(unittest.TestCase):
    """Test trajectory_end triggering learn."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="ace_traj_test_")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()
        self.fs = VikingFS(data_root=self.temp_dir, vector_store=self.storage)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _make_engine(self, llm_fn=None):
        engine = ACEngine(
            storage=self.storage,
            embedder=self.embedder,
            viking_fs=self.fs,
            llm_fn=llm_fn,
            tenant_id="test",
            user_id="alice",
        )
        _run(engine.init())
        return engine

    def test_15_trajectory_end_triggers_learn(self):
        """trajectory_end with steps triggers learn and includes learn_result."""
        engine = self._make_engine(llm_fn=None)

        _run(engine.trajectory_begin("t1", "initial task"))
        _run(engine.trajectory_step("t1", "action1", 0.5))
        _run(engine.trajectory_step("t1", "action2", 0.8))

        result = _run(engine.trajectory_end("t1", quality_score=0.9))

        self.assertEqual(result["trajectory_id"], "t1")
        self.assertEqual(result["steps"], 2)
        self.assertIn("learn_result", result)
        self.assertTrue(result["learn_result"]["success"])

    def test_16_trajectory_end_no_steps_no_learn(self):
        """trajectory_end without steps does NOT trigger learn."""
        engine = self._make_engine(llm_fn=None)

        _run(engine.trajectory_begin("t2", "initial task"))

        result = _run(engine.trajectory_end("t2", quality_score=0.5))

        self.assertEqual(result["steps"], 0)
        self.assertNotIn("learn_result", result)


# =============================================================================
# TestNewTypes
# =============================================================================


class TestNewTypes(unittest.TestCase):
    """Test new data type construction."""

    def test_17_learning_creation(self):
        """Learning dataclass creates correctly."""
        l = Learning(
            learning="Use retry with backoff",
            evidence="Network timeout in production",
            justification="Transient errors are common in distributed systems",
        )
        self.assertEqual(l.learning, "Use retry with backoff")
        self.assertEqual(l.evidence, "Network timeout in production")
        self.assertTrue(l.justification)

    def test_18_skill_tag_creation(self):
        """SkillTag dataclass creates correctly."""
        st = SkillTag(skill_id="strat-00001", tag="helpful")
        self.assertEqual(st.skill_id, "strat-00001")
        self.assertEqual(st.tag, "helpful")

    def test_19_reflector_output_creation(self):
        """ReflectorOutput creates with defaults."""
        ro = ReflectorOutput(
            reasoning="test",
            error_identification="none",
            root_cause_analysis="ok",
            key_insight="insight",
        )
        self.assertEqual(len(ro.extracted_learnings), 0)
        self.assertEqual(len(ro.skill_tags), 0)
        self.assertEqual(ro.reasoning, "test")

    def test_20_learn_result_new_fields(self):
        """LearnResult has new fields with defaults."""
        lr = LearnResult()
        self.assertTrue(lr.success)
        self.assertEqual(lr.best_action, "")
        self.assertEqual(lr.message, "")
        self.assertEqual(lr.operations_applied, 0)
        self.assertEqual(lr.reflection_key_insight, "")

        # With new fields set
        lr2 = LearnResult(
            success=True,
            best_action="act",
            message="msg",
            operations_applied=5,
            reflection_key_insight="key insight",
        )
        self.assertEqual(lr2.operations_applied, 5)
        self.assertEqual(lr2.reflection_key_insight, "key insight")


if __name__ == "__main__":
    unittest.main(verbosity=2)
