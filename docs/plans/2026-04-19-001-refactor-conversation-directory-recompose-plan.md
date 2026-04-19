---
title: Refactor Full Recompose to OpenViking-Style Directory Summarization
type: refactor
status: active
date: 2026-04-19
---

# Refactor Full Recompose to OpenViking-Style Directory Summarization

## Overview

Refactor conversation mode's `_run_full_session_recomposition()` from "merge-and-rewrite" to "preserve leaves + add directory summaries". The current approach re-derives leaf content via LLM, losing detail. The new approach keeps original merged chunks intact and adds a layer of directory parent records that aggregate clusters of semantically related chunks — matching OpenViking's directory overview pattern.

## Problem Frame

Benchmark results (LoCoMo 50 QA sample, 3 conversations, 243 messages) show:

- **24 final chunks**, averaging **10 messages** each (~680 chars overview per chunk)
- 10 chunks' overviews combined = only **6,800 chars (~2,000 tokens)** of context
- LLM-Judge Accuracy: OC 0.58 vs Baseline 0.80 — OC context too compressed to preserve specific facts
- Evidence IS in the retrieved context but compressed overviews bury it

Root causes:
1. **Merge budget 4096 tokens** — produces many small chunks
2. **Cluster cap 3000 tokens** — full_recompose can't effectively merge, resulting in 24 tiny chunks
3. **LLM re-derivation** — overviews are compressed summaries, not original text

## Requirements Trace

- R1. Original merged leaf chunks MUST NOT be re-derived or deleted during full_recompose
- R2. Directory parent records MUST aggregate semantically related leaves via anchor clustering
- R3. Each directory summary MUST be generated from children's L0 abstracts using `_derive_parent_summary()`
- R4. Merge trigger threshold MUST increase from 4096 to 8192 tokens
- R5. Cluster caps MUST align with 8K merge budget
- R6. Session summary MUST aggregate directory abstracts (and any ungrouped leaf abstracts)
- R7. Retrieval MUST NOT return directory records as leaf content

## Scope Boundaries

- **In scope**: `_run_full_session_recomposition()`, merge/cluster parameters, session summary, retrieval filtering
- **Out of scope**: Online tail recompose (latency-sensitive, keeps current merge approach), LoCoMo benchmark adapter changes, retrieval ranking changes

### Deferred to Separate Tasks

- Online tail recompose refactor (currently uses same clustering function — may need separate path later)
- Benchmark adapter alignment for directory-aware URI mapping

## Context & Research

### Relevant Code and Patterns

- **`_generate_session_summary()`** in `src/opencortex/context/manager.py` (line ~1875) — exact pattern to replicate for directories: loads children abstracts → `_derive_parent_summary()` → `add(is_leaf=False)`
- **`_derive_parent_summary()`** in `src/opencortex/orchestrator.py` (line ~1573) — takes `doc_title` + `children_abstracts`, returns `{abstract, overview, keywords}`
- **`_build_anchor_clustered_segments()`** in `src/opencortex/context/manager.py` (line ~1398) — Jaccard-based clustering, returns segments with `source_records` and `msg_range`
- **Document mode bottom-up** in `src/opencortex/orchestrator.py` (lines 1071-1127) — multi-level hierarchy reference
- **`_merged_leaf_uri()`** in `src/opencortex/context/manager.py` (line ~914) — URI pattern: `opencortex://{tenant}/{user}/memories/events/conversation-{hash}-{start:06d}-{end:06d}`

### Institutional Learnings

- Always read L2 content from CortexFS for recompose, not Qdrant overview (prevents cascading corruption) — see Plan 001 (2026-04-18)
- Probe/planner/executor boundaries: retrieval changes must not leak scope decisions into probe — see Plan 002 (2026-04-18)

## Key Technical Decisions

- **Directory records use `layer="directory"`** — distinct from `"merged"` (leaves) and `"session_summary"` (top-level). This enables clean filtering in retrieval and session summary generation.
- **Leaves keep their existing URIs** — no URI rewriting. Directory records get a new URI pattern with `/dir-` prefix.
- **Clusters of size 1 skip directory creation** — a single chunk doesn't need a directory wrapper. Only appears in session summary.
- **`_build_anchor_clustered_segments()` is reused** with larger caps. The function already returns `source_records` per segment, which gives us the children for directory creation.
- **Online tail recompose unchanged** — it still merges and re-derives for latency reasons. Directory structure is only applied at session end.

## Open Questions

### Deferred to Implementation

- Exact directory overview word count target — should match `build_parent_summarization_prompt()` spec (200-500 words) but may need tuning based on cluster size

## Implementation Units

- [x] **Unit 1: Update merge threshold and cluster caps**

**Goal:** Increase conversation merge budget and recompose cluster caps to produce fewer, larger chunks.

**Requirements:** R4, R5

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/context/manager.py`

**Approach:**
Change the following constants:
- `_merge_trigger_threshold` default: 4096 → 8192 (the config attribute `conversation_merge_token_budget`)
- `_RECOMPOSE_CLUSTER_MAX_TOKENS`: 3000 → 8000
- `_RECOMPOSE_CLUSTER_MAX_MESSAGES`: 30 → 60
- `_RECOMPOSE_CLUSTER_JACCARD_THRESHOLD`: 0.15 → 0.15 (unchanged — the threshold is reasonable, the cap was the bottleneck)

**Test scenarios:**
- Happy path: merge triggers at ~8192 tokens instead of ~4096, producing fewer initial chunks
- Edge case: session with < 8192 tokens still produces at least one merged chunk at session end (flush_all)

**Verification:**
- Existing tests pass (merge threshold change is backward-compatible)
- New conversations produce ~4-6 merged chunks instead of 24 for a 243-message conversation

---

- [x] **Unit 2: Add directory URI builder and rewrite `_run_full_session_recomposition()`**

**Goal:** Replace the merge-and-rewrite recompose with directory parent creation that preserves original leaves.

**Requirements:** R1, R2, R3

**Dependencies:** Unit 1

**Files:**
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_context_manager.py`

**Approach:**

1. Add `_directory_uri()` static method — URI pattern: `opencortex://{tenant}/{user}/memories/events/conversation-{hash}/dir-{index:03d}`

2. Rewrite `_run_full_session_recomposition()`:
   - **Keep**: Loading merged records, reading L2 content, building entries, anchor clustering via `_build_anchor_clustered_segments()`
   - **Remove**: The loop that calls `orchestrator.add()` with concatenated content + `defer_derive=True` + `_complete_deferred_derive`
   - **Remove**: The `superseded_merged_uris` deletion step — we no longer delete original leaves
   - **Add**: For each cluster with >= 2 source records:
     a. Collect children L0 abstracts from `source_records`
     b. Call `self._orchestrator._derive_parent_summary(cluster_title, abstracts)`
     c. Create directory record via `orchestrator.add()` with `is_leaf=False`, `layer="directory"`, `meta.session_id`, `meta.msg_range` (span of cluster)
     d. Patch keywords via `storage.update()` (same pattern as session summary)
     e. Write to CortexFS with `is_leaf=False`
   - **Skip**: Clusters of size 1 (single leaf — no directory needed)
   - **Error handling**: On failure, delete only the created directory records (not the leaves)

3. The original merged leaf records remain untouched — no deletion, no re-derivation.

**Patterns to follow:**
- `_generate_session_summary()` (line ~1875) for directory record creation pattern
- Document mode bottom-up in `orchestrator.py` (lines 1071-1127) for multi-level hierarchy

**Test scenarios:**
- Happy path: 6 merged records clustered into 2 directories. Original 6 records still exist. 2 new directory records with `layer="directory"` created.
- Edge case: All records cluster into 1 group → 1 directory created
- Edge case: All records are singleton clusters → 0 directories created (leaves stand alone)
- Error path: LLM derive fails for one directory → that directory skipped, other directories still created, all leaves preserved
- Integration: After full_recompose, `_load_session_merged_records()` returns original leaves (filtered by `layer="merged"`), directories are separate

**Verification:**
- Existing test `test_full_session_recomposition_replaces_merged_set` updated: original records are NO LONGER deleted
- New test: directory records have correct `layer="directory"`, `is_leaf=False`, abstract, overview, keywords
- New test: leaf records have unchanged URIs and content after full_recompose

---

- [x] **Unit 3: Update `_generate_session_summary()` to aggregate directory abstracts**

**Goal:** Session summary should aggregate from directory abstracts when directories exist, falling back to leaf abstracts when no directories were created.

**Requirements:** R6

**Dependencies:** Unit 2

**Files:**
- Modify: `src/opencortex/context/manager.py`
- Test: `tests/test_context_manager.py`

**Approach:**

Update `_generate_session_summary()`:
1. After loading merged records, also load directory records (filter by `layer="directory"`, same session_id)
2. If directories exist: collect directory abstracts as children for session summary
3. If no directories: fall back to merged leaf abstracts (current behavior)
4. Include singleton leaves (those not belonging to any directory) in the aggregation

This requires a new helper `_load_session_directory_records()` — similar to `_load_session_merged_records()` but filtering by `layer="directory"`.

**Patterns to follow:**
- `_load_session_merged_records()` for Qdrant filter + sort pattern

**Test scenarios:**
- Happy path: 2 directories with abstracts + 1 ungrouped leaf → session summary aggregates all 3 abstracts
- Edge case: 0 directories → session summary aggregates leaf abstracts directly (backward compatible)
- Edge case: all leaves belong to directories → session summary aggregates directory abstracts only

**Verification:**
- Session summary overview reflects directory-level themes, not individual chunk details
- No regression when no directories exist

---

- [x] **Unit 4: Filter directory records from retrieval results**

**Goal:** Ensure search/retrieval does not return directory records as answer content — directories serve as broad semantic surfaces for matching, but leaf records provide the actual content.

**Requirements:** R7

**Dependencies:** Unit 2

**Files:**
- Modify: `src/opencortex/context/manager.py` (prepare/retrieval path)
- Modify: `src/opencortex/http/server.py` (if search endpoint returns directories)

**Approach:**

Two strategies to evaluate during implementation:

**Option A — Exclude directories from search results**: Add a Qdrant filter `layer != "directory"` to the search query. Directories are only used internally for session summary generation.

**Option B — Include directories in search, exclude from context building**: Let directories be retrieved (they have good overview text for broad queries), but skip them when building the OC context prompt in the eval pipeline.

Recommended: **Option A** — simpler, directories are an internal structure not meant for end-user retrieval. They serve the session summary, not the search path.

Check if the retrieval path (`_prepare` / `context_recall`) applies any layer filters already, and add `layer != "directory"` to exclude them.

**Test scenarios:**
- Happy path: search returns leaf records only, no directory records
- Integration: context_recall with session_scope still works correctly

**Verification:**
- Search results contain no records with `layer="directory"`

---

- [x] **Unit 5: Update and add tests**

**Goal:** Update existing tests for the new recompose behavior and add tests for directory creation.

**Requirements:** R1-R7

**Dependencies:** Units 1-4

**Files:**
- Modify: `tests/test_context_manager.py`
- Modify: `tests/test_conversation_merge.py` (if it covers recompose)

**Approach:**

Update existing tests:
- `test_full_session_recomposition_replaces_merged_set` — change expectation: original records are preserved, not deleted. Verify new directory records exist.
- `test_full_session_recomposition_reuses_stable_uri_without_self_deletion` — verify leaf URIs unchanged.

Add new tests:
- `test_full_recompose_creates_directory_records` — verify directory records with correct layer, is_leaf, abstract, overview
- `test_full_recompose_preserves_leaf_records` — verify no leaf records deleted
- `test_full_recompose_skips_singleton_clusters` — single-record clusters get no directory
- `test_session_summary_uses_directory_abstracts` — verify session summary aggregates directory abstracts when present
- `test_directory_records_excluded_from_retrieval` — verify search doesn't return directory records

**Verification:**
- `uv run python3 -m unittest tests/test_context_manager.py tests/test_conversation_merge.py -v` passes
- Full suite `uv run python3 -m unittest discover -s tests -v` shows no regressions

## System-Wide Impact

- **Interaction graph**: `_end()` calls `_run_full_session_recomposition()` then `_generate_session_summary()`. Both change but the call sequence stays the same.
- **Error propagation**: Full recompose failure no longer risks deleting original leaf data — only directory records are cleaned up on error.
- **State lifecycle risks**: No partial-write concern — original leaves are never touched. Only additive directory creation.
- **API surface parity**: HTTP endpoints unchanged. The directory structure is internal to conversation mode.
- **Integration coverage**: Benchmark adapter URI mapping (`msg_range` overlap) still works because leaf URIs are unchanged.
- **Unchanged invariants**: Online tail recompose, immediate record lifecycle, CortexFS structure for leaf records.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Larger merge budget (8K) increases per-chunk LLM derive latency | Acceptable — happens in background, not on hot path |
| Directory clustering produces too few directories (over-broad summaries) | Jaccard threshold 0.15 still allows splitting when anchors diverge |
| Session summary quality degrades when aggregating from directory abstracts instead of leaf abstracts | Directory abstracts are already LLM-synthesized from leaf abstracts — two levels of summarization. Monitor via benchmark. |
| Retrieval path may need to distinguish directory vs leaf records | Option A (exclude directories from search) is simplest mitigation |
