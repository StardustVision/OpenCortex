---
date: 2026-04-15
topic: probe-starting-point-locator
---

# Probe Starting-Point Locator — Requirements

**Status**: Ready for Planning  
**Scope**: `src/opencortex/intent/probe.py` + `src/opencortex/intent/planner.py` + `src/opencortex/intent/executor.py`  
**Inspirations**: OpenViking global search as starting-point discovery (docs/design/2026-04-14-opencortex-openviking-borrowable-retrieval-optimization.md) | m_flow Cone Retrieval (docs/superpowers/specs/2026-04-05-cone-retrieval-design.md)

---

## Problem Frame

The current `probe` phase fails at its job for two related reasons:

1. **anchor_probe is broken**: The filter requires `is_leaf=True` but anchor_projection records are written with `is_leaf=False`, making the anchor search return zero results on every query. Benchmark evidence: all 5 sampled LoCoMo queries show `anchor_candidates=0`.

2. **Probe has no "starting point" concept**: It searches for leaf memory objects directly instead of finding session/document roots that establish scope boundaries. There is no `level` field, no hierarchical traversal, and no scope inheritance from parent to child. The probe is effectively a flat vector search with extra steps.

The result: retrieval quality depends entirely on raw embedding similarity, with no hierarchy awareness and no entity-path propagation. LongMemEval Recall@1=0.05 is the measurable symptom.

---

## Requirements

### R1 — Probe Searches for Starting Points (Not Leaf Objects)

Probe must find **session/document roots** as starting points, not leaf memories.

**Definition of a starting point** (v1):
- Has `session_id` set AND (`parent_uri` is empty OR `parent_uri` is a session-scoped root)
- OR has `source_doc_id` set AND is the root of a document
- These are the "containers", not the "contents"

**Clarification — "session-scoped root"**: A record whose `parent_uri` points to the session root URI (the top-level URI for a session, e.g. `{tid}/{uid}/memories/events/{session_id}`). Such a record is the immediate child of the session root and functions as the session's entry point container. Immediates (per-message records with `is_leaf=True`) are NOT starting points even if they have `session_id` + `parent_uri`, because they are contents rather than containers.

**Probe output must include** for each starting point:
- `uri` — the starting point's URI
- `session_id` — its session scope (for session recall)
- `parent_uri` — its parent boundary (for recursive traversal upward if needed)
- `entities` — named entities from its `structured_slots` (inherited by descendants)
- `time_refs` — time anchors from its `structured_slots`
- `score` — vector similarity of the starting point match

**Non-goal**: We do NOT add a `level` field or migrate storage schema. Starting points are identified by field presence/absence, not a level enumeration.

### R2 — Anchor Extraction from Query Becomes First-Class Probe Surface

`_query_anchor_terms()` currently extracts anchor candidates from query tokens but feeds them into a broken `anchor_probe` search. Anchors must become a first-class output of probe.

**Anchors are derived from**:
- Named entities from query tokens (`_query_anchor_terms` regex patterns: time tokens, CamelCase, quoted phrases, CJK tokens)
- `structured_slots` of matched starting points (entities, time_refs, topics)

**Probe output** must emit:
- `query_entities` — list of entity strings extracted from the query itself
- `starting_point_anchors` — entities/time_refs inherited from the matched starting points

### R3 — Probe Scope Filter is Derived from Starting Points

The scope filter passed to execution must be constructed from the starting point's attributes, not just from the session context.

**Scope hierarchy** (from most specific to least):
1. `session_id` + `parent_uri` — most specific: a specific container within a session
2. `session_id` alone — scoped to an entire session
3. `source_doc_id` alone — scoped to a document
4. No scope — global fallback

The probe must decide which scope level the matched starting points justify, and emit that as `scope_level`, with these explicit values:
- `container_scoped` — `session_id` + `parent_uri` (most specific: a specific container within a session)
- `session_only` — `session_id` set, no `parent_uri` (scoped to an entire session)
- `document_only` — `source_doc_id` set, no `session_id` (scoped to a document)
- `global` — no scope fields (no boundaries, full recall)

### R4 — Anchor Candidates Flow into Cone Expansion

The `query_entities` and `starting_point_anchors` emitted by probe must be usable by the Cone Scorer (`src/opencortex/retrieve/cone_scorer.py`) as first-class inputs.

This is not about changing the Cone Scorer algorithm — it is about ensuring probe's anchor output is in a form the scorer can consume. The existing cone retrieval design already specifies this interface (Section 4.4 and 6.3 of the cone retrieval spec).

### R5 — Planner Uses Starting-Point Evidence to Constrain Retrieval

The planner must use probe's starting point evidence to decide depth and scope, not just to decide whether to do L0/L1/L2 retrieval.

**Planner decisions gated on starting-point evidence**:
- `start_point_count > 0` AND `start_point_anchors` non-empty → enable scope-constrained retrieval with cone expansion (primary path)
- `start_point_count > 0` AND `start_point_anchors` empty → enable scope-constrained retrieval using `session_id` from starting points (session-only scope), without cone expansion
- `start_point_count == 0` AND `query_entities` non-empty → enable global retrieval with cone expansion driven by `query_entities` only
- `start_point_count == 0` AND `query_entities` empty → fall back to global (no scope constraint, no cone expansion)
- `session_scope` determined by whether starting points have `session_id`

### R6 — Executor Bounded Traversal (No Flat Leaf-Only Search)

Executor must support bounded recursive retrieval within scope boundaries, controlled by the starting point's `parent_uri`.

**What executor must NOT do**: Treat `is_leaf=True` records as the only retrieval target. A starting point with `is_leaf=False` that has children must be expandable.

**What executor must do**: When a starting point has children in the store, retrieve those children rather than flat-leaf-searching across the entire session.

### R7 — Anchor Probe Filter Bug Is Fixed

**Immediately**: Remove `is_leaf=True` from the `anchor_probe` filter. Anchor_projection records are correctly `is_leaf=False` — the filter requirement was wrong.

```python
# CURRENT (broken):
filter = _merge_filters(
    _merge_filters(base_filter, _LEAF_FILTER),      # ← is_leaf=True
    _ANCHOR_SURFACE_FILTER,                        # ← anchor_surface=True
    {"op": "must", "field": "anchor_hits", "conds": [term]},
)

# REQUIRED (fixed):
filter = _merge_filters(
    _merge_filters(base_filter, _ANCHOR_SURFACE_FILTER),
    {"op": "must", "field": "anchor_hits", "conds": [term]},
)
# _LEAF_FILTER removed
```

---

## Success Criteria

1. **anchor_candidates > 0** on real queries — anchor_probe returns non-zero candidates after R7
2. **Starting-point evidence in probe trace** — `probe.trace.starting_points` array is non-empty on scoped queries
3. **Scope-level attribution** — each probe result has `scope_level` field (session_only | document_only | container_scoped | global)
4. **LongMemEval Recall@1 recovers** from 0.05 toward at least 0.30 (the gap vs Naive RAG baseline)
5. **No regression on LoCoMo** — J-Score does not drop relative to the 2026-04-15 sample baseline (0.650)
6. **LoCoMo latency p50 decreases** from ~5s toward <3s (fewer hydration calls due to better first-pass recall)

---

## Scope Boundaries

- **In scope**: probe phase behavior, probe→planner interface, executor scope inheritance
- **Out of scope**: Cone Scorer algorithm changes (already designed in cone retrieval spec), storage schema migration (no `level` field), changes to LLM derive prompts
- **Deferred**: whether `parent_uri` recursive traversal is implemented in probe phase or executor phase (technical decision, not product behavior)

---

## Key Decisions

- **No `level` field**: We identify starting points by field presence/absence (`session_id` set + `parent_uri` empty/root), not a level enumeration. Rationale: avoids storage migration, aligns with existing `parent_uri` infrastructure.

- **Anchors from two sources**: query tokens (existing `_query_anchor_terms`) PLUS starting point structured_slots. Rationale: m_flow's first-anchor insight is that query entities and record entities must meet — only retrieving records whose anchors overlap with query anchors produces meaningful path propagation.

- **Scope inherits from starting point**: The starting point's `session_id` and `parent_uri` become the scope filter for downstream retrieval. Rationale: this is the core OpenViking mechanism — the starting point IS the scope boundary.

- **Executor traversal is bounded by scope**: Retrieval expands within the scope defined by the starting point, not across the full session. Rationale: unbounded expansion is what causes LoCoMo hydration to trigger 100% — better scope means fewer false candidates.

---

## Dependencies / Assumptions

- **D1**: `structured_slots` (entities, time_refs, topics) are correctly populated on memory write. Verified in `src/opencortex/memory/mappers.py:memory_abstract_from_record` (write-side projection, which calls `memory_object_view_from_record` internally).
- **D2**: `anchor_projection` records are correctly written with `is_leaf=False` and `anchor_surface=True`. Verified in `orchestrator.py:1359`.
- **D3**: Cone Scorer interface accepts entity lists as input. The cone retrieval spec (Section 6.3) specifies this already — confirm during planning.
- **D4**: `parent_uri` exists on conversation immediate records. Verified in orchestrator.py — immediate writes use `is_leaf=True` and `parent_uri` points to session root.

---

## Outstanding Questions

### Resolve Before Planning

None — the product behavior is resolved by the requirements above.

### Deferred to Planning

- **[T1][Implementation]** Whether bounded recursive retrieval lives in the probe phase (collect starting points then immediately fetch children) or executor phase (probe emits URI constraints, executor handles traversal). Both are feasible — choose based on which minimizes code churn.

- **[T2][Implementation]** How to handle the case where a starting point has zero direct children but is not a leaf (orphan intermediate node). Determine fallback behavior.

- **[T3][Performance]** Whether entity index (from Cone Retrieval design) should be queried during probe phase or deferred to execution. Probe-phase query gives earlier cutoff; execution-phase query is simpler.

- **[T4][Testing]** Whether to add a dedicated probe-level integration test (separate from full recall end-to-end) to validate starting-point selection in isolation.
