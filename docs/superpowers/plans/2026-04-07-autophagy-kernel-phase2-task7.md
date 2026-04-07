# Autophagy Kernel Phase 2 Task 7 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add paged cognitive state scanning + an autophagy metabolism sweep entrypoint, and wire startup/periodic sweeps into `MemoryOrchestrator` without blocking init or request paths.

**Architecture:** Introduce a `CognitiveStateStore.scroll_states(...)` wrapper over `StorageInterface.scroll(...)` to page through cognitive states. Add `AutophagyKernel.sweep_metabolism(...)` that pulls one page, runs metabolism, persists updates via the existing mutation batch ledger, and returns paging info. `MemoryOrchestrator` will start a fire-and-forget startup sweep and a periodic background loop (default 15 minutes) that processes bounded batches and maintains cursor state across ticks; `close()` will cancel/await the loop cleanly.

**Tech Stack:** Python asyncio, existing `StorageInterface.scroll`, unittest `IsolatedAsyncioTestCase`.

---

### Task 1: Store Paging API (`scroll_states`)

**Files:**
- Modify: `src/opencortex/cognition/state_store.py`
- Test: `tests/test_cognitive_state_store.py`

- [ ] **Step 1: Write failing test for paging**

```python
async def test_scroll_states_pages_through_filtered_rows(self):
    await self.store.save_state(self._make_state("mem-1", owner_type=OwnerType.MEMORY))
    await self.store.save_state(self._make_state("mem-2", owner_type=OwnerType.MEMORY))
    await self.store.save_state(self._make_state("trace-1", owner_type=OwnerType.TRACE))

    page1, cursor = await self.store.scroll_states(owner_type=OwnerType.MEMORY, limit=1)
    self.assertEqual([s.owner_id for s in page1], ["mem-1"])
    self.assertIsNotNone(cursor)

    page2, cursor2 = await self.store.scroll_states(owner_type=OwnerType.MEMORY, limit=1, cursor=cursor)
    self.assertEqual([s.owner_id for s in page2], ["mem-2"])
    self.assertIsNone(cursor2)
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_cognitive_state_store.py::TestCognitiveStateStore::test_scroll_states_pages_through_filtered_rows -q`
Expected: FAIL with `AttributeError: 'CognitiveStateStore' object has no attribute 'scroll_states'` (or similar).

- [ ] **Step 3: Minimal implementation**

Implement `CognitiveStateStore.scroll_states(...)` using `self._storage.scroll(...)` and composing a storage filter via `must` / `and` with optional fields. Return `List[CognitiveState]` + `next_cursor`.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/test_cognitive_state_store.py::TestCognitiveStateStore::test_scroll_states_pages_through_filtered_rows -q`
Expected: PASS

- [ ] **Step 5: Refactor (optional)**
Keep filter composition in a small helper.

---

### Task 2: Kernel Sweep Entrypoint (`sweep_metabolism`)

**Files:**
- Modify: `src/opencortex/cognition/kernel.py`
- Test: `tests/test_autophagy_kernel.py`

- [ ] **Step 1: Write failing tests for sweep behavior**

```python
async def test_sweep_metabolism_fetches_one_page_ticks_and_persists_updates(self):
    state_store = _StateStoreSpy()
    state_store.scroll_states_results = [
        ([self._state("mem-1", version=4)], "cursor-1"),
    ]
    metabolism = _MetabolismControllerSpy(MetabolismResult(
        state_updates=[{
            "owner_type": OwnerType.MEMORY,
            "owner_id": "mem-1",
            "expected_version": 4,
            "fields": {"lifecycle_state": "compressed"},
        }]
    ))
    kernel = AutophagyKernel(..., state_store=state_store, metabolism_controller=metabolism, ...)

    out = await kernel.sweep_metabolism(owner_type=OwnerType.MEMORY, limit=50, cursor=None)

    self.assertEqual(out.next_cursor, "cursor-1")
    self.assertEqual(out.processed_owner_ids, ["mem-1"])
    self.assertEqual(len(state_store.persist_batch_calls), 1)
```

```python
async def test_sweep_metabolism_empty_batch_is_safe(self):
    state_store = _StateStoreSpy()
    state_store.scroll_states_results = [([], None)]
    kernel = AutophagyKernel(...)
    out = await kernel.sweep_metabolism(owner_type=OwnerType.MEMORY, limit=10, cursor=None)
    self.assertEqual(out.processed_owner_ids, [])
    self.assertIsNone(out.next_cursor)
    self.assertEqual(state_store.persist_batch_calls, [])
```

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_autophagy_kernel.py::TestAutophagyKernel::test_sweep_metabolism_fetches_one_page_ticks_and_persists_updates -q`
Expected: FAIL with missing method/attribute.

- [ ] **Step 3: Minimal implementation**
Add:
- A small result dataclass (contains `next_cursor`, processed ids/count, updated ids/count, and `committed_batch_id` when applicable).
- `AutophagyKernel.sweep_metabolism(...)` that:
  - calls `state_store.scroll_states(...)`
  - runs `metabolism_controller.tick(...)`
  - persists updates via `state_store.persist_batch(...)` when updates exist
  - returns cursor + counts; safe on empty pages

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/test_autophagy_kernel.py::TestAutophagyKernel::test_sweep_metabolism_fetches_one_page_ticks_and_persists_updates -q`
Expected: PASS

---

### Task 3: Orchestrator Startup + Periodic Sweeps

**Files:**
- Modify: `src/opencortex/orchestrator.py`
- Modify (config knobs): `src/opencortex/config.py`
- Test: `tests/test_perf_fixes.py` (add a focused async test)

- [ ] **Step 1: Write failing orchestration test**

Test should prove:
- Startup sweep is scheduled non-blocking (via create_task) after cognition init.
- Periodic loop calls one-page sweep on interval using bounded batch size.
- `close()` cancels/awaits the periodic task.

- [ ] **Step 2: Verify RED**

Run: `pytest tests/test_perf_fixes.py::TestAutophagySweeperLifecycle -q`
Expected: FAIL (missing wiring / missing config fields / missing cancellation behavior).

- [ ] **Step 3: Minimal implementation**
Add `CortexConfig.autophagy_sweep_interval_seconds = 900` and `autophagy_sweep_batch_size = 200`.

In `MemoryOrchestrator`:
- Track `self._autophagy_sweep_task`, `self._autophagy_startup_sweep_task`, `self._autophagy_sweep_cursor`.
- Start tasks after cognition init (fire-and-forget startup + periodic loop).
- Periodic loop processes exactly one page per interval (bounded), carrying cursor forward and resetting on exhaustion.
- `close()` cancels/awaits the periodic task (and optionally startup task) cleanly.

- [ ] **Step 4: Verify GREEN**

Run: `pytest tests/test_perf_fixes.py::TestAutophagySweeperLifecycle -q`
Expected: PASS

---

### Task 4: Full Verification + Commit

**Files:**
- Modify: `src/opencortex/cognition/state_store.py`
- Modify: `src/opencortex/cognition/kernel.py`
- Modify: `src/opencortex/orchestrator.py`
- Modify: `src/opencortex/config.py`
- Modify: `tests/test_autophagy_kernel.py`
- Modify: `tests/test_cognitive_state_store.py`
- Modify: `tests/test_perf_fixes.py`

- [ ] **Step 1: Run targeted pytest**

Run:
```bash
pytest tests/test_cognitive_state_store.py tests/test_autophagy_kernel.py -q
pytest tests/test_perf_fixes.py::TestAutophagySweeperLifecycle -q
```

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "feat: add paged autophagy metabolism sweeps"
```

