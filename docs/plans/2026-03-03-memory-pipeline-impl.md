# Memory Pipeline Enhancement Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement dual ID system (Snowflake + semantic URI), session LLM extraction, and deterministic document import with scope promotion.

**Architecture:** Four phases — (1) Snowflake ID + semantic URI naming, (2) session hooks → LLM extraction, (3) oc-scan + batch_store, (4) promote_to_shared. Each phase is independently testable and deployable.

**Tech Stack:** Python 3.10+ async, Node.js 18+ (hooks/scan), Qdrant embedded, FastAPI, httpx

**Design doc:** `docs/plans/2026-03-03-memory-pipeline-design.md`

---

## Phase 1: Dual ID System

### Task 1: Snowflake ID Generator

**Files:**
- Create: `src/opencortex/utils/id_generator.py`
- Test: `tests/test_id_generator.py`

**Step 1: Write the failing test**

```python
# tests/test_id_generator.py
import unittest
import threading


class TestSnowflakeGenerator(unittest.TestCase):

    def test_generates_positive_int(self):
        from opencortex.utils.id_generator import generate_id
        sid = generate_id()
        self.assertIsInstance(sid, int)
        self.assertGreater(sid, 0)

    def test_uniqueness_single_thread(self):
        from opencortex.utils.id_generator import generate_id
        ids = [generate_id() for _ in range(1000)]
        self.assertEqual(len(set(ids)), 1000)

    def test_monotonically_increasing(self):
        from opencortex.utils.id_generator import generate_id
        ids = [generate_id() for _ in range(100)]
        self.assertEqual(ids, sorted(ids))

    def test_uniqueness_multi_thread(self):
        from opencortex.utils.id_generator import generate_id
        results = []
        def worker():
            for _ in range(500):
                results.append(generate_id())
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(results)), 2000)

    def test_fits_in_64_bits(self):
        from opencortex.utils.id_generator import generate_id
        sid = generate_id()
        self.assertLess(sid, 2**63)


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_id_generator -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'opencortex.utils.id_generator'`

**Step 3: Write implementation**

Port from OpenViking `/Users/hugo/CodeSpace/Work/OpenViking/openviking/storage/vectordb/utils/id_generator.py`:

```python
# src/opencortex/utils/id_generator.py
# Ported from OpenViking (https://github.com/volcengine/openviking)
# SPDX-License-Identifier: Apache-2.0
"""
Distributed unique ID generator based on Twitter's Snowflake algorithm.

Generates 64-bit integers suitable for Qdrant point IDs and future
distributed storage migration.

Structure (64 bits):
  1 bit unused | 41 bits timestamp(ms) | 5 bits datacenter | 5 bits worker | 12 bits sequence
"""
import os
import random
import threading
import time


class SnowflakeGenerator:
    EPOCH = 1704067200000  # 2024-01-01 00:00:00 UTC

    worker_id_bits = 5
    datacenter_id_bits = 5
    sequence_bits = 12

    max_worker_id = -1 ^ (-1 << worker_id_bits)          # 31
    max_datacenter_id = -1 ^ (-1 << datacenter_id_bits)   # 31
    max_sequence = -1 ^ (-1 << sequence_bits)              # 4095

    worker_id_shift = sequence_bits                        # 12
    datacenter_id_shift = sequence_bits + worker_id_bits   # 17
    timestamp_left_shift = sequence_bits + worker_id_bits + datacenter_id_bits  # 22

    def __init__(self, worker_id: int = None, datacenter_id: int = None):
        if worker_id is None:
            worker_id = os.getpid() & self.max_worker_id
        if datacenter_id is None:
            datacenter_id = random.randint(0, self.max_datacenter_id)

        if not (0 <= worker_id <= self.max_worker_id):
            raise ValueError(f"worker_id must be 0..{self.max_worker_id}")
        if not (0 <= datacenter_id <= self.max_datacenter_id):
            raise ValueError(f"datacenter_id must be 0..{self.max_datacenter_id}")

        self.worker_id = worker_id
        self.datacenter_id = datacenter_id
        self.sequence = 0
        self.last_timestamp = -1
        self.lock = threading.Lock()

    def _current_timestamp(self) -> int:
        return int(time.time() * 1000)

    def next_id(self) -> int:
        with self.lock:
            timestamp = self._current_timestamp()

            if timestamp < self.last_timestamp:
                offset = self.last_timestamp - timestamp
                if offset <= 5:
                    time.sleep(offset / 1000.0 + 0.001)
                    timestamp = self._current_timestamp()
                if timestamp < self.last_timestamp:
                    raise RuntimeError(
                        f"Clock moved backwards by {self.last_timestamp - timestamp}ms"
                    )

            if self.last_timestamp == timestamp:
                self.sequence = (self.sequence + 1) & self.max_sequence
                if self.sequence == 0:
                    while timestamp <= self.last_timestamp:
                        timestamp = self._current_timestamp()
            else:
                self.sequence = 0

            self.last_timestamp = timestamp

            return (
                ((timestamp - self.EPOCH) << self.timestamp_left_shift)
                | (self.datacenter_id << self.datacenter_id_shift)
                | (self.worker_id << self.worker_id_shift)
                | self.sequence
            )


_default_generator = SnowflakeGenerator()


def generate_id() -> int:
    """Generate a globally unique 64-bit integer ID."""
    return _default_generator.next_id()
```

**Step 4: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.test_id_generator -v`
Expected: All 5 tests PASS

**Step 5: Commit**

```bash
git add src/opencortex/utils/id_generator.py tests/test_id_generator.py
git commit -m "feat: add Snowflake ID generator (port from OpenViking)"
```

---

### Task 2: Integrate Snowflake ID into Qdrant Adapter

**Files:**
- Modify: `src/opencortex/storage/qdrant/adapter.py:735-759` (`_to_point` method)
- Test: `uv run python3 -m unittest tests.test_e2e_phase1 -v` (existing tests must still pass)

**Step 1: Modify `_to_point` to accept integer IDs**

In `src/opencortex/storage/qdrant/adapter.py`, update `_to_point` (line 735) and `_to_point_id` (line 784):

```python
# Replace _to_point_id (line 783-795)
@staticmethod
def _to_point_id(raw_id) -> int | str:
    """Convert raw ID to a Qdrant-compatible point ID.

    Accepts:
      - int (Snowflake) -> use directly
      - valid UUID string -> use as UUID
      - other string -> derive UUID5
    """
    if isinstance(raw_id, int):
        return raw_id
    try:
        return str(uuid.UUID(str(raw_id)))
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, str(raw_id)))
```

Update `_to_point` (line 735) — change the fallback from `str(uuid.uuid4())` to use Snowflake:

```python
def _to_point(self, data: Dict[str, Any]) -> models.PointStruct:
    """Convert a VikingDBInterface data dict to a Qdrant PointStruct."""
    raw_id = data.pop("id", None)
    if raw_id is None:
        from opencortex.utils.id_generator import generate_id
        raw_id = generate_id()
    point_id = self._to_point_id(raw_id)
    # ... rest unchanged
```

**Step 2: Run existing tests to verify no regression**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All 24 tests PASS (existing records with UUID IDs still work via `_to_point_id`)

**Step 3: Commit**

```bash
git add src/opencortex/storage/qdrant/adapter.py
git commit -m "feat: integrate Snowflake ID as default Qdrant point ID"
```

---

### Task 3: Semantic URI Naming

**Files:**
- Create: `src/opencortex/utils/semantic_name.py`
- Test: `tests/test_semantic_name.py`

**Step 1: Write the failing test**

```python
# tests/test_semantic_name.py
import unittest


class TestSemanticNodeName(unittest.TestCase):

    def test_ascii_passthrough(self):
        from opencortex.utils.semantic_name import semantic_node_name
        self.assertEqual(semantic_node_name("hello_world"), "hello_world")

    def test_chinese_preserved(self):
        from opencortex.utils.semantic_name import semantic_node_name
        result = semantic_node_name("用户偏好深色主题")
        self.assertEqual(result, "用户偏好深色主题")

    def test_special_chars_replaced(self):
        from opencortex.utils.semantic_name import semantic_node_name
        result = semantic_node_name("Fix: import error (PYTHONPATH)")
        self.assertNotIn(":", result)
        self.assertNotIn("(", result)
        self.assertNotIn(")", result)

    def test_consecutive_underscores_merged(self):
        from opencortex.utils.semantic_name import semantic_node_name
        result = semantic_node_name("a:::b")
        self.assertNotIn("__", result)

    def test_truncation_with_hash(self):
        from opencortex.utils.semantic_name import semantic_node_name
        long_text = "a" * 100
        result = semantic_node_name(long_text, max_length=50)
        self.assertLessEqual(len(result), 50)
        # Should end with _<8-char-hash>
        self.assertRegex(result, r"_[a-f0-9]{8}$")

    def test_empty_returns_unnamed(self):
        from opencortex.utils.semantic_name import semantic_node_name
        self.assertEqual(semantic_node_name(""), "unnamed")
        self.assertEqual(semantic_node_name("!!!"), "unnamed")

    def test_deterministic(self):
        from opencortex.utils.semantic_name import semantic_node_name
        a = semantic_node_name("同一输入每次结果相同")
        b = semantic_node_name("同一输入每次结果相同")
        self.assertEqual(a, b)

    def test_short_text_no_hash(self):
        from opencortex.utils.semantic_name import semantic_node_name
        result = semantic_node_name("short")
        self.assertNotIn("_", result.replace("short", ""))


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_semantic_name -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write implementation**

```python
# src/opencortex/utils/semantic_name.py
# SPDX-License-Identifier: Apache-2.0
"""
Semantic node naming for OpenCortex URIs.

Ported from OpenViking's VikingURI.sanitize_segment pattern.
Produces deterministic, human-readable URI segments from arbitrary text.
"""
import hashlib
import re


def semantic_node_name(text: str, max_length: int = 50) -> str:
    """Sanitize text for use as a URI node name.

    Preserves letters, digits, CJK characters, underscores, and hyphens.
    Replaces all other characters with underscores. Merges consecutive
    underscores. If the result exceeds *max_length*, truncates and appends
    a SHA-256 hash suffix for uniqueness.

    Args:
        text: Input text (e.g., abstract, filename).
        max_length: Maximum output length (default 50).

    Returns:
        URI-safe, deterministic node name. Returns ``"unnamed"`` for empty input.
    """
    safe = re.sub(
        r"[^\w\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af\u3400-\u4dbf-]",
        "_",
        text,
    )
    safe = re.sub(r"_+", "_", safe).strip("_")

    if not safe:
        return "unnamed"

    if len(safe) > max_length:
        hash_suffix = hashlib.sha256(text.encode()).hexdigest()[:8]
        safe = f"{safe[:max_length - 9]}_{hash_suffix}"

    return safe
```

**Step 4: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.test_semantic_name -v`
Expected: All 8 tests PASS

**Step 5: Commit**

```bash
git add src/opencortex/utils/semantic_name.py tests/test_semantic_name.py
git commit -m "feat: add semantic node naming for URIs (OpenViking pattern)"
```

---

### Task 4: Replace `_auto_uri()` with Semantic Names + Conflict Resolution

**Files:**
- Modify: `src/opencortex/orchestrator.py:2026-2066` (`_auto_uri`)
- Add: `_resolve_unique_uri` method near `_auto_uri`
- Test: `uv run python3 -m unittest tests.test_e2e_phase1 -v` (existing tests must still pass)

**Step 1: Add `_uri_exists` and `_resolve_unique_uri` to orchestrator**

Add after `_auto_uri` (around line 2067):

```python
async def _uri_exists(self, uri: str) -> bool:
    """Check if a URI already exists in the context collection."""
    try:
        results = await self._storage.search(
            _CONTEXT_COLLECTION,
            filter={"op": "must", "field": "uri", "conds": [uri]},
            limit=1,
        )
        return len(results) > 0
    except Exception:
        return False

async def _resolve_unique_uri(self, uri: str, max_attempts: int = 100) -> str:
    """Ensure URI is unique, appending _1, _2, ... if needed."""
    if not await self._uri_exists(uri):
        return uri
    for i in range(1, max_attempts + 1):
        candidate = f"{uri}_{i}"
        if not await self._uri_exists(candidate):
            return candidate
    raise ValueError(f"URI conflict unresolved after {max_attempts} attempts: {uri}")
```

**Step 2: Update `_auto_uri` to use semantic names**

Replace lines 2026-2066:

```python
def _auto_uri(self, context_type: str, category: str, abstract: str = "") -> str:
    """Generate a URI based on context type, category, and abstract text.

    Uses semantic node names (deterministic) instead of random UUIDs.
    """
    from opencortex.utils.semantic_name import semantic_node_name

    tid, uid = get_effective_identity()
    node_name = semantic_node_name(abstract) if abstract else uuid4().hex[:12]

    if context_type == "memory":
        cat = category if category in self._USER_MEMORY_CATEGORIES else "events"
        return CortexURI.build_private(tid, uid, "memories", cat, node_name)

    elif context_type == "case":
        return CortexURI.build_shared(tid, "shared", "cases", node_name)

    elif context_type == "pattern":
        return CortexURI.build_shared(tid, "shared", "patterns", node_name)

    elif context_type == "skill":
        section = category or "general"
        return CortexURI.build_shared(tid, "shared", "skills", section, node_name)

    elif context_type == "resource":
        project = get_effective_project_id()
        if category:
            return CortexURI.build_shared(tid, "resources", project, category, node_name)
        return CortexURI.build_shared(tid, "resources", project, node_name)

    elif context_type == "staging":
        return CortexURI.build_private(tid, uid, "staging", node_name)

    return CortexURI.build_private(tid, uid, "memories", "events", node_name)
```

**Step 3: Update `add()` to pass abstract to `_auto_uri` and resolve conflicts**

In `add()` (line 584-586), change:

```python
# Before
if not uri:
    uri = self._auto_uri(context_type or "memory", category)

# After
if not uri:
    uri = self._auto_uri(context_type or "memory", category, abstract=abstract)
    uri = await self._resolve_unique_uri(uri)
```

**Step 4: Run existing tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All 24 tests PASS

**Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py
git commit -m "feat: semantic URI naming with conflict resolution"
```

---

## Phase 2: Session LLM Extraction

### Task 5: Add `extract_turn` to SessionManager

**Files:**
- Modify: `src/opencortex/session/manager.py`
- Test: `tests/test_session_extract_turn.py`

**Step 1: Write the failing test**

```python
# tests/test_session_extract_turn.py
import asyncio
import unittest
from unittest.mock import AsyncMock


class TestExtractTurn(unittest.TestCase):

    def test_extract_turn_returns_extraction_result(self):
        async def _test():
            from opencortex.session.manager import SessionManager
            from opencortex.session.types import ExtractionResult

            llm = AsyncMock(return_value='[{"abstract": "User likes dark mode", "content": "Explicit preference", "category": "preferences", "context_type": "memory", "confidence": 0.9}]')
            store = AsyncMock()
            store.return_value = type("Ctx", (), {"meta": {"dedup_action": "created"}})()

            mgr = SessionManager(llm_completion=llm, store_fn=store)
            await mgr.begin("s1")
            await mgr.add_message("s1", "user", "I prefer dark mode")
            await mgr.add_message("s1", "assistant", "Noted, dark mode it is")

            result = await mgr.extract_turn("s1")
            self.assertIsInstance(result, ExtractionResult)
            self.assertGreater(result.stored_count + result.merged_count + result.skipped_count, 0)

        asyncio.run(_test())

    def test_extract_turn_no_session(self):
        async def _test():
            from opencortex.session.manager import SessionManager
            from opencortex.session.types import ExtractionResult

            mgr = SessionManager()
            result = await mgr.extract_turn("nonexistent")
            self.assertIsInstance(result, ExtractionResult)
            self.assertEqual(result.stored_count, 0)

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
```

**Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.test_session_extract_turn -v`
Expected: FAIL with `AttributeError: 'SessionManager' object has no attribute 'extract_turn'`

**Step 3: Implement `extract_turn` in SessionManager**

Add to `src/opencortex/session/manager.py` after `end()` (line 199):

```python
async def extract_turn(
    self,
    session_id: str,
    quality_score: float = 0.5,
) -> ExtractionResult:
    """Extract memories from the latest turn without ending the session.

    Takes the last 2 messages (1 user + 1 assistant) and runs LLM extraction.
    Does NOT remove the session — it continues accumulating messages.
    """
    ctx = self._sessions.get(session_id)
    if not ctx:
        logger.warning("[SessionManager] extract_turn: session not found: %s", session_id)
        return ExtractionResult(session_id=session_id)

    result = ExtractionResult(session_id=session_id, quality_score=quality_score)

    if not self._extractor:
        return result

    # Take last 2 messages (the latest turn)
    recent = ctx.messages[-2:] if len(ctx.messages) >= 2 else ctx.messages[:]
    if not recent:
        return result

    extracted = await self._extractor.extract(
        messages=recent,
        quality_score=quality_score,
    )
    result.memories = extracted

    for memory in extracted:
        if memory.confidence < _MIN_CONFIDENCE:
            result.skipped_count += 1
            continue
        stored = await self._store_memory(memory)
        if stored == "merged":
            result.merged_count += 1
        elif stored == "skipped":
            result.skipped_count += 1
        elif stored == "created":
            result.stored_count += 1
        else:
            result.skipped_count += 1

    logger.info(
        "[SessionManager] extract_turn %s: stored=%d, merged=%d, skipped=%d",
        session_id, result.stored_count, result.merged_count, result.skipped_count,
    )
    return result
```

**Step 4: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.test_session_extract_turn -v`
Expected: All 2 tests PASS

**Step 5: Commit**

```bash
git add src/opencortex/session/manager.py tests/test_session_extract_turn.py
git commit -m "feat: add extract_turn to SessionManager for per-turn LLM extraction"
```

---

### Task 6: Add `/session/extract_turn` HTTP Endpoint

**Files:**
- Modify: `src/opencortex/http/models.py` — add `SessionExtractTurnRequest`
- Modify: `src/opencortex/http/server.py` — add endpoint
- Modify: `src/opencortex/orchestrator.py` — add `session_extract_turn()` method

**Step 1: Add request model**

In `src/opencortex/http/models.py`, after `SessionEndRequest` (line 137):

```python
class SessionExtractTurnRequest(BaseModel):
    session_id: str
    quality_score: float = 0.5
```

**Step 2: Add orchestrator method**

In `src/opencortex/orchestrator.py`, find `session_end()` and add after it:

```python
async def session_extract_turn(
    self,
    session_id: str,
    quality_score: float = 0.5,
) -> Dict[str, Any]:
    """Extract memories from the latest turn without ending the session."""
    self._ensure_init()
    if not self._session_manager:
        return {"status": "error", "error": "Session manager not initialized"}

    result = await self._session_manager.extract_turn(
        session_id=session_id,
        quality_score=quality_score,
    )
    return {
        "status": "ok",
        "session_id": session_id,
        "stored": result.stored_count,
        "merged": result.merged_count,
        "skipped": result.skipped_count,
        "total_extracted": len(result.memories),
    }
```

**Step 3: Add HTTP route**

In `src/opencortex/http/server.py`, find the Session section and add:

```python
@app.post("/api/v1/session/extract_turn")
async def session_extract_turn(req: SessionExtractTurnRequest) -> Dict[str, Any]:
    return await _orchestrator.session_extract_turn(
        session_id=req.session_id,
        quality_score=req.quality_score,
    )
```

Add the import of `SessionExtractTurnRequest` to the models import line.

**Step 4: Run existing tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/opencortex/http/models.py src/opencortex/http/server.py src/opencortex/orchestrator.py
git commit -m "feat: add /session/extract_turn endpoint for per-turn LLM extraction"
```

---

### Task 7: Enhance Stop Hook to Buffer Messages + Extract

**Files:**
- Modify: `plugins/opencortex-memory/hooks/handlers/stop.mjs`

**Step 1: Update stop.mjs**

Add session message buffering and extract_turn call after the existing `memory/store` call (around line 35-45):

```javascript
// After existing memory/store call...

// Buffer turn messages in SessionManager
if (state.session_id) {
  try {
    if (turn.userText) {
      await httpPost(`${state.http_url}/api/v1/session/message`, {
        session_id: state.session_id,
        role: 'user',
        content: turn.userText.slice(0, 2000),
      }, 5000);
    }
    if (turn.assistantText) {
      await httpPost(`${state.http_url}/api/v1/session/message`, {
        session_id: state.session_id,
        role: 'assistant',
        content: turn.assistantText.slice(0, 2000),
      }, 5000);
    }

    // Extract memories from this turn (LLM, best-effort)
    await httpPost(`${state.http_url}/api/v1/session/extract_turn`, {
      session_id: state.session_id,
    }, 15000);
  } catch {
    // Best-effort — don't fail the hook
  }
}
```

Note: `session_id` must be set during `session-start` hook. Check `session-start.mjs` to ensure it stores `session_id` in state.

**Step 2: Verify session_id is set in session-start**

Read `plugins/opencortex-memory/hooks/handlers/session-start.mjs` and ensure `state.session_id` is populated. If not, add it by generating a UUID-like ID from timestamp:

```javascript
state.session_id = state.session_id || `${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
```

Also ensure session-start calls `POST /api/v1/session/begin` with the session_id.

**Step 3: Test manually**

Start a Claude Code session with OpenCortex plugin. Send a message. Check server logs for `[SessionManager] extract_turn` entries.

**Step 4: Commit**

```bash
git add plugins/opencortex-memory/hooks/handlers/stop.mjs plugins/opencortex-memory/hooks/handlers/session-start.mjs
git commit -m "feat: enhance stop hook with session buffering and per-turn extraction"
```

---

### Task 8: Enhance Session-End Hook to Trigger Full Extraction

**Files:**
- Modify: `plugins/opencortex-memory/hooks/handlers/session-end.mjs`

**Step 1: Update session-end.mjs**

Replace the simple summary store with a call to `session/end` which triggers full MemoryExtractor:

```javascript
// Replace the existing memory/store call with:
if (state.active && state.session_id) {
  try {
    // Trigger full session extraction via SessionManager
    const result = await httpPost(`${state.http_url}/api/v1/session/end`, {
      session_id: state.session_id,
      quality_score: 0.5,
    }, 30000);  // LLM analysis needs time

    if (result && result.stored > 0) {
      console.error(`[opencortex] session extraction: stored=${result.stored} merged=${result.merged} skipped=${result.skipped}`);
    }
  } catch {
    // Best-effort
  }
}
```

Keep the existing local server shutdown and state cleanup logic unchanged.

**Step 2: Test manually**

Run a Claude Code session, end it, check server logs for full extraction.

**Step 3: Commit**

```bash
git add plugins/opencortex-memory/hooks/handlers/session-end.mjs
git commit -m "feat: enhance session-end hook to trigger full LLM extraction"
```

---

## Phase 3: oc-scan + Batch Store

### Task 9: Create oc-scan.mjs

**Files:**
- Create: `plugins/opencortex-memory/bin/oc-scan.mjs`

**Step 1: Write the scanner**

```javascript
#!/usr/bin/env node
/**
 * oc-scan.mjs — Deterministic file scanner for OpenCortex document import.
 * Pure Node.js, zero external dependencies.
 *
 * Usage: node oc-scan.mjs <directory> [--json]
 * Output: JSON to stdout with { items, source_path, scan_meta }
 */
import { execSync } from 'node:child_process';
import { readFileSync, statSync, readdirSync } from 'node:fs';
import { join, relative, extname, basename } from 'node:path';

const MAX_FILE_SIZE = 1024 * 1024; // 1 MB

const SUPPORTED_EXTS = new Set([
  '.md', '.mdx',
  '.py', '.js', '.mjs', '.ts', '.tsx', '.jsx',
  '.go', '.rs', '.java', '.c', '.cpp', '.h', '.hpp',
  '.rb', '.sh', '.yaml', '.yml', '.toml', '.json',
  '.css', '.html', '.txt', '.rst',
]);

const SKIP_DIRS = new Set([
  '.git', 'node_modules', '__pycache__', '.venv', 'venv',
  'dist', 'build', '.tox', '.mypy_cache', '.next', '.nuxt',
  'coverage', '.cache', '.turbo', '.claude',
]);

function detectGit(dir) {
  try {
    const toplevel = execSync('git rev-parse --show-toplevel', {
      cwd: dir, stdio: ['ignore', 'pipe', 'ignore'], encoding: 'utf-8',
    }).trim();
    return { hasGit: true, projectId: basename(toplevel) };
  } catch {
    return { hasGit: false, projectId: 'public' };
  }
}

function discoverFiles(dir) {
  // Try git ls-files first
  try {
    const output = execSync('git ls-files --cached --others --exclude-standard', {
      cwd: dir, stdio: ['ignore', 'pipe', 'ignore'], encoding: 'utf-8', maxBuffer: 10 * 1024 * 1024,
    });
    return output.trim().split('\n').filter(Boolean).map(f => join(dir, f));
  } catch {
    // Fallback: recursive walk
    return walkDir(dir);
  }
}

function walkDir(dir) {
  const results = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (SKIP_DIRS.has(entry.name)) continue;
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...walkDir(full));
    } else if (entry.isFile()) {
      results.push(full);
    }
  }
  return results;
}

function fileType(ext) {
  if (['.md', '.mdx'].includes(ext)) return 'markdown';
  if (['.txt', '.rst'].includes(ext)) return 'text';
  return 'code';
}

// --- Main ---
const targetDir = process.argv[2];
if (!targetDir) {
  console.error('Usage: node oc-scan.mjs <directory>');
  process.exit(1);
}

const { hasGit, projectId } = detectGit(targetDir);
const files = discoverFiles(targetDir)
  .filter(f => {
    const ext = extname(f).toLowerCase();
    if (!SUPPORTED_EXTS.has(ext)) return false;
    try { return statSync(f).size <= MAX_FILE_SIZE; } catch { return false; }
  });

const items = files.map(f => {
  const relPath = relative(targetDir, f);
  const ext = extname(f).toLowerCase();
  const content = readFileSync(f, 'utf-8');
  return {
    content,
    category: 'documents',
    context_type: 'resource',
    meta: {
      source: 'scan',
      file_path: relPath,
      file_type: fileType(ext),
    },
  };
});

const output = {
  items,
  source_path: targetDir,
  scan_meta: {
    total_files: items.length,
    has_git: hasGit,
    project_id: projectId,
  },
};

console.log(JSON.stringify(output));
```

**Step 2: Test manually**

```bash
node plugins/opencortex-memory/bin/oc-scan.mjs docs/ | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Files: {d[\"scan_meta\"][\"total_files\"]}')"
```

Expected: `Files: <number>` with no errors.

**Step 3: Commit**

```bash
chmod +x plugins/opencortex-memory/bin/oc-scan.mjs
git add plugins/opencortex-memory/bin/oc-scan.mjs
git commit -m "feat: add oc-scan.mjs deterministic file scanner"
```

---

### Task 10: Add batch_store Endpoint

**Files:**
- Modify: `src/opencortex/http/models.py` — add batch models
- Modify: `src/opencortex/http/server.py` — add endpoint
- Modify: `src/opencortex/orchestrator.py` — add `batch_add()`

**Step 1: Add request models**

In `src/opencortex/http/models.py`:

```python
class MemoryBatchItem(BaseModel):
    content: str
    category: str = "documents"
    context_type: str = "resource"
    meta: Optional[Dict[str, Any]] = None

class MemoryBatchStoreRequest(BaseModel):
    items: List[MemoryBatchItem]
    source_path: str = ""
    scan_meta: Optional[Dict[str, Any]] = None
```

**Step 2: Add `batch_add()` to orchestrator**

```python
async def batch_add(
    self,
    items: List[Dict[str, Any]],
    source_path: str = "",
    scan_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Batch add documents. LLM generates abstract + overview per item."""
    self._ensure_init()

    imported = 0
    errors = []
    uris = []

    for i, item in enumerate(items):
        try:
            content = item.get("content", "")
            file_path = (item.get("meta") or {}).get("file_path", f"item_{i}")

            # LLM generate abstract + overview
            abstract, overview = await self._generate_abstract_overview(content, file_path)

            result = await self.add(
                abstract=abstract,
                content=content,
                overview=overview,
                category=item.get("category", "documents"),
                context_type=item.get("context_type", "resource"),
                meta=item.get("meta"),
                dedup=False,  # Deterministic URIs handle dedup via upsert
            )
            uris.append(result.uri)
            imported += 1
        except Exception as exc:
            errors.append({"index": i, "error": str(exc)})

    has_git = (scan_meta or {}).get("has_git", False)
    project_id = (scan_meta or {}).get("project_id", "public")

    return {
        "status": "ok" if not errors else "partial",
        "total": len(items),
        "imported": imported,
        "errors": errors,
        "has_git_project": has_git and project_id != "public",
        "project_id": project_id,
        "uris": uris,
    }

async def _generate_abstract_overview(self, content: str, file_path: str) -> tuple:
    """Use LLM to generate abstract (L0) and overview (L1) from content."""
    if not self._llm_completion:
        # Fallback: filename as abstract, first 500 chars as overview
        return file_path, content[:500]

    prompt = f"""Summarize this document for a memory system.

File: {file_path}
Content (first 3000 chars):
{content[:3000]}

Return JSON: {{"abstract": "1-2 sentence summary", "overview": "1 paragraph overview"}}"""

    try:
        response = await self._llm_completion(prompt)
        from opencortex.utils.json_parse import parse_json_from_response
        data = parse_json_from_response(response)
        if isinstance(data, dict):
            return data.get("abstract", file_path), data.get("overview", content[:500])
    except Exception:
        pass

    return file_path, content[:500]
```

**Step 3: Add HTTP endpoint**

In `src/opencortex/http/server.py`:

```python
@app.post("/api/v1/memory/batch_store")
async def memory_batch_store(req: MemoryBatchStoreRequest) -> Dict[str, Any]:
    return await _orchestrator.batch_add(
        items=[item.model_dump() for item in req.items],
        source_path=req.source_path,
        scan_meta=req.scan_meta,
    )
```

**Step 4: Add MCP tool**

In `plugins/opencortex-memory/lib/mcp-server.mjs`, add to TOOLS:

```javascript
memory_batch_store: ['POST', '/api/v1/memory/batch_store',
  'Batch store multiple documents. Use with oc-scan for deterministic import.', {
    items:       { type: 'array',  description: 'Array of {content, category, context_type, meta}', required: true },
    source_path: { type: 'string', description: 'Source directory path', default: '' },
    scan_meta:   { type: 'object', description: 'Scan metadata {total_files, has_git, project_id}' },
  }],
```

**Step 5: Run existing tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add src/opencortex/http/models.py src/opencortex/http/server.py src/opencortex/orchestrator.py plugins/opencortex-memory/lib/mcp-server.mjs
git commit -m "feat: add batch_store endpoint with LLM abstract generation"
```

---

## Phase 4: Scope Promotion

### Task 11: Add `promote_to_shared` Endpoint

**Files:**
- Modify: `src/opencortex/http/models.py` — add `PromoteToSharedRequest`
- Modify: `src/opencortex/orchestrator.py` — add `promote_to_shared()`
- Modify: `src/opencortex/http/server.py` — add endpoint

**Step 1: Add request model**

```python
class PromoteToSharedRequest(BaseModel):
    uris: List[str]
    project_id: str
```

**Step 2: Add orchestrator method**

```python
async def promote_to_shared(
    self,
    uris: List[str],
    project_id: str,
) -> Dict[str, Any]:
    """Promote private resources to shared project scope.

    Rewrites URIs from user/{uid}/resources/... to resources/{project}/documents/...
    Updates Qdrant scope field and CortexFS paths.
    """
    self._ensure_init()
    tid, uid = get_effective_identity()
    promoted = 0
    errors = []

    for uri in uris:
        try:
            # 1. Get existing record
            results = await self._storage.search(
                _CONTEXT_COLLECTION,
                filter={"op": "must", "field": "uri", "conds": [uri]},
                limit=1,
            )
            if not results:
                errors.append({"uri": uri, "error": "not found"})
                continue

            record = results[0]

            # 2. Build new shared URI
            # Extract node name from old URI (last path segment)
            parts = uri.rstrip("/").split("/")
            node_name = parts[-1] if parts else "unnamed"
            new_uri = CortexURI.build_shared(tid, "resources", project_id, "documents", node_name)

            # 3. Update record fields
            record["uri"] = new_uri
            record["scope"] = "shared"
            record["project_id"] = project_id
            # Preserve parent_uri update
            record["parent_uri"] = CortexURI.build_shared(tid, "resources", project_id, "documents")

            # 4. Upsert with new URI
            await self._storage.upsert(_CONTEXT_COLLECTION, record)

            # 5. Delete old record
            old_id = record.get("id", "")
            if old_id:
                await self._storage.delete(_CONTEXT_COLLECTION, [old_id])

            promoted += 1
        except Exception as exc:
            errors.append({"uri": uri, "error": str(exc)})

    return {
        "status": "ok" if not errors else "partial",
        "promoted": promoted,
        "total": len(uris),
        "errors": errors,
    }
```

**Step 3: Add HTTP endpoint**

```python
@app.post("/api/v1/memory/promote_to_shared")
async def memory_promote_to_shared(req: PromoteToSharedRequest) -> Dict[str, Any]:
    return await _orchestrator.promote_to_shared(
        uris=req.uris,
        project_id=req.project_id,
    )
```

**Step 4: Run existing tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add src/opencortex/http/models.py src/opencortex/http/server.py src/opencortex/orchestrator.py
git commit -m "feat: add promote_to_shared endpoint for scope upgrade"
```

---

## Phase 5: Final Integration Test

### Task 12: End-to-End Smoke Test

**Step 1: Test oc-scan → batch_store → promote flow**

```bash
# 1. Scan docs directory
node plugins/opencortex-memory/bin/oc-scan.mjs docs/ > /tmp/scan_output.json

# 2. Upload via batch_store
curl -X POST http://10.46.35.24:18921/api/v1/memory/batch_store \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: netops" \
  -H "X-User-ID: liaowh4" \
  -H "X-Project-ID: OpenCortex" \
  -d @/tmp/scan_output.json

# 3. Verify documents are searchable
curl -X POST http://10.46.35.24:18921/api/v1/memory/search \
  -H "Content-Type: application/json" \
  -H "X-Tenant-ID: netops" \
  -H "X-User-ID: liaowh4" \
  -d '{"query": "memory pipeline design", "limit": 3}'
```

Expected: batch_store returns `{status: "ok", imported: N}`, search returns relevant results.

**Step 2: Run full test suite**

```bash
uv run python3 -m unittest tests.test_e2e_phase1 tests.test_id_generator tests.test_semantic_name tests.test_session_extract_turn -v
```

Expected: All tests PASS.

**Step 3: Final commit**

```bash
git add -A
git commit -m "chore: memory pipeline enhancement complete (v0.4.0)"
```
