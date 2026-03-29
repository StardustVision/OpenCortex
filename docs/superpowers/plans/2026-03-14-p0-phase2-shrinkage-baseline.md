# P0: Phase 2 Shrinkage + Diagnostic Baseline

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Disable Phase 2 components (TraceSplitter, Archivist, TraceStore, KnowledgeStore, Knowledge integration) from the default runtime path, and build a benchmark framework to establish diagnostic baseline metrics.

**Architecture:** Phase 2 components currently initialize and run in the default path. This plan: (1) flips config defaults so LLM-driven components are off by default, (2) gates TraceStore/KnowledgeStore init behind config flags, (3) gates HTTP endpoints, (4) disables knowledge integration in recall. The benchmark seeds isolated test data per run (unique tenant), queries via JWT-authenticated HTTP, and computes metrics via existing `memory_eval.py`.

**Tech Stack:** Python 3.10+, unittest, FastAPI, JSON fixtures, `src/opencortex/eval/memory_eval.py`

**Scope:** This plan covers P0 only. Subsequent plans will cover P1 (pipeline correctness), P2 (data model), P3 (persistence), P4 (retrieval accuracy), P5 (explainability).

---

## File Structure

### Files to Modify

| File | Responsibility | Change |
|------|---------------|--------|
| `src/opencortex/config.py:44,47` | CortexAlphaConfig defaults | `trace_splitter_enabled=False`, `archivist_enabled=False` |
| `src/opencortex/orchestrator.py:235-256` | `_init_alpha()` | Gate TraceStore init on `trace_splitter_enabled`, KnowledgeStore init on `archivist_enabled` |
| `src/opencortex/context/manager.py:172` | ContextManager prepare | `include_knowledge` default `True` → `False` |
| `plugins/opencortex-memory/lib/mcp-server.mjs:85` | MCP recall tool | `include_knowledge` default `true` → `false` |
| `src/opencortex/http/server.py:340-364` | Knowledge/Archivist HTTP endpoints | Add config gate returning `{"error": "feature disabled"}` |
| `tests/test_alpha_config.py` | Alpha config tests | Update assertions for new defaults |

### Files to Create

| File | Responsibility |
|------|---------------|
| `tests/test_phase2_shrinkage.py` | Integration tests verifying Phase 2 is shrunk |
| `tests/benchmark/dataset.json` | 50 queries + seed memories with ground truth |
| `tests/benchmark/runner.py` | Benchmark runner: seed → query → metrics → report |
| `tests/test_benchmark_runner.py` | Unit tests for benchmark metric computation |

### Existing Files Referenced (read-only)

| File | Why |
|------|-----|
| `tests/test_e2e_phase1.py` | `MockEmbedder`, `InMemoryStorage` test utilities |
| `tests/test_context_manager.py` | ContextManager test patterns |
| `src/opencortex/eval/memory_eval.py` | `_query_metrics()`, `_aggregate()`, `evaluate_dataset()`, `load_dataset()` |
| `src/opencortex/orchestrator.py:227-284` | `_init_alpha()` — understand what gets initialized |
| `src/opencortex/auth/token.py` | `ensure_secret()`, `generate_token()` — JWT generation for benchmark runner |

---

## Chunk 1: Phase 2 Shrinkage

### Task 1: Flip CortexAlphaConfig Defaults

**Files:**
- Modify: `src/opencortex/config.py` — lines containing `trace_splitter_enabled` and `archivist_enabled` defaults
- Modify: `tests/test_alpha_config.py` — update existing assertions

- [ ] **Step 1: Update test assertions for new defaults**

In `tests/test_alpha_config.py`, `test_default_alpha_config` currently asserts `assertTrue(cfg.cortex_alpha.trace_splitter_enabled)` (line 11). Change to match new defaults:

```python
# tests/test_alpha_config.py — test_default_alpha_config method
def test_default_alpha_config(self):
    cfg = CortexConfig()
    self.assertIsNotNone(cfg.cortex_alpha)
    self.assertTrue(cfg.cortex_alpha.observer_enabled)
    # Phase 2 LLM components default OFF (Phase 1 shrinkage)
    self.assertFalse(cfg.cortex_alpha.trace_splitter_enabled)
    self.assertFalse(cfg.cortex_alpha.archivist_enabled)
    self.assertEqual(cfg.cortex_alpha.archivist_trigger_threshold, 20)
    self.assertEqual(cfg.cortex_alpha.archivist_max_delay_hours, 24)
    self.assertEqual(cfg.cortex_alpha.sandbox_min_traces, 3)
    self.assertEqual(cfg.cortex_alpha.sandbox_min_success_rate, 0.7)
    self.assertTrue(cfg.cortex_alpha.sandbox_require_human_approval)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python3 -m unittest tests.test_alpha_config.TestAlphaConfig.test_default_alpha_config -v
```

Expected: FAIL — `AssertionError: True is not false` (current defaults are True)

- [ ] **Step 3: Flip defaults in config.py**

In `src/opencortex/config.py`, in the `CortexAlphaConfig` dataclass:

```python
# Change these two lines:
trace_splitter_enabled: bool = False    # was True; Phase 1 shrinkage
archivist_enabled: bool = False         # was True; Phase 1 shrinkage
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python3 -m unittest tests.test_alpha_config.TestAlphaConfig.test_default_alpha_config -v
```

Expected: PASS

- [ ] **Step 5: Run full alpha config test suite to check for regressions**

```bash
uv run python3 -m unittest tests.test_alpha_config -v
```

Expected: All 4 tests pass. `test_alpha_config_from_dict` explicitly sets `observer_enabled=False` so it won't be affected by default changes.

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/config.py tests/test_alpha_config.py
git commit -m "feat(p0): flip Phase 2 defaults — trace_splitter and archivist disabled by default

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 1b: Gate TraceStore/KnowledgeStore Init in `_init_alpha()`

**Files:**
- Modify: `src/opencortex/orchestrator.py:235-256` — gate TraceStore/KnowledgeStore init
- Modify: `tests/test_phase2_shrinkage.py` — add init-level verification

Currently `_init_alpha()` unconditionally initializes TraceStore and KnowledgeStore whenever `storage + embedder` are available (`orchestrator.py:235-256`). This creates Qdrant collections on startup even when Phase 2 is disabled. Gate them behind the existing config flags: TraceStore behind `trace_splitter_enabled`, KnowledgeStore behind `archivist_enabled`.

- [ ] **Step 1: Add init-gating tests to `tests/test_phase2_shrinkage.py`**

Add these two tests to the existing `TestPhase2Shrinkage` class (they will be created in Task 4, but the assertions belong here logically — add them when creating the file):

```python
def test_trace_store_not_initialized_when_disabled(self):
    """TraceStore should not be initialized when trace_splitter disabled."""
    async def _test():
        async with _shrinkage_test_app() as (client, orch):
            self.assertIsNone(orch._trace_store)
    self._run(_test())

def test_knowledge_store_not_initialized_when_disabled(self):
    """KnowledgeStore should not be initialized when archivist disabled."""
    async def _test():
        async with _shrinkage_test_app() as (client, orch):
            self.assertIsNone(orch._knowledge_store)
    self._run(_test())
```

- [ ] **Step 2: Gate TraceStore init**

In `src/opencortex/orchestrator.py`, in `_init_alpha()`, wrap the TraceStore init block (lines 235-245) with the `trace_splitter_enabled` check:

```python
# Before (lines 235-245):
if self._storage and self._embedder:
    from opencortex.alpha.trace_store import TraceStore
    self._trace_store = TraceStore(...)
    await self._trace_store.init()

# After:
if self._storage and self._embedder and alpha_cfg.trace_splitter_enabled:
    from opencortex.alpha.trace_store import TraceStore
    self._trace_store = TraceStore(...)
    await self._trace_store.init()
```

- [ ] **Step 3: Gate KnowledgeStore init**

Same file, wrap the KnowledgeStore init block (lines 247-256) with the `archivist_enabled` check:

```python
# Before (lines 247-256):
    # KnowledgeStore
    from opencortex.alpha.knowledge_store import KnowledgeStore
    self._knowledge_store = KnowledgeStore(...)
    await self._knowledge_store.init()

# After:
if self._storage and self._embedder and alpha_cfg.archivist_enabled:
    from opencortex.alpha.knowledge_store import KnowledgeStore
    self._knowledge_store = KnowledgeStore(...)
    await self._knowledge_store.init()
```

> **Note:** The existing TraceStore/KnowledgeStore init is nested inside the `if self._storage and self._embedder:` block. After this change, TraceStore gets its own `if self._storage and self._embedder and alpha_cfg.trace_splitter_enabled:` block, and KnowledgeStore gets `if self._storage and self._embedder and alpha_cfg.archivist_enabled:`. The TraceSplitter guard at line 259 (`if self._llm_completion and alpha_cfg.trace_splitter_enabled:`) remains unchanged.

- [ ] **Step 4: Run E2E regression (init-gating tests deferred to Task 4)**

The init-gating tests from Step 1 live in `tests/test_phase2_shrinkage.py`, which is created in Task 4. For now, verify no regression in existing tests:

```bash
uv run python3 -m unittest tests.test_e2e_phase1 tests.test_context_manager tests.test_alpha_config -v
```

Expected: All pass. E2E tests use `MockEmbedder` without LLM, so TraceSplitter/Archivist were already `None` — no regression.

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py
git commit -m "feat(p0): gate TraceStore/KnowledgeStore init behind config flags

TraceStore only initializes when trace_splitter_enabled=True.
KnowledgeStore only initializes when archivist_enabled=True.
No new config flags — reuses existing CortexAlphaConfig.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 2: Flip ContextManager include_knowledge Default

**Files:**
- Modify: `src/opencortex/context/manager.py:172`
- Modify: `tests/test_context_manager.py` — add test for new default

- [ ] **Step 1: Write the failing test**

Add to `tests/test_context_manager.py`, in `TestContextManager` class (after the existing `test_09_prepare_routes_once` at line 339):

```python
# -----------------------------------------------------------------
# 10. include_knowledge defaults to False (Phase 1 shrinkage)
# -----------------------------------------------------------------

def test_10_include_knowledge_default_false(self):
    """prepare() with default config does NOT call knowledge_search."""
    orch = self._make_orchestrator()
    self._run(orch.init())
    cm = orch._context_manager

    # Spy on orchestrator.knowledge_search to verify it's never called
    ks_calls = []
    original_ks = getattr(orch, 'knowledge_search', None)
    async def spy_ks(*args, **kwargs):
        ks_calls.append((args, kwargs))
        if original_ks:
            return await original_ks(*args, **kwargs)
        return []
    orch.knowledge_search = spy_ks

    # recall_mode=always ensures retrieval path runs — isolates the
    # include_knowledge variable from keyword routing behavior
    result = self._run(cm.handle(
        session_id="sess_know",
        phase="prepare",
        tenant_id="testteam",
        user_id="alice",
        turn_id="t1",
        messages=[{"role": "user", "content": "test knowledge default"}],
        config={"recall_mode": "always"},
    ))
    # knowledge_search must NOT be called when include_knowledge defaults to False
    self.assertEqual(len(ks_calls), 0, "knowledge_search should not be called")
    self.assertEqual(result.get("knowledge", []), [])

    if original_ks:
        orch.knowledge_search = original_ks
    self._run(orch.close())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python3 -m unittest tests.test_context_manager.TestContextManager.test_10_include_knowledge_default_false -v
```

Expected: FAIL — with `recall_mode=always` forcing retrieval and the current default (`include_knowledge=True`), `knowledge_search` will be called. The spy captures the call, so `len(ks_calls) == 1` triggers `AssertionError: 1 != 0 : knowledge_search should not be called`.

- [ ] **Step 3: Change the default in manager.py**

In `src/opencortex/context/manager.py`, line 172:

```python
# Change from:
include_knowledge = config.get("include_knowledge", True)
# To:
include_knowledge = config.get("include_knowledge", False)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python3 -m unittest tests.test_context_manager.TestContextManager.test_10_include_knowledge_default_false -v
```

Expected: PASS

- [ ] **Step 5: Run full context manager test suite for regressions**

```bash
uv run python3 -m unittest tests.test_context_manager -v
```

Expected: All 10 tests pass. Existing `test_01_full_lifecycle` asserts `knowledge` key exists in result but doesn't assert it's non-empty, so it should pass regardless.

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/context/manager.py tests/test_context_manager.py
git commit -m "feat(p0): include_knowledge defaults to False in ContextManager.prepare()

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 3: Flip MCP include_knowledge Default

**Files:**
- Modify: `plugins/opencortex-memory/lib/mcp-server.mjs:85`

- [ ] **Step 1: Change the default**

In `plugins/opencortex-memory/lib/mcp-server.mjs`, in the `recall` tool definition (around line 85), find:

```javascript
include_knowledge: { type: 'boolean', description: 'Also search approved knowledge base (beliefs, SOPs, rules). Default: true', default: true },
```

Change to:

```javascript
include_knowledge: { type: 'boolean', description: 'Also search approved knowledge base. Default: false (Phase 2 feature)', default: false },
```

- [ ] **Step 2: Verify Node.js syntax**

```bash
node -c plugins/opencortex-memory/lib/mcp-server.mjs
```

Expected: No output (syntax OK)

- [ ] **Step 3: Commit**

```bash
git add plugins/opencortex-memory/lib/mcp-server.mjs
git commit -m "feat(p0): MCP recall include_knowledge defaults to false

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 4: Add HTTP Endpoint Config Gating

**Files:**
- Modify: `src/opencortex/http/server.py:340-364`
- Create: `tests/test_phase2_shrinkage.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_phase2_shrinkage.py`. Uses the same `httpx.AsyncClient` + `ASGITransport` + `_register_routes` pattern as `tests/test_http_server.py` — this bypasses JWT auth middleware and lifespan, testing endpoint logic directly:

```python
"""
Tests verifying Phase 2 HTTP endpoints are gated by config.

When archivist_enabled=False (Phase 1 default), knowledge/* and archivist/*
endpoints should return {"error": "feature disabled"}.

Uses same test pattern as test_http_server.py:
  - httpx.AsyncClient + ASGITransport (no JWT auth, no lifespan)
  - http_server._orchestrator = orch (direct injection)
  - http_server._register_routes(app) (routes without middleware)
"""

import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx
from fastapi import FastAPI
from httpx import ASGITransport

from opencortex.config import CortexConfig, init_config
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.orchestrator import MemoryOrchestrator
from tests.test_e2e_phase1 import MockEmbedder, InMemoryStorage


@asynccontextmanager
async def _shrinkage_test_app():
    """Create test app with default config (Phase 2 disabled)."""
    import opencortex.http.server as http_server

    temp_dir = tempfile.mkdtemp(prefix="p2s_test_")
    config = CortexConfig(
        data_root=temp_dir,
        embedding_dimension=MockEmbedder.DIMENSION,
        rerank_provider="disabled",
    )
    init_config(config)

    tokens = set_request_identity("testteam", "alice")
    storage = InMemoryStorage()
    embedder = MockEmbedder()
    orch = MemoryOrchestrator(config=config, storage=storage, embedder=embedder)
    await orch.init()
    http_server._orchestrator = orch

    app = FastAPI()
    http_server._register_routes(app)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        try:
            yield client, orch
        finally:
            await orch.close()
            http_server._orchestrator = None
            reset_request_identity(tokens)
            shutil.rmtree(temp_dir, ignore_errors=True)


class TestPhase2Shrinkage(unittest.TestCase):
    """Verify Phase 2 features are disabled by default."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_default_config_disables_trace_splitter(self):
        """TraceSplitter should not be initialized with default config."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                self.assertIsNone(orch._trace_splitter)
        self._run(_test())

    def test_default_config_disables_archivist(self):
        """Archivist should not be initialized with default config."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                self.assertIsNone(orch._archivist)
        self._run(_test())

    def test_observer_still_enabled(self):
        """Observer should always be initialized (lightweight, needed for transcript)."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                self.assertIsNotNone(orch._observer)
        self._run(_test())

    def test_session_end_no_traces_when_disabled(self):
        """session_end should produce traces=0 when TraceSplitter disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                cm = orch._context_manager
                # Run full lifecycle
                await cm.handle(session_id="s1", phase="prepare",
                                tenant_id="testteam", user_id="alice",
                                turn_id="t1",
                                messages=[{"role": "user", "content": "hello"}])
                await cm.handle(session_id="s1", phase="commit",
                                tenant_id="testteam", user_id="alice",
                                turn_id="t1",
                                messages=[{"role": "user", "content": "hello"},
                                          {"role": "assistant", "content": "hi"}])
                result = await cm.handle(session_id="s1", phase="end",
                                         tenant_id="testteam", user_id="alice")
                self.assertEqual(result["traces"], 0)
        self._run(_test())

    def test_knowledge_search_returns_disabled(self):
        """POST /api/v1/knowledge/search returns error when disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                resp = await client.post("/api/v1/knowledge/search",
                                         json={"query": "test", "limit": 5})
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("error", data)
                self.assertEqual(data["error"], "feature disabled")
        self._run(_test())

    def test_knowledge_candidates_returns_disabled(self):
        """GET /api/v1/knowledge/candidates returns error when disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                resp = await client.get("/api/v1/knowledge/candidates")
                data = resp.json()
                self.assertIn("error", data)
                self.assertEqual(data["error"], "feature disabled")
        self._run(_test())

    def test_archivist_trigger_returns_disabled(self):
        """POST /api/v1/archivist/trigger returns error when disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                resp = await client.post("/api/v1/archivist/trigger")
                data = resp.json()
                self.assertIn("error", data)
                self.assertEqual(data["error"], "feature disabled")
        self._run(_test())

    def test_archivist_status_returns_disabled(self):
        """GET /api/v1/archivist/status returns error when disabled."""
        async def _test():
            async with _shrinkage_test_app() as (client, orch):
                resp = await client.get("/api/v1/archivist/status")
                data = resp.json()
                self.assertIn("error", data)
                self.assertEqual(data["error"], "feature disabled")
        self._run(_test())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run python3 -m unittest tests.test_phase2_shrinkage -v
```

Expected: First 4 tests (config defaults + session_end) should PASS (from Task 1 changes). The 4 HTTP endpoint tests (`test_knowledge_*`, `test_archivist_*`) should FAIL — endpoints currently don't check config.

- [ ] **Step 3: Add config gating to knowledge/archivist endpoints**

In `src/opencortex/http/server.py`, modify each knowledge/archivist endpoint (lines 340-364). Add a config check at the top of each handler:

```python
@app.post("/api/v1/knowledge/search")
async def knowledge_search(req: KnowledgeSearchRequest) -> Dict[str, Any]:
    if not _orchestrator._config.cortex_alpha.archivist_enabled:
        return {"error": "feature disabled"}
    return await _orchestrator.knowledge_search(
        query=req.query, types=req.types, limit=req.limit,
    )

@app.post("/api/v1/knowledge/approve")
async def knowledge_approve(req: KnowledgeApproveRequest) -> Dict[str, Any]:
    if not _orchestrator._config.cortex_alpha.archivist_enabled:
        return {"error": "feature disabled"}
    return await _orchestrator.knowledge_approve(req.knowledge_id)

@app.post("/api/v1/knowledge/reject")
async def knowledge_reject(req: KnowledgeRejectRequest) -> Dict[str, Any]:
    if not _orchestrator._config.cortex_alpha.archivist_enabled:
        return {"error": "feature disabled"}
    return await _orchestrator.knowledge_reject(req.knowledge_id)

@app.get("/api/v1/knowledge/candidates")
async def knowledge_candidates() -> Dict[str, Any]:
    if not _orchestrator._config.cortex_alpha.archivist_enabled:
        return {"error": "feature disabled"}
    return await _orchestrator.knowledge_list_candidates()

@app.post("/api/v1/archivist/trigger")
async def archivist_trigger() -> Dict[str, Any]:
    if not _orchestrator._config.cortex_alpha.archivist_enabled:
        return {"error": "feature disabled"}
    return await _orchestrator.archivist_trigger()

@app.get("/api/v1/archivist/status")
async def archivist_status() -> Dict[str, Any]:
    if not _orchestrator._config.cortex_alpha.archivist_enabled:
        return {"error": "feature disabled"}
    return await _orchestrator.archivist_status()
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run python3 -m unittest tests.test_phase2_shrinkage -v
```

Expected: All tests PASS

- [ ] **Step 5: Run broader regression**

```bash
uv run python3 -m unittest tests.test_alpha_config tests.test_context_manager tests.test_phase2_shrinkage -v
```

Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/http/server.py tests/test_phase2_shrinkage.py
git commit -m "feat(p0): gate knowledge/archivist HTTP endpoints on archivist_enabled config

Endpoints return {\"error\": \"feature disabled\"} when archivist_enabled=False.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Chunk 2: Benchmark Framework + Diagnostic Baseline

### Task 5: Create Benchmark Dataset

**Files:**
- Create: `tests/benchmark/dataset.json`

The dataset contains seed memories (to insert) and queries (to evaluate). 50 queries across 5 categories (10 each), with ground truth memory IDs.

> **Constraint category deferred:** The spec (section 11.1) lists "constraint recall" as one of 5 benchmark categories, but `memory_type=constraint` does not exist in the current schema (added in P2). Constraint queries will be added to the benchmark in the P2 plan. For P0, the 5th category is `hard_keyword` (specialized name/path/config recall).

> **Overview field:** Each memory includes an `overview` field to bypass LLM generation during seeding. Without this, the server would need LLM access to generate three-layer summaries on every seed write.

- [ ] **Step 1: Create dataset file**

Create `tests/benchmark/dataset.json`:

```json
{
  "version": "1.0",
  "description": "Phase 1 P0 diagnostic baseline — 50 queries across 5 categories",
  "memories": [
    {"id": "pref_001", "content": "I always prefer dark mode in all editors and IDEs", "abstract": "User prefers dark mode", "category": "preferences", "context_type": "memory"},
    {"id": "pref_002", "content": "Use 4-space indentation, never tabs", "abstract": "User uses 4-space indentation", "category": "preferences", "context_type": "memory"},
    {"id": "pref_003", "content": "I prefer Python over JavaScript for backend work", "abstract": "User prefers Python for backend", "category": "preferences", "context_type": "memory"},
    {"id": "pref_004", "content": "Always use type hints in Python code", "abstract": "User requires Python type hints", "category": "preferences", "context_type": "memory"},
    {"id": "pref_005", "content": "I like using pytest over unittest for testing", "abstract": "User prefers pytest", "category": "preferences", "context_type": "memory"},
    {"id": "pref_006", "content": "Use async/await whenever possible, avoid synchronous I/O", "abstract": "User prefers async code", "category": "preferences", "context_type": "memory"},
    {"id": "pref_007", "content": "I prefer vim keybindings in all editors", "abstract": "User prefers vim keybindings", "category": "preferences", "context_type": "memory"},
    {"id": "pref_008", "content": "Use conventional commits format for git messages", "abstract": "User uses conventional commits", "category": "preferences", "context_type": "memory"},
    {"id": "pref_009", "content": "I prefer YAML over JSON for configuration files", "abstract": "User prefers YAML for config", "category": "preferences", "context_type": "memory"},
    {"id": "pref_010", "content": "Use uv instead of pip for Python package management", "abstract": "User uses uv package manager", "category": "preferences", "context_type": "memory"},

    {"id": "prof_001", "content": "User is a senior backend engineer at TechCorp, 8 years experience", "abstract": "Senior backend engineer at TechCorp", "category": "profile", "context_type": "memory"},
    {"id": "prof_002", "content": "User's primary programming languages are Python and Go", "abstract": "Codes in Python and Go", "category": "profile", "context_type": "memory"},
    {"id": "prof_003", "content": "User works on the OpenCortex project, a memory system for AI agents", "abstract": "Works on OpenCortex project", "category": "profile", "context_type": "memory"},
    {"id": "prof_004", "content": "User's timezone is Asia/Shanghai, UTC+8", "abstract": "User timezone is UTC+8 Shanghai", "category": "profile", "context_type": "memory"},
    {"id": "prof_005", "content": "User's name is Hugo and he speaks both Chinese and English", "abstract": "Hugo, bilingual Chinese/English", "category": "profile", "context_type": "memory"},
    {"id": "prof_006", "content": "User uses macOS as primary development platform with M2 chip", "abstract": "Uses macOS with Apple M2", "category": "profile", "context_type": "memory"},
    {"id": "prof_007", "content": "User has a background in distributed systems and vector databases", "abstract": "Background in distributed systems", "category": "profile", "context_type": "memory"},
    {"id": "prof_008", "content": "User is the lead maintainer of the OpenCortex open-source project", "abstract": "Lead maintainer of OpenCortex", "category": "profile", "context_type": "memory"},
    {"id": "prof_009", "content": "User's development setup includes Cursor IDE and iTerm2", "abstract": "Uses Cursor IDE and iTerm2", "category": "profile", "context_type": "memory"},
    {"id": "prof_010", "content": "User has experience with Qdrant, Milvus, and Pinecone vector databases", "abstract": "Experience with multiple vector DBs", "category": "profile", "context_type": "memory"},

    {"id": "ent_001", "content": "OpenCortex uses Qdrant as its vector storage backend, running in embedded local mode", "abstract": "OpenCortex uses embedded Qdrant", "category": "entities", "context_type": "memory"},
    {"id": "ent_002", "content": "The HierarchicalRetriever implements wave-based frontier batching search with RL score fusion", "abstract": "HierarchicalRetriever: wave-based search", "category": "entities", "context_type": "memory"},
    {"id": "ent_003", "content": "CortexFS is the three-layer filesystem: L0 abstract, L1 overview, L2 content", "abstract": "CortexFS: three-layer filesystem", "category": "entities", "context_type": "memory"},
    {"id": "ent_004", "content": "IntentRouter has 3-layer query analysis: keywords, LLM, memory triggers", "abstract": "IntentRouter: 3-layer query analysis", "category": "entities", "context_type": "memory"},
    {"id": "ent_005", "content": "The MCP server is a pure Node.js stdio proxy with 9 tools, zero external dependencies", "abstract": "MCP server: Node.js stdio proxy, 9 tools", "category": "entities", "context_type": "memory"},
    {"id": "ent_006", "content": "TraceSplitter uses LLM to decompose conversations into task-level traces", "abstract": "TraceSplitter: LLM conversation decomposition", "category": "entities", "context_type": "memory"},
    {"id": "ent_007", "content": "The scoring formula is: fused = beta * rerank + (1-beta) * retrieval + reward_weight * reward + hot_weight * hotness", "abstract": "Scoring: 4-factor fusion formula", "category": "entities", "context_type": "memory"},
    {"id": "ent_008", "content": "BM25SparseEmbedder is integrated via CompositeHybridEmbedder wrapping all embedding providers", "abstract": "BM25 sparse via CompositeHybridEmbedder", "category": "entities", "context_type": "memory"},
    {"id": "ent_009", "content": "The project structure: src/opencortex/ contains config, orchestrator, http/, context/, storage/, retrieve/, alpha/, ingest/, parse/", "abstract": "Project directory structure overview", "category": "entities", "context_type": "memory"},
    {"id": "ent_010", "content": "ContextManager implements three-phase Memory Context Protocol: prepare, commit, end", "abstract": "ContextManager: prepare/commit/end lifecycle", "category": "entities", "context_type": "memory"},

    {"id": "evt_001", "content": "2026-03-12: Fixed IntentRouter cache key bug — was missing session_context in key, causing stale results", "abstract": "Fixed IntentRouter cache key bug", "category": "events", "context_type": "memory"},
    {"id": "evt_002", "content": "2026-03-10: Completed legacy cleanup — deleted SessionManager, MemoryExtractor, ACE client headers", "abstract": "Legacy cleanup completed", "category": "events", "context_type": "memory"},
    {"id": "evt_003", "content": "2026-03-08: Integrated local embedding (BGE-M3 ONNX via FastEmbed) and reranker (jina-reranker-v2-base-multilingual)", "abstract": "Local embedding + reranker integrated", "category": "events", "context_type": "memory"},
    {"id": "evt_004", "content": "2026-03-05: Migrated 6 files from json to orjson for faster serialization", "abstract": "orjson migration completed", "category": "events", "context_type": "memory"},
    {"id": "evt_005", "content": "2026-03-14: Started Phase 1 optimization planning — reviewed design docs, identified 6 GPT-5.4 findings", "abstract": "Phase 1 optimization planning started", "category": "events", "context_type": "memory"},
    {"id": "evt_006", "content": "2026-03-01: Deployed document scan batch import — ParserRegistry, MarkdownParser, heading-based chunking", "abstract": "Document scan batch import deployed", "category": "events", "context_type": "memory"},
    {"id": "evt_007", "content": "2026-03-11: Fixed ContextManager._end() trying to extract knowledge_candidates from session_end() — key always missing", "abstract": "Fixed knowledge_candidates extraction bug", "category": "events", "context_type": "memory"},
    {"id": "evt_008", "content": "2026-02-28: Ran LoCoMo benchmark — OpenCortex overall F1=0.22 vs baseline F1=0.34", "abstract": "LoCoMo benchmark: OC 0.22 vs BL 0.34", "category": "events", "context_type": "memory"},
    {"id": "evt_009", "content": "2026-03-09: Centralized 9 prompts into src/opencortex/prompts.py — zero internal deps, pure leaf module", "abstract": "Prompt centralization completed", "category": "events", "context_type": "memory"},
    {"id": "evt_010", "content": "2026-03-13: Reviewed Phase 1 optimization plan with GPT-5.4 — fixed constraint merge, baseline timing, dedup scope", "abstract": "GPT-5.4 review findings applied to plan", "category": "events", "context_type": "memory"},

    {"id": "kw_001", "content": "Error QDRANT_COLLECTION_NOT_FOUND (code 404) means the collection was not initialized — call init() on storage adapter first", "abstract": "QDRANT_COLLECTION_NOT_FOUND fix: call init()", "category": "entities", "context_type": "memory"},
    {"id": "kw_002", "content": "Config file paths: server config at ~/.opencortex/server.json, client config at ~/.opencortex/mcp.json", "abstract": "Config paths: server.json + mcp.json", "category": "entities", "context_type": "memory"},
    {"id": "kw_003", "content": "The embedding dimension for multilingual-e5-large is 1024, for BGE-M3 it is 1024", "abstract": "Embedding dimensions: e5-large=1024, BGE-M3=1024", "category": "entities", "context_type": "memory"},
    {"id": "kw_004", "content": "URI format: opencortex://{team}/user/{uid}/{type}/{category}/{node_id}", "abstract": "URI format: opencortex://team/user/uid/type/cat/id", "category": "entities", "context_type": "memory"},
    {"id": "kw_005", "content": "Python entry point: uv run opencortex-server --host 127.0.0.1 --port 8921", "abstract": "Server entry: opencortex-server on port 8921", "category": "entities", "context_type": "memory"},
    {"id": "kw_006", "content": "The _HOTNESS_LAMBDA constant is math.log(2)/7.0 giving a 7-day half-life for hotness decay", "abstract": "_HOTNESS_LAMBDA: ln(2)/7 = 7-day half-life", "category": "entities", "context_type": "memory"},
    {"id": "kw_007", "content": "RequestContextMiddleware parses X-Tenant-ID and X-User-ID from HTTP headers into contextvars", "abstract": "RequestContextMiddleware: headers → contextvars", "category": "entities", "context_type": "memory"},
    {"id": "kw_008", "content": "The file src/opencortex/storage/qdrant/filter_translator.py translates VikingDB DSL into Qdrant Filter objects", "abstract": "filter_translator.py: VikingDB DSL → Qdrant Filter", "category": "entities", "context_type": "memory"},
    {"id": "kw_009", "content": "SCORE_PROPAGATION_ALPHA = 0.5 controls parent score propagation in hierarchical search", "abstract": "SCORE_PROPAGATION_ALPHA=0.5 for hierarchy", "category": "entities", "context_type": "memory"},
    {"id": "kw_010", "content": "Docker: docker compose up -d starts the OpenCortex server container", "abstract": "Docker deployment: docker compose up -d", "category": "entities", "context_type": "memory"}
  ],
  "queries": [
    {"id": "q_pref_01", "query": "What editor theme does the user prefer?", "expected_ids": ["pref_001"], "category": "preference", "difficulty": "easy"},
    {"id": "q_pref_02", "query": "How should I format indentation?", "expected_ids": ["pref_002"], "category": "preference", "difficulty": "easy"},
    {"id": "q_pref_03", "query": "Which language should I use for the backend?", "expected_ids": ["pref_003"], "category": "preference", "difficulty": "medium"},
    {"id": "q_pref_04", "query": "Should I add type annotations?", "expected_ids": ["pref_004"], "category": "preference", "difficulty": "medium"},
    {"id": "q_pref_05", "query": "What test framework does the user like?", "expected_ids": ["pref_005"], "category": "preference", "difficulty": "easy"},
    {"id": "q_pref_06", "query": "Should this API call be async or sync?", "expected_ids": ["pref_006"], "category": "preference", "difficulty": "hard"},
    {"id": "q_pref_07", "query": "What keybindings does the user use?", "expected_ids": ["pref_007"], "category": "preference", "difficulty": "easy"},
    {"id": "q_pref_08", "query": "How should I format the commit message?", "expected_ids": ["pref_008"], "category": "preference", "difficulty": "medium"},
    {"id": "q_pref_09", "query": "Should I use JSON or YAML for the config file?", "expected_ids": ["pref_009"], "category": "preference", "difficulty": "medium"},
    {"id": "q_pref_10", "query": "How to install Python packages in this project?", "expected_ids": ["pref_010"], "category": "preference", "difficulty": "medium"},

    {"id": "q_prof_01", "query": "What is the user's job title?", "expected_ids": ["prof_001"], "category": "profile", "difficulty": "easy"},
    {"id": "q_prof_02", "query": "What programming languages does the user know?", "expected_ids": ["prof_002"], "category": "profile", "difficulty": "easy"},
    {"id": "q_prof_03", "query": "What project is the user working on?", "expected_ids": ["prof_003", "prof_008"], "category": "profile", "difficulty": "easy"},
    {"id": "q_prof_04", "query": "What timezone is the user in?", "expected_ids": ["prof_004"], "category": "profile", "difficulty": "easy"},
    {"id": "q_prof_05", "query": "What is the user's name?", "expected_ids": ["prof_005"], "category": "profile", "difficulty": "easy"},
    {"id": "q_prof_06", "query": "What operating system does the user use for development?", "expected_ids": ["prof_006"], "category": "profile", "difficulty": "easy"},
    {"id": "q_prof_07", "query": "What is the user's technical background?", "expected_ids": ["prof_007", "prof_010"], "category": "profile", "difficulty": "medium"},
    {"id": "q_prof_08", "query": "Who maintains the OpenCortex project?", "expected_ids": ["prof_008"], "category": "profile", "difficulty": "easy"},
    {"id": "q_prof_09", "query": "What IDE does the user use?", "expected_ids": ["prof_009"], "category": "profile", "difficulty": "easy"},
    {"id": "q_prof_10", "query": "What vector databases has the user worked with?", "expected_ids": ["prof_010"], "category": "profile", "difficulty": "medium"},

    {"id": "q_ent_01", "query": "What vector database does OpenCortex use?", "expected_ids": ["ent_001"], "category": "entity", "difficulty": "easy"},
    {"id": "q_ent_02", "query": "How does the search retriever work?", "expected_ids": ["ent_002"], "category": "entity", "difficulty": "medium"},
    {"id": "q_ent_03", "query": "What are the three content layers L0 L1 L2?", "expected_ids": ["ent_003"], "category": "entity", "difficulty": "easy"},
    {"id": "q_ent_04", "query": "How does query intent analysis work?", "expected_ids": ["ent_004"], "category": "entity", "difficulty": "medium"},
    {"id": "q_ent_05", "query": "How is the MCP server implemented?", "expected_ids": ["ent_005"], "category": "entity", "difficulty": "easy"},
    {"id": "q_ent_06", "query": "How are conversations split into traces?", "expected_ids": ["ent_006"], "category": "entity", "difficulty": "medium"},
    {"id": "q_ent_07", "query": "What is the scoring formula for search results?", "expected_ids": ["ent_007"], "category": "entity", "difficulty": "medium"},
    {"id": "q_ent_08", "query": "How is sparse embedding integrated?", "expected_ids": ["ent_008"], "category": "entity", "difficulty": "hard"},
    {"id": "q_ent_09", "query": "What is the source code directory structure?", "expected_ids": ["ent_009"], "category": "entity", "difficulty": "easy"},
    {"id": "q_ent_10", "query": "What is the memory context protocol lifecycle?", "expected_ids": ["ent_010"], "category": "entity", "difficulty": "medium"},

    {"id": "q_evt_01", "query": "What bugs were fixed this week?", "expected_ids": ["evt_001", "evt_007"], "category": "event", "difficulty": "medium"},
    {"id": "q_evt_02", "query": "When was the legacy code cleanup done?", "expected_ids": ["evt_002"], "category": "event", "difficulty": "easy"},
    {"id": "q_evt_03", "query": "When was local embedding integrated?", "expected_ids": ["evt_003"], "category": "event", "difficulty": "easy"},
    {"id": "q_evt_04", "query": "What was the orjson migration about?", "expected_ids": ["evt_004"], "category": "event", "difficulty": "medium"},
    {"id": "q_evt_05", "query": "What happened with Phase 1 planning?", "expected_ids": ["evt_005", "evt_010"], "category": "event", "difficulty": "medium"},
    {"id": "q_evt_06", "query": "When was document scan batch import deployed?", "expected_ids": ["evt_006"], "category": "event", "difficulty": "easy"},
    {"id": "q_evt_07", "query": "What was the knowledge_candidates bug?", "expected_ids": ["evt_007"], "category": "event", "difficulty": "medium"},
    {"id": "q_evt_08", "query": "What was the LoCoMo benchmark F1 score?", "expected_ids": ["evt_008"], "category": "event", "difficulty": "hard"},
    {"id": "q_evt_09", "query": "When were prompts centralized?", "expected_ids": ["evt_009"], "category": "event", "difficulty": "easy"},
    {"id": "q_evt_10", "query": "What did the GPT-5.4 review find?", "expected_ids": ["evt_010"], "category": "event", "difficulty": "hard"},

    {"id": "q_kw_01", "query": "What does QDRANT_COLLECTION_NOT_FOUND error mean?", "expected_ids": ["kw_001"], "category": "hard_keyword", "difficulty": "easy"},
    {"id": "q_kw_02", "query": "Where is server.json located?", "expected_ids": ["kw_002"], "category": "hard_keyword", "difficulty": "easy"},
    {"id": "q_kw_03", "query": "What is the embedding dimension for BGE-M3?", "expected_ids": ["kw_003"], "category": "hard_keyword", "difficulty": "easy"},
    {"id": "q_kw_04", "query": "What is the opencortex URI format?", "expected_ids": ["kw_004"], "category": "hard_keyword", "difficulty": "easy"},
    {"id": "q_kw_05", "query": "How to start opencortex-server?", "expected_ids": ["kw_005"], "category": "hard_keyword", "difficulty": "easy"},
    {"id": "q_kw_06", "query": "What is _HOTNESS_LAMBDA and its half-life?", "expected_ids": ["kw_006"], "category": "hard_keyword", "difficulty": "medium"},
    {"id": "q_kw_07", "query": "How does RequestContextMiddleware work?", "expected_ids": ["kw_007"], "category": "hard_keyword", "difficulty": "medium"},
    {"id": "q_kw_08", "query": "What does filter_translator.py do?", "expected_ids": ["kw_008"], "category": "hard_keyword", "difficulty": "medium"},
    {"id": "q_kw_09", "query": "What is SCORE_PROPAGATION_ALPHA?", "expected_ids": ["kw_009"], "category": "hard_keyword", "difficulty": "hard"},
    {"id": "q_kw_10", "query": "How to deploy with Docker?", "expected_ids": ["kw_010"], "category": "hard_keyword", "difficulty": "easy"}
  ]
}
```

- [ ] **Step 2: Verify JSON is valid**

```bash
python3 -c "import json; d=json.load(open('tests/benchmark/dataset.json')); print(f'{len(d[\"memories\"])} memories, {len(d[\"queries\"])} queries')"
```

Expected: `50 memories, 50 queries`

- [ ] **Step 3: Commit**

```bash
git add tests/benchmark/dataset.json
git commit -m "feat(p0): add benchmark dataset — 50 memories + 50 queries across 5 categories

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 6: Create Benchmark Runner

**Files:**
- Create: `tests/benchmark/runner.py`

The runner seeds memories, queries them, computes metrics via existing `memory_eval.py`, and outputs a structured report.

- [ ] **Step 1: Create the runner**

Create `tests/benchmark/runner.py`:

```python
#!/usr/bin/env python3
"""
OpenCortex Phase 1 Benchmark Runner.

Seeds test memories into a running server, runs queries, computes metrics,
and saves a structured report.

Each run creates an isolated tenant (bench_<uuid>) to prevent cross-run pollution.
JWT is auto-generated from the server's auth_secret.key via --data-root.

Usage:
    # Against running server (real embeddings)
    python tests/benchmark/runner.py --base-url http://127.0.0.1:8921 --data-root ~/.opencortex

    # Save report
    python tests/benchmark/runner.py --data-root /path/to/data --output tests/benchmark/baseline/report.json
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "src"))

from opencortex.auth.token import ensure_secret, generate_token
from opencortex.eval.memory_eval import evaluate_dataset

DATASET_PATH = Path(__file__).resolve().parent / "dataset.json"
DEFAULT_KS = [1, 3, 5]


def _auth_headers(jwt_token: str) -> Dict[str, str]:
    """Build request headers with JWT Bearer auth."""
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {jwt_token}",
    }


def _http_post(base_url: str, path: str, payload: Dict, jwt_token: str, timeout: int = 30) -> Dict:
    """POST JSON to server, return parsed response."""
    url = base_url.rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=_auth_headers(jwt_token), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def seed_memories(base_url: str, memories: List[Dict], jwt_token: str, timeout: int = 30) -> Dict[str, str]:
    """Write benchmark memories to server. Returns {memory_id: uri}."""
    id_to_uri: Dict[str, str] = {}
    for mem in memories:
        payload = {
            "content": mem["content"],
            "abstract": mem.get("abstract", ""),
            "overview": mem.get("overview", mem.get("abstract", "")),
            "category": mem.get("category", ""),
            "context_type": mem.get("context_type", "memory"),
            "dedup": False,
        }
        try:
            result = _http_post(base_url, "/api/v1/memory/store", payload, jwt_token, timeout)
            uri = result.get("uri", "")
            if uri:
                id_to_uri[mem["id"]] = uri
        except Exception as exc:
            print(f"  WARN: Failed to seed {mem['id']}: {exc}", file=sys.stderr)
    return id_to_uri


def search_via_http(
    base_url: str, query: str, limit: int, jwt_token: str, timeout: int = 30
) -> List[str]:
    """Search and return ranked URIs."""
    payload = {"query": query, "limit": limit, "detail_level": "l1"}
    result = _http_post(base_url, "/api/v1/memory/search", payload, jwt_token, timeout)
    items = result.get("results", [])
    return [item.get("uri", "") for item in items if isinstance(item, dict) and item.get("uri")]


def run_benchmark(
    base_url: str,
    data_root: str,
    dataset_path: Optional[str] = None,
    ks: Optional[List[int]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Run full benchmark: generate isolated tenant → seed → query → metrics."""
    ds_path = dataset_path or str(DATASET_PATH)
    ks = ks or DEFAULT_KS

    with open(ds_path, encoding="utf-8") as f:
        dataset = json.load(f)

    memories = dataset.get("memories", [])
    queries = dataset.get("queries", [])

    # Isolation: unique tenant per run prevents cross-run pollution
    run_id = f"bench_{uuid4().hex[:8]}"
    user_id = "runner"
    jwt_token = generate_token(run_id, user_id, ensure_secret(data_root))
    print(f"Run ID: {run_id} (isolated tenant)", file=sys.stderr)

    # Phase 1: Seed memories (always — no skip-seed)
    print(f"Seeding {len(memories)} memories...", file=sys.stderr)
    id_to_uri = seed_memories(base_url, memories, jwt_token, timeout)
    print(f"  Seeded {len(id_to_uri)}/{len(memories)} memories", file=sys.stderr)
    # Brief pause for indexing
    time.sleep(1)

    # Phase 2: Build eval rows with URI-based ground truth
    eval_rows: List[Dict[str, Any]] = []
    for q in queries:
        expected_ids = q.get("expected_ids", [])
        expected_uris = [id_to_uri[mid] for mid in expected_ids if mid in id_to_uri]

        eval_rows.append({
            "query": q["query"],
            "expected_uris": expected_uris,
            "category": q.get("category", "unknown"),
            "difficulty": q.get("difficulty", "unknown"),
            "query_id": q["id"],
        })

    # Phase 3: Run search + compute metrics
    print(f"Running {len(eval_rows)} queries (k={ks})...", file=sys.stderr)

    def _search(item: Dict[str, Any], k: int) -> List[str]:
        return search_via_http(base_url, item["query"], k, jwt_token, timeout)

    report = evaluate_dataset(dataset=eval_rows, ks=ks, search_fn=_search)

    # Add metadata
    report["metadata"] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "base_url": base_url,
        "dataset": ds_path,
        "memories_seeded": len(id_to_uri),
        "queries_total": len(queries),
        "ks": ks,
    }

    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="OpenCortex Phase 1 Benchmark Runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:8921", help="Server URL")
    parser.add_argument("--data-root", default=None,
                        help="Server data_root for JWT generation (default: ~/.opencortex)")
    parser.add_argument("--dataset", default=None, help="Dataset JSON path (default: tests/benchmark/dataset.json)")
    parser.add_argument("--output", default=None, help="Save report to JSON file")
    parser.add_argument("--k", default="1,3,5", help="Comma-separated k values")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout")
    args = parser.parse_args(argv)

    data_root = args.data_root or str(Path.home() / ".opencortex")
    ks = [int(x.strip()) for x in args.k.split(",")]

    report = run_benchmark(
        base_url=args.base_url,
        data_root=data_root,
        dataset_path=args.dataset,
        ks=ks,
        timeout=args.timeout,
    )

    output_str = json.dumps(report, indent=2, ensure_ascii=False)
    print(output_str)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(output_str + "\n", encoding="utf-8")
        print(f"\nReport saved to {args.output}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Verify syntax**

```bash
python3 -c "import ast; ast.parse(open('tests/benchmark/runner.py').read()); print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tests/benchmark/runner.py
git commit -m "feat(p0): add benchmark runner — seed, query, compute metrics, save report

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 7: Unit Test for Benchmark Metric Computation

**Files:**
- Create: `tests/test_benchmark_runner.py`

Tests the metric computation with known inputs (no server needed).

- [ ] **Step 1: Write the test**

Create `tests/test_benchmark_runner.py`:

```python
"""
Unit tests for benchmark runner metric computation.

Tests use known inputs to verify Recall@k, Precision@k, MRR, and
category-level aggregation from memory_eval.py.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.eval.memory_eval import _query_metrics, _aggregate, compute_report


class TestBenchmarkMetrics(unittest.TestCase):
    """Verify metric computation with known inputs."""

    def test_perfect_recall_at_5(self):
        """All expected URIs in top-5 → recall@5 = 1.0."""
        metrics = _query_metrics(
            predicted=["uri_a", "uri_b", "uri_c", "uri_d", "uri_e"],
            expected=["uri_a", "uri_c"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["recall@5"], 1.0)
        self.assertAlmostEqual(metrics["precision@5"], 0.4)  # 2/5
        self.assertAlmostEqual(metrics["hit_rate@5"], 1.0)
        self.assertAlmostEqual(metrics["mrr"], 1.0)  # first hit at rank 1

    def test_zero_recall(self):
        """No expected URIs in results → recall = 0."""
        metrics = _query_metrics(
            predicted=["uri_x", "uri_y"],
            expected=["uri_a"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["recall@5"], 0.0)
        self.assertAlmostEqual(metrics["mrr"], 0.0)

    def test_partial_recall(self):
        """One of two expected found → recall@5 = 0.5."""
        metrics = _query_metrics(
            predicted=["uri_x", "uri_a", "uri_y", "uri_z", "uri_w"],
            expected=["uri_a", "uri_b"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["recall@5"], 0.5)
        self.assertAlmostEqual(metrics["mrr"], 0.5)  # first hit at rank 2

    def test_mrr_rank_position(self):
        """MRR reflects rank of first relevant result."""
        metrics = _query_metrics(
            predicted=["uri_x", "uri_y", "uri_a"],
            expected=["uri_a"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["mrr"], 1.0 / 3)

    def test_multiple_k_values(self):
        """Different k values produce different metrics."""
        metrics = _query_metrics(
            predicted=["uri_x", "uri_y", "uri_a"],
            expected=["uri_a"],
            ks=[1, 3, 5],
        )
        self.assertAlmostEqual(metrics["recall@1"], 0.0)   # not in top 1
        self.assertAlmostEqual(metrics["recall@3"], 1.0)   # in top 3
        self.assertAlmostEqual(metrics["recall@5"], 1.0)   # in top 5

    def test_aggregate_averages(self):
        """Aggregate averages per-query metrics."""
        row1 = {"recall@5": 1.0, "precision@5": 0.4, "hit_rate@5": 1.0, "accuracy@5": 1.0, "mrr": 1.0}
        row2 = {"recall@5": 0.0, "precision@5": 0.0, "hit_rate@5": 0.0, "accuracy@5": 0.0, "mrr": 0.0}
        agg = _aggregate([row1, row2], ks=[5])
        self.assertAlmostEqual(agg["recall@5"], 0.5)
        self.assertAlmostEqual(agg["mrr"], 0.5)
        self.assertAlmostEqual(agg["count"], 2.0)

    def test_compute_report_with_categories(self):
        """Report groups metrics by category."""
        rows = [
            {"query": "q1", "expected_uris": ["a"], "predicted_uris": ["a", "b"], "category": "preference"},
            {"query": "q2", "expected_uris": ["c"], "predicted_uris": ["x", "y"], "category": "preference"},
            {"query": "q3", "expected_uris": ["d"], "predicted_uris": ["d", "e"], "category": "entity"},
        ]
        report = compute_report(rows, ks=[5])
        self.assertEqual(report["scored_count"], 3)
        self.assertIn("preference", report["by_category"])
        self.assertIn("entity", report["by_category"])
        self.assertAlmostEqual(report["by_category"]["entity"]["recall@5"], 1.0)
        self.assertAlmostEqual(report["by_category"]["preference"]["recall@5"], 0.5)

    def test_empty_predicted_gives_zero(self):
        """No predictions → all metrics zero."""
        metrics = _query_metrics(
            predicted=[],
            expected=["uri_a"],
            ks=[5],
        )
        self.assertAlmostEqual(metrics["recall@5"], 0.0)
        self.assertAlmostEqual(metrics["mrr"], 0.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests**

```bash
uv run python3 -m unittest tests.test_benchmark_runner -v
```

Expected: All 8 tests PASS (these test existing `memory_eval.py` logic, no code changes needed)

- [ ] **Step 3: Commit**

```bash
git add tests/test_benchmark_runner.py
git commit -m "test(p0): add unit tests for benchmark metric computation

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

### Task 8: Run Diagnostic Baseline

This task requires a running OpenCortex server with real embeddings. It produces the P0 diagnostic baseline report.

**Files:**
- Create: `tests/benchmark/baseline/` directory
- Create: `tests/benchmark/baseline/p0-diagnostic.json` (output)

- [ ] **Step 1: Start the server (if not running)**

```bash
uv run opencortex-server --host 127.0.0.1 --port 8921 &
sleep 3
curl -s http://127.0.0.1:8921/api/v1/system/status | python3 -m json.tool
```

Expected: Server status response with `"status": "ok"`

- [ ] **Step 2: Run the benchmark**

```bash
python3 tests/benchmark/runner.py \
    --base-url http://127.0.0.1:8921 \
    --data-root ~/.opencortex \
    --output tests/benchmark/baseline/p0-diagnostic.json
```

Expected: Prints JSON report to stdout + saves to file. Metrics will reflect current system quality (with known link defects — this is the diagnostic baseline, not the regression red line).

- [ ] **Step 3: Review baseline results**

```bash
python3 -c "
import json
r = json.load(open('tests/benchmark/baseline/p0-diagnostic.json'))
s = r['summary']
print(f'Recall@5:    {s.get(\"recall@5\", 0):.3f}')
print(f'Precision@5: {s.get(\"precision@5\", 0):.3f}')
print(f'MRR:         {s.get(\"mrr\", 0):.3f}')
print(f'Hit Rate@5:  {s.get(\"hit_rate@5\", 0):.3f}')
print()
for cat, m in r.get('by_category', {}).items():
    print(f'  {cat:15s}  R@5={m.get(\"recall@5\",0):.3f}  MRR={m.get(\"mrr\",0):.3f}  n={int(m.get(\"count\",0))}')
"
```

- [ ] **Step 4: Commit baseline**

```bash
git add tests/benchmark/baseline/p0-diagnostic.json
git commit -m "data(p0): diagnostic baseline — Phase 2 shrunk, pre-P1 link fixes

This baseline measures the system with known link defects.
It is NOT the regression red line — that comes after P1 fixes.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>"
```

---

## Post-Plan: Verification Checklist

After all tasks are complete, verify:

- [ ] `CortexAlphaConfig` defaults: `trace_splitter_enabled=False`, `archivist_enabled=False`
- [ ] `ContextManager.prepare()` defaults: `include_knowledge=False`
- [ ] MCP `recall` tool: `include_knowledge` defaults to `false`
- [ ] Knowledge/archivist HTTP endpoints return `{"error": "feature disabled"}` when config is off
- [ ] Observer still initialized (transcript recording preserved)
- [ ] All existing tests pass: `uv run python3 -m unittest discover -s tests -v`
- [ ] Benchmark dataset has 50 memories + 50 queries
- [ ] Benchmark runner can seed, query, and produce report
- [ ] Diagnostic baseline saved to `tests/benchmark/baseline/p0-diagnostic.json`

## Subsequent Plans

This plan covers P0 only. After P0 is complete:

- **P1 plan**: Pipeline correctness (protocol passthrough, filter DSL, Alpha interface breakpoints, acceptance baseline)
- **P2 plan**: Data model convergence (memory_type, source_type, status, migration)
- **P3 plan**: Persistence reliability (durable buffer, contract unification)
- **P4 plan**: Retrieval accuracy (time_scope, Ebbinghaus freshness, 3-factor scoring)
- **P5 plan**: Explainability (explain API, query plan visualization)
