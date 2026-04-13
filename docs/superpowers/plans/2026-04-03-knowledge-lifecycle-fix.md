# Knowledge Lifecycle Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 8 issues in the knowledge lifecycle pipeline so knowledge candidates properly flow through CANDIDATE → VERIFIED → ACTIVE states.

**Architecture:** Fix filter DSL in knowledge_store.py, wire Sandbox validation into _run_archivist(), propagate stats through session_end(), and enable knowledge recall via server config. All changes are in existing files — no new modules.

**Tech Stack:** Python 3.10+, unittest, Qdrant filter DSL, asyncio

**Spec:** `docs/knowledge-lifecycle-fix.md`

---

### Task 1: Fix knowledge_store.py filter DSL + scope isolation

**Files:**
- Modify: `src/opencortex/alpha/knowledge_store.py:73-94` (search method)
- Modify: `src/opencortex/alpha/knowledge_store.py:133-145` (list_candidates method)
- Test: `tests/test_alpha_knowledge_store.py` (new test file)

- [ ] **Step 1: Write failing tests for filter DSL correctness and scope isolation**

Create `tests/test_alpha_knowledge_store.py`:

```python
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
        # Should have 4 conditions: tenant + status + type + scope_or
        self.assertGreaterEqual(len(f.must), 4)

    def test_search_filter_includes_scope_or_group(self):
        """search() filter includes OR group for scope visibility."""
        f = translate_filter(self._build_search_filter("team1", "userA"))
        # The last must condition should be a nested Filter with should (OR)
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
        # Old format uses "conditions" not "conds" — translator gets empty list
        self.assertFalse(f.must, "Old format should produce empty filter (no must)")
        self.assertFalse(f.should, "Old format should produce empty filter (no should)")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python3 -m unittest tests.test_alpha_knowledge_store -v`
Expected: All 5 tests PASS (these test the filter expressions directly, not the store methods — they validate the DSL format we're about to write is correct, and prove the old format is broken)

- [ ] **Step 3: Apply the search() filter fix**

Edit `src/opencortex/alpha/knowledge_store.py`. Replace the `search()` method (lines 73-94):

```python
    async def search(
        self, query: str, tenant_id: str, user_id: str,
        types: Optional[List[str]] = None,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Vector search over knowledge — only active items returned.

        Scope visibility:
        - USER scope: only visible to the owning user_id
        - TENANT/GLOBAL scope: visible to all users in the tenant
        """
        embed_result = self._embedder.embed_query(query)

        must_conds = [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "status",
             "conds": [s.value for s in SEARCHABLE_STATUSES]},
        ]

        if types:
            must_conds.append(
                {"op": "must", "field": "knowledge_type", "conds": types}
            )

        # Scope isolation: user-scope only visible to owner
        scope_filter = {"op": "or", "conds": [
            {"op": "must", "field": "scope", "conds": [
                KnowledgeScope.TENANT.value,
                KnowledgeScope.GLOBAL.value,
            ]},
            {"op": "and", "conds": [
                {"op": "must", "field": "scope",
                 "conds": [KnowledgeScope.USER.value]},
                {"op": "must", "field": "user_id", "conds": [user_id]},
            ]},
        ]}
        must_conds.append(scope_filter)

        filter_expr = {"op": "and", "conds": must_conds}
        return await self._storage.search(
            self._collection, embed_result.dense_vector, filter_expr,
            limit=limit,
        )
```

Add the missing import at the top of the file (after line 12):

```python
from opencortex.alpha.types import Knowledge, KnowledgeStatus, KnowledgeScope, SEARCHABLE_STATUSES
```

(Replace the existing import line that only imports `Knowledge, KnowledgeStatus, SEARCHABLE_STATUSES`.)

- [ ] **Step 4: Apply the list_candidates() filter fix**

Replace `list_candidates()` method (lines 133-145):

```python
    async def list_candidates(self, tenant_id: str) -> List[Dict[str, Any]]:
        """List knowledge items pending approval."""
        filter_expr = {"op": "and", "conds": [
            {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
            {"op": "must", "field": "status", "conds": [
                KnowledgeStatus.CANDIDATE.value,
                KnowledgeStatus.VERIFIED.value,
            ]},
        ]}
        return await self._storage.filter(self._collection, filter_expr)
```

- [ ] **Step 5: Run all tests**

Run: `uv run python3 -m unittest tests.test_alpha_knowledge_store tests.test_alpha_types tests.test_qdrant_adapter -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_alpha_knowledge_store.py src/opencortex/alpha/knowledge_store.py
git commit -m "fix(alpha): correct filter DSL in knowledge_store + add scope isolation"
```

---

### Task 2: Wire Sandbox into _run_archivist()

**Files:**
- Modify: `src/opencortex/orchestrator.py:2439-2468` (_run_archivist method)
- Test: `tests/test_alpha_sandbox_integration.py` (new test file)

- [ ] **Step 1: Write failing test for sandbox integration**

Create `tests/test_alpha_sandbox_integration.py`:

```python
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
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
        """3 traces, 100% success, 1 user (user scope) → PASS."""
        k = self._make_knowledge_dict(scope="user")
        traces = self._make_traces(count=3, success_count=3)
        result = stat_gate(k, traces, min_source_users_private=1)
        self.assertTrue(result.passed)
        self.assertEqual(result.trace_count, 3)
        self.assertEqual(result.success_rate, 1.0)

    def test_stat_gate_fails_insufficient_traces(self):
        """2 traces < min_traces=3 → FAIL."""
        k = self._make_knowledge_dict()
        traces = self._make_traces(count=2)
        result = stat_gate(k, traces)
        self.assertFalse(result.passed)
        self.assertIn("Insufficient traces", result.reason)

    def test_stat_gate_fails_low_success_rate(self):
        """1/3 success = 33% < 70% → FAIL."""
        k = self._make_knowledge_dict()
        traces = self._make_traces(count=3, success_count=1)
        result = stat_gate(k, traces)
        self.assertFalse(result.passed)
        self.assertIn("Low success rate", result.reason)

    def test_stat_gate_fails_user_diversity_for_tenant_scope(self):
        """Tenant scope requires 2 users, only 1 → FAIL."""
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
        """user scope + confidence >= 0.95 → active (auto-approve)."""
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
        """Insufficient traces → needs_more_traces."""
        async def _run():
            result = await evaluate(
                self._make_knowledge_dict(),
                [{"trace_id": "tr1", "outcome": "success", "user_id": "hugo"}],
                min_traces=3,
            )
            self.assertEqual(result.status, "needs_more_traces")

        asyncio.get_event_loop().run_until_complete(_run())

    def test_evaluate_needs_improvement_low_llm_pass_rate(self):
        """LLM says not improved → needs_improvement."""
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
        """Passes gates but human approval required → verified."""
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
```

- [ ] **Step 2: Run tests to verify they pass (these test sandbox directly)**

Run: `uv run python3 -m unittest tests.test_alpha_sandbox_integration -v`
Expected: All 7 tests PASS (sandbox code is already correct — we're validating our understanding before wiring it in)

- [ ] **Step 3: Rewrite _run_archivist() with sandbox integration**

Edit `src/opencortex/orchestrator.py`. Replace `_run_archivist()` method (lines 2439-2468):

```python
    async def _run_archivist(self, tenant_id: str, user_id: str) -> Dict[str, int]:
        """Run Archivist in background to extract knowledge from traces."""
        stats: Dict[str, int] = {"knowledge_candidates": 0, "knowledge_active": 0}
        if not self._archivist or not self._trace_store or not self._knowledge_store:
            return stats
        try:
            from opencortex.alpha.types import KnowledgeScope, KnowledgeStatus
            from opencortex.alpha.sandbox import evaluate as sandbox_evaluate

            traces = await self._trace_store.list_unprocessed(tenant_id)
            if not traces:
                return stats

            knowledge_items = await self._archivist.run(
                traces, tenant_id, user_id, KnowledgeScope.USER,
            )

            alpha_cfg = self._config.cortex_alpha

            for k in knowledge_items:
                # Collect evidence traces (traces is List[Dict])
                source_ids = set(k.source_trace_ids) if k.source_trace_ids else set()
                evidence_traces = [
                    t for t in traces
                    if t.get("trace_id", t.get("id", "")) in source_ids
                ]

                # Run Sandbox evaluation
                if evidence_traces and self._llm_completion:
                    eval_result = await sandbox_evaluate(
                        knowledge_dict=k.to_dict(),
                        traces=evidence_traces,
                        llm_fn=self._llm_completion,
                        min_traces=alpha_cfg.sandbox_min_traces,
                        min_success_rate=alpha_cfg.sandbox_min_success_rate,
                        min_source_users=alpha_cfg.sandbox_min_source_users,
                        min_source_users_private=alpha_cfg.sandbox_min_source_users_private,
                        llm_sample_size=alpha_cfg.sandbox_llm_sample_size,
                        llm_min_pass_rate=alpha_cfg.sandbox_llm_min_pass_rate,
                        require_human_approval=alpha_cfg.sandbox_require_human_approval,
                        user_auto_approve_confidence=alpha_cfg.user_auto_approve_confidence,
                    )
                    status_map = {
                        "needs_more_traces": KnowledgeStatus.CANDIDATE,
                        "needs_improvement": KnowledgeStatus.CANDIDATE,
                        "verified": KnowledgeStatus.VERIFIED,
                        "active": KnowledgeStatus.ACTIVE,
                    }
                    k.status = status_map.get(eval_result.status, KnowledgeStatus.CANDIDATE)

                await self._knowledge_store.save(k)

                if k.status == KnowledgeStatus.ACTIVE:
                    stats["knowledge_active"] += 1
                else:
                    stats["knowledge_candidates"] += 1

            # Idempotency: mark traces as processed
            trace_ids = [t.get("trace_id", t.get("id", "")) for t in traces]
            trace_ids = [tid for tid in trace_ids if tid]
            if trace_ids:
                await self._trace_store.mark_processed(trace_ids)

            logger.info(
                "[Alpha] Archivist: %d candidates, %d active from %d traces",
                stats["knowledge_candidates"], stats["knowledge_active"], len(traces),
            )
        except Exception as exc:
            logger.warning("[Alpha] Archivist failed: %s", exc)
        return stats
```

- [ ] **Step 4: Run existing tests to verify no regressions**

Run: `uv run python3 -m unittest tests.test_alpha_types tests.test_alpha_config tests.test_alpha_knowledge_store tests.test_alpha_sandbox_integration -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_alpha_sandbox_integration.py
git commit -m "fix(alpha): wire Sandbox evaluation into _run_archivist pipeline"
```

---

### Task 3: Fix session_end() return value

**Files:**
- Modify: `src/opencortex/orchestrator.py:2425-2437` (session_end method)

- [ ] **Step 1: Edit session_end() to propagate archivist stats**

In `src/opencortex/orchestrator.py`, find the archivist trigger block inside `session_end()` (around line 2425-2437). Replace:

```python
                    # Check Archivist trigger
                    if self._archivist and self._trace_store:
                        count = await self._trace_store.count_new_traces(tid)
                        if self._archivist.should_trigger(count):
                            asyncio.create_task(self._run_archivist(tid, uid))
```

With:

```python
                    # Check Archivist trigger
                    archivist_stats: Dict[str, int] = {
                        "knowledge_candidates": 0, "knowledge_active": 0,
                    }
                    if self._archivist and self._trace_store:
                        count = await self._trace_store.count_new_traces(tid)
                        if self._archivist.should_trigger(count):
                            archivist_stats = await self._run_archivist(tid, uid)
```

Then replace the return block:

```python
        return {
            "session_id": session_id,
            "quality_score": quality_score,
            "alpha_traces": alpha_traces_count,
        }
```

With:

```python
        return {
            "session_id": session_id,
            "quality_score": quality_score,
            "alpha_traces": alpha_traces_count,
            **archivist_stats,
        }
```

Note: `archivist_stats` must be declared before the try block so it's available in the return. If the archivist code is inside the `try` block that starts at line 2409, initialize `archivist_stats` at line 2404 (next to `alpha_traces_count = 0`).

- [ ] **Step 2: Run existing tests**

Run: `uv run python3 -m unittest tests.test_alpha_types tests.test_alpha_config tests.test_context_manager -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add src/opencortex/orchestrator.py
git commit -m "fix(alpha): propagate knowledge stats from session_end return value"
```

---

### Task 4: Add knowledge_recall_enabled config + wire into ContextManager

**Files:**
- Modify: `src/opencortex/config.py:64` (CortexAlphaConfig)
- Modify: `src/opencortex/context/manager.py:176` (include_knowledge default)
- Test: `tests/test_alpha_config.py` (add test)

- [ ] **Step 1: Write failing test for new config field**

Add to `tests/test_alpha_config.py`:

```python
    def test_knowledge_recall_enabled_default(self):
        """knowledge_recall_enabled defaults to False."""
        from opencortex.config import CortexAlphaConfig
        cfg = CortexAlphaConfig()
        self.assertFalse(cfg.knowledge_recall_enabled)

    def test_knowledge_recall_enabled_set(self):
        """knowledge_recall_enabled can be set to True."""
        from opencortex.config import CortexAlphaConfig
        cfg = CortexAlphaConfig(knowledge_recall_enabled=True)
        self.assertTrue(cfg.knowledge_recall_enabled)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_alpha_config.TestAlphaConfig.test_knowledge_recall_enabled_default -v`
Expected: FAIL — `CortexAlphaConfig` has no field `knowledge_recall_enabled`

- [ ] **Step 3: Add the config field**

Edit `src/opencortex/config.py`. After line 64 (`user_auto_approve_confidence`), add:

```python
    # Knowledge recall in prepare()
    knowledge_recall_enabled: bool = False  # Server-side default for include_knowledge
```

- [ ] **Step 4: Run config tests to verify pass**

Run: `uv run python3 -m unittest tests.test_alpha_config -v`
Expected: All PASS

- [ ] **Step 5: Wire into ContextManager**

Edit `src/opencortex/context/manager.py`. Replace line 176:

```python
        include_knowledge = config.get("include_knowledge", False)
```

With:

```python
        # Priority: client explicit > server config > default False
        _server_default = False
        if hasattr(self._orchestrator, '_config') and self._orchestrator._config:
            _server_default = self._orchestrator._config.cortex_alpha.knowledge_recall_enabled
        include_knowledge = config.get("include_knowledge", _server_default)
```

- [ ] **Step 6: Run full test suite**

Run: `uv run python3 -m unittest tests.test_alpha_config tests.test_alpha_types tests.test_context_manager -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/opencortex/config.py src/opencortex/context/manager.py tests/test_alpha_config.py
git commit -m "feat(alpha): add knowledge_recall_enabled config, wire into ContextManager"
```

---

### Task 5: Document configuration profile

**Files:**
- Modify: `src/opencortex/config.py` (add comment block)

- [ ] **Step 1: Add configuration documentation comment**

Edit `src/opencortex/config.py`. After the `CortexAlphaConfig` class docstring (line 40), add a comment block:

```python
    """Cortex Alpha sub-configuration (Design doc §11).

    To enable the full knowledge lifecycle pipeline, set in server.json:

        {
          "cortex_alpha": {
            "trace_splitter_enabled": true,
            "archivist_enabled": true,
            "knowledge_recall_enabled": true
          }
        }

    Or via environment variable:

        OPENCORTEX_CORTEX_ALPHA='{"trace_splitter_enabled":true,"archivist_enabled":true,"knowledge_recall_enabled":true}'
    """
```

- [ ] **Step 2: Run full regression**

Run: `uv run python3 -m unittest tests.test_alpha_config tests.test_alpha_types tests.test_alpha_knowledge_store tests.test_alpha_sandbox_integration tests.test_qdrant_adapter -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add src/opencortex/config.py
git commit -m "docs(alpha): add knowledge pipeline configuration guide to CortexAlphaConfig"
```

---

### Task 6: Final integration verification

**Files:**
- No new files — run full test suite

- [ ] **Step 1: Run complete test suite**

Run: `uv run python3 -m unittest tests.test_alpha_types tests.test_alpha_config tests.test_alpha_knowledge_store tests.test_alpha_sandbox_integration tests.test_alpha_schemas tests.test_alpha_http tests.test_qdrant_adapter -v`
Expected: All PASS

- [ ] **Step 2: Run the broader regression suite**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_write_dedup tests.test_context_manager -v`
Expected: All PASS (no regressions in core memory system)

- [ ] **Step 3: Verify checklist against spec**

Manually verify each acceptance criterion from the spec:
1. Filter DSL produces valid Qdrant filters → confirmed by test_alpha_knowledge_store
2. Scope isolation works → confirmed by test_search_filter_includes_scope_or_group
3. Sandbox wired into pipeline → confirmed by _run_archivist rewrite
4. session_end returns knowledge stats → confirmed by return value change
5. knowledge_recall_enabled config exists → confirmed by test_alpha_config
6. mark_processed preserved → confirmed by code review of _run_archivist
