---
date: 2026-04-13
topic: memory-runtime-bounded-adaptive
---

# Memory Executor Bounded Adaptive Evolution

> Naming alignment: this document now treats Phase 3 as `executor`. Older `runtime` wording, when it appears in file names or historical references, should be read as `executor`.

## Problem Frame

Under the revised hot-path direction, OpenCortex now converges toward:

- Phase 1 as a cheap bootstrap `L0` probe
- Phase 2 as an evidence-driven and object-aware planner
- Phase 4 store evolution as the place where memory-object quality and surfaces improve over time

That leaves Phase 3 executor with a narrower but still critical job: execute planner posture safely under real latency, failure, and payload pressure without silently turning into a second planner.

Executor still should not be fully passive, because staged retrieval needs controlled escalation and bounded execution adaptation. But executor also must not become "smart" enough to reinterpret query intent after planner has already judged the probe evidence.

Under the current hybrid direction, executor is also the natural place to host **anchor-first cone expansion**:

- probe finds a small set of likely anchors from cheap `L0/L1` surfaces
- planner decides whether cone is worth opening and how much budget it gets
- executor executes cone as a bounded second-stage expansion around selected anchors

The correct target remains a **bounded adaptive executor**:

- adaptive enough to protect latency and absorb execution failures
- bounded enough that retrieval judgment still belongs to planner

## Requirements

**Executor Responsibility**
- R1. Phase 3 executor must run only after Phase 2 planner has produced retrieval posture from `query + probe_result`.
- R2. Executor must consume planner posture as authoritative retrieval intent.
- R3. Executor must remain a bounded hot-path execution layer and must not depend on remote LLM reasoning in the production recall path.
- R4. Executor must adapt execution only within explicit bounded executor rules.

**Allowed Executor Adaptation**
- R5. Executor may perform only these four classes of adaptive action:
  - `degrade`
  - `early_stop`
  - `hydrate`
  - `fallback`
- R6. `degrade` must mean reducing execution cost while preserving planner intent.
- R7. `early_stop` must mean stopping execution once executor has sufficient evidence under planner posture.
- R8. `hydrate` must mean deepening or enriching already selected candidates without changing planner intent.
- R9. `fallback` must mean execution-level fallback only, not semantic fallback.

**Forbidden Executor Adaptation**
- R10. Executor must not replace planner retrieval judgment with a new query interpretation pass.
- R11. Executor must not create a hidden classifier or semantic router from `query`, `probe_result`, or planner metadata.
- R12. Executor must not redefine planner posture by inventing a new semantic branch.
- R13. Executor must not replace planner-selected `target_memory_kinds` with a newly inferred semantic kind set.

**Execution-Level Fallback**
- R14. Executor fallback must be strictly limited to execution-level substitutions or downgrades.
- R15. Valid executor fallback examples include:
  - rerank-disabled execution when rerank fails
  - association-disabled execution when association path fails
  - shallower or simpler read path when hydration fails
  - source-level substitution when a retrieval backend fails
- R16. Fallback must remain explainable as an execution recovery step, not as hidden replanning.

**Hydration and Escalation Responsibility**
- R17. Executor must be allowed to hydrate already selected candidates into richer evidence when planner posture requires it.
- R18. Hydration must be candidate-local rather than query-replanning.
- R19. Hydration may increase effective evidence depth for selected candidates, but must not reinterpret query semantics.
- R20. Executor trace must explicitly distinguish planned retrieval depth from effective hydrated depth when they differ.
- R21. Executor may execute planner-requested escalation such as deeper reads, association expansion, or rerank, but must not decide those escalations semantically on its own.

**Degrade Responsibility**
- R22. Executor must own latency-protection degrade behavior.
- R23. Degrade may reduce execution cost by actions such as:
  - narrowing candidate breadth
  - disabling optional association expansion
  - disabling rerank
  - skipping non-essential hydration
- R24. Degrade must not justify semantic replanning.
- R25. Degrade order must be predictable and deterministic enough for benchmark replay and debugging.

**Early Stop Responsibility**
- R26. Executor may stop execution early when sufficient evidence is already available under current planner posture.
- R27. Early stop must not be used to hide planner under-retrieval mistakes.
- R28. Early stop criteria must be based on executor evidence sufficiency, not on a new semantic judgment about what the user "really meant."

**Executor Outputs**
- R29. Executor must output:
  - `items`
  - `trace`
  - `degrade`
- R30. `trace` must be structured machine-readable execution facts, not free-form logs.
- R31. `trace` must include enough information to distinguish:
  - probe posture
  - planner posture
  - effective execution posture
  - hydration actions
  - fallback actions
  - latency breakdown
- R32. `degrade` must record whether executor degraded, why, and what concrete actions were taken.

**Boundary With Planner**
- R33. Planner remains the sole owner of retrieval judgment after the bootstrap probe.
- R34. Executor may constrain execution, but only at execution level.
- R35. Executor must not reinterpret planner outputs as vague hints.

**Boundary With Store**
- R36. Executor may rely on store-provided memory surfaces and quality layers during execution.
- R37. Executor may request richer material from store through hydration behavior.
- R38. Executor must not redefine store taxonomy or memory-object meaning.

**Anchor-First Cone Execution**
- R39. Executor must execute cone expansion only after anchor candidates have already been selected from the probe/planner path.
- R40. Executor must not run unconstrained cone expansion as part of first-pass probe retrieval.
- R41. Executor may execute cone only when planner posture explicitly requests it.
- R42. Planner must remain the owner of cone posture, including whether cone is enabled and what execution budget it receives.
- R43. Executor cone behavior must stay bounded by explicit execution limits such as:
  - maximum anchor count
  - maximum expansion fan-out per anchor
  - maximum hop count
  - maximum total expanded candidate budget
- R44. First-version cone execution should default to one-hop anchor-local expansion rather than recursive unbounded graph walking.
- R45. Executor cone expansion must prefer structure-aware links exposed by store, such as:
  - shared entities
  - typed relations
  - near-time links
  - shared topics
  - same session / episode / document lineage
- R46. Executor must preserve a clear distinction between:
  - anchor hits
  - cone-expanded hits
  - hydrated evidence
- R47. Cone-expanded candidates must not automatically outrank direct anchor hits solely because they were reachable through expansion.
- R48. Executor rerank, when enabled, must score both semantic relevance and expansion provenance so that cone acts as evidence completion rather than free-form drift.
- R49. Executor degrade rules may reduce or disable cone execution under latency pressure, but only as an execution-cost reduction and not as semantic replanning.
- R50. Executor trace must record whether cone was requested, whether it ran, which expansion edges were used, and whether cone was degraded or skipped.

## Success Criteria

- Executor can protect latency and handle failures without silently turning into a second planner.
- Planner remains the main source of escalation judgment after the probe.
- Hydration and fallback remain explainable as execution actions rather than hidden semantic reinterpretation.
- Benchmark attribution can clearly separate probe weakness, planner escalation choice, and executor degrade/fallback effects.
- The system gains production safety without collapsing architectural boundaries.
- Cone expansion becomes a controlled second-stage evidence completer rather than a latency-amplifying first-stage recall path.

## Scope Boundaries

- This document does not define exact backend-specific executor code paths.
- This document does not define exact store schema or memory-kind taxonomy.
- This document does not define precise timeout numbers, cache TTLs, or breaker thresholds.
- This document does not define exact evidence-sufficiency formulas for early stop.
- This document does not define exact telemetry field names beyond the requirement for machine-readable trace and degrade outputs.
- This document does not define the final rerank formula for anchor versus cone-expanded candidates.

## Key Decisions

- Executor remains bounded adaptive, not semantic: The new architecture pushes retrieval judgment into planner, not executor.
- Probe and planner posture must stay visible in trace: Otherwise staged retrieval becomes opaque and hard to benchmark.
- Fallback is execution-level only: Once fallback mutates retrieval intent, planner stops being the true owner of the path.
- Hydration is allowed: This keeps executor useful without allowing it to replan.
- Early stop remains executor-owned: It protects latency, but must operate inside planner posture rather than against it.
- Cone belongs in executor, not probe: Cone should deepen evidence around likely anchors rather than widen first-pass search cost.
- Cone remains bounded and provenance-aware: Expanded evidence is useful only if executor can still distinguish what was directly matched versus structurally reached.

## Dependencies / Assumptions

- The new Phase 1 bootstrap probe direction is accepted:
  - `docs/brainstorms/2026-04-13-memory-router-coarse-gating-requirements.md`
- The new evidence-driven Phase 2 planner direction is accepted:
  - `docs/brainstorms/2026-04-13-memory-planner-object-aware-requirements.md`
- Future store evolution may improve hydration and evidence-layer handling, but executor boundary decisions should be fixed now.
- Cone-capable store surfaces will eventually expose enough typed linkage for executor to expand beyond plain vector neighbors.

## Outstanding Questions

### Deferred to Planning
- [Affects R15][Technical] What exact execution fallback ladder should executor implement for source, rerank, association, and hydration failures?
- [Affects R20][Technical] What exact trace schema should distinguish probe posture, planned depth, and effective hydrated depth?
- [Affects R23][Technical] What degrade order should be standardized for predictable replay and latency protection?
- [Affects R26][Technical] What evidence-sufficiency signals should allow `early_stop` without masking planner weakness?
- [Affects R36][Technical] How should executor request richer material from store without coupling itself too tightly to store internals?
- [Affects R38][Needs research] Which benchmark cases are most sensitive to executor hydration and fallback behavior under staged retrieval?
- [Affects R43][Technical] What exact default cone budget should be used for anchor count, fan-out, hop count, and total expansion cap?
- [Affects R45][Technical] Which expansion edges should be available on day one, and which should wait for stronger store structure?
- [Affects R48][Technical] How should rerank combine anchor relevance, edge provenance, and hop penalty without letting cone swamp direct evidence?

## Next Steps

-> /ce:plan for structured implementation planning
