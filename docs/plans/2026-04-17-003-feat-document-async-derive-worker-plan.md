---
date: 2026-04-17
sequence: "003"
title: "Document Async Derive Worker"
status: active
origin: docs/brainstorms/2026-04-17-document-async-derive-worker-requirements.md
plan_depth: standard
---

# Plan 003: Document Async Derive Worker

## Problem Frame

`store()` 对 document mode 同步执行 LLM derive + embed + Qdrant upsert，一篇 10-chunk QASPER 论文耗时 120-300s，超出所有客户端 timeout。Plan-002 已将 chunk derive 并发化（从串行 → level-by-level gather），但总耗时仍在分钟级。

**根因**：derive 逻辑和 HTTP 请求在同一调用链串行执行。

**目标**：`store()` 写入 CortexFS L2 后立即返回（<1s），derive 在后台 worker 完成（最终一致）。

## Requirements Traceability

| Req | Origin | Plan Coverage |
|-----|--------|---------------|
| R1: store() < 1s | origin R1 | Unit 2 — Phase A 即时返回 |
| R2: asyncio.Queue + worker | origin R2 | Unit 1 — DeriveTask + queue + worker coroutine |
| R3: 无占位记录 | origin R3 | Unit 2/3 — Phase A 仅写 CortexFS，Phase B 写 Qdrant |
| R4: 启动恢复扫描 | origin R4 | Unit 4 — `.derive_pending` marker 扫描 |
| R5: 失败不重试 | origin R5 | Unit 3 — catch-all log，no retry |
| R6: 无新 API | origin R6 | 无接口变更，store 仍返回 `{uri}` |

## Scope Boundary

- **仅改 document mode**（`_add_document`）；memory / conversation mode 不变
- **仅改 multi-chunk 路径**；single-chunk fast path 保持同步（足够快）
- 无进程外队列（无 Redis/Celery）
- 无 status API / task_id
- 客户端 timeout 已在 plan-002 修为 300s，本计划不再改

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| `asyncio.Queue` 而非 `create_task` per doc | 代码库 `_spawn_merge_task` 模式是 session-scoped 一对一任务。文档 derive 需要跨文档串行（避免 N 文档×3 chunk 并发 = LLM 过载）、FIFO 排序、recovery 入口共用、sentinel shutdown。Queue 天然满足这 4 点 |
| DeriveWorker 方法放在 orchestrator.py | 需要 `self.add()`, `_derive_layers()`, `_derive_parent_summary()`, `_fs`, `_storage`, `_embedder` 等 10+ 内部依赖。抽为独立模块需大量注入 |
| Phase A 仅写 CortexFS，不写 Qdrant | 与 OpenViking 对齐：derive 期间 Qdrant 无该文档记录 |
| `.derive_pending` marker 含完整元数据 | Marker JSON 存储 `parent_uri` + `tenant_id` + `user_id` + task meta，recovery 直接读取，无需从文件路径反推 URI（`_uri_to_path` 有 `_shorten_component` 截断，逆映射不可靠） |
| `_inflight_derive_uris` set 防 URI 碰撞 | Phase A 不写 Qdrant，`_resolve_unique_uri()` 无法检测并发 store() 的重复 URI。进程内 set 补充去重 |
| Single-chunk 保持同步 | 单 chunk derive 约 5s，不值得引入 worker 开销 |
| Queue unbounded（maxsize=0）| CortexFS 是持久层，不需要 queue 持久化。chunk-level semaphore 限流 LLM 并发，queue 不需要额外背压 |
| 接受 partial-completion 重复 | Worker 中途失败 → marker 保留 → recovery 重跑全文档 → 已完成 chunk 产生重复记录。可接受：低频场景，Qdrant 查询按 score 排序不影响召回质量 |

## Architecture

```
store(document)
  │
  ├─ single chunk → self.add() 同步 (existing, no change)
  │
  └─ multi chunk → Phase A (sync, <1s)
       ├─ parse content → chunks
       ├─ generate parent_uri (_auto_uri + _resolve_unique_uri)
       ├─ check+add _inflight_derive_uris (防并发碰撞)
       ├─ CortexFS: write .derive_pending (JSON: parent_uri, tid, uid, meta)
       ├─ CortexFS: write content.md at parent_uri
       ├─ enqueue _DeriveTask to _derive_queue
       └─ return Context(uri=parent_uri, abstract=doc_title)

_derive_worker() loop:
  ├─ pop _DeriveTask from queue
  ├─ set_request_identity(tid, uid)
  ├─ create parent record (is_leaf=False) via self.add()
  ├─ level-by-level: self.add() per chunk (reuse existing logic)
  │   └─ meta.ingest_mode="memory" prevents re-enter document mode
  ├─ bottom-up summarization via self.update()
  ├─ delete .derive_pending marker
  ├─ remove parent_uri from _inflight_derive_uris
  └─ reset_request_identity()

startup:
  ├─ _recover_pending_derives() → find .derive_pending markers
  └─ re-read content.md → re-parse → enqueue _DeriveTask

shutdown:
  ├─ put None sentinel on queue
  ├─ await worker with 30s timeout
  └─ cancel if stuck
```

## Existing Patterns to Follow

| Pattern | Location | Reuse |
|---------|----------|-------|
| `_spawn_merge_task` | `context/manager.py:1545` | Task tracking + cleanup callback + `_pending_tasks` |
| `_startup_maintenance` | `orchestrator.py:864` | Background startup task pattern |
| `_autophagy_sweep_task` | `orchestrator.py:420` | Shutdown cancel + await pattern in `close()` |
| `set_request_identity` / `reset_request_identity` | `context/manager.py:_merge_buffer` | Identity propagation in background worker |
| CortexFS `write_context` | `cortex_fs.py:1157` | Conditional L0/L1/L2 write (empty fields → skip) |
| `document_derive_concurrency` | `config.py:166` | Existing semaphore config for chunk-level concurrency |

---

## Implementation Units

### Unit 1: DeriveTask dataclass + queue + worker skeleton

**Files:**
- `src/opencortex/orchestrator.py`

**Changes:**

1. Add `_DeriveTask` dataclass at module level:
   ```
   Fields: parent_uri, content, abstract, chunks (List[ParsedChunk]),
           category, context_type, meta, session_id, source_path,
           source_doc_id, source_doc_title, tenant_id, user_id
   ```

2. Add to `MemoryOrchestrator.__init__`:
   - `self._derive_queue: asyncio.Queue = asyncio.Queue()`
   - `self._derive_worker_task: Optional[asyncio.Task] = None`
   - `self._inflight_derive_uris: set = set()` — 防止并发 store() 生成重复 URI

3. Add `_start_derive_worker()` — creates `asyncio.create_task(self._derive_worker())`

4. Add `_derive_worker()` coroutine skeleton:
   - `while True`: pop from queue, break on `None` sentinel
   - `try/except` catch-all: log error, continue
   - Placeholder for `_process_derive_task()` call

5. Call `_start_derive_worker()` in `init()` after step 8 (after `_startup_maintenance`)

**Test scenarios:**
- [ ] Worker starts when orchestrator initializes
- [ ] Worker continues after receiving None sentinel (it should stop)
- [ ] Queue accepts DeriveTask objects

**Test file:** `tests/test_document_async_derive.py`

---

### Unit 2: Refactor `_add_document` for immediate return

**Files:**
- `src/opencortex/orchestrator.py`

**Changes:**

Refactor `_add_document()` multi-chunk path (lines 1146-1323):

**Before (current):** parse → create parent via `self.add()` → level-by-level `self.add()` per chunk → bottom-up summary → return parent_ctx

**After:** parse → generate parent_uri → CortexFS writes → enqueue → return Context

Phase A (the new `_add_document` body for multi-chunk):
1. Parse chunks (existing lines 1092-1095, unchanged)
2. Single-chunk fast path (existing lines 1114-1144, unchanged)
3. `doc_title` computation (existing lines 1148-1154, unchanged)
4. Generate parent_uri: `_auto_uri(context_type, category, abstract=doc_title)` + `_resolve_unique_uri()`
5. **Inflight 碰撞检查**：`if parent_uri in self._inflight_derive_uris: _resolve_unique_uri()` 再生成。然后 `self._inflight_derive_uris.add(parent_uri)`
6. Write `.derive_pending` marker（先写 marker，后写 content — 如果 content 写失败，store() 报错；如果进程崩溃在 marker 之后 content 之前，recovery 发现 marker 无 content → 删除 stale marker）：
   - 直接 AGFS write：`self._fs.agfs.write(f"{path}/.derive_pending", json.dumps(marker).encode())`
   - Marker JSON 完整 schema：`{parent_uri, category, context_type, source_path, source_doc_id, source_doc_title, meta, tenant_id, user_id}`
7. `await self._fs.write_context(uri=parent_uri, content=content)` — writes only content.md (abstract/overview empty → skipped)
8. Enqueue `_DeriveTask(...)` with parsed chunks + identity from `get_effective_identity()`
9. Return `Context(uri=parent_uri, abstract=doc_title, overview="", content="", context_type=context_type, category=category, meta={**(meta or {}), "dedup_action": "created", "derive_pending": True})`

**Key details:**
- `_resolve_unique_uri()` does a fast Qdrant scroll (limit=1). This stays synchronous and is <10ms.
- `write_context` with only `content` set writes `content.md` only. L0/L1 files are skipped (conditional writes in `_sync_write`).
- `.derive_pending` marker 通过 AGFS 直接写（避免 `write_context` content 参数语义歧义）。
- Identity (`tenant_id`, `user_id`) captured from `get_effective_identity()` at enqueue time, passed in `_DeriveTask`.
- `_inflight_derive_uris` 是进程内 set，单线程 asyncio 无需锁。Worker 完成后 remove。

**Behavioral change:**
- `store()` for multi-chunk documents now returns in <1s instead of 60-300s
- Returned Context has `abstract=doc_title` (filename/user-abstract), not LLM-derived summary
- No Qdrant records until derive completes (最终一致)

**Test scenarios:**
- [ ] store() returns in <1s for a 10-chunk document (mock LLM)
- [ ] Returned Context has correct parent_uri and doc_title abstract
- [ ] CortexFS has content.md at parent_uri after store returns
- [ ] CortexFS has .derive_pending marker after store returns
- [ ] DeriveTask is in the queue after store returns
- [ ] Single-chunk path still works synchronously (unchanged)

**Test file:** `tests/test_document_async_derive.py`

---

### Unit 3: DeriveWorker processing logic

**Files:**
- `src/opencortex/orchestrator.py`

**Changes:**

Implement `_process_derive_task(task: _DeriveTask)`:

1. `set_request_identity(task.tenant_id, task.user_id)` in `try` block

2. Create parent record via `self.add()`:
   ```python
   parent_ctx = await self.add(
       abstract=task.abstract,  # doc_title
       content=task.content,
       category=task.category,
       parent_uri=None,
       uri=task.parent_uri,     # use pre-generated URI
       is_leaf=False,
       context_type=task.context_type,
       meta={..., "ingest_mode": "memory", ...},
       session_id=task.session_id,
   )
   ```
   - Passing `uri=task.parent_uri` to `add()` reuses the Phase-A generated URI
   - `is_leaf=False` → skips `_derive_layers()` (fast)

3. Precompute `is_dir_chunk` + topological `levels` (move existing lines 1176-1191)

4. Level-by-level chunk processing (move existing lines 1196-1259):
   - `_process_chunk(idx)` with `asyncio.Semaphore(self._config.document_derive_concurrency)`
   - Each chunk calls `self.add(meta={"ingest_mode": "memory", ...})` — reuses full add() pipeline

5. Bottom-up summarization (move existing lines 1262-1321):
   - `_derive_parent_summary()` per section → `self.update()`
   - Doc parent summary → `self.update()`

6. Delete `.derive_pending` marker：`self._fs.agfs.rm(f"{path}/.derive_pending")`

7. `self._inflight_derive_uris.discard(task.parent_uri)` — 在 `finally` block 中，确保成功/失败都释放

8. `reset_request_identity()` in `finally` block

**Error handling:**
- Individual chunk `self.add()` failure: logged, skipped (existing `return_exceptions` pattern via try/except in `_process_chunk`)
- Whole document failure: catch-all in `_derive_worker()`, log error, `.derive_pending` preserved for recovery（inflight set 已释放，下次 store 同 URI 不阻塞）
- No retry (R5)
- **Partial-completion**: 失败后 recovery 重跑全文档，已完成 chunk 产生重复 Qdrant 记录。可接受——低频场景，搜索按 score 排序不影响质量

**Test scenarios:**
- [ ] Worker processes DeriveTask: creates parent + chunks in Qdrant
- [ ] Chunks have correct parent_uri hierarchy
- [ ] Bottom-up summary updates section/parent abstracts
- [ ] `.derive_pending` marker deleted on success
- [ ] `.derive_pending` preserved on failure
- [ ] Chunk failure doesn't block other chunks
- [ ] Identity propagation: records have correct tid/uid

**Test file:** `tests/test_document_async_derive.py`

---

### Unit 4: Startup recovery scan

**Files:**
- `src/opencortex/orchestrator.py`

**Changes:**

Add `_recover_pending_derives()` async method:

1. Walk filesystem for `.derive_pending` files
   - `pathlib.Path(self._config.data_root).rglob(".derive_pending")`
   - LocalAGFS maps `/local/x` → `{data_root}/x`，所以扫描 `data_root` 直接找到所有 marker

2. For each marker found:
   - Read marker JSON → 得到 `parent_uri`, `tenant_id`, `user_id`, `category`, `context_type`, `source_path`, `source_doc_id`, `source_doc_title`, `meta`（URI 直接从 marker 读取，无需 `_path_to_uri` 反推）
   - Read `content.md` from same directory（`marker_path.parent / "content.md"`）
   - 若 `content.md` 不存在 → log warning + delete stale marker → continue
   - Re-parse content via `ParserRegistry`
   - Construct `_DeriveTask` from marker fields + parsed chunks
   - `self._inflight_derive_uris.add(parent_uri)` — 防止 recovery 期间新 store() 碰撞
   - Enqueue to `_derive_queue`

3. Log count of recovered tasks

Call from `_startup_maintenance()` — after worker is started, after all migrations complete.

**Design notes:**
- Recovery 无需查 Qdrant——marker 的存在即信号（Phase B 成功删 marker，失败保留）
- 如果 marker 有 `parent_uri` 但 content.md 缺失（进程在 marker 写入后、content 写入前崩溃）→ 删 stale marker
- `_inflight_derive_uris` 在 recovery enqueue 时填充，worker 完成后释放

**Test scenarios:**
- [ ] Recovery finds `.derive_pending` marker, reads content, re-enqueues
- [ ] Multiple markers → multiple DeriveTask enqueued
- [ ] Missing content.md alongside marker → warning logged, marker cleaned
- [ ] No markers → no tasks enqueued, clean startup

**Test file:** `tests/test_document_async_derive.py`

---

### Unit 5: Lifecycle management (shutdown + close)

**Files:**
- `src/opencortex/orchestrator.py`

**Changes:**

Modify `close()` (line 5104) to drain the derive worker:

1. After cancelling autophagy tasks but before `self._context_manager.close()`:
   ```python
   if self._derive_worker_task and not self._derive_worker_task.done():
       await self._derive_queue.put(None)  # sentinel
       try:
           await asyncio.wait_for(self._derive_worker_task, timeout=30.0)
       except asyncio.TimeoutError:
           self._derive_worker_task.cancel()
           with suppress(asyncio.CancelledError):
               await self._derive_worker_task
   ```

2. Worker must be drained BEFORE `self._storage.close()` (line 5131) — otherwise Qdrant writes in the worker fail.

**Ordering in close():**
```
1. Cancel autophagy tasks
2. Cancel recall bookkeeping tasks
3. → NEW: drain derive worker (sentinel + await)
4. Close context_manager (drains conversation merge tasks)
5. Close storage (Qdrant)
```

**Test scenarios:**
- [ ] Clean shutdown: worker finishes current task, stops on sentinel
- [ ] Shutdown timeout: worker cancelled after 30s
- [ ] close() with empty queue: immediate worker stop

**Test file:** `tests/test_document_async_derive.py`

---

## Sequencing

```
Unit 1 (skeleton) → Unit 2 (Phase A refactor) → Unit 3 (Phase B logic)
                                                       ↓
                                                  Unit 4 (recovery)
                                                       ↓
                                                  Unit 5 (lifecycle)
```

Units 1→2→3 are sequential (each builds on previous). Units 4 and 5 are independent of each other but require Unit 3.

## Risks

| Risk | Impact | Mitigation |
|------|--------|------------|
| store() returns but derive never completes (worker stuck) | Document permanently unsearchable | `.derive_pending` marker + startup recovery scan; worker catch-all log |
| 并发 store() URI 碰撞 | 两个文档写入同一 URI，content 互相覆盖 | `_inflight_derive_uris` set 在 Phase A 检查+注册，worker 完成释放 |
| Worker crash on identity propagation | Qdrant records written under wrong tenant | `set_request_identity` in try, `reset_request_identity` in finally |
| Partial-completion 重复记录 | Recovery 重跑全文档，已完成 chunk 重复写入 | 接受：低频场景，Qdrant 按 score 排序不影响召回。若需 cleanup 可后续加 |
| `.derive_pending` rglob slow on large data dirs | Startup delay | Marker 数量 = 未完成文档数（通常 <10），rglob 仅找 marker 文件名 |
| `_resolve_unique_uri()` in Phase A hits Qdrant | Adds latency to "synchronous" path | scroll(limit=1) <5ms，可接受 |
| 现有 test_document_mode.py 断言 Qdrant 记录 | Test failures | 测试需用 `await _derive_queue.join()` 等待 worker 完成后再断言（见下方测试策略） |

## Test Synchronization Strategy

现有 `test_document_mode.py` 在 `add()` 返回后立即查 Qdrant。async worker 后 Qdrant 为空。

**方案**：在测试中暴露 drain helper：
```python
async def _drain_derive_queue(self):
    """Wait for all pending derive tasks to complete. Test-only."""
    await self._derive_queue.join()
```
- 测试在 `add()` 后调用 `await orch._drain_derive_queue()` 再断言 Qdrant
- 生产代码不调用（fire-and-forget）
- `_derive_worker` 在每个 task 完成后调用 `self._derive_queue.task_done()`

## Deferred

- **Queue depth monitoring / health_check exposure**: Log queue depth in `health_check()` for observability
- **batch_add integration**: `batch_add` → `self.add()` → `_add_document` → async queue. Works naturally but not explicitly tested
- **`_SEGMENT_MAX_TOKENS` fix**: Pre-existing issue in `full_recompose`, unrelated
- **Partial-completion cleanup**: 可选：recovery 前先删除同 `source_doc_id` 的已有 chunk records，避免重复
