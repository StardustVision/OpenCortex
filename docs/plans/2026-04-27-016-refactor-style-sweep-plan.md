---
title: "refactor: Style sweep + facade hardening (Phase 6)"
type: refactor
status: active
date: 2026-04-27
origin: docs/plans/2026-04-25-010-refactor-orchestrator-memory-service-plan.md
---

# refactor: Style sweep + facade hardening (Phase 6)

## Overview

Phase 6 of the God Object decomposition. Mechanical style sweep across the orchestrator facade and extracted services. Adds missing Google-style docstrings, tightens bare `Any` type hints, and breaks long lines. Also includes facade hardening: cleaning up delegate stubs, consolidating duplicate imports, and removing dead code (volcengine branch).

---

## Requirements Trace

- R1. Every public/protected method on `MemoryOrchestrator` has a non-empty Google-style docstring. (origin: audit §1)
- R2. Every extracted service method has a non-empty docstring. (origin: Phases 1-5 review feedback)
- R3. No bare `Any` type hints where the type is knowable. (origin: audit §3)
- R4. No lines over 100 characters in target files. (origin: audit §4)
- R5. Facade hardening: consolidate triple import of `retrieval_support`, remove dead volcengine branch in bootstrapper. (origin: Phase 5 review findings M3, M6)
- R6. All existing tests pass without modification.

---

## Scope Boundaries

- This is a STYLE sweep, not a structural refactor. No methods are extracted or moved.
- No changes to test files.
- `context/manager.py` docstrings are in scope but structural changes (Phase 7) are NOT.
- Phase 5 branch (`refactor/subsystem-bootstrapper`) is NOT merged yet — style sweep applies to current master state.

---

## Target Files & Issue Count

| File | Missing docstrings | Bare Any | Lines >100 chars | Other |
|------|-------------------|----------|-------------------|-------|
| `src/opencortex/orchestrator.py` | 11 | 3 | 6 | Triple import (M6), dead delegates cleanup |
| `src/opencortex/services/memory_service.py` | 4 | 0 | ? | — |
| `src/opencortex/lifecycle/background_tasks.py` | 1 | 0 | ? | — |
| `src/opencortex/context/manager.py` | 22 | 0 | 16 | — |
| `src/opencortex/insights/agent.py` | 2 | 0 | 0 | — |

**Total:** ~40 docstrings, 3 bare Any, ~22 long lines.

---

## Implementation Units

### U1. Orchestrator facade docstrings + type fixes

**Goal:** Add docstrings to 11 methods, fix 3 bare Any, break 6 long lines.

**Files:**
- Modify: `src/opencortex/orchestrator.py`

**Methods needing docstrings:**
- `config` (line 2065) — property
- `storage` (line 2069) — property
- `fs` (line 2074) — property
- `user` (line 2079) — property
- `_get_record_by_uri` (line 3109)
- `_initialize_autophagy_owner_state` (line 3123)
- `_on_trace_saved` (line 3152)
- `_run_field` (line 1398)
- `_build_one` (line 2610)
- `_get_target_uri` (line 2849)
- `_one` (line 3181)

**Bare Any to fix:**
- `_coerce_derived_string.value: Any` → `str`
- `_coerce_derived_list.value: Any` → `Any` (keep — genuinely dynamic)
- `_on_trace_saved.trace: Any` → check actual type

**Verification:** `pylint --disable=all --enable=missing-docstring` shows zero findings on orchestrator.

### U2. Extracted services docstrings

**Goal:** Add docstrings to 5 methods across memory_service and background_tasks.

**Files:**
- Modify: `src/opencortex/services/memory_service.py`
- Modify: `src/opencortex/lifecycle/background_tasks.py`

**Methods:**
- `memory_service._context_type_from_value` (line 1768)
- `memory_service._detail_level_from_retrieval_depth` (line 1775)
- `memory_service._on_fs_done` (line 727)
- `memory_service._process_one` (line 1175)
- `background_tasks._process_chunk` (line 447)

### U3. ContextManager + insights docstrings

**Goal:** Add docstrings to 24 methods across context/manager and insights/agent.

**Files:**
- Modify: `src/opencortex/context/manager.py`
- Modify: `src/opencortex/insights/agent.py`

**Methods (manager.py, 22):**
- `_safe_remove`, `_prepare`, `_session_summary_uri`, `_commit`, `_merge_trigger_threshold`, `_end`, `_make_session_key`, `_touch_session`, `_remember_session_project`, `_run_autophagy_metabolism`, and 12 more

**Methods (insights/agent.py, 2):**
- `_gen`, `_extract_facet`

### U4. Long line breaks

**Goal:** Break all lines over 100 characters in target files.

**Files:**
- Modify: `src/opencortex/orchestrator.py` (6 lines)
- Modify: `src/opencortex/context/manager.py` (16 lines)

---

## Risks

| Risk | Mitigation |
|------|------------|
| Docstring content is inaccurate | Keep docstrings concise — describe purpose, not implementation details |
| Breaking long strings changes formatting | Use parentheses for string continuation, not backslash |
| `Any` type change breaks callers | Only change where type is unambiguous from code context |
