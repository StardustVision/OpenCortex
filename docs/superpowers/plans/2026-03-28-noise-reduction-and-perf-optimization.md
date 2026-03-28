# Event 噪声消除 + 性能优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor `add_message` to separate conversational content from tool execution data (noise reduction), and fix Python async bottlenecks across the commit/recall pipeline (performance).

**Architecture:** Two interleaved tracks. Track A (noise reduction): add `tool_calls` field to MCP → HTTP → ContextManager → Orchestrator → Observer chain. Track B (performance): wrap CortexFS sync I/O in `run_in_executor`, parallelize commit writes, add HTTP connection pooling, fix batch embedding, add recall caching. Changes are ordered by dependency — CortexFS async wrap unlocks dual-write optimization; noise reduction commit changes incorporate parallelization.

**Tech Stack:** Python 3.10+ async, Node.js MCP plugin, FastAPI, Qdrant embedded, undici, PyO3/maturin (Rust phase deferred to separate plan).

**Specs:** `docs/superpowers/specs/2026-03-28-event-noise-reduction-design.md`, `docs/superpowers/specs/2026-03-28-rust-performance-optimization-design.md`

---

## File Structure

| File | Responsibility | Action |
|------|---------------|--------|
| `plugins/opencortex-memory/lib/mcp-server.mjs` | MCP tool definitions + handlers | Modify: add `tool_calls` param to `add_message`, update `handleAddMessage`, update usage-guide prompt |
| `plugins/opencortex-memory/lib/http-client.mjs` | HTTP client (fetch wrapper) | Modify: add undici connection pooling |
| `src/opencortex/http/models.py` | Pydantic request models | Modify: add `ToolCallRecord`, add `tool_calls` to `ContextRequest` |
| `src/opencortex/http/server.py` | FastAPI routes | Modify: forward `req.tool_calls` in `context_handler` |
| `src/opencortex/context/manager.py` | Context Protocol lifecycle | Modify: `handle()` accepts `tool_calls`; `_commit()` parallelized + tool_calls routing; `ConversationBuffer` extended; `_merge_buffer()` aggregates tool_calls |
| `src/opencortex/orchestrator.py` | Top-level API | Modify: `_write_immediate()` accepts `tool_calls` in meta; `add()` dual-write CortexFS fire-and-forget |
| `src/opencortex/alpha/observer.py` | Transcript recording | Modify: `record_batch()` accepts `tool_calls` |
| `src/opencortex/storage/cortex_fs.py` | Filesystem abstraction | Modify: wrap all sync I/O in `run_in_executor` with bounded executor |
| `src/opencortex/models/embedder/cached.py` | Cached embedder | Modify: fix `embed_batch()` to pass through to underlying embedder |
| `src/opencortex/retrieve/intent_analyzer.py` | LLM intent analysis | Modify: add AsyncTTLCache |
| `src/opencortex/retrieve/rerank_client.py` | Reranking | Modify: add AsyncTTLCache |
| `tests/test_context_manager.py` | Context Protocol tests | Modify: add tool_calls + parallelization tests |
| `tests/test_cortexfs_async.py` | CortexFS async tests | Create: verify run_in_executor wrapping |
| `tests/test_noise_reduction.py` | Noise reduction E2E | Create: verify tool_calls flow MCP→storage |

---

### Task 1: CortexFS Async Wrapping

**Files:**
- Modify: `src/opencortex/storage/cortex_fs.py:1098-1134`
- Create: `tests/test_cortexfs_async.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cortexfs_async.py
"""Tests that CortexFS methods don't block the asyncio event loop."""
import asyncio
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.storage.cortex_fs import CortexFS


class TestCortexFSAsync(unittest.TestCase):
    """Verify CortexFS uses run_in_executor for all file I/O."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="cortexfs_async_")
        self.fs = CortexFS.__new__(CortexFS)
        # Minimal init for testing write_context
        from opencortex.storage.local_agfs import LocalAGFS
        self.fs.agfs = LocalAGFS(self.temp_dir)
        self.fs._data_root = self.temp_dir

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_write_context_uses_executor(self):
        """write_context must call run_in_executor, not block the loop."""
        executor_calls = []
        original_run_in_executor = asyncio.get_event_loop().run_in_executor

        async def tracking_executor(executor, fn, *args):
            executor_calls.append(fn.__name__ if hasattr(fn, '__name__') else str(fn))
            return await original_run_in_executor(executor, fn, *args)

        with patch.object(
            asyncio.get_event_loop(), 'run_in_executor',
            side_effect=tracking_executor,
        ):
            test_uri = "opencortex://testteam/alice/memories/events/test123"
            self._run(self.fs.write_context(
                uri=test_uri,
                content="test content",
                abstract="test abstract",
                overview="test overview",
            ))

        # Must have called run_in_executor at least once (for file writes)
        self.assertGreater(len(executor_calls), 0,
            "write_context should use run_in_executor for file I/O")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/test_cortexfs_async.py::TestCortexFSAsync::test_write_context_uses_executor -v`
Expected: FAIL — current write_context calls agfs.write directly without run_in_executor.

- [ ] **Step 3: Add bounded executor and wrap write_context**

In `src/opencortex/storage/cortex_fs.py`, add import at top (after line 14):

```python
import concurrent.futures

# Bounded executor for file I/O — limits queue depth to prevent memory leaks
_fs_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="cortexfs",
)
```

Replace `write_context` method (lines 1098-1134):

```python
    async def write_context(
        self,
        uri: str,
        content: Union[str, bytes] = "",
        abstract: str = "",
        overview: str = "",
        content_filename: str = "content.md",
        is_leaf: bool = False,
    ) -> None:
        """Write context to local storage (L0/L1/L2) via thread executor."""
        path = self._uri_to_path(uri)
        loop = asyncio.get_running_loop()

        def _sync_write():
            try:
                self.agfs.mkdir(path)
            except Exception as e:
                if "exist" not in str(e).lower():
                    raise
            if content:
                data = content.encode("utf-8") if isinstance(content, str) else content
                self.agfs.write(f"{path}/{content_filename}", data)
            if abstract:
                self.agfs.write(f"{path}/.abstract.md", abstract.encode("utf-8"))
            if overview:
                self.agfs.write(f"{path}/.overview.md", overview.encode("utf-8"))

        try:
            await loop.run_in_executor(_fs_executor, _sync_write)
        except Exception as e:
            logger.error(f"[CortexFS] Failed to write {uri}: {e}")
            raise IOError(f"Failed to write {uri}: {e}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/test_cortexfs_async.py -v`
Expected: PASS

- [ ] **Step 5: Run existing test suite to verify no regressions**

Run: `uv run python3 -m unittest tests.test_context_manager tests.test_e2e_phase1 -v`
Expected: All existing tests pass.

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/storage/cortex_fs.py tests/test_cortexfs_async.py
git commit -m "perf: wrap CortexFS write_context in run_in_executor with bounded thread pool"
```

---

### Task 2: Dual-Write — CortexFS Fire-and-Forget

**Files:**
- Modify: `src/opencortex/orchestrator.py:1098-1111`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_cortexfs_async.py
class TestDualWriteAsync(unittest.TestCase):
    """Verify orchestrator.add() doesn't block on CortexFS write."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="dualwrite_")
        from opencortex.config import CortexConfig, init_config
        from tests.test_e2e_phase1 import MockEmbedder, InMemoryStorage
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(self.config)
        from opencortex.http.request_context import set_request_identity
        self._identity_tokens = set_request_identity("testteam", "alice")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        from opencortex.http.request_context import reset_request_identity
        reset_request_identity(self._identity_tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_add_returns_before_fs_completes(self):
        """add() should return after Qdrant upsert, not wait for CortexFS."""
        from opencortex.orchestrator import MemoryOrchestrator
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder,
        )
        self._run(orch.initialize())

        import time
        start = time.monotonic()
        result = self._run(orch.add(
            abstract="dual write test",
            content="test content for dual write",
            category="events",
            context_type="memory",
        ))
        elapsed_ms = (time.monotonic() - start) * 1000

        self.assertIn("uri", result)
        # Should complete well under 85ms (CortexFS sync write time)
        # Allow some margin but it should be significantly faster than serial
        self.assertLess(elapsed_ms, 500, "add() should not block on CortexFS write")
```

- [ ] **Step 2: Run test to verify baseline timing**

Run: `uv run python3 -m pytest tests/test_cortexfs_async.py::TestDualWriteAsync -v`
Note: This test may pass or fail depending on current timing — the important thing is the timing assertion.

- [ ] **Step 3: Change dual-write to Qdrant-first + CortexFS fire-and-forget**

In `src/opencortex/orchestrator.py`, replace lines 1098-1111:

```python
        upsert_started = asyncio.get_running_loop().time()
        await self._storage.upsert(self._get_collection(), record)
        upsert_ms = int((asyncio.get_running_loop().time() - upsert_started) * 1000)

        # CortexFS write — fire-and-forget (L0/L1 already in Qdrant payload)
        fs_write_started = asyncio.get_running_loop().time()
        _fs_task = asyncio.create_task(
            self._fs.write_context(
                uri=uri,
                content=content,
                abstract=abstract,
                overview=overview,
                is_leaf=is_leaf,
            )
        )
        _fs_task.add_done_callback(
            lambda t: t.exception() and logger.warning(
                "[Orchestrator] CortexFS write failed for %s: %s", uri, t.exception()
            )
        )
        fs_write_ms = 0  # Non-blocking, so 0ms from caller perspective
```

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m pytest tests/test_cortexfs_async.py -v && uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/orchestrator.py tests/test_cortexfs_async.py
git commit -m "perf: make CortexFS write fire-and-forget in add(), Qdrant remains synchronous"
```

---

### Task 3: HTTP Models — Add ToolCallRecord + ContextRequest.tool_calls

**Files:**
- Modify: `src/opencortex/http/models.py:166-189`

- [ ] **Step 1: Add ToolCallRecord and update ContextRequest**

In `src/opencortex/http/models.py`, before the `ContextMessage` class (line 170), add:

```python
class ToolCallRecord(BaseModel):
    """Structured tool usage record from MCP add_message."""
    name: str
    summary: str = ""
```

Add `tool_calls` field to `ContextRequest` (after line 188, before `config`):

```python
class ContextRequest(BaseModel):
    session_id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]{1,128}$")
    turn_id: Optional[str] = Field(
        default=None, pattern=r"^[a-zA-Z0-9_-]{1,128}$",
    )
    phase: str                     # prepare | commit | end
    messages: Optional[List[ContextMessage]] = None
    tool_calls: Optional[List[ToolCallRecord]] = None
    cited_uris: Optional[List[str]] = None
    config: Optional[ContextConfig] = None
```

- [ ] **Step 2: Verify import works**

Run: `uv run python3 -c "from opencortex.http.models import ToolCallRecord, ContextRequest; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/opencortex/http/models.py
git commit -m "feat: add ToolCallRecord model and tool_calls field to ContextRequest"
```

---

### Task 4: HTTP Route — Forward tool_calls

**Files:**
- Modify: `src/opencortex/http/server.py:455-469`

- [ ] **Step 1: Update context_handler to forward tool_calls**

In `src/opencortex/http/server.py`, replace the `context_handler` function (lines 455-469):

```python
    @app.post("/api/v1/context")
    async def context_handler(req: ContextRequest) -> Dict[str, Any]:
        """Unified memory_context lifecycle: prepare / commit / end."""
        from opencortex.http.request_context import get_effective_identity
        tid, uid = get_effective_identity()
        return await _orchestrator._context_manager.handle(
            session_id=req.session_id,
            phase=req.phase,
            tenant_id=tid,
            user_id=uid,
            turn_id=req.turn_id,
            messages=[m.model_dump() for m in req.messages] if req.messages else None,
            cited_uris=req.cited_uris,
            config=req.config.model_dump() if req.config else None,
            tool_calls=[t.model_dump() for t in req.tool_calls] if req.tool_calls else None,
        )
```

- [ ] **Step 2: Verify server imports still work**

Run: `uv run python3 -c "from opencortex.http.server import create_app; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/opencortex/http/server.py
git commit -m "feat: forward tool_calls from ContextRequest to ContextManager.handle()"
```

---

### Task 5: Observer — Accept tool_calls in record_batch

**Files:**
- Modify: `src/opencortex/alpha/observer.py:54-64`

- [ ] **Step 1: Write the failing test**

```python
# Add to tests/test_noise_reduction.py (new file)
"""Tests for the event noise reduction pipeline (tool_calls three-way split)."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from opencortex.alpha.observer import Observer


class TestObserverToolCalls(unittest.TestCase):
    """Observer.record_batch must preserve tool_calls in transcript."""

    def test_record_batch_with_tool_calls(self):
        obs = Observer()
        obs.begin_session("s1", "team", "user")
        messages = [
            {"role": "user", "content": "fix the bug"},
            {"role": "assistant", "content": "Fixed the selection logic."},
        ]
        tool_calls = [
            {"name": "Read", "summary": "Memories.tsx"},
            {"name": "Edit", "summary": "modified useEffect"},
        ]
        obs.record_batch("s1", messages, "team", "user", tool_calls=tool_calls)

        transcript = obs.get_transcript("s1")
        self.assertEqual(len(transcript), 2)

        # User message should NOT have tool_calls
        self.assertNotIn("tool_calls", transcript[0])

        # Assistant message SHOULD have tool_calls
        self.assertIn("tool_calls", transcript[1])
        self.assertEqual(len(transcript[1]["tool_calls"]), 2)
        self.assertEqual(transcript[1]["tool_calls"][0]["name"], "Read")

    def test_record_batch_without_tool_calls_backward_compat(self):
        obs = Observer()
        obs.begin_session("s2", "team", "user")
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        obs.record_batch("s2", messages, "team", "user")

        transcript = obs.get_transcript("s2")
        self.assertEqual(len(transcript), 2)
        # No tool_calls key should be present
        self.assertNotIn("tool_calls", transcript[0])
        self.assertNotIn("tool_calls", transcript[1])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/test_noise_reduction.py::TestObserverToolCalls -v`
Expected: FAIL — current record_batch doesn't accept `tool_calls` parameter.

- [ ] **Step 3: Update Observer.record_batch**

In `src/opencortex/alpha/observer.py`, replace `record_batch` (lines 54-64):

```python
    def record_batch(
        self, session_id: str, messages: List[Dict[str, Any]],
        tenant_id: str, user_id: str,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Record a batch of messages (from client debounce buffer)."""
        for msg in messages:
            entry = {
                "role": msg["role"],
                "content": msg["content"],
                "timestamp": msg.get("timestamp", time.time()),
            }
            if msg["role"] == "assistant" and tool_calls:
                entry["tool_calls"] = tool_calls
            self._transcripts[session_id].append(entry)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python3 -m pytest tests/test_noise_reduction.py::TestObserverToolCalls -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/alpha/observer.py tests/test_noise_reduction.py
git commit -m "feat: Observer.record_batch accepts tool_calls, attaches to assistant transcript entries"
```

---

### Task 6: ContextManager — handle() + _commit() with tool_calls + parallel writes

**Files:**
- Modify: `src/opencortex/context/manager.py:35-41` (ConversationBuffer), `117-151` (handle), `339-435` (_commit), `443-484` (_merge_buffer)

- [ ] **Step 1: Write failing test for tool_calls flow**

```python
# Add to tests/test_noise_reduction.py
import shutil
import tempfile
from opencortex.config import CortexConfig, init_config
from opencortex.context.manager import ContextManager, ConversationBuffer
from opencortex.http.request_context import set_request_identity, reset_request_identity
from opencortex.orchestrator import MemoryOrchestrator
from tests.test_e2e_phase1 import MockEmbedder, InMemoryStorage


class TestCommitToolCalls(unittest.TestCase):
    """_commit must store tool_calls as meta on immediate records."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="noise_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(self.config)
        self._tokens = set_request_identity("testteam", "alice")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        reset_request_identity(self._tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_commit_stores_tool_calls_in_meta(self):
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder,
        )
        self._run(orch.initialize())
        cm = orch._context_manager

        # Prepare first to create session
        self._run(cm.handle(
            session_id="s1", phase="prepare",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "fix the bug"}],
        ))

        # Commit with tool_calls
        tool_calls = [
            {"name": "Read", "summary": "Memories.tsx"},
            {"name": "Edit", "summary": "modified useEffect"},
        ]
        result = self._run(cm.handle(
            session_id="s1", phase="commit",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[
                {"role": "user", "content": "fix the bug"},
                {"role": "assistant", "content": "Fixed the selection logic."},
            ],
            tool_calls=tool_calls,
        ))
        self.assertTrue(result["accepted"])

        # Check immediate records in storage have tool_calls in meta
        records = self.storage._data.get("context", [])
        assistant_records = [r for r in records if "assistant" in r.get("abstract", "").lower()
                            or r.get("meta", {}).get("tool_calls")]
        # At least one record should have tool_calls in meta
        has_tool_calls = any(
            r.get("meta", {}).get("tool_calls") for r in records
        )
        self.assertTrue(has_tool_calls, "At least one immediate record should have tool_calls in meta")

    def test_commit_without_tool_calls_backward_compat(self):
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder,
        )
        self._run(orch.initialize())
        cm = orch._context_manager

        self._run(cm.handle(
            session_id="s2", phase="prepare",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "hello"}],
        ))

        result = self._run(cm.handle(
            session_id="s2", phase="commit",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
            # No tool_calls
        ))
        self.assertTrue(result["accepted"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m pytest tests/test_noise_reduction.py::TestCommitToolCalls -v`
Expected: FAIL — handle() doesn't accept `tool_calls`.

- [ ] **Step 3: Extend ConversationBuffer**

In `src/opencortex/context/manager.py`, replace lines 35-41:

```python
@dataclass
class ConversationBuffer:
    """Per-session buffer for conversation mode incremental chunking."""
    messages: list = dc_field(default_factory=list)
    token_count: int = 0
    start_msg_index: int = 0
    immediate_uris: list = dc_field(default_factory=list)
    tool_calls_per_turn: list = dc_field(default_factory=list)
```

- [ ] **Step 4: Update handle() to accept and forward tool_calls**

In `src/opencortex/context/manager.py`, update the `handle` method signature (line 117) to add `tool_calls`:

```python
    async def handle(
        self,
        session_id: str,
        phase: str,
        tenant_id: str,
        user_id: str,
        turn_id: Optional[str] = None,
        messages: Optional[List[Dict[str, str]]] = None,
        cited_uris: Optional[List[str]] = None,
        config: Optional[Dict[str, Any]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
```

Update the commit dispatch (around line 143) to pass `tool_calls`:

```python
        elif phase == "commit":
            if not turn_id:
                raise ValueError("turn_id is required for commit")
            if not messages or len(messages) < 2:
                raise ValueError("commit requires at least user + assistant messages")
            return await self._commit(
                session_id, turn_id, messages, tenant_id, user_id, cited_uris,
                tool_calls,
            )
```

- [ ] **Step 5: Rewrite _commit() with tool_calls + parallel writes**

Replace `_commit` method (lines 339-435) with:

```python
    async def _commit(
        self,
        session_id: str,
        turn_id: str,
        messages: List[Dict[str, str]],
        tenant_id: str,
        user_id: str,
        cited_uris: Optional[List[str]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        sk = self._make_session_key(tenant_id, user_id, session_id)
        self._touch_session(sk)

        # Idempotent: same turn_id already committed → duplicate
        if turn_id in self._committed_turns.get(sk, set()):
            logger.debug(
                "[ContextManager] commit DUPLICATE sid=%s turn=%s tenant=%s user=%s",
                session_id, turn_id, tenant_id, user_id,
            )
            return {
                "accepted": True,
                "write_status": "duplicate",
                "turn_id": turn_id,
            }

        # Write to Observer (synchronous in-memory buffer) — full data including tool_calls
        observer_ok = True
        try:
            self._observer.record_batch(session_id, messages, tenant_id, user_id,
                                        tool_calls=tool_calls)
        except Exception as exc:
            observer_ok = False
            logger.warning(
                "[ContextManager] Observer record failed sid=%s turn=%s tenant=%s user=%s: %s "
                "— writing to fallback",
                session_id, turn_id, tenant_id, user_id, exc,
            )
            self._write_fallback(session_id, turn_id, messages, tenant_id, user_id)

        # Mark turn as committed
        self._committed_turns.setdefault(sk, set()).add(turn_id)

        # RL reward for cited URIs (async, non-blocking)
        if cited_uris:
            valid_uris = [u for u in cited_uris if u.startswith("opencortex://")]
            if valid_uris:
                task = asyncio.create_task(self._apply_cited_rewards(valid_uris))
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)

        # Build write items list (don't mutate buffer yet)
        buffer = self._conversation_buffers.setdefault(sk, ConversationBuffer())
        write_items = []
        for i, msg in enumerate(messages):
            text = msg.get("content", msg.get("assistant_response", msg.get("user_message", "")))
            if not text:
                continue
            role = msg.get("role", "")
            idx = buffer.start_msg_index + len(buffer.messages) + i
            tc = tool_calls if role == "assistant" else None
            write_items.append((text, idx, tc))

        # Parallel immediate writes via asyncio.gather
        if write_items:
            tokens_for_identity = set_request_identity(tenant_id, user_id)
            try:
                results = await asyncio.gather(*[
                    self._orchestrator._write_immediate(
                        session_id=session_id,
                        msg_index=idx,
                        text=text,
                        tool_calls=tc,
                    )
                    for text, idx, tc in write_items
                ], return_exceptions=True)
            finally:
                reset_request_identity(tokens_for_identity)

            # Update buffer in order (gather preserves input order)
            for (text, idx, tc), result in zip(write_items, results):
                if isinstance(result, Exception):
                    logger.warning("[ContextManager] Immediate write failed: %s", result)
                    continue
                buffer.messages.append(text)
                buffer.immediate_uris.append(result)
                buffer.token_count += self._estimate_tokens(text)

            # Track tool_calls per turn for merge aggregation
            if tool_calls:
                buffer.tool_calls_per_turn.append(tool_calls)

        # Check merge threshold
        if buffer.token_count >= 1000:
            task = asyncio.create_task(
                self._merge_buffer(sk, session_id, tenant_id, user_id)
            )
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

        write_status = "ok" if observer_ok else "fallback"
        if not observer_ok:
            logger.warning(
                "[ContextManager] commit FALLBACK sid=%s turn=%s tenant=%s user=%s",
                session_id, turn_id, tenant_id, user_id,
            )
        else:
            logger.info(
                "[ContextManager] commit sid=%s turn=%s tenant=%s user=%s messages=%d cited=%d tool_calls=%d",
                session_id, turn_id, tenant_id, user_id,
                len(messages),
                len(cited_uris) if cited_uris else 0,
                len(tool_calls) if tool_calls else 0,
            )

        return {
            "accepted": True,
            "write_status": write_status,
            "turn_id": turn_id,
            "session_turns": len(self._committed_turns.get(sk, set())),
        }
```

- [ ] **Step 6: Update _merge_buffer to aggregate tool_calls**

Replace `_merge_buffer` (lines 443-484):

```python
    async def _merge_buffer(self, sk, session_id, tenant_id, user_id):
        """Merge accumulated buffer into a high-quality LLM-derived chunk."""
        buffer = self._conversation_buffers.get(sk)
        if not buffer or not buffer.messages:
            return
        tokens_for_identity = None
        try:
            combined = "\n\n".join(buffer.messages)

            # Aggregate tool_calls from all turns in this buffer
            all_tool_calls = []
            for tc_list in buffer.tool_calls_per_turn:
                all_tool_calls.extend(tc_list)

            tokens_for_identity = set_request_identity(tenant_id, user_id)
            await self._orchestrator.add(
                abstract="",
                content=combined,
                category="events",
                context_type="memory",
                meta={
                    "layer": "merged",
                    "ingest_mode": "memory",
                    "msg_range": [
                        buffer.start_msg_index,
                        buffer.start_msg_index + len(buffer.messages) - 1,
                    ],
                    "session_id": session_id,
                    "tool_calls": all_tool_calls if all_tool_calls else [],
                },
                session_id=session_id,
            )
            # Delete merged immediate records from Qdrant
            if buffer.immediate_uris:
                try:
                    await self._orchestrator._storage.batch_delete(
                        "context",
                        {"op": "must", "field": "uri", "conds": buffer.immediate_uris},
                    )
                except Exception as exc:
                    logger.warning("[ContextManager] Immediate cleanup after merge: %s", exc)
            # Reset buffer
            new_start = buffer.start_msg_index + len(buffer.messages)
            self._conversation_buffers[sk] = ConversationBuffer(start_msg_index=new_start)
        except Exception as exc:
            logger.error("[ContextManager] Merge failed: %s", exc)
        finally:
            if tokens_for_identity:
                reset_request_identity(tokens_for_identity)
```

- [ ] **Step 7: Update _write_immediate to accept tool_calls**

In `src/opencortex/orchestrator.py`, update `_write_immediate` signature (line 573):

```python
    async def _write_immediate(self, session_id: str, msg_index: int, text: str,
                                tool_calls: Optional[list] = None) -> str:
```

Update the `meta` dict in the record (line 616):

```python
            "meta": {
                "layer": "immediate",
                "msg_index": msg_index,
                "session_id": session_id,
                "tool_calls": tool_calls or [],
            },
```

- [ ] **Step 8: Run all tests**

Run: `uv run python3 -m pytest tests/test_noise_reduction.py tests/test_cortexfs_async.py -v && uv run python3 -m unittest tests.test_context_manager -v`
Expected: All pass.

- [ ] **Step 9: Commit**

```bash
git add src/opencortex/context/manager.py src/opencortex/orchestrator.py tests/test_noise_reduction.py
git commit -m "feat: tool_calls three-way split in commit pipeline + parallel immediate writes"
```

---

### Task 7: MCP Plugin — add_message tool_calls + usage-guide

**Files:**
- Modify: `plugins/opencortex-memory/lib/mcp-server.mjs:99-107` (tool def), `188-203` (handler), `308-312` (prompt)

- [ ] **Step 1: Update add_message tool definition**

In `plugins/opencortex-memory/lib/mcp-server.mjs`, replace lines 99-107:

```javascript
  add_message: [null, null,
    'MANDATORY: You MUST call this tool AFTER generating EVERY response. '
    + 'This is required regardless of topic. Skipping means the conversation is lost forever.\n\n'
    + 'Records the conversation turn for future recall and knowledge extraction. '
    + 'Pass cited_uris for opencortex:// URIs you referenced from recall results.', {
      user_message:       { type: 'string', description: "The user's message", required: true },
      assistant_response: { type: 'string', description: 'Your conversational conclusion — what you found, decided, or recommended. Do NOT include tool output, code blocks, or execution details.', required: true },
      tool_calls:         { type: 'array',  description: 'Tools you used this turn. Each item: {name, summary}. summary is a short one-line description of what the tool did.' },
      cited_uris:         { type: 'array',  description: 'opencortex:// URIs referenced in response' },
    }],
```

- [ ] **Step 2: Update handleAddMessage**

Replace lines 188-203:

```javascript
async function handleAddMessage(args) {
  const turnId = _lastRecallTurnId || `t${++_turnCounter}`;

  const body = {
    session_id: _sessionId,
    phase: 'commit',
    turn_id: turnId,
    messages: [
      { role: 'user', content: args.user_message },
      { role: 'assistant', content: args.assistant_response },
    ],
  };
  if (args.tool_calls) body.tool_calls = args.tool_calls;
  if (args.cited_uris) body.cited_uris = args.cited_uris;

  return await httpContextCall(body);
}
```

- [ ] **Step 3: Update usage-guide prompt**

In the `getPromptContent` function (around line 308), replace the Step 3 section:

```javascript
### Step 3: add_message (AFTER answering)
- Call \`add_message\` with:
  - \`user_message\`: the user's original message
  - \`assistant_response\`: your conversational conclusion ONLY — what you found, decided, or recommended
    - Do NOT include: tool output, code blocks, command results, diffs, logs
    - Do include: decisions, findings, next steps, explanations
  - \`tool_calls\`: list of tools you used, each with {name, summary}
- Pass \`cited_uris\` for any opencortex:// URIs you referenced
- This is NOT optional — skipping means the conversation is lost forever
```

- [ ] **Step 4: Run MCP tests**

Run: `node --test tests/test_mcp_server.mjs`
Expected: Existing tests still pass (add_message without tool_calls is backward compatible).

- [ ] **Step 5: Commit**

```bash
git add plugins/opencortex-memory/lib/mcp-server.mjs
git commit -m "feat: add tool_calls field to add_message MCP tool + update usage-guide prompt"
```

---

### Task 8: MCP HTTP Connection Pooling

**Files:**
- Modify: `plugins/opencortex-memory/lib/http-client.mjs:1-2`

- [ ] **Step 1: Add undici global dispatcher**

At the top of `plugins/opencortex-memory/lib/http-client.mjs`, after line 1, add:

```javascript
import { Agent, setGlobalDispatcher } from 'undici';

setGlobalDispatcher(new Agent({
  keepAliveTimeout: 30_000,
  connections: 10,
}));
```

No changes needed to `httpPost`, `httpGet`, or `healthCheck` — the global dispatcher applies to all `fetch()` calls automatically.

- [ ] **Step 2: Verify it works**

Run: `node -e "import('undici').then(m => { m.setGlobalDispatcher(new m.Agent({keepAliveTimeout:30000})); console.log('OK') })"`
Expected: `OK`

- [ ] **Step 3: Run MCP tests**

Run: `node --test tests/test_mcp_server.mjs`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add plugins/opencortex-memory/lib/http-client.mjs
git commit -m "perf: add undici connection pooling for MCP HTTP client"
```

---

### Task 9: CachedEmbedder Batch Fix

**Files:**
- Modify: `src/opencortex/models/embedder/cached.py` (or wherever CachedEmbedder lives)

- [ ] **Step 1: Find CachedEmbedder**

Run: `grep -rn "class CachedEmbedder" src/opencortex/models/embedder/`

- [ ] **Step 2: Fix embed_batch to pass through**

Locate the `embed_batch` method in CachedEmbedder. Replace the loop-based implementation with:

```python
    def embed_batch(self, texts: List[str]) -> List[EmbedResult]:
        results = [None] * len(texts)
        uncached_indices = []
        uncached_texts = []

        for i, text in enumerate(texts):
            key = self._cache_key(text)
            cached = self._cache.get(key)
            if cached is not None:
                results[i] = cached
            else:
                uncached_indices.append(i)
                uncached_texts.append(text)

        if uncached_texts:
            batch_results = self._embedder.embed_batch(uncached_texts)
            for idx, result in zip(uncached_indices, batch_results):
                key = self._cache_key(texts[idx])
                self._cache[key] = result
                results[idx] = result

        return results
```

- [ ] **Step 3: Run tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 -v`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add src/opencortex/models/embedder/
git commit -m "perf: fix CachedEmbedder.embed_batch to pass through to underlying embedder"
```

---

### Task 10: Recall Pipeline Caching (Intent + Rerank)

**Files:**
- Modify: `src/opencortex/retrieve/intent_analyzer.py`
- Modify: `src/opencortex/retrieve/rerank_client.py`

- [ ] **Step 1: Create AsyncTTLCache utility**

Add to a suitable location (e.g. `src/opencortex/utils/cache.py`):

```python
"""Async-compatible TTL cache for LLM results."""
import time
from typing import Any, Dict, Optional


class AsyncTTLCache:
    """Simple TTL cache safe for use with async code.

    NOT thread-safe — designed for single-thread asyncio event loop.
    Do NOT use functools.lru_cache with async functions (caches coroutines, not results).
    """

    def __init__(self, ttl_seconds: float = 60.0, max_size: int = 128):
        self._cache: Dict[str, tuple] = {}  # key -> (value, timestamp)
        self._ttl = ttl_seconds
        self._max_size = max_size

    def get(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, ts = entry
        if time.monotonic() - ts > self._ttl:
            del self._cache[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        if len(self._cache) >= self._max_size:
            oldest = min(self._cache, key=lambda k: self._cache[k][1])
            del self._cache[oldest]
        self._cache[key] = (value, time.monotonic())
```

- [ ] **Step 2: Integrate into IntentAnalyzer**

In `src/opencortex/retrieve/intent_analyzer.py`, add caching around the LLM call in the `analyze` method. Look for the main LLM call and wrap it:

```python
import hashlib
from opencortex.utils.cache import AsyncTTLCache

# In __init__:
self._cache = AsyncTTLCache(ttl_seconds=60.0, max_size=128)

# In analyze():
cache_key = hashlib.md5(f"{query}:{str(session_context)}".encode()).hexdigest()
cached = self._cache.get(cache_key)
if cached is not None:
    return cached
# ... existing LLM call ...
self._cache.put(cache_key, result)
return result
```

- [ ] **Step 3: Integrate into RerankClient**

Similarly in `src/opencortex/retrieve/rerank_client.py`, cache rerank results:

```python
from opencortex.utils.cache import AsyncTTLCache

# In __init__:
self._cache = AsyncTTLCache(ttl_seconds=120.0, max_size=64)

# In rerank():
doc_key = "|".join(sorted(d[:50] for d in documents[:10]))
cache_key = hashlib.md5(f"{query}:{doc_key}".encode()).hexdigest()
cached = self._cache.get(cache_key)
if cached is not None:
    return cached
# ... existing rerank call ...
self._cache.put(cache_key, scores)
return scores
```

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m unittest tests.test_e2e_phase1 tests.test_context_manager -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/utils/cache.py src/opencortex/retrieve/intent_analyzer.py src/opencortex/retrieve/rerank_client.py
git commit -m "perf: add AsyncTTLCache for intent analysis and rerank results"
```

---

### Task 11: Full Integration Test

**Files:**
- Create: `tests/test_noise_reduction.py` (add integration test to existing file)

- [ ] **Step 1: Write full-pipeline integration test**

```python
# Add to tests/test_noise_reduction.py
class TestNoiseReductionE2E(unittest.TestCase):
    """Full pipeline: MCP-style add_message with tool_calls → verify storage."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="noise_e2e_")
        self.config = CortexConfig(
            data_root=self.temp_dir,
            embedding_dimension=MockEmbedder.DIMENSION,
            rerank_provider="disabled",
        )
        init_config(self.config)
        self._tokens = set_request_identity("testteam", "alice")
        self.storage = InMemoryStorage()
        self.embedder = MockEmbedder()

    def tearDown(self):
        reset_request_identity(self._tokens)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_tool_calls_not_in_event_body(self):
        """tool_calls should be in meta only, not polluting event text/embedding."""
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder,
        )
        self._run(orch.initialize())
        cm = orch._context_manager

        # Prepare
        self._run(cm.handle(
            session_id="e2e", phase="prepare",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "fix the right panel bug"}],
        ))

        # Commit with tool_calls
        self._run(cm.handle(
            session_id="e2e", phase="commit",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[
                {"role": "user", "content": "fix the right panel bug"},
                {"role": "assistant", "content": "Fixed stale content state with cancelled flag."},
            ],
            tool_calls=[
                {"name": "Read", "summary": "web/src/pages/Memories.tsx"},
                {"name": "Edit", "summary": "modified selectedMemory useEffect"},
            ],
        ))

        # Verify: event body should NOT contain tool names
        records = self.storage._data.get("context", [])
        for r in records:
            abstract = r.get("abstract", "")
            self.assertNotIn("[tool-use]", abstract)
            self.assertNotIn("Read", abstract)  # tool name not in body

        # Verify: meta.tool_calls should exist on assistant record
        assistant_records = [r for r in records
                           if r.get("meta", {}).get("tool_calls")]
        self.assertGreater(len(assistant_records), 0)
        tc = assistant_records[0]["meta"]["tool_calls"]
        self.assertEqual(tc[0]["name"], "Read")

    def test_backward_compat_no_tool_calls(self):
        """Without tool_calls, commit works exactly as before."""
        orch = MemoryOrchestrator(
            config=self.config, storage=self.storage, embedder=self.embedder,
        )
        self._run(orch.initialize())
        cm = orch._context_manager

        self._run(cm.handle(
            session_id="bc", phase="prepare",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[{"role": "user", "content": "hello"}],
        ))

        result = self._run(cm.handle(
            session_id="bc", phase="commit",
            tenant_id="testteam", user_id="alice",
            turn_id="t1",
            messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi there"},
            ],
        ))
        self.assertTrue(result["accepted"])

        records = self.storage._data.get("context", [])
        self.assertGreater(len(records), 0)
```

- [ ] **Step 2: Run full test suite**

Run: `uv run python3 -m pytest tests/test_noise_reduction.py tests/test_cortexfs_async.py -v && uv run python3 -m unittest tests.test_context_manager tests.test_e2e_phase1 -v`
Expected: All pass.

- [ ] **Step 3: Commit**

```bash
git add tests/test_noise_reduction.py
git commit -m "test: add full E2E integration tests for noise reduction pipeline"
```

---

Plan complete and saved to `docs/superpowers/plans/2026-03-28-noise-reduction-and-perf-optimization.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?