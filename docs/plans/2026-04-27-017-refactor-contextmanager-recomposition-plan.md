---
title: "refactor: Extract SessionRecompositionEngine from ContextManager (Phase 7)"
type: refactor
status: active
date: 2026-04-27
origin: docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md
---

# refactor: Extract SessionRecompositionEngine from ContextManager (Phase 7)

## Overview

Phase 7 targets the SECOND God Object identified in the repo-wide audit: `ContextManager` at `src/opencortex/context/manager.py` (4047 lines, 85 methods, 27 `self._X` attributes). Extracts the `SessionRecompositionEngine` — the self-contained state machine that builds segments from conversation records, clusters them by anchor similarity, derives parent summaries via LLM, and writes directory/session-summary records. The engine accounts for ~1100 lines and 27 methods. After extraction, ContextManager shrinks to ~2900 lines focused on lifecycle coordination (handle/start/close/_end), session state, cache, and instruction formatting.

---

## Problem Frame

ContextManager has 9 distinct responsibility areas. The recomposition subsystem (segmentation, clustering, merging, LLM derivation) is the largest at ~1244 lines and is the most complex — it has deep internal call chains, manages 10 of the 27 `self._X` attributes, and contains the most error-prone async task coordination in the codebase. It is already partially decomposed (BenchmarkConversationIngestService and SessionRecordsRepository extracted in earlier work), but the core recomposition engine remains inline.

---

## Requirements Trace

- R1. `SessionRecompositionEngine` lives at `src/opencortex/context/recomposition_engine.py` following the back-reference pattern: `SessionRecompositionEngine(manager)` with `self._mgr` access.
- R2. All 27 recomposition methods move verbatim — `self._X` becomes `self._mgr._X` for manager-level attributes.
- R3. ContextManager gains a lazy `_recomposition_engine` property (same pattern as orchestrator services).
- R4. All existing tests pass without modification.
- R5. Follows Google Python Style: docstrings on every public method, full type hints, `[SessionRecompositionEngine]` logger prefix.

---

## Scope Boundaries

- This is a MOVE, not a REWRITE.
- `ContextPrepareService` extraction (~500 lines) is deferred to a follow-up — the recomposition engine is the higher-value extraction.
- `_prepare` / `_commit` / `_end` lifecycle methods stay on ContextManager. They delegate to the engine where needed.
- Shared utilities (`_merge_unique_strings`, `_split_topic_values`, `_estimate_tokens`) stay in manager.py as module-level functions.
- No changes to test files.

---

## Method Inventory — Methods to Move (27 methods, ~1100 lines)

### Core Recomposition Orchestration (3 methods, ~550 lines)
| Method | Lines | Role |
|--------|-------|------|
| `_run_full_session_recomposition` | 280 | Full session recompose coordinator |
| `_merge_buffer` | 200 | Online incremental merge |
| `_generate_session_summary` | 173 | Session-level summary from directories |

### Segment Building (6 methods, ~320 lines)
| Method | Lines | Role |
|--------|-------|------|
| `_build_recomposition_entries` | 101 | Entry construction from snapshots |
| `_build_recomposition_segments` | 63 | Sequential segmentation |
| `_build_anchor_clustered_segments` | 102 | Jaccard-based clustering |
| `_finalize_recomposition_segment` | 30 | Segment payload materialization |
| `_select_tail_merged_records` | 33 | Tail window selection |
| `_aggregate_records_metadata` | 58 | Anchor aggregation |

### Task Coordination (4 methods, ~114 lines)
| Method | Lines | Role |
|--------|-------|------|
| `_spawn_merge_task` | 38 | Merge task spawning |
| `_spawn_full_recompose_task` | 35 | Recompose task spawning |
| `_track_session_merge_followup_task` | 25 | Follow-up task tracking |
| `_wait_for_merge_task` | 16 | Merge task waiting |
| `_wait_for_merge_followup_tasks` | 25 | Follow-up task waiting |

### Snapshot / Restore (2 methods, ~50 lines)
| Method | Lines | Role |
|--------|-------|------|
| `_take_merge_snapshot` | 27 | Buffer snapshot detachment |
| `_restore_merge_snapshot` | 23 | Buffer snapshot restore |

### Cleanup (3 methods, ~92 lines)
| Method | Lines | Role |
|--------|-------|------|
| `_purge_records_and_fs_subtree` | 41 | Record + FS subtree cleanup |
| `_list_immediate_uris` | 16 | Immediate URI listing |
| `_load_immediate_records` | 35 | Immediate record loading |

### URI Construction (2 methods, ~37 lines)
| Method | Lines | Role |
|--------|-------|------|
| `_merged_leaf_uri` | 21 | URI construction |
| `_directory_uri` | 19 | URI construction |

### Segmentation Helpers (4 methods, ~59 lines)
| Method | Lines | Role |
|--------|-------|------|
| `_segment_anchor_terms` | 18 | Anchor term extraction |
| `_segment_time_refs` | 16 | Time ref extraction |
| `_is_coarse_time_ref` | 11 | Coarse time detection |
| `_time_refs_overlap` | 14 | Time ref overlap check |

---

## State That Moves to Engine (10 of 27 attributes)

- `self._session_merge_tasks`
- `self._session_merge_task_failures`
- `self._session_merge_followup_tasks`
- `self._session_merge_followup_failures`
- `self._session_full_recompose_tasks`
- `self._derive_semaphore`
- `self._directory_derive_semaphore`
- `self._session_pending_immediate_cleanup`
- `self._conversation_buffers`
- `self._session_merge_locks`

---

## Intra-service Call Analysis

After move, same-service calls stay as `self.method()`:
- `_merge_buffer()` → `self._take_merge_snapshot()`, `self._load_immediate_records()`, `self._select_tail_merged_records()`, `self._build_recomposition_entries()`, etc.
- `_run_full_session_recomposition()` → `self._segment_anchor_terms()`, `self._segment_time_refs()`, `self._build_anchor_clustered_segments()`, etc.

Cross-service calls go through `self._mgr`:
- `self._mgr._conversation_source_uri()` (stays on manager)
- `self._mgr._session_summary_uri()` (stays on manager)
- `self._mgr._orchestrator._derive_parent_summary()` → `self._mgr._orchestrator._derive_parent_summary()`
- `self._mgr._orchestrator.add()` → same
- `self._mgr._orchestrator._storage.filter()` → same

---

## Implementation Units

### U1. Create SessionRecompositionEngine shell + lazy property

**Goal:** Land the module, class, constructor, and lazy property.

**Files:**
- Create: `src/opencortex/context/recomposition_engine.py`
- Modify: `src/opencortex/context/manager.py` (add `_recomposition_engine_instance` + lazy property)
- Create: `tests/test_recomposition_engine.py` (construction smoke tests)

### U2. Move all 27 recomposition methods

**Goal:** Move method bodies verbatim, replace `self._X` with `self._mgr._X` for manager attributes.

**Files:**
- Modify: `src/opencortex/context/recomposition_engine.py` (add all methods)
- Modify: `src/opencortex/context/manager.py` (remove method bodies, add delegates)

### U3. Move state attributes and wire lifecycle

**Goal:** Move 10 state attributes to engine constructor, update `_end` and `_commit` to delegate.

**Files:**
- Modify: `src/opencortex/context/recomposition_engine.py`
- Modify: `src/opencortex/context/manager.py`

---

## Risks

| Risk | Mitigation |
|------|------------|
| Attribute rename typo | Same `self._X` → `self._mgr._X` pattern as Phases 1-5. Full test suite covers recomposition path. |
| `_end` coordination split | `_end` stays on manager but delegates merge + cleanup to engine. Careful call chain analysis. |
| Callback wiring | `_spawn_merge_task` wires `asyncio.Task` callbacks that mutate session state — must reference correct `self._mgr` path. |
| Import cycle | Both files in same `context/` package. `TYPE_CHECKING` guard for forward reference. |
