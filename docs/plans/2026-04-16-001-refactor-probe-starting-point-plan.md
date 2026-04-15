---
title: "refactor: probe starting-point locator"
type: refactor
status: obsolete
date: 2026-04-16
origin: docs/brainstorms/2026-04-15-probe-starting-point-locator-requirements.md
superseded_by: docs/plans/2026-04-15-001-refactor-scoped-root-anchor-probe-plan.md
---

# Refactor: Probe Starting-Point Locator

## Obsolete

This plan is superseded by `docs/plans/2026-04-15-001-refactor-scoped-root-anchor-probe-plan.md`.

Do not continue implementation from this document. The adopted direction is the smaller `single bucket -> in-scope anchors -> scoped miss/no widening` path, not the broader starting-point/fallback design explored here.

## Overview

Refactor the probe phase to find session/document root "starting points" instead of leaf memory objects, fixing the broken `anchor_probe` filter and establishing scope inheritance from parent to child. Inspired by OpenViking's starting-point discovery (find scope boundary first, then traverse) and m_flow's entity anchor propagation (query anchors + record anchors must meet).

## Problem Frame

The current `probe` phase fails at its job for two related reasons:
1. `anchor_probe` is broken: the filter requires `is_leaf=True` but `anchor_projection` records are written with `is_leaf=False`, making every anchor search return zero results (benchmark: `anchor_candidates=0` on all 5 sampled LoCoMo queries).
2. Probe has no "starting point" concept: it searches for leaf memory objects directly instead of finding session/document roots that establish scope boundaries. The result is flat vector search with no hierarchy awareness.

## Requirements Trace

- **R1**: Probe finds session/document roots as starting points, not leaf objects. Starting points identified by field presence (`session_id` + empty/root `parent_uri`, OR `source_doc_id` set with `parent_uri` empty or equal to document root URI). Probe output includes `uri`, `session_id`, `source_doc_id`, `parent_uri`, `entities`, `time_refs`, `score` per starting point.
- **R2**: Anchor extraction is a first-class probe output. Derived from `_query_anchor_terms` (query tokens) AND `structured_slots` of matched starting points. Probe emits `query_entities` and `starting_point_anchors`.
- **R3**: Scope filter derived from starting points. Scope hierarchy: `container_scoped` (session_id+parent_uri) > `session_only` (session_id) > `document_only` (source_doc_id) > `global` (no boundaries). Probe emits `scope_level` with derivation: (1) non-empty parent_uri → CONTAINER_SCOPED; (2) session_id without parent_uri → SESSION_ONLY; (3) source_doc_id set → DOCUMENT_ONLY; (4) none → GLOBAL. Most specific level across all matched starting points wins.
- **R4**: Anchor candidates flow to Cone Scorer as `query_entities` + `starting_point_anchors`.
- **R5**: Planner gates retrieval decisions on `start_point_count` and `start_point_anchors`. Four cases: (1) start_points>0+anchors non-empty→scope+cone; (2) start_points>0+anchors empty→scope only; (3) start_points=0+query_entities non-empty→global+cone; (4) start_points=0+query_entities empty→global fallback.
- **R6**: Orchestrator retrieval layer does bounded traversal within scope boundaries established by starting points, not flat leaf search. `is_leaf=True` is NOT the only retrieval target. MemoryExecutor only arbitrates parameters — bounded traversal is implemented in `_execute_object_query` (Unit 5).
- **R7**: Fix `anchor_probe` filter. Remove `_LEAF_FILTER` from anchor_probe filter; `_ANCHOR_SURFACE_FILTER` alone correctly identifies anchor_projection records. The `is_leaf=False` on anchor_projection records is correct (orchestrator.py:1359) — the `is_leaf=True` requirement was the bug. No line-number reference to avoid staleness.

## Scope Boundaries

**In scope:**
- probe.py: starting-point search surface, anchor extraction output
- intent/types.py: new output types
- planner.py: new gating signals
- orchestrator.py: bounded retrieval layer (replace `is_leaf=True` with scope-level filters)

**Out of scope:**
- Cone Scorer algorithm changes (already designed in cone retrieval spec)
- Storage schema migration (no `level` field — identified by field presence)
- Changes to LLM derive prompts
- Entity index changes (Cone Scorer handles this)

**Deferred to Implementation:**
- T2: Orphan intermediate node handling (starting point with is_leaf=False but zero children — fallback behavior)
- T4: Probe-level integration test (separate from full recall end-to-end)

## Key Technical Decisions

- **Option B for bounded traversal**: Bounded recursive retrieval lives in orchestrator retrieval layer, not probe phase or executor. `MemoryExecutor` has no retrieval capability — it only arbitrates parameters. The orchestrator's `_execute_object_query` at line 2779 is where retrieval happens, with hardcoded `is_leaf=True` at line 2814.
- **No level field**: Starting points identified by field presence (`session_id` set + `parent_uri` empty/root OR `source_doc_id` + is document root). No storage schema change.
- **Option B for anchor_probe fix**: Remove `_LEAF_FILTER` from anchor_probe filter. `_ANCHOR_SURFACE_FILTER` already correctly identifies anchor_projection records.
- **`scope_level` in SearchResult**: New enum field on `SearchResult` tells orchestrator which filter mode to use. Orchestrator changes `is_leaf=True` to conditional on `scope_level == global`.
- **`session-scoped root` definition**: A record whose `parent_uri` points to the session root URI (top-level session URI, e.g., `{tid}/{uid}/memories/events/{session_id}`). Immediate records (per-message, `is_leaf=True`) are NOT starting points even with `session_id` + `parent_uri`.

## Implementation Units

- [ ] **Unit 1: Fix anchor_probe filter bug (R7)**

**Goal:** Restore anchor_probe to functional — currently returns zero on every query.

**Requirements:** R7

**Dependencies:** None

**Files:**
- Modify: `src/opencortex/intent/probe.py`

**Approach:**
Remove `_LEAF_FILTER` from the anchor_probe filter at lines 278-282. The `_ANCHOR_SURFACE_FILTER` (`anchor_surface=True`) already correctly identifies anchor_projection records. The `is_leaf=False` on anchor_projection records is correct — the filter was wrong.

**Patterns to follow:**
- `_merge_filter_clauses` usage in probe.py for composing filters
- Existing anchor_probe structure (lines 265-314)

**Test scenarios:**
- Happy path: With the fix, anchor_probe returns anchor_projection records that match `anchor_hits` terms (verified by reading orchestrator.py line 1359 to confirm `is_leaf=False` on projection records)
- Regression: Existing object_probe behavior is unaffected

**Verification:**
- `anchor_candidates > 0` in benchmark traces for queries with anchor terms
- No change to object_probe results

---

- [ ] **Unit 3: Add starting-point discovery search to probe.py (R1)**

**Goal:** Add a new search surface that finds session roots and document roots as starting points, identified by field presence.

**Requirements:** R1, R3 (part: scope_level determination)

**Dependencies:** Unit 2 (types must exist first — Unit 2 is the prerequisite)

**Files:**
- Modify: `src/opencortex/intent/probe.py`
- Modify: `src/opencortex/intent/types.py`
- Test: `tests/test_memory_probe.py` (or new test file)

**Approach:**
Add `_starting_point_probe()` method to `MemoryBootstrapProbe`:
- Search for records matching: `session_id` is not empty AND (`parent_uri` is empty OR `parent_uri` matches session-root URI pattern)
- OR: `source_doc_id` is not empty AND record is document root
- Surface: `retrieval_surface` values OR direct field matching on `session_id` + `parent_uri`
- Top-k: 3 (consistent with OpenViking global search `top_k=3`)
- Output: list of `StartingPoint` objects (uri, session_id, parent_uri, entities, time_refs, score)

Add `scope_level` determination per starting point:
- `container_scoped` if `session_id` + `parent_uri` (non-empty parent_uri)
- `session_only` if `session_id` without parent_uri
- `document_only` if `source_doc_id` without session_id
- `global` if no scope fields

Emit `scope_level` as the most specific level across all matched starting points.

**Patterns to follow:**
- Existing `_object_probe` and `_anchor_probe` structure
- OpenViking starting-point discovery: global vector search for top-3 roots (docs/design/2026-04-14-opencortex-openviking-borrowable-retrieval-optimization.md)

**Test scenarios:**
- Edge case: Query with no session context → starting points empty, scope_level global
- Edge case: Query within session → session roots returned, scope_level session_only or container_scoped
- Edge case: Document query → document roots returned, scope_level document_only
- Edge case: Starting point that is an immediate per-message record (is_leaf=True, parent_uri=session_root) → must NOT be classified as starting point (contents, not containers). Exclude: immediates don't set `retrieval_surface` (defaults to empty string); check this or `meta.layer='immediate'` in the search payload to filter them out.
- Happy path: Session with multiple containers → most-specific scope_level wins

**Verification:**
- Starting points appear in probe trace for session-scoped queries
- scope_level field is populated per starting point

---

- [ ] **Unit 2: Add new output types to intent/types.py (R1, R2, R3)**

**Goal:** Add `StartingPoint`, `query_entities`, `starting_point_anchors`, and `scope_level` fields to probe output types. This unit has no dependencies and must be completed first.

**Requirements:** R1, R2, R3

**Dependencies:** None (base types — prerequisite for all other units)

**Files:**
- Modify: `src/opencortex/intent/types.py`
- Test: N/A (types only)

**Approach:**
Add to `SearchResult` (or create new `ProbeResult`):
- `starting_points: List[StartingPoint]` — session/document roots found by `_starting_point_probe()`
- `query_entities: List[str]` — entities extracted from query tokens by `_query_anchor_terms()`
- `starting_point_anchors: List[str]` — entities/time_refs inherited from matched starting points' `structured_slots`
- `scope_level: ScopeLevel` — enum: `container_scoped | session_only | document_only | global`

Define `StartingPoint` dataclass:
```python
@dataclass
class StartingPoint:
    uri: str
    session_id: Optional[str]
    source_doc_id: Optional[str]  # for document roots
    parent_uri: Optional[str]
    entities: List[str]  # from structured_slots
    time_refs: List[str]  # from structured_slots
    score: float
```

Define `ScopeLevel` enum:
```python
class ScopeLevel(str, Enum):
    CONTAINER_SCOPED = "container_scoped"
    SESSION_ONLY = "session_only"
    DOCUMENT_ONLY = "document_only"
    GLOBAL = "global"
```

**Patterns to follow:**
- Existing `SearchCandidate`, `SearchResult` patterns in types.py
- `MemoryKind` enum pattern for ScopeLevel

**Test scenarios:**
- Test expectation: none — types only, no behavior

**Verification:**
- Types are constructable and pass type checking

---

- [ ] **Unit 4: Update planner to use starting-point signals (R5)**

**Goal:** Update `RecallPlanner.semantic_plan()` to gate on `start_point_count` and `start_point_anchors`.

**Requirements:** R5, R4 (Cone Scorer interface)

**Dependencies:** Unit 2 (types), Unit 3 (discovery — must come after types)

**Files:**
- Modify: `src/opencortex/intent/planner.py`
- Test: `tests/test_recall_planner.py` (or probe/planner integration)

**Approach:**
Update `planner.py` to consume the new probe signals:
- Read `probe_result.starting_points` (Unit 2 output) instead of relying solely on `candidate_entries` for scope
- Read `probe_result.query_entities` and `probe_result.starting_point_anchors` for cone expansion gating
- Update `_extract_anchors()` to use `starting_point_anchors` in addition to `anchor_hits`
- Gate retrieval depth decisions using the four-case table:
  - `start_point_count > 0` AND `starting_point_anchors` non-empty → enable scope-constrained retrieval with cone expansion
  - `start_point_count > 0` AND `starting_point_anchors` empty → enable scope-constrained retrieval (session_only), no cone
  - `start_point_count == 0` AND `query_entities` non-empty → global retrieval with cone via `query_entities`
  - `start_point_count == 0` AND `query_entities` empty → global fallback, no cone
- Set `session_scope` on `RetrievalPlan` based on whether starting points have `session_id`

**Patterns to follow:**
- Existing `_extract_anchors()` method structure
- Existing `_infer_class_prior()` pattern for consuming probe signals
- `retrieve_plan.query_plan.anchors` for cone expansion

**Test scenarios:**
- Case 1: start_points>0 + anchors non-empty → scope+cone path
- Case 2: start_points>0 + anchors empty → scope only (no cone)
- Case 3: start_points=0 + query_entities non-empty → global+cone
- Case 4: start_points=0 + query_entities empty → global fallback
- Happy path: Multiple starting points with different scope levels → most-specific scope used
- Edge case: Query with entities but no starting points → cone expansion from query_entities only

**Verification:**
- Planner trace shows correct gating path for each of the four cases
- `session_scope` is set correctly on `RetrievalPlan`

---

- [ ] **Unit 5: Update orchestrator retrieval layer for bounded parent_uri traversal (R3, R6)**

**Goal:** Replace hardcoded `is_leaf=True` in `_execute_object_query` with `scope_level`-conditional filters, enabling bounded traversal from starting points.

**Requirements:** R3, R6

**Dependencies:** Unit 4

**Files:**
- Modify: `src/opencortex/orchestrator.py`

**Approach:**
In `_execute_object_query()` (line 2779), change the filter construction at lines 2811-2816:

**Before (current, broken for starting-point retrieval):**
```python
final_filter = self._merge_filter_clauses(
    search_filter,
    kind_filter,
    {"op": "must", "field": "is_leaf", "conds": [True]},   # hardcoded leaf-only
    start_point_filter,
)
```

**After (scope-level aware):**
```python
# Determine retrieval mode from scope_level
if retrieval_plan.scope_level == ScopeLevel.GLOBAL:
    leaf_filter = {"op": "must", "field": "is_leaf", "conds": [True]}
elif retrieval_plan.scope_level == ScopeLevel.CONTAINER_SCOPED:
    # Retrieve children of the matched starting points (parent_uri IN (...))
    # Starting point URIs come from probe_result.starting_points
    parent_uri_values = [sp.uri for sp in probe_result.starting_points]
    leaf_filter = {"op": "must", "field": "parent_uri", "conds": parent_uri_values}
elif retrieval_plan.scope_level == ScopeLevel.SESSION_ONLY:
    # Retrieve all records in the session (session_id filter)
    session_ids = list({sp.session_id for sp in probe_result.starting_points if sp.session_id})
    leaf_filter = {"op": "must", "field": "session_id", "conds": session_ids}
elif retrieval_plan.scope_level == ScopeLevel.DOCUMENT_ONLY:
    doc_ids = list({sp.source_doc_id for sp in probe_result.starting_points if sp.source_doc_id})
    leaf_filter = {"op": "must", "field": "source_doc_id", "conds": doc_ids}

final_filter = self._merge_filter_clauses(
    search_filter,
    kind_filter,
    leaf_filter,         # conditional, not always is_leaf=True
    start_point_filter,
)
```

Also update `_start_point_filter()` to include starting point URIs alongside anchor URIs:
```python
uri_values = [sp.uri for sp in probe_result.starting_points] + \
             [c.uri for c in probe_result.candidate_entries[:uri_cap]]
```

**Patterns to follow:**
- Existing `_merge_filter_clauses` usage
- Existing `_start_point_filter()` structure at line 2464
- Session filter pattern in existing code

**Test scenarios:**
- Global scope → is_leaf=True (backward compatible with existing behavior)
- Container_scoped → parent_uri IN (...) from starting point URIs
- Session_only → session_id IN (...) from starting points
- Document_only → source_doc_id IN (...) from starting points
- Edge case: Starting point URI list is empty → fall back to is_leaf=True
- Integration: Probe returns starting points → orchestrator applies correct scope filter → bounded results returned

**Verification:**
- Benchmark: anchor_candidates > 0 (from Unit 1 fix)
- Benchmark: starting_points non-empty in probe trace
- Benchmark: scope_level correctly set and used
- Benchmark: LoCoMo p50 latency decreases (fewer hydration calls)
- No regression: existing queries still return results with global scope

---

- [ ] **Unit 6: Integrate starting-point anchor output into Cone Scorer flow (R2, R4)**

**Goal:** Ensure `query_entities` and `starting_point_anchors` from probe flow into the Cone Scorer as first-class inputs.

**Requirements:** R2, R4

**Dependencies:** Unit 2 (types), Unit 3 (discovery), Unit 4 (planner). Unit 6 integration with Cone Scorer means passing `query_entities` + `starting_point_anchors` to the existing `ConeScorer.extract_query_entities()` interface — no algorithm changes. "Out of scope" means the cone scoring algorithm itself is not being redesigned; the integration wiring is in scope.

**Files:**
- Modify: `src/opencortex/intent/probe.py` (emit query_entities)
- Modify: `src/opencortex/intent/planner.py` (pass to RetrievalPlan)
- Modify: `src/opencortex/orchestrator.py` (pass to cone_scorer)
- Test: existing cone_scorer tests

**Approach:**
1. In `MemoryBootstrapProbe.probe()`: extract `query_entities` via `_query_anchor_terms()` and emit in `SearchResult`
2. In `RecallPlanner.semantic_plan()`: read `starting_point_anchors` from probe result, merge with `query_entities` into `retrieve_plan.query_plan.anchors` (existing cone expansion signal)
3. In `orchestrator.py`: pass merged anchor list to `ConeScorer.extract_query_entities()` and `ConeScorer.expand_candidates()`

**Patterns to follow:**
- Existing `_query_anchor_terms()` method in probe.py
- Existing `retrieve_plan.query_plan.anchors` flow to cone_scorer
- `ConeScorer` interface in `retrieve/cone_scorer.py`

**Test scenarios:**
- Happy path: Query with entities → entities extracted → cone expansion uses them
- Happy path: Starting points with structured_slots → starting_point_anchors extracted → cone expansion uses them
- Integration: Both query_entities and starting_point_anchors are merged for cone scoring

**Verification:**
- Cone Scorer receives non-empty entity lists for queries with anchors
- Cone expansion adds relevant candidates that were missed by vector search alone

---

## Open Questions

### Resolved During Planning

- **T1 (bounded traversal location)**: Confirmed Option B — bounded traversal lives in orchestrator retrieval layer (`_execute_object_query`), not probe phase or executor. `MemoryExecutor` has no retrieval capability. The hardcoded `is_leaf=True` at orchestrator.py:2814 becomes conditional on `scope_level`.
- **T3 (entity index query timing)**: Deferred to execution phase (Cone Scorer handles this). Probe emits anchors; Cone Scorer uses them post-retrieval. No change to Cone Scorer algorithm.

### Deferred to Implementation

- **T2**: Orphan intermediate node handling — starting point with `is_leaf=False` but zero direct children. Fallback: treat as `is_leaf=True` for that starting point (retrieve it directly), or fall back to global. Behavior to be determined during Unit 5 implementation.
- **T4**: Probe-level integration test — add dedicated test for starting-point selection in isolation (separate from full recall end-to-end test). Can be added as part of Unit 2 or Unit 3 test file.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| Unit 5 changes `_execute_object_query` hardcoded filter — could break existing retrieval if scope_level is mis-set | Unit 1 (R7 fix) is isolated and safe; scope_level defaults to `global` so existing queries unaffected |
| `parent_uri` traversal could return zero results if starting point URIs are wrong | Unit 2 defines starting points carefully (session root, not immediate records) |
| Planner R5 gating changes could alter retrieval depth unexpectedly | Four-case table is explicit; add planner unit tests covering all four cases |
| Anchor fix (Unit 1) could increase anchor_candidates dramatically and slow retrieval | Anchor probe is keyword search on pre-filtered set; monitor benchmark latency |

## System-Wide Impact

- **Interaction graph**: `_execute_object_query` is called by `ContextManager._local_recall_memory()` (context/manager.py). The `scope_level` signal flows through `RetrievalPlan`. No other caller of `_execute_object_query` changes behavior (scope_level defaults to global for non-probe paths). Guard: if `probe_result` is `None` or `scope_level` is undefined, restore `is_leaf=True` (global legacy behavior).
- **Error propagation**: `scope_level` must be `global` when `probe_result.starting_points` is empty — this is an explicit invariant, not just a graceful fallback. Unit 4 planner must enforce this: if `start_point_count == 0`, scope_level MUST be `global`. If the orchestrator receives a non-global scope_level with zero starting_points, it must override to `global`.
- **API surface parity**: No external API changes. `SearchResult` types are internal to the intent layer.
- **Unchanged invariants**: Global queries (no session context) continue to use `is_leaf=True` leaf-only search. Document queries without session scope still work. The new behavior is additive for scoped queries.

## Sources & References

- **Origin document:** [docs/brainstorms/2026-04-15-probe-starting-point-locator-requirements.md](docs/brainstorms/2026-04-15-probe-starting-point-locator-requirements.md)
- **OpenViking design:** [docs/design/2026-04-14-opencortex-openviking-borrowable-retrieval-optimization.md](docs/design/2026-04-14-openvortex-openviking-borrowable-retrieval-optimization.md)
- **Cone retrieval spec:** [docs/superpowers/specs/2026-04-05-cone-retrieval-design.md](docs/superpowers/specs/2026-04-05-cone-retrieval-design.md)
- **Previous alignment plan:** [docs/plans/2026-04-14-001-refactor-memory-retrieval-openviking-alignment-plan.md](docs/plans/2026-04-14-001-refactor-memory-retrieval-openviking-alignment-plan.md)
- **Gap analysis:** [docs/benchmark/2026-04-15-retrieval-gap-analysis.md](docs/benchmark/2026-04-15-retrieval-gap-analysis.md)
- Probe implementation: `src/opencortex/intent/probe.py`
- Planner implementation: `src/opencortex/intent/planner.py`
- Executor implementation: `src/opencortex/intent/executor.py`
- Retrieval layer: `src/opencortex/orchestrator.py:_execute_object_query`
- Intent types: `src/opencortex/intent/types.py`
- Cone Scorer: `src/opencortex/retrieve/cone_scorer.py`
