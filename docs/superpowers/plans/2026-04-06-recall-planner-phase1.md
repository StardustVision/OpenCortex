# Recall Planner Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract explicit recall planning from the current `orchestrator + context manager + IntentRouter` flow so OpenCortex has a concrete `RecallPlan` seam before the larger Autophagy cognitive-kernel refactor.

**Architecture:** Keep existing retrieval behavior intact while introducing a new internal planning layer. `IntentRouter` remains the intent analyzer, `MemoryOrchestrator.plan_recall()` becomes the only legal entry point for turning request inputs into `SearchIntent + RecallPlan`, and `ContextManager._prepare()` consumes that plan instead of re-implementing routing, detail selection, and recall-surface decisions.

**Tech Stack:** Python 3.10+, dataclasses, existing OpenCortex retrieval stack (`IntentRouter`, `SearchIntent`, `FindResult`), FastAPI HTTP server, unittest-based tests

---

## Scope Note

The approved north-star spec spans multiple subsystems. This plan intentionally implements only the first architectural seam:

- make recall planning explicit
- centralize recall authority in `MemoryOrchestrator`
- preserve current memory recall behavior
- expose planner explainability on existing response surfaces

This plan does **not** implement:

- Autophagy Kernel
- cognitive mutation state machine
- knowledge governance split
- skill boundary refactor
- knowledge lifecycle state machines

Those belong to later plans.

## File Structure

### New files

- `src/opencortex/cognition/__init__.py`
  - package marker for cognition-layer services
- `src/opencortex/cognition/recall_planner.py`
  - `RecallPlanner` that derives `RecallPlan` from request knobs plus `SearchIntent`
- `tests/test_recall_planner.py`
  - unit tests for `RecallPlan`, `RecallPlanner`, and orchestrator planning integration

### Modified files

- `src/opencortex/retrieve/types.py`
  - add `RecallSurface`, `RecallPlan`, `SearchIntent.recall_plan`, and `FindResult` recall-plan serialization
- `src/opencortex/orchestrator.py`
  - add `plan_recall()` and route `search()` through the explicit planner seam
- `src/opencortex/context/manager.py`
  - replace duplicated routing/surface/detail logic with `plan_recall()` output
- `src/opencortex/http/server.py`
  - expose recall-plan explainability on the existing `/api/v1/memory/search` response
- `tests/test_context_manager.py`
  - assert `ContextManager` uses planner-driven surfaces, limits, and detail level
- `tests/test_http_server.py`
  - assert HTTP search includes recall-plan explainability

## Task 1: Add RecallPlan Types And Serialization

**Files:**
- Create: `tests/test_recall_planner.py`
- Modify: `src/opencortex/retrieve/types.py`

- [ ] **Step 1: Write the failing type and serialization tests**

```python
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.retrieve.types import DetailLevel, FindResult, RecallPlan, RecallSurface, SearchIntent


class TestRecallPlanTypes(unittest.TestCase):
    def test_recall_plan_to_dict_serializes_surfaces(self):
        plan = RecallPlan(
            should_recall=True,
            surfaces=[RecallSurface.MEMORY, RecallSurface.KNOWLEDGE],
            detail_level=DetailLevel.L1,
            memory_limit=5,
            knowledge_limit=3,
            enable_cone=True,
            fusion_policy="memory_then_knowledge",
            reasoning="intent=recent_recall include_knowledge=true",
        )

        self.assertEqual(
            plan.to_dict(),
            {
                "should_recall": True,
                "surfaces": ["memory", "knowledge"],
                "detail_level": "l1",
                "memory_limit": 5,
                "knowledge_limit": 3,
                "enable_cone": True,
                "fusion_policy": "memory_then_knowledge",
                "reasoning": "intent=recent_recall include_knowledge=true",
            },
        )

    def test_find_result_to_dict_emits_recall_plan(self):
        intent = SearchIntent(intent_type="recent_recall", top_k=5)
        intent.recall_plan = RecallPlan(
            should_recall=True,
            surfaces=[RecallSurface.MEMORY],
            detail_level=DetailLevel.L1,
            memory_limit=5,
            knowledge_limit=0,
            enable_cone=True,
            fusion_policy="memory_only",
            reasoning="default memory-only plan",
        )

        payload = FindResult(
            memories=[],
            resources=[],
            skills=[],
            search_intent=intent,
        ).to_dict()

        self.assertEqual(payload["recall_plan"]["surfaces"], ["memory"])
        self.assertEqual(payload["recall_plan"]["fusion_policy"], "memory_only")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_recall_planner.TestRecallPlanTypes -v`

Expected: FAIL with import or attribute errors for `RecallPlan` / `RecallSurface`

- [ ] **Step 3: Add the recall-planning dataclasses**

```python
# src/opencortex/retrieve/types.py
class RecallSurface(str, Enum):
    MEMORY = "memory"
    TRACE = "trace"
    KNOWLEDGE = "knowledge"
```

```python
# src/opencortex/retrieve/types.py
@dataclass
class RecallPlan:
    should_recall: bool
    surfaces: List[RecallSurface]
    detail_level: DetailLevel
    memory_limit: int
    knowledge_limit: int
    enable_cone: bool
    fusion_policy: str
    reasoning: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "should_recall": self.should_recall,
            "surfaces": [surface.value for surface in self.surfaces],
            "detail_level": self.detail_level.value,
            "memory_limit": self.memory_limit,
            "knowledge_limit": self.knowledge_limit,
            "enable_cone": self.enable_cone,
            "fusion_policy": self.fusion_policy,
            "reasoning": self.reasoning,
        }
```

```python
# src/opencortex/retrieve/types.py
@dataclass
class SearchIntent:
    intent_type: str = "quick_lookup"
    top_k: int = 5
    detail_level: DetailLevel = DetailLevel.L1
    time_scope: str = "all"
    need_rerank: bool = True
    should_recall: bool = True
    trigger_categories: List[str] = field(default_factory=list)
    queries: List[TypedQuery] = field(default_factory=list)
    lexical_boost: float = 0.3
    recall_plan: Optional["RecallPlan"] = None
```

```python
# src/opencortex/retrieve/types.py inside FindResult.to_dict()
        if self.search_intent and self.search_intent.recall_plan:
            result["recall_plan"] = self.search_intent.recall_plan.to_dict()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_recall_planner.TestRecallPlanTypes -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_recall_planner.py src/opencortex/retrieve/types.py
git commit -m "feat: add recall plan datatypes"
```

## Task 2: Implement RecallPlanner

**Files:**
- Create: `src/opencortex/cognition/__init__.py`
- Create: `src/opencortex/cognition/recall_planner.py`
- Modify: `tests/test_recall_planner.py`

- [ ] **Step 1: Write the failing planner behavior tests**

```python
from opencortex.cognition.recall_planner import RecallPlanner


class TestRecallPlannerBehavior(unittest.TestCase):
    def test_plan_auto_mode_includes_knowledge_when_enabled(self):
        planner = RecallPlanner(cone_enabled=True)
        intent = SearchIntent(
            intent_type="recent_recall",
            top_k=7,
            detail_level=DetailLevel.L1,
            should_recall=True,
        )

        plan = planner.plan(
            query="最近怎么修这个问题？",
            intent=intent,
            max_items=5,
            recall_mode="auto",
            include_knowledge=True,
            detail_level_override=None,
        )

        self.assertTrue(plan.should_recall)
        self.assertEqual(plan.surfaces, [RecallSurface.MEMORY, RecallSurface.KNOWLEDGE])
        self.assertEqual(plan.memory_limit, 7)
        self.assertEqual(plan.knowledge_limit, 3)
        self.assertTrue(plan.enable_cone)

    def test_plan_never_mode_turns_everything_off(self):
        planner = RecallPlanner(cone_enabled=True)
        intent = SearchIntent(should_recall=True, top_k=10, detail_level=DetailLevel.L2)

        plan = planner.plan(
            query="anything",
            intent=intent,
            max_items=8,
            recall_mode="never",
            include_knowledge=True,
            detail_level_override=None,
        )

        self.assertFalse(plan.should_recall)
        self.assertEqual(plan.surfaces, [])
        self.assertEqual(plan.memory_limit, 0)
        self.assertEqual(plan.knowledge_limit, 0)
        self.assertFalse(plan.enable_cone)

    def test_detail_override_wins_over_intent_default(self):
        planner = RecallPlanner(cone_enabled=False)
        intent = SearchIntent(detail_level=DetailLevel.L0, should_recall=True)

        plan = planner.plan(
            query="show me details",
            intent=intent,
            max_items=4,
            recall_mode="auto",
            include_knowledge=False,
            detail_level_override="l2",
        )

        self.assertEqual(plan.detail_level, DetailLevel.L2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_recall_planner.TestRecallPlannerBehavior -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'opencortex.cognition'`

- [ ] **Step 3: Implement planner module**

```python
# src/opencortex/cognition/__init__.py
"""Cognition-layer planning services."""

from .recall_planner import RecallPlanner

__all__ = ["RecallPlanner"]
```

```python
# src/opencortex/cognition/recall_planner.py
from opencortex.retrieve.types import DetailLevel, RecallPlan, RecallSurface, SearchIntent


class RecallPlanner:
    def __init__(self, *, cone_enabled: bool):
        self._cone_enabled = cone_enabled

    def plan(
        self,
        *,
        query: str,
        intent: SearchIntent,
        max_items: int,
        recall_mode: str,
        include_knowledge: bool,
        detail_level_override: str | None,
    ) -> RecallPlan:
        should_recall = recall_mode == "always" or (
            recall_mode == "auto" and intent.should_recall
        )
        if recall_mode == "never":
            should_recall = False

        detail_level = (
            DetailLevel(detail_level_override)
            if detail_level_override
            else intent.detail_level
        )

        if not should_recall:
            return RecallPlan(
                should_recall=False,
                surfaces=[],
                detail_level=detail_level,
                memory_limit=0,
                knowledge_limit=0,
                enable_cone=False,
                fusion_policy="none",
                reasoning=f"recall_mode={recall_mode}",
            )

        surfaces = [RecallSurface.MEMORY]
        if include_knowledge:
            surfaces.append(RecallSurface.KNOWLEDGE)

        return RecallPlan(
            should_recall=True,
            surfaces=surfaces,
            detail_level=detail_level,
            memory_limit=max(max_items, intent.top_k),
            knowledge_limit=min(3, max_items) if include_knowledge else 0,
            enable_cone=self._cone_enabled and RecallSurface.MEMORY in surfaces,
            fusion_policy="memory_then_knowledge" if include_knowledge else "memory_only",
            reasoning=(
                f"intent={intent.intent_type} "
                f"recall_mode={recall_mode} "
                f"include_knowledge={include_knowledge}"
            ),
        )
```

- [ ] **Step 4: Run planner tests**

Run: `python -m unittest tests.test_recall_planner.TestRecallPlannerBehavior -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/cognition/__init__.py src/opencortex/cognition/recall_planner.py tests/test_recall_planner.py
git commit -m "feat: add explicit recall planner"
```

## Task 3: Make MemoryOrchestrator The Only Recall Planning Entry Point

**Files:**
- Modify: `src/opencortex/orchestrator.py`
- Modify: `tests/test_recall_planner.py`

- [ ] **Step 1: Write the failing orchestrator planning tests**

```python
import asyncio
import shutil
import tempfile

from opencortex.config import CortexConfig, init_config
from opencortex.http.request_context import reset_request_identity, set_request_identity
from opencortex.orchestrator import MemoryOrchestrator
from tests.test_e2e_phase1 import InMemoryStorage, MockEmbedder


class TestOrchestratorRecallPlanning(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="opencortex_recall_plan_")
        init_config(CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        ))
        self.identity = set_request_identity("team", "alice")

    def tearDown(self):
        reset_request_identity(self.identity)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_plan_recall_attaches_plan_to_intent(self):
        orch = MemoryOrchestrator(
            storage=InMemoryStorage(),
            embedder=MockEmbedder(),
        )
        asyncio.run(orch.init())

        intent, plan = asyncio.run(
            orch.plan_recall(
                query="最近修了什么问题？",
                max_items=5,
                recall_mode="auto",
                include_knowledge=True,
                detail_level_override=None,
                context_type=None,
                session_context=None,
                search_intent=None,
            )
        )

        self.assertIs(intent.recall_plan, plan)
        self.assertEqual(plan.memory_limit, 5)
        self.assertEqual(plan.knowledge_limit, 3)

        asyncio.run(orch.close())

    def test_plan_recall_never_skips_routing(self):
        llm_calls = []

        async def fake_llm(_prompt: str) -> str:
            llm_calls.append(_prompt)
            return '{"intent_type":"deep_analysis","top_k":10}'

        orch = MemoryOrchestrator(
            storage=InMemoryStorage(),
            embedder=MockEmbedder(),
            llm_completion=fake_llm,
        )
        asyncio.run(orch.init())

        intent, plan = asyncio.run(
            orch.plan_recall(
                query="不要召回",
                max_items=5,
                recall_mode="never",
                include_knowledge=True,
                detail_level_override=None,
                context_type=None,
                session_context={"session_id": "sess-1"},
                search_intent=None,
            )
        )

        self.assertEqual(llm_calls, [])
        self.assertFalse(plan.should_recall)
        self.assertFalse(intent.should_recall)

        asyncio.run(orch.close())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_recall_planner.TestOrchestratorRecallPlanning -v`

Expected: FAIL with `AttributeError: 'MemoryOrchestrator' object has no attribute 'plan_recall'`

- [ ] **Step 3: Add `plan_recall()` and make `search()` always use it**

```python
# src/opencortex/orchestrator.py
from opencortex.cognition.recall_planner import RecallPlanner
from opencortex.retrieve.types import ContextType, DetailLevel, FindResult, MatchedContext, MERGEABLE_CATEGORIES, QueryResult, RecallPlan, SearchIntent, TypedQuery
```

```python
# src/opencortex/orchestrator.py inside __init__
        self._recall_planner = RecallPlanner(
            cone_enabled=self._config.cone_retrieval_enabled,
        )
```

```python
# src/opencortex/orchestrator.py
    async def plan_recall(
        self,
        *,
        query: str,
        max_items: int,
        recall_mode: str,
        include_knowledge: bool,
        detail_level_override: Optional[str],
        context_type: Optional[ContextType],
        session_context: Optional[Dict[str, Any]],
        search_intent: Optional[SearchIntent],
    ) -> tuple[SearchIntent, RecallPlan]:
        intent = search_intent

        if recall_mode == "never":
            intent = intent or SearchIntent(
                intent_type="quick_lookup",
                top_k=0,
                detail_level=DetailLevel(detail_level_override or "l1"),
                should_recall=False,
            )
        elif intent is None:
            router = IntentRouter(llm_completion=self._llm_completion)
            intent = await router.route(
                query,
                context_type=context_type,
                session_context=session_context,
            )

        plan = self._recall_planner.plan(
            query=query,
            intent=intent,
            max_items=max_items,
            recall_mode=recall_mode,
            include_knowledge=include_knowledge,
            detail_level_override=detail_level_override,
        )
        intent.should_recall = plan.should_recall
        intent.recall_plan = plan
        return intent, plan
```

```python
# src/opencortex/orchestrator.py inside search()
        intent, recall_plan = await self.plan_recall(
            query=query,
            max_items=limit,
            recall_mode="auto",
            include_knowledge=False,
            detail_level_override=detail_level,
            context_type=context_type,
            session_context=session_context,
            search_intent=search_intent,
        )

        if not recall_plan.should_recall:
            return FindResult(
                memories=[],
                resources=[],
                skills=[],
                search_intent=intent,
            )

        effective_limit = recall_plan.memory_limit
        detail_level = recall_plan.detail_level.value
```

- [ ] **Step 4: Run orchestrator planning tests**

Run: `python -m unittest tests.test_recall_planner.TestOrchestratorRecallPlanning -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_recall_planner.py
git commit -m "feat: centralize recall planning in orchestrator"
```

## Task 4: Make ContextManager Consume RecallPlan Instead Of Recomputing It

**Files:**
- Modify: `src/opencortex/context/manager.py`
- Modify: `tests/test_context_manager.py`

- [ ] **Step 1: Write the failing ContextManager plan-consumption tests**

```python
    def test_11_prepare_uses_planner_limits_and_detail_level(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        async def fake_plan_recall(**kwargs):
            from opencortex.retrieve.types import DetailLevel, RecallPlan, RecallSurface, SearchIntent

            intent = SearchIntent(intent_type="recent_recall", top_k=5, detail_level=DetailLevel.L1)
            plan = RecallPlan(
                should_recall=True,
                surfaces=[RecallSurface.MEMORY],
                detail_level=DetailLevel.L2,
                memory_limit=4,
                knowledge_limit=0,
                enable_cone=True,
                fusion_policy="memory_only",
                reasoning="test stub",
            )
            intent.recall_plan = plan
            return intent, plan

        search_calls = []
        knowledge_calls = []

        async def fake_search(**kwargs):
            search_calls.append(kwargs)
            from opencortex.retrieve.types import FindResult
            return FindResult(memories=[], resources=[], skills=[], search_intent=kwargs["search_intent"])

        async def fake_knowledge_search(**kwargs):
            knowledge_calls.append(kwargs)
            return {"results": [], "count": 0}

        orch.plan_recall = fake_plan_recall
        orch.search = fake_search
        orch.knowledge_search = fake_knowledge_search

        result = self._run(cm.handle(
            session_id="sess_plan_001",
            phase="prepare",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "最近修了什么问题？"}],
            config={"include_knowledge": True, "max_items": 9},
        ))

        self.assertEqual(search_calls[0]["limit"], 4)
        self.assertEqual(search_calls[0]["detail_level"], "l2")
        self.assertEqual(knowledge_calls, [])
        self.assertEqual(result["intent"]["detail_level"], "l2")
        self.assertEqual(result["intent"]["recall_plan"]["surfaces"], ["memory"])

        self._run(orch.close())

    def test_12_prepare_returns_recall_plan_payload(self):
        orch = self._make_orchestrator()
        self._run(orch.init())
        cm = orch._context_manager

        result = self._run(cm.handle(
            session_id="sess_plan_002",
            phase="prepare",
            tenant_id="testteam",
            user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "test query"}],
        ))

        self.assertIn("recall_plan", result["intent"])
        self.assertIsNotNone(result["intent"]["recall_plan"])

        self._run(orch.close())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_context_manager.TestContextManager.test_11_prepare_uses_planner_limits_and_detail_level tests.test_context_manager.TestContextManager.test_12_prepare_returns_recall_plan_payload -v`

Expected: FAIL because `_prepare()` still computes routing, detail level, and knowledge inclusion on its own

- [ ] **Step 3: Replace duplicated prepare logic with planner output**

```python
# src/opencortex/context/manager.py
from opencortex.retrieve.types import ContextType, DetailLevel, RecallSurface, SearchIntent
```

```python
# src/opencortex/context/manager.py inside _prepare()
        context_type_value = (
            ContextType(context_type_filter) if context_type_filter else None
        )
        session_ctx = {
            "session_id": session_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
        }
        intent, recall_plan = await self._orchestrator.plan_recall(
            query=query,
            max_items=max_items,
            recall_mode=recall_mode,
            include_knowledge=include_knowledge,
            detail_level_override=detail_level_override,
            context_type=context_type_value,
            session_context=session_ctx,
            search_intent=None,
        )

        should_recall = recall_plan.should_recall
        detail_level = recall_plan.detail_level.value
        include_knowledge = RecallSurface.KNOWLEDGE in recall_plan.surfaces
```

```python
# src/opencortex/context/manager.py inside _memory_search()
                    search_kwargs: Dict[str, Any] = {
                        "query": query,
                        "limit": recall_plan.memory_limit,
                        "detail_level": detail_level,
                        "search_intent": intent,
                    }
```

```python
# src/opencortex/context/manager.py inside _knowledge_search()
                    k_result = await self._orchestrator.knowledge_search(
                        query=query,
                        limit=recall_plan.knowledge_limit,
                    )
```

```python
# src/opencortex/context/manager.py in result payload
            "intent": {
                "should_recall": should_recall,
                "intent_type": intent.intent_type,
                "detail_level": recall_plan.detail_level.value,
                "recall_plan": recall_plan.to_dict(),
            },
```

- [ ] **Step 4: Run ContextManager regression tests**

Run: `python -m unittest tests.test_context_manager.TestContextManager.test_09_prepare_routes_once tests.test_context_manager.TestContextManager.test_10_include_knowledge_default_false tests.test_context_manager.TestContextManager.test_11_prepare_uses_planner_limits_and_detail_level tests.test_context_manager.TestContextManager.test_12_prepare_returns_recall_plan_payload -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/context/manager.py tests/test_context_manager.py
git commit -m "refactor: drive context prepare from recall plans"
```

## Task 5: Expose RecallPlan Explainability On HTTP Search

**Files:**
- Modify: `src/opencortex/http/server.py`
- Modify: `tests/test_http_server.py`

- [ ] **Step 1: Write the failing HTTP explainability test**

```python
    def test_03_search(self):
        """POST /api/v1/memory/search returns results after storing."""
        async def check():
            async with _test_app_context() as client:
                await client.post("/api/v1/memory/store", json={
                    "abstract": "User prefers dark theme in editors",
                    "category": "preferences",
                })
                await client.post("/api/v1/memory/store", json={
                    "abstract": "Project uses Python 3.12",
                    "category": "tech",
                })
                resp = await client.post("/api/v1/memory/search", json={
                    "query": "What theme does the user prefer?",
                    "limit": 5,
                })
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("results", data)
                self.assertGreater(data["total"], 0)
                self.assertIn("search_intent", data)
                self.assertIn("recall_plan", data)
                self.assertEqual(data["recall_plan"]["surfaces"], ["memory"])

        self._run(check())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_http_server.TestHttpServer.test_03_search -v`

Expected: FAIL because `/api/v1/memory/search` does not yet include `recall_plan`

- [ ] **Step 3: Add recall-plan payload to the HTTP search response**

```python
# src/opencortex/http/server.py inside memory_search()
        if result.search_intent:
            resp["search_intent"] = {
                "intent_type": result.search_intent.intent_type,
                "top_k": result.search_intent.top_k,
                "detail_level": result.search_intent.detail_level.value,
                "time_scope": result.search_intent.time_scope,
                "should_recall": result.search_intent.should_recall,
                "lexical_boost": result.search_intent.lexical_boost,
            }
            if result.search_intent.recall_plan:
                resp["recall_plan"] = result.search_intent.recall_plan.to_dict()
```

- [ ] **Step 4: Run HTTP regression test**

Run: `python -m unittest tests.test_http_server.TestHttpServer.test_03_search -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/http/server.py tests/test_http_server.py
git commit -m "feat: expose recall plan in search responses"
```

## Spec Coverage Check

- Explicit `RecallPlan` seam: covered by Task 1 and Task 2
- Recall authority centralized in `MemoryOrchestrator`: covered by Task 3
- `IntentRouter` reduced to intent analysis, not recall ownership: covered by Task 3
- `ContextManager` stops owning routing/detail/surface policy: covered by Task 4
- Explainability on active response surfaces: covered by Task 1, Task 4, and Task 5

Deferred to later plans:

- Autophagy Kernel shell
- recall mutation engine
- consolidation gate
- knowledge governance split
- skill input-contract enforcement

## Self-Review

- Spec coverage: every in-scope requirement from the north-star spec maps to at least one task in this plan
- Placeholder scan: no `TODO`, `TBD`, or “implement later” placeholders remain in tasks
- Type consistency:
  - `RecallSurface`, `RecallPlan`, and `SearchIntent.recall_plan` are defined once and reused consistently
  - `MemoryOrchestrator.plan_recall()` is the single recall-planning entry point in both `search()` and `ContextManager`
  - `detail_level` is sourced from `RecallPlan` after planning, avoiding drift between planner output and response payloads
- Reality check against current code:
  - preserves existing `search_intent` pass-through behavior
  - preserves `recall_mode=never` as a no-routing short-circuit
  - avoids claiming knowledge governance or Autophagy state-machine work in Phase 1
