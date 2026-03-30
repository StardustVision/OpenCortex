# Auto Memory Enhancement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add memory index API, prompt-level storage guidance, and server-side store warnings to improve memory quality.

**Architecture:** Three independent changes: (1) new `memory_index()` orchestrator method + HTTP endpoint + MCP tool for listing all memories grouped by type, (2) text additions to the MCP usage-guide prompt, (3) warning checks in the HTTP store handler. No enum/schema/pipeline changes.

**Tech Stack:** Python (FastAPI, Qdrant), Node.js (MCP server)

---

### Task 1: Orchestrator `memory_index()` method

**Files:**
- Modify: `src/opencortex/orchestrator.py` (after `list_memories` method, ~line 1784)
- Test: `tests/test_e2e_phase1.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py`, at the end of `TestE2EPhase1`:

```python
def test_25_memory_index(self):
    """memory_index returns memories grouped by context_type."""
    orch = self._init_orch()

    # Store memories of different types
    self._run(orch.add(abstract="User prefers dark theme", category="preferences"))
    self._run(orch.add(abstract="API docs at example.com", context_type="resource"))
    self._run(orch.add(abstract="Retry pattern for flaky tests", context_type="pattern"))

    # Full index (no filter)
    result = self._run(orch.memory_index())
    self.assertIn("index", result)
    self.assertIn("total", result)
    self.assertGreaterEqual(result["total"], 3)

    # All returned items must have uri + abstract + context_type + category + created_at
    for group_items in result["index"].values():
        for item in group_items:
            self.assertIn("uri", item)
            self.assertIn("abstract", item)
            self.assertIn("context_type", item)
            self.assertIn("category", item)
            self.assertIn("created_at", item)
            self.assertLessEqual(len(item["abstract"]), 150)

    # Filter by context_type
    filtered = self._run(orch.memory_index(context_type="resource"))
    self.assertIn("resource", filtered["index"])
    # Should not contain memory or pattern groups
    for key in filtered["index"]:
        self.assertEqual(key, "resource")

    # Limit
    limited = self._run(orch.memory_index(limit=1))
    self.assertLessEqual(limited["total"], 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestE2EPhase1.test_25_memory_index -v`
Expected: FAIL with `AttributeError: 'MemoryOrchestrator' object has no attribute 'memory_index'`

- [ ] **Step 3: Implement `memory_index()` in orchestrator**

Add after the `list_memories` method (around line 1784) in `src/opencortex/orchestrator.py`:

```python
async def memory_index(
    self,
    context_type: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Return a lightweight index of all memories, grouped by context_type.

    Uses filter() instead of scroll() to reuse the existing scope/project
    isolation logic from list_memories. No embedding needed.
    """
    self._ensure_init()
    tid, uid = get_effective_identity()

    scope_filter = {"op": "or", "conds": [
        {"op": "must", "field": "scope", "conds": ["shared", ""]},
        {"op": "and", "conds": [
            {"op": "must", "field": "scope", "conds": ["private"]},
            {"op": "must", "field": "source_user_id", "conds": [uid]},
        ]},
    ]}

    conds: List[Dict[str, Any]] = [
        {"op": "must_not", "field": "context_type", "conds": ["staging"]},
        {"op": "must", "field": "is_leaf", "conds": [True]},
        scope_filter,
    ]
    if tid:
        conds.append({"op": "must", "field": "source_tenant_id", "conds": [tid, ""]})

    if context_type:
        types = [t.strip() for t in context_type.split(",") if t.strip()]
        conds.append({"op": "must", "field": "context_type", "conds": types})

    project_id = get_effective_project_id()
    if project_id and project_id != "public":
        conds.append({"op": "or", "conds": [
            {"op": "must", "field": "project_id", "conds": [project_id, "public"]},
        ]})

    records = await self._storage.filter(
        self._get_collection(),
        {"op": "and", "conds": conds},
        limit=limit,
        offset=0,
        order_by="created_at",
        order_desc=True,
    )

    index: Dict[str, list] = {}
    for r in records:
        abstract = r.get("abstract", "")
        if not abstract:
            continue
        ct = r.get("context_type", "memory")
        if ct not in index:
            index[ct] = []
        index[ct].append({
            "uri": r.get("uri", ""),
            "abstract": abstract[:150],
            "context_type": ct,
            "category": r.get("category", ""),
            "created_at": r.get("created_at", ""),
        })

    total = sum(len(v) for v in index.values())
    return {"index": index, "total": total}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestE2EPhase1.test_25_memory_index -v`
Expected: PASS

- [ ] **Step 5: Run full e2e test suite to check no regressions**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All existing tests pass

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_e2e_phase1.py
git commit -m "feat: add memory_index() orchestrator method"
```

---

### Task 2: HTTP endpoint `GET /api/v1/memory/index`

**Files:**
- Modify: `src/opencortex/http/server.py` (after `memory_list` endpoint, ~line 328)

- [ ] **Step 1: Add the endpoint**

In `src/opencortex/http/server.py`, add after the `memory_list` endpoint (line 328):

```python
@app.get("/api/v1/memory/index")
async def memory_index(
    context_type: Optional[str] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    """Lightweight index of all memories, grouped by type."""
    return await _orchestrator.memory_index(
        context_type=context_type,
        limit=limit,
    )
```

- [ ] **Step 2: Verify server starts**

Run: `uv run python3 -c "from opencortex.http.server import create_app; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/opencortex/http/server.py
git commit -m "feat: add GET /api/v1/memory/index endpoint"
```

---

### Task 3: MCP `memory_index` tool

**Files:**
- Modify: `plugins/opencortex-memory/lib/mcp-server.mjs` (TOOLS dict, ~line 79)

- [ ] **Step 1: Add tool definition to TOOLS dict**

In `plugins/opencortex-memory/lib/mcp-server.mjs`, add after the `system_status` entry (line 82), inside the `const TOOLS = {` block:

```javascript
  memory_index: ['GET', '/api/v1/memory/index',
    'Get a lightweight index of all stored memories, grouped by type. '
    + 'Call at session start to understand what context is available. '
    + 'Returns {index: {memory: [...], resource: [...]}, total}. '
    + 'Each entry has: uri, abstract (≤150 chars), context_type, category, created_at.', {
      context_type: { type: 'string', description: 'Comma-separated types to include (memory,resource,skill,case,pattern). Omit for all' },
      limit:        { type: 'integer', description: 'Max records to return', default: 200 },
    }],
```

The tool is automatically proxied via `callProxyTool` (GET method + query params) — no handler code needed.

- [ ] **Step 2: Verify syntax**

Run: `node --check plugins/opencortex-memory/lib/mcp-server.mjs`
Expected: no output (clean parse)

- [ ] **Step 3: Commit**

```bash
git add plugins/opencortex-memory/lib/mcp-server.mjs
git commit -m "feat: add memory_index MCP tool"
```

---

### Task 4: Storage guidance in usage-guide prompt

**Files:**
- Modify: `plugins/opencortex-memory/lib/mcp-server.mjs` (getPromptContent function, ~line 274)

- [ ] **Step 1: Add Memory Storage Guide section to usage-guide prompt**

In `plugins/opencortex-memory/lib/mcp-server.mjs`, in the `getPromptContent()` function, add the following text BEFORE the closing backtick of the template literal (line 334, before the final `` ` ``). Insert it after the Tool Quick Reference table:

```javascript
## Memory Storage Guide

### What to Store
- **User context**: Role, expertise, preferences, working style, communication style
- **Behavioral feedback**: Corrections to your approach, confirmed good patterns, things to avoid
- **Project context**: Active goals, deadlines (use absolute dates), key decisions, blockers
- **Reference pointers**: URLs, doc locations, tool configurations, reusable procedures

### What NOT to Store
- Code structure, file paths, architecture — derivable from reading the codebase
- Git history, recent changes — use git log / git blame
- Debugging steps or fix recipes — the fix is in the code, the context in the commit
- Anything already in CLAUDE.md, AGENTS.md, or project docs
- Ephemeral task state or current conversation context
- Raw code snippets — store a description of the pattern instead

### Storage Tips
- Use descriptive abstracts (>10 chars) that capture the "why" not just the "what"
- Set a meaningful category to improve dedup and retrieval
- Convert relative dates to absolute dates before storing
```

- [ ] **Step 2: Verify syntax**

Run: `node --check plugins/opencortex-memory/lib/mcp-server.mjs`
Expected: no output (clean parse)

- [ ] **Step 3: Commit**

```bash
git add plugins/opencortex-memory/lib/mcp-server.mjs
git commit -m "feat: add memory storage guidance to usage-guide prompt"
```

---

### Task 5: Server soft checks (store warnings)

**Files:**
- Modify: `src/opencortex/http/server.py` (store handler, ~line 216)
- Test: `tests/test_e2e_phase1.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_e2e_phase1.py`, at the end of `TestE2EPhase1`:

```python
def test_26_store_warnings_short_abstract(self):
    """Store with short abstract returns warning."""
    orch = self._init_orch()
    # The warning logic is in the HTTP layer, not orchestrator.
    # Test the helper function directly.
    from opencortex.http.server import _check_store_warnings
    warnings = _check_store_warnings("hi")
    self.assertEqual(len(warnings), 1)
    self.assertEqual(warnings[0]["key"], "abstract_too_short")

def test_27_store_warnings_code_snippet(self):
    """Store with code-heavy abstract returns warning."""
    from opencortex.http.server import _check_store_warnings
    code_text = (
        "def foo():\n"
        "    return 42\n"
        "def bar():\n"
        "    return 99\n"
        "class Baz:\n"
        "    pass\n"
    )
    warnings = _check_store_warnings(code_text)
    self.assertEqual(len(warnings), 1)
    self.assertEqual(warnings[0]["key"], "code_snippet_detected")

def test_28_store_warnings_clean(self):
    """Normal abstract returns no warnings."""
    from opencortex.http.server import _check_store_warnings
    warnings = _check_store_warnings("User prefers dark theme in all editors and IDEs")
    self.assertEqual(len(warnings), 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestE2EPhase1.test_26_store_warnings_short_abstract tests.test_e2e_phase1.TestE2EPhase1.test_27_store_warnings_code_snippet tests.test_e2e_phase1.TestE2EPhase1.test_28_store_warnings_clean -v`
Expected: FAIL with `ImportError: cannot import name '_check_store_warnings'`

- [ ] **Step 3: Add `_check_store_warnings` helper and integrate into store handler**

In `src/opencortex/http/server.py`, add at module level (after imports, before `create_app`):

```python
import re

_CODE_PATTERN = re.compile(
    r"^\s*(def |class |import |from |if |for |while |return |"
    r"const |let |var |function |\{|\}|//|#!)"
)


def _check_store_warnings(abstract: str) -> list:
    """Return advisory warnings for a store request. Never blocks storage."""
    warnings = []
    stripped = abstract.strip()
    if len(stripped) < 10:
        warnings.append({
            "key": "abstract_too_short",
            "message": "Memory abstract should be at least 10 characters for useful retrieval",
        })
        return warnings  # skip code check for very short text

    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) >= 2:
        code_lines = sum(1 for ln in lines if _CODE_PATTERN.match(ln))
        if code_lines / len(lines) > 0.8:
            warnings.append({
                "key": "code_snippet_detected",
                "message": "Consider storing a description of the code pattern rather than raw code",
            })
    return warnings
```

Then modify the `memory_store` handler (line 216) to include warnings:

```python
@app.post("/api/v1/memory/store")
async def memory_store(req: MemoryStoreRequest) -> Dict[str, Any]:
    warnings = _check_store_warnings(req.abstract)
    result = await _orchestrator.add(
        abstract=req.abstract,
        content=req.content,
        overview=req.overview,
        category=req.category,
        context_type=req.context_type,
        meta=req.meta,
        dedup=req.dedup,
        embed_text=req.embed_text,
    )
    resp: Dict[str, Any] = {
        "uri": result.uri,
        "context_type": result.context_type,
        "category": result.category,
        "abstract": result.abstract,
    }
    if result.overview:
        resp["overview"] = result.overview
    dedup_action = result.meta.get("dedup_action")
    if dedup_action:
        resp["dedup_action"] = dedup_action
    if warnings:
        resp["warnings"] = warnings
    return resp
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python3 -m unittest tests.test_e2e_phase1.TestE2EPhase1.test_26_store_warnings_short_abstract tests.test_e2e_phase1.TestE2EPhase1.test_27_store_warnings_code_snippet tests.test_e2e_phase1.TestE2EPhase1.test_28_store_warnings_clean -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run full e2e test suite**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/http/server.py tests/test_e2e_phase1.py
git commit -m "feat: add store quality warnings (short abstract, code detection)"
```
