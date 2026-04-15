---
date: 2026-04-15
topic: scoped-root-anchor-probe
---

# Scoped Root + Anchor Probe

## Problem Frame

OpenCortex needs a simpler retrieval shape that absorbs the strongest parts of both OpenViking and m_flow without importing their full complexity:

- OpenViking is strongest when it determines **where to search first** by binding retrieval to a root path or container boundary.
- m_flow is strongest when it uses **sharp anchors** to improve retrieval precision inside an already meaningful semantic neighborhood.
- OpenCortex currently has pieces of both, but the hot path still treats scope and anchors as parallel signals instead of an ordered control flow.

The result is predictable:

- scope is not hard enough early in the path
- anchors can help ranking, but they do not reliably prevent the system from searching the wrong area
- execution still spends too much work on broad candidate pools and late correction

The goal of this brainstorm is to define a minimal retrieval strategy where:

- root scope is determined first
- anchors refine retrieval inside that scope
- local expansion stays inside that scope

## Requirements

**Root Scope First**
- R1. Phase 1 must determine retrieval scope before anchor-guided expansion begins.
- R2. Scope inputs are ordered by strength: `target_uri` first, then `session_id`, then `source_doc_id`, then `context_type` roots, then a tiny global root search.
- R3. Phase 1 must select exactly one active scope bucket for the normal path. It may keep a tiny capped set of roots inside that bucket, but it must not combine roots from different bucket levels into one search.
- R4. Probe must treat root discovery as a dedicated output, not as an accidental byproduct of leaf hits.
- R5. When an explicit scope exists, probe must search inside that scope first instead of reopening a broad leaf pool.
- R6. When no explicit scope exists, probe may run a tiny global root search, but it must return only a small set of starting roots rather than broad final candidates.
- R7. An explicit caller-provided scope is authoritative. If `target_uri`, `session_id`, or `source_doc_id` yields no usable candidates, the default path must return a scoped miss rather than silently widening to a weaker bucket in the same pass.

**Anchor Guidance Inside Scope**
- R8. After root scope is determined, probe must extract or retrieve lightweight anchors from the query and available anchor surfaces.
- R9. Anchor signals may rank, filter, or prioritize candidates inside the selected root scope, but they must not widen scope beyond that root scope.
- R10. Local expansion may use entity, time, and topic anchors, but only within the chosen scope boundary.
- R11. Root-derived anchors must preserve their semantic type through handoff where possible. Entity and time anchors from the selected roots must not be flattened into generic topic-only signals before scoped rerank or expansion.
- R12. A strong anchor may improve confidence inside a scope, but it must not override an already chosen stronger path boundary.

**Simple Execution Path**
- R13. The default hot path must remain simple: root scope detection, anchor-guided scoped retrieval, bounded local expansion, final cap.
- R14. In v1, bounded local expansion must stay within capabilities the current store can already support reliably. If child traversal under a selected root is not reliable, execution must degrade to bucket-local filtering (`session_id` or `source_doc_id`) rather than inventing a deeper recursive walk.
- R15. The system must not introduce a separate binding scorer, cross-signal arbitration matrix, or multi-stage global rescoring layer to combine roots and anchors.
- R16. If scoped retrieval produces no usable candidates, fallback must degrade in a clear order: selected root bucket first, tiny global root fallback second, broad search last. This fallback chain applies only to inferred scope, not to authoritative caller-provided scope.
- R17. Trace output must make it obvious which scope bucket was selected, which root URIs were retained, which typed anchors were used, and whether fallback was triggered.

## Success Criteria

- Scoped conversation queries visibly bind to the correct `session_id` before anchor-guided retrieval runs.
- Document-scoped queries visibly bind to the correct `source_doc_id` or root path before anchor-guided retrieval runs.
- Probe traces can distinguish root discovery from anchor guidance.
- Anchor signals improve precision inside scope without reopening global search on the normal path.
- Retrieval work shifts earlier toward bounded scope selection and later away from unnecessary hydration.
- The new path is simpler to explain than the current mixed model.
- Explicitly scoped misses stay scoped and do not silently leak into weaker global search on the normal path.

## Scope Boundaries

- In scope: probe behavior, scope ordering, anchor usage rules, scoped execution handoff.
- Out of scope: storage schema migration, adding a `level` field, redesigning cone scoring, full graph-style global propagation, introducing a complex root-anchor scoring layer.
- Out of scope: copying OpenViking's full directory taxonomy or m_flow's full bundle-scoring algorithm.

## Key Decisions

- **Path before anchor**: Root path selection happens first because wrong scope cannot be repaired cheaply by later anchor matches.
- **One active bucket per pass**: The system may keep a few roots, but only inside one chosen scope bucket. It does not merge `session_id` and `source_doc_id` roots into one normal-path search.
- **Anchors are soft signals**: Anchors improve precision inside scope; they do not own global boundary selection.
- **Expansion is local only**: Any m_flow-inspired propagation must stay inside the already chosen scope.
- **Explicit scope is authoritative**: Caller-provided `target_uri`, `session_id`, or `source_doc_id` does not silently downgrade to a weaker bucket on the normal path.
- **v1 expansion follows storage reality**: The first version should only use one-hop or bucket-local expansion that the current store can support reliably; deeper recursive traversal is not assumed.
- **Tiny global fallback**: When no explicit scope exists, the system should still search for roots first rather than immediately searching the full leaf pool.
- **Keep the control flow explainable**: The best version of this design is one a human can describe in one sentence: find the right tree, then use anchors to find the right branch.

## Dependencies / Assumptions

- `session_id`, `source_doc_id`, and `parent_uri` already exist as usable scope fields in the current store contract, but `parent_uri` should only be treated as authoritative where current child lookup is already reliable.
- Anchor-oriented payload fields already exist in current records and can remain lightweight.
- Execution can already consume bounded scope information through `probe_result` and `retrieve_plan`, but planning must make the selected bucket, retained roots, and typed anchor handoff explicit in the trace contract.

## Outstanding Questions

### Resolve Before Planning

None.

### Deferred to Planning

- [Affects R5][Technical] What exact cap should the tiny global root search use in the default path.
- [Affects R8][Technical] Whether local expansion should begin as one-hop child retrieval only, or allow a small bounded recursive pass.
- [Affects R12][Technical] What precise condition should trigger the final broad fallback rather than returning an empty scoped result.

## Next Steps

-> /ce:plan for structured implementation planning
