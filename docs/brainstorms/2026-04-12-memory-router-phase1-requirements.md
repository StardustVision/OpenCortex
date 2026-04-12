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

This creates blurred ownership across `src/opencortex/retrieve/intent_router.py`, `src/opencortex/cognition/recall_planner.py`, and `src/opencortex/context/manager.py`. The result is a system that is hard to reason about, hard to benchmark fairly, and too easy to regress when optimizing latency or recall quality.

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
- R4a. When `should_recall=false`, `confidence` must also be null because stage-2 task classification is not executed and no planner-facing task confidence exists.
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
- R10. The router must be treated as a hot-path component with a target latency of `p50 < 20ms`.
- R11. The router must not depend on remote LLM inference in production hot path.
- R12. The router should be implemented as a lightweight local classifier rather than a rule-dominant router.
- R13. Rule usage in the router must be minimal and limited to narrow guardrail cases rather than being the primary routing mechanism.
- R14. The router must be implemented as a two-stage cascade:
  - stage 1: `should_recall` classifier
  - stage 2: `task_class` classifier, only when stage 1 returns true
- R15. `should_recall` must be probability-based internally, with a thresholded final boolean decision.
- R16. The `confidence` field exposed to downstream planner logic must be the `task_class` confidence produced by stage 2, not the raw probability from stage 1.

**Boundary With Planner**
- R17. The router must not decide retrieval source, retrieval scope, query rewrite, multi-query expansion, `top_k`, `detail_level`, reranking, or cone/entity expansion behavior.
- R18. A downstream planner must consume `should_recall`, `task_class`, and `confidence` and translate them into concrete retrieval strategy.
- R19. The planner must use `confidence` as a widening signal, not a suppression signal: low-confidence router outputs should allow broader downstream retrieval strategies rather than prematurely reducing recall.

**Boundary With Runtime**
- R20. Timeout policy, cache policy, degrade/fail-open behavior, circuit breaking, and other execution-time reliability controls must not live inside the router.
- R21. A separate runtime layer must own execution reliability so that slow planning or infrastructure instability cannot silently redefine router semantics.

**API and Ownership**
- R22. External callers must not directly control memory-router outputs through fields such as `detail_level`, `top_k`, or retrieval source selection.
- R23. Service-layer business logic may still constrain downstream planner behavior, but those constraints must be applied after routing rather than baked into router semantics.

## Success Criteria

- Router behavior can be specified and tested as pure `query -> decision`.
- Router latency stays within `p50 < 20ms` on target production hardware.
- Planner strategy can change without requiring router category redesign.
- Runtime reliability policy can change without redefining semantic routing.
- Benchmark analysis can separately attribute failures to router, planner, or runtime rather than treating recall as a single opaque step.
- Future optimizations to cone retrieval, reranking, or query expansion do not require widening the router's scope.

## Scope Boundaries

- This document does not define planner strategy internals for each `task_class`.
- This document does not define runtime implementation details such as cache keys, timeout numbers, or circuit-breaker thresholds.
- This document does not redesign non-memory routing such as document-only or tool-dispatch routing.
- This document does not yet define training data, exact classifier architecture, or evaluation methodology for the router model.

## Key Decisions

- Router is query-only: This keeps router semantics deterministic, cacheable, and independently testable.
- Semantic task classes over retrieval-action classes: This preserves clean separation between classification and planning.
- Single-label router output: Complex query expansion belongs in the planner, not in the router.
- No `open_reasoning` bucket: Vague categories hide classification weakness and push ambiguity into every downstream consumer.
- Two-stage cascade over single monolithic classifier: This separates the decision to enter memory recall from the decision about which semantic recall task to run.
- Lightweight classifier over remote LLM: Router is a hot-path system component and must stay within strict latency budget.
- `should_recall=false` is absolute: This keeps router semantics measurable and prevents hidden memory retrieval paths from undermining threshold tuning.
- Cone/entity expansion belongs to planner: It is a retrieval strategy choice, not a routing decision.
- Confidence widens recall: In memory systems, false negatives are usually more damaging than slightly broader retrieval.

## Dependencies / Assumptions

- The current codebase already has separable concepts that can evolve toward this split:
  - `src/opencortex/retrieve/intent_router.py`
  - `src/opencortex/cognition/recall_planner.py`
  - `src/opencortex/context/manager.py`
- Downstream planner logic will have access to service-layer constraints such as allowed retrieval sources even though the router will not.
- The first production router may need a temporary compatibility shim while public APIs are migrated away from direct retrieval-control parameters.

## Outstanding Questions

### Deferred to Planning
- [Affects R18][Technical] What planner strategy matrix should map each `task_class` to retrieval source, breadth, depth, and cone behavior?
- [Affects R19][Technical] How should `confidence` be bucketed or scaled when widening downstream retrieval?
- [Affects R20][Technical] Where should fail-open behavior live for `context_recall`: planner wrapper, runtime executor, or context manager orchestration?
- [Affects R22][Technical] Which existing public API fields should be deprecated, removed, or reinterpreted after the router boundary is enforced?
- [Affects R10][Needs research] Which lightweight classifier family best satisfies `p50 < 20ms` while maintaining adequate router accuracy?
- [Affects R1][Needs research] What evaluation set should be built to measure router accuracy independently from planner and answer-model quality?

## Next Steps

-> /ce:plan for structured implementation planning
