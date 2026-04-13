---
date: 2026-04-12
topic: memory-router-phase1
---

# Memory Router Phase 1

## Problem Frame

OpenCortex's current recall hot path mixes three different concerns:

- semantic routing: whether memory recall is needed
- retrieval planning: how recall should be performed
- execution reliability: what happens when planning is slow or fails

This creates blurred ownership across `src/opencortex/intent/router.py`, `src/opencortex/intent/planner.py`, and `src/opencortex/context/manager.py`. The result is a system that is hard to reason about, hard to benchmark fairly, and too easy to regress when optimizing latency or recall quality.

This brainstorm defines the target responsibility of the **memory router** only. It does not redesign the full retrieval stack in implementation detail.

## Requirements

**Router Responsibility**
- R1. The memory router must be a dedicated semantic decision layer whose sole purpose is to classify a `query` into a memory-recall decision.
- R2. The memory router must accept only `query` as its semantic input. It must not depend on caller overrides, scene-specific retrieval controls, planner hints, or runtime reliability state.
- R3. The memory router must output exactly three fields:
  - `should_recall`
  - `task_class`
  - `confidence`
- R4. When `should_recall=false`, `task_class` must be null rather than encoded as a pseudo-category such as `no_recall`.
- R4a. When `should_recall=false`, `confidence` must also be null because task classification is not executed and no planner-facing task confidence exists.
- R5. `should_recall=false` must mean the query does not enter the memory path at all. It must not silently fall through into cheap memory lookup, vector recall, cone expansion, or any other memory retrieval mode.

**Task Model**
- R6. `task_class` must use semantic task names rather than retrieval-action names so that the router remains stable even if retrieval strategy changes later.
- R7. The first supported `task_class` set must be limited to:
  - `fact`
  - `temporal`
  - `profile`
  - `aggregate`
  - `summarize`
- R8. The router must emit exactly one primary `task_class` for recallable queries. Multi-label decomposition is out of scope for the router.
- R9. The router must not include a catch-all category such as `open_reasoning`. Query complexity must be handled downstream by planning rather than by adding a vague router bucket.

**Router Performance and Model Shape**
- R10. The router must be treated as a hot-path component with a target latency of `p95 < 1ms` and `p99 < 2ms` on target production hardware.
- R11. The router must not depend on remote LLM inference in production hot path.
- R12. The first production router must not require model training or a heavyweight NLP runtime.
- R13. The router should be implemented as a lightweight local NLP-lite classifier rather than a bare keyword matcher or a remote model.
- R14. The router must be implemented as a single-stage scoring pass over the query:
  - first: a very narrow `should_recall=false` guard
  - then: one scoring pass across `fact`, `temporal`, `profile`, `aggregate`, and `summarize`
- R15. `should_recall=false` must be reserved for a very small set of deterministic non-memory queries:
  - pure greeting / thanks / closing
  - pure acknowledgment such as `好的` or `收到`
  - empty or near-empty text
- R16. All other queries must default to `should_recall=true`, even when `task_class` confidence is low.
- R17. The router scoring pass must prefer conservative classification:
  - if the query is ambiguous, the router must fall back to `task_class=fact`
  - ambiguous fallback must still return `should_recall=true`
- R18. The `confidence` field must remain numeric and must reflect both:
  - absolute support for the selected `task_class`
  - margin between the selected `task_class` and the next-best alternative
- R19. The router must use a small, curated set of high-value patterns and structural signals rather than large language-specific keyword lists.
- R20. The first production router must provide high-quality support for:
  - Chinese
  - English
  - mixed Chinese/English queries
- R21. For other languages, the router must fail safely by preferring `should_recall=true` and conservative fallback to `fact` when high-confidence classification is unavailable.

**Boundary With Planner**
- R22. The router must not decide retrieval source, retrieval scope, query rewrite, multi-query expansion, `top_k`, `detail_level`, reranking, or cone/entity expansion behavior.
- R23. A downstream planner must consume `should_recall`, `task_class`, and `confidence` and translate them into concrete retrieval strategy.
- R24. The planner must use `confidence` as a widening signal, not a suppression signal: low-confidence router outputs should allow broader downstream retrieval strategies rather than prematurely reducing recall.

**Boundary With Runtime**
- R25. Timeout policy, cache policy, degrade/fail-open behavior, circuit breaking, and other execution-time reliability controls must not live inside the router.
- R26. A separate runtime layer must own execution reliability so that slow planning or infrastructure instability cannot silently redefine router semantics.

**API and Ownership**
- R27. External callers must not directly control memory-router outputs through fields such as `detail_level`, `top_k`, or retrieval source selection.
- R28. Service-layer business logic may still constrain downstream planner behavior, but those constraints must be applied after routing rather than baked into router semantics.

## Success Criteria

- Router behavior can be specified and tested as pure `query -> decision`.
- Router latency stays within `p95 < 1ms` and `p99 < 2ms` on target production hardware.
- Planner strategy can change without requiring router category redesign.
- Runtime reliability policy can change without redefining semantic routing.
- Benchmark analysis can separately attribute failures to router, planner, or runtime rather than treating recall as a single opaque step.
- Future optimizations to cone retrieval, reranking, or query expansion do not require widening the router's scope.
- Ambiguous queries do not get over-classified into deep planner paths when a conservative `fact` fallback would be safer.
- Chinese, English, and mixed Chinese/English queries achieve stable task classification without introducing a training dependency.

## Scope Boundaries

- This document does not define planner strategy internals for each `task_class`.
- This document does not define runtime implementation details such as cache keys, timeout numbers, or circuit-breaker thresholds.
- This document does not redesign non-memory routing such as document-only or tool-dispatch routing.
- This document does not define training data because the first production router is intentionally training-free.
- This document does not define an ML classifier architecture because the first production router is an NLP-lite scoring classifier.
- This document does not yet define the exact evaluation corpus, scoring thresholds, or pattern inventory.

## Key Decisions

- Router is query-only: This keeps router semantics deterministic, cacheable, and independently testable.
- Semantic task classes over retrieval-action classes: This preserves clean separation between classification and planning.
- Single-label router output: Complex query expansion belongs in the planner, not in the router.
- No `open_reasoning` bucket: Vague categories hide classification weakness and push ambiguity into every downstream consumer.
- Single-stage scoring over staged cascade: Router should stay simple, cacheable, and extremely fast while still avoiding bare keyword matching.
- NLP-lite classifier over remote LLM or trained model: Router is a hot-path system component and must stay within strict latency budget without introducing a training lifecycle.
- `should_recall=false` is absolute: This keeps router semantics measurable and prevents hidden memory retrieval paths from undermining threshold tuning.
- Cone/entity expansion belongs to planner: It is a retrieval strategy choice, not a routing decision.
- Confidence widens recall: In memory systems, false negatives are usually more damaging than slightly broader retrieval.
- Conservative fallback to `fact`: Over-confident classification is more dangerous than planner-visible uncertainty.
- Chinese/English first, safe fallback for other languages: Router quality should be strongest where current product usage is concentrated without causing cross-language false negatives.

## Dependencies / Assumptions

- The current codebase already has separable concepts that can evolve toward this split:
  - `src/opencortex/intent/router.py`
  - `src/opencortex/intent/planner.py`
  - `src/opencortex/context/manager.py`
- Downstream planner logic will have access to service-layer constraints such as allowed retrieval sources even though the router will not.
- The current router implementation is too dependent on bare substring matches and fixed precedence, so a breaking internal rewrite is expected.

## Outstanding Questions

### Deferred to Planning
- [Affects R23][Technical] What planner strategy matrix should map each `task_class` to retrieval source, breadth, depth, and cone behavior?
- [Affects R24][Technical] How should numeric `confidence` widen downstream retrieval without introducing brittle planner thresholds?
- [Affects R25][Technical] Where should fail-open behavior live for `context_recall`: planner wrapper, runtime executor, or context manager orchestration?
- [Affects R27][Technical] Which existing public API fields should be deprecated, removed, or reinterpreted after the router boundary is enforced?
- [Affects R10][Needs research] What benchmark harness should measure router-only p95/p99 latency independently from retrieval latency?
- [Affects R13][Needs research] What minimum pattern inventory is needed to materially improve accuracy without turning the router into a large rule base?
- [Affects R1][Needs research] What evaluation set should be built to measure router accuracy independently from planner and answer-model quality?

## Next Steps

-> /ce:plan for structured implementation planning
