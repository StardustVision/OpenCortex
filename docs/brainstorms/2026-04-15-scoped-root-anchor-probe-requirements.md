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
- OpenCortex also still pays for a retrieval-time query rewrite step, which adds latency to the normal path and hides the more important question: which scope should this query enter first?

The result is predictable:

- scope is not hard enough early in the path
- anchors can help ranking, but they do not reliably prevent the system from searching the wrong area
- execution still spends too much work on broad candidate pools and late correction
- retrieval latency is inflated by per-query rewrite work that should not sit on the hot path

The goal of this brainstorm is to define a minimal retrieval strategy where:

- root scope is determined first
- write-time anchor handles are distilled once, then reused at retrieval time
- anchors refine retrieval inside that scope
- local expansion stays inside that scope
- the normal retrieval path does not block on an LLM query rewrite step

## Flow Shape

```text
query
  -> choose one scope bucket / root set
  -> retrieve against write-time anchors inside that scope
  -> run bounded local expansion inside the same scope
  -> return scoped results or a scoped miss
```

## Requirements

**Root Scope First**
- R1. Phase 1 must determine retrieval scope before anchor-guided expansion begins.
- R2. Scope inputs are ordered by strength: `target_uri` first, then `session_id`, then `source_doc_id`, then `context_type` roots, then a tiny global root search.
- R3. Phase 1 must select exactly one active scope bucket for the normal path. It may keep a tiny capped set of roots inside that bucket, but it must not combine roots from different bucket levels into one search.
- R4. Probe must treat root discovery as a dedicated output, not as an accidental byproduct of leaf hits.
- R5. When an explicit scope exists, probe must search inside that scope first instead of reopening a broad leaf pool.
- R6. When no explicit scope exists, probe may run a tiny global root search, but it must return only a small set of starting roots rather than broad final candidates.
- R7. An explicit caller-provided scope is authoritative. If `target_uri`, `session_id`, or `source_doc_id` yields no usable candidates, the default path must return a scoped miss rather than silently widening to a weaker bucket in the same pass.

**Write-Time Anchor Distill**
- R8. Ingest must distill lightweight anchor or facet handles from stored content before retrieval time, so probe can search against sharper surfaces than raw summary text alone.
- R9. Distilled handles are additive retrieval surfaces. They must not replace or overwrite canonical content, abstracts, or overviews.
- R10. Distilled handles should prefer concrete anchors such as entities, numbers, paths, module names, operations, and time markers, and should avoid paragraph-style or overly generic labels.
- R11. A memory object may keep a small set of aliases or sibling handles for the same idea, but these handles must remain lightweight and tied to that object or root neighborhood.
- R12. After root scope is determined, probe should consume write-time anchors together with query-extracted hints; anchor signals may rank, filter, or prioritize candidates inside the selected root scope, but they must not widen scope beyond that root scope.

**Simple Execution Path**
- R13. The default hot path must remain simple: root scope detection, anchor-guided scoped retrieval, bounded local expansion, final cap.
- R14. In v1, bounded local expansion must stay within capabilities the current store can already support reliably. If child traversal under a selected root is not reliable, execution must degrade to bucket-local filtering (`session_id` or `source_doc_id`) rather than inventing a deeper recursive walk.
- R15. The normal retrieval path must not depend on a per-query LLM rewrite or HyDE step before vector search begins.
- R16. The system must not introduce a separate binding scorer, cross-signal arbitration matrix, multi-stage global rescoring layer, or fallback ladder to combine roots and anchors.
- R17. If scoped retrieval produces no usable candidates, the normal path must return a scoped miss or low-confidence empty result for that scope rather than silently widening to a weaker or global search.
- R18. Trace output must make it obvious which scope bucket was selected, which root URIs were retained, which typed anchors were used, and that no retrieval-time rewrite stage ran.

## Success Criteria

- Scoped conversation queries visibly bind to the correct `session_id` before anchor-guided retrieval runs.
- Document-scoped queries visibly bind to the correct `source_doc_id` or root path before anchor-guided retrieval runs.
- Probe traces can distinguish root discovery from anchor guidance.
- Anchor signals improve precision inside scope without reopening global search on the normal path.
- The normal retrieval path no longer depends on an extra LLM rewrite round trip.
- Retrieval work shifts earlier toward bounded scope selection and later away from unnecessary hydration.
- Write-time anchor handles are easier to explain than hypothetical answer generation at search time.
- The new path is simpler to explain than the current mixed model.
- Explicitly scoped misses stay scoped and do not silently leak into weaker global search on the normal path.

## Scope Boundaries

- In scope: probe behavior, scope ordering, write-time anchor distill rules, anchor usage rules, scoped execution handoff.
- Out of scope: storage schema migration, adding a `level` field, redesigning cone scoring, full graph-style global propagation, introducing a complex root-anchor scoring layer.
- Out of scope: retrieval-time HyDE or other query rewrite stages on the normal path.
- Out of scope: fallback ladders that silently widen from scoped search into broad global search.
- Out of scope: copying OpenViking's full directory taxonomy or m_flow's full bundle-scoring algorithm.

## Key Decisions

- **Path before anchor**: Root path selection happens first because wrong scope cannot be repaired cheaply by later anchor matches.
- **One active bucket per pass**: The system may keep a few roots, but only inside one chosen scope bucket. It does not merge `session_id` and `source_doc_id` roots into one normal-path search.
- **Write once, use many**: If anchor handles need LLM help, do that at ingest time, not on every query.
- **Anchors are soft signals**: Anchors improve precision inside scope; they do not own global boundary selection.
- **Distilled handles are additive**: Rewrite improves retrieval surfaces, but it does not replace canonical stored meaning.
- **Expansion is local only**: Any m_flow-inspired propagation must stay inside the already chosen scope.
- **Explicit scope is authoritative**: Caller-provided `target_uri`, `session_id`, or `source_doc_id` does not silently downgrade to a weaker bucket on the normal path.
- **v1 expansion follows storage reality**: The first version should only use one-hop or bucket-local expansion that the current store can support reliably; deeper recursive traversal is not assumed.
- **No retrieval-time rewrite**: Query understanding may extract hints, but the hot path should not wait on hypothetical answer generation.
- **No silent fallback ladder**: The normal path should either succeed inside scope or return a scoped miss; it should not quietly widen after failing inside a chosen scope.
- **Keep the control flow explainable**: The best version of this design is one a human can describe in one sentence: find the right tree, then use anchors to find the right branch.

## Dependencies / Assumptions

- `session_id`, `source_doc_id`, and `parent_uri` already exist as usable scope fields in the current store contract, but `parent_uri` should only be treated as authoritative where current child lookup is already reliable.
- Anchor-oriented payload fields and write-time summary derivation hooks already exist in current records and can remain lightweight.
- Execution can already consume bounded scope information through `probe_result` and `retrieve_plan`, but planning must make the selected bucket, retained roots, and typed anchor handoff explicit in the trace contract.
- Query hint extraction may still exist, but it is assumed to stay lightweight and non-LLM on the normal retrieval path.

## Outstanding Questions

### Resolve Before Planning

None.

### Deferred to Planning

- [Affects R5][Technical] What exact cap should the tiny global root search use in the default path.
- [Affects R8][Technical] How many distilled anchor handles per object are enough before added aliases stop paying for themselves.
- [Affects R10][Technical] What exact rejection rules should define a bad anchor handle versus a usable one.
- [Affects R14][Technical] Whether local expansion should begin as one-hop child retrieval only, or allow a small bounded recursive pass.

## Next Steps

-> /ce:plan for structured implementation planning
