---
date: 2026-04-12
topic: memory-planner-phase2
---

# Memory Planner Phase 2

## Problem Frame

After Phase 1 router rearchitecture, memory routing is reduced to a pure semantic decision:

- `should_recall`
- `task_class`
- `confidence`

That split only works if a downstream planner takes over the next responsibility cleanly: translating router output into a retrieval strategy without reintroducing router concerns or runtime concerns.

This document defines the target responsibility of the **memory planner**. It assumes the Phase 1 router boundary documented in `docs/brainstorms/2026-04-12-memory-router-rearchitecture-requirements.md`.

## Requirements

**Planner Responsibility**
- R1. The memory planner must only run when Phase 1 router has already decided `should_recall=true`.
- R2. The planner must consume only semantic router output:
  - `task_class`
  - `confidence`
- R3. The planner must translate router semantics into a **semantic executable retrieval plan**, not directly into a storage-bound execution request.
- R3a. The planner must be treated as a bounded local hot-path component with a target latency of `p50 < 100ms`.

**Planner Outputs**
- R4. The planner must output these primary strategy fields:
  - `strategy`
  - `rewrite`
  - `breadth_budget`
  - `depth`
  - `cone_budget`
  - `rerank`
- R5. The first supported `strategy` set must be:
  - `exact`
  - `time_aware`
  - `profile_aware`
  - `broad`
  - `synthesis`
- R5a. Each `task_class` must map to one primary default strategy:
  - `fact -> exact`
  - `temporal -> time_aware`
  - `profile -> profile_aware`
  - `aggregate -> broad`
  - `summarize -> synthesis`
- R5b. This primary strategy mapping defines planner default posture, but parameter tuning may still significantly change plan shape without changing primary strategy.
- R6. `rewrite` must be expressed as:
  - `none`
  - `light`
  - `aggressive`
- R7. `breadth_budget` must be numeric rather than expressed as fixed labels.
- R7a. `breadth_budget` must use a unified `0.0 ~ 1.0` scale across all planner strategies.
- R8. `depth` must continue to use existing retrieval depth levels:
  - `l0`
  - `l1`
  - `l2`
- R9. `cone_budget` must be numeric rather than expressed as fixed labels.
- R9a. `cone_budget` must use the same unified `0.0 ~ 1.0` scale as `breadth_budget`.
- R10. `rerank` must be an explicit boolean planner output.
- R10a. `rerank` must always be present in planner output even when false.

**Planner Explainability**
- R11. The planner must emit structured explanation alongside strategy outputs.
- R12. The explanation payload must include:
  - `evidence`
  - `decision_trace`
  - `summary`
- R13. Explanation must be mixed-format:
  - evidence-like key signals that influenced planning
  - a compact decision-chain summary showing how final strategy was selected
- R13a. `evidence` and `decision_trace` must be machine-parseable structured data rather than free-form logs.
- R13b. `summary` must be a short human-readable explanation of why the final plan was produced.
- R13c. Explain output should be detailed enough to support benchmark analysis and planner debugging without requiring re-execution.

**Planner Decision Rules**
- R14. The planner must use `task_class` as its primary strategy-selection key.
- R15. `confidence` must tune strategy parameters rather than replacing `task_class` as the primary key.
- R16. Lower confidence must widen retrieval strategy rather than suppress recall.
- R16a. `confidence` must not switch the planner to a different primary strategy; it must tune plan parameters around the primary strategy selected from `task_class`.
- R16b. Lower confidence should make `rerank=true` more likely.
- R17. Complex query handling must live in the planner rather than in the router. This includes stronger rewrite behavior, broader breadth budget, and more expansive cone/rerank choices when needed.

**Boundary With Router**
- R18. The planner must not re-decide `should_recall`.
- R19. The planner must not redefine router semantic categories or introduce a second competing semantic classifier.

**Boundary With Execution Binding**
- R20. The planner must not decide concrete retrieval source or concrete retrieval scope.
- R21. The planner must not bind semantic strategy to tenant, project, session, document set, or storage-specific execution targets.
- R22. A later binding stage may map planner outputs onto source/scope-specific execution, but that binding must remain outside planner semantics.

**Boundary With Runtime**
- R23. The planner must not own timeout policy, fail-open policy, cache policy, degrade policy, or circuit-breaking behavior.
- R24. Runtime reliability must remain separate so planner semantics stay stable even when execution policy changes.

## Success Criteria

- Given the same `task_class` and `confidence`, planner output is deterministic and testable.
- Router and planner can be evaluated separately.
- Planner behavior is interpretable through `evidence` and `decision_trace`.
- Changes to cone retrieval, reranking, or rewrite mechanics do not require redesigning planner input contract.
- Source/scope binding can evolve independently from planner semantics.

## Scope Boundaries

- This document does not define the source/scope binding stage.
- This document does not define runtime implementation details.
- This document does not define exact numeric calibration for budgets.
- This document does not define storage-engine-specific execution payloads.
- This document does not define the internal model or rule system used to implement planning.

## Key Decisions

- Planner only runs on recallable queries: This keeps no-op handling entirely in Phase 1 router.
- Semantic executable plan over storage-executable plan: This preserves planner portability and avoids coupling strategy logic to current retrieval backend.
- `task_class` as primary key: Router semantics remain the stable top-level abstraction.
- One primary strategy per task class: Planner remains interpretable while still allowing parameter-level flexibility.
- `confidence` as parameter tuner: Confidence should widen or relax strategy, not replace semantic category.
- Numeric budgets for breadth and cone: This keeps planner outputs tunable without hardcoding a small set of labels.
- Shared `0.0 ~ 1.0` budget scale: Breadth and cone remain comparable and easier to tune/debug.
- Existing `l0/l1/l2` retained for depth: This reduces migration cost and keeps continuity with current retrieval stack.
- Structured explanation required: Planner must be debuggable and benchmark-analyzable, not a black box.
- Rerank as explicit plan field: Reranking stays visible and testable rather than being hidden inside execution.

## Dependencies / Assumptions

- Phase 1 router contract is accepted as input boundary:
  - `docs/brainstorms/2026-04-12-memory-router-rearchitecture-requirements.md`
- A downstream binding stage will exist even if it is lightweight in the first implementation.
- Current code concepts that may evolve toward this split include:
  - `src/opencortex/cognition/recall_planner.py`
  - `src/opencortex/context/manager.py`

## Outstanding Questions

### Deferred to Planning
- [Affects R7a][Technical] How should `breadth_budget` map onto concrete candidate pool sizes or expansion counts?
- [Affects R9a][Technical] How should `cone_budget` map onto concrete entity expansion behavior?
- [Affects R10][Technical] What exact planner rules should toggle `rerank` on/off under each strategy and confidence range?
- [Affects R12][Technical] What exact machine-readable schema should `evidence` and `decision_trace` use?
- [Affects R20][Technical] What component should own source/scope binding after planner output is produced?
- [Affects R16][Needs research] How should `confidence` be calibrated so widening behavior is predictable and benchmarkable?

## Next Steps

-> Resume /ce:brainstorm for Runtime boundary, or /ce:plan if Planner scope is sufficient to begin implementation planning
