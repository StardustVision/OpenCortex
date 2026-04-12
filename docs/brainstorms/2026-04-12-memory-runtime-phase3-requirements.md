---
date: 2026-04-12
topic: memory-runtime-phase3
---

# Memory Runtime Phase 3

## Problem Frame

After Phase 1 router and Phase 2 planner, the system still needs one final layer to turn semantic retrieval intent into an actual recall result without collapsing semantic logic back into execution logic.

That layer is the **memory runtime**.

Its job is not to decide whether recall should happen, nor what semantic strategy should be used. Its job is to take the planner's semantic plan, bind it into a legal executable recall request, execute recall, merge results, and protect the hot path under latency and reliability pressure.

This document defines the target responsibility of the **memory runtime**. It assumes the router and planner boundaries documented in:

- `docs/brainstorms/2026-04-12-memory-router-phase1-requirements.md`
- `docs/brainstorms/2026-04-12-memory-planner-phase2-requirements.md`

## Requirements

**Runtime Responsibility**
- R1. The memory runtime must run only after Phase 2 planner has already produced a semantic retrieval plan.
- R2. The runtime must accept this minimal explicit input contract:
  - `planner_plan`
  - `session_id`
- R2a. The runtime core must also accept explicit execution-boundary context:
  - `tenant_id`
  - `user_id`
  - `project_id`
- R3. Server integration may hydrate this execution-boundary context from request context, but runtime core logic must not depend on ambient HTTP-only state in order to remain testable and replayable.
- R4. The runtime must not require any new client-facing retrieval-control fields beyond existing request inputs such as `session_id`.
- R5. The runtime must remain a bounded hot-path execution layer and must not depend on remote LLM reasoning in the production recall path.

**Runtime Internal Shape**
- R6. The runtime must internally separate four execution responsibilities:
  - `Binder`
  - `Executor`
  - `Merger`
  - `Protector`
- R7. `Binder` must be the first runtime stage and must translate semantic planner output into a concrete executable recall request.
- R8. `Executor` must run the bound recall request against concrete retrieval sources.
- R9. `Merger` must combine multi-route or multi-source candidates into the final recall item set.
- R10. `Protector` must own execution-time protection concerns such as timeout, cache, degrade, breaker, and observability.

**Binder Responsibility**
- R11. `Binder` must consume:
  - `planner_plan`
  - `session_id`
  - explicit execution-boundary context (`tenant_id`, `user_id`, `project_id`)
- R12. `Binder` must decide:
  - legal `sources`
  - legal `scope`
  - executable budgets/policies derived from planner outputs
- R13. `Binder` must be allowed to **shrink or constrain** execution according to legal/runtime boundaries, but it must not redefine planner semantics.
- R14. `Binder` must not change:
  - provenance `task_class`
  - provenance `confidence`
  - primary `strategy`
- R15. `Binder` may translate planner outputs such as `rewrite`, `breadth_budget`, `depth`, `cone_budget`, and `rerank` into concrete runtime policies, but that translation must preserve planner meaning.
- R15a. `Binder` must treat planner-carried provenance as read-only traceability metadata, not as a second input to re-plan semantics.

**Execution Boundary**
- R16. The runtime must treat `session_id` as an explicit runtime input rather than hiding it inside request context.
- R17. The runtime must treat `tenant_id`, `user_id`, and `project_id` as execution boundary context rather than semantic planner inputs.
- R18. Execution boundary constraints such as tenant isolation, user isolation, project isolation, visibility, and session scoping must be non-negotiable and must never be relaxed by degrade logic.

**Runtime Outputs**
- R19. The runtime must output:
  - `items`
  - `trace`
  - `degrade`
- R20. `items` must contain the final recall results returned to downstream answer generation or context assembly.
- R21. `trace` must be structured machine-readable execution facts rather than free-form logs.
- R22. `trace` must be sufficient to explain how runtime actually executed the plan, including at minimum:
  - sources used
  - effective depth
  - effective cone usage
  - effective rerank usage
  - execution latency
- R23. `degrade` must be structured machine-readable runtime downgrade metadata rather than free-form logs.
- R24. `degrade` must indicate:
  - whether degrade was applied
  - why degrade was applied
  - what actions were taken

**Cone Responsibility**
- R25. The runtime must not model cone retrieval as a single undifferentiated on/off switch.
- R26. Cone execution must be split into:
  - `core_cone`
  - `extended_cone`
- R26a. Runtime binding must deterministically map planner `cone_budget` into cone execution posture using this default contract:
  - `0.0` -> `core_cone=off`, `extended_cone=off`
  - `(0.0, 0.4]` -> `core_cone=narrow`, `extended_cone=off`
  - `(0.4, 0.7]` -> `core_cone=normal`, `extended_cone=light`
  - `(0.7, 1.0]` -> `core_cone=full`, `extended_cone=on`
- R27. `core_cone` must represent the narrow, primary, semantically essential structure-following association path.
- R28. `extended_cone` must represent broader optional expansion such as wider fanout, secondary associative expansion, or less essential enrichment paths.
- R29. Runtime degrade policy must preserve `core_cone` by default when the active strategy depends on structured association.
- R30. Runtime degrade policy may disable `extended_cone` before touching `core_cone`.
- R31. If `core_cone` must be degraded under severe latency pressure, runtime should narrow it before disabling it entirely.

**Runtime Degrade Ladder**
- R32. Runtime degrade must follow a fixed predictable order rather than ad hoc case-by-case mutation.
- R33. The default degrade order must be:
  - disable `extended_cone`
  - shrink `breadth`
  - drop secondary or non-core sources
  - disable `rerank`
  - narrow `core_cone`
  - reduce `depth`
- R33a. When planner sets `rerank=true`, runtime should preserve reranking through the first three degrade steps unless the remaining latency budget makes reranking itself the dominant failure risk.
- R34. Runtime degrade must not use latency pressure as justification to rewrite semantic plan identity.
- R35. Runtime degrade must never relax execution boundaries such as tenant, user, project, session, auth, or visibility.

**Boundary With Router**
- R36. Runtime must not re-decide `should_recall`.
- R37. Runtime must not classify query semantics or produce a new `task_class`.

**Boundary With Planner**
- R38. Runtime must not replace planner strategy selection with a second planning system.
- R39. Runtime may bind and constrain planner outputs, but must not redefine planner semantic intent.

**Boundary With Client API**
- R40. Clients must not directly control runtime internals through fields such as:
  - `top_k`
  - `detail_level`
  - `source`
  - `cone_budget`
  - `rerank`
- R41. Existing explicit request identity fields such as `session_id` may remain part of the public API, but retrieval behavior must remain system-controlled.

## Success Criteria

- Runtime can be evaluated separately from router and planner.
- Runtime can explain not only what results were returned, but how execution actually happened.
- Benchmark failures can be attributed to execution behavior rather than being collapsed into an opaque retrieval failure.
- `session_id` remains explicit and testable.
- `tenant_id`, `user_id`, and `project_id` stay outside semantic routing/planning while still constraining execution safely.
- Runtime behavior can be replayed in tests and benchmarks without requiring ambient request-context setup.
- Structured association remains available through `core_cone` without forcing runtime to keep all optional expansion enabled under latency pressure.

## Scope Boundaries

- This document does not define exact source inventory or source-specific adapters.
- This document does not define the exact concrete schema of returned recall items.
- This document does not define exact timeout numbers, cache TTLs, or breaker thresholds.
- This document does not define exact fanout mapping formulas for `breadth_budget`.
- This document does not define the exact implementation class layout of `Binder`, `Executor`, `Merger`, or `Protector`.

## Key Decisions

- Runtime is wide, not thin: It owns bind, execute, merge, and protect from planner output to recall result.
- `session_id` stays explicit: It is a business identity key for recall scope, not hidden ambient auth context.
- Execution-boundary context is explicit at runtime core: Server adapters may source it from request context, but runtime itself remains replayable and testable.
- No new client retrieval knobs: This preserves semantic ownership inside the system.
- Binder can constrain but not redefine: Execution safety must not mutate semantic meaning.
- `items + trace + degrade` required: Runtime must not become a black box again.
- Cone is split into core and extended modes: Structured association is important, but optional expansion must still be degradable.
- Fixed but strategy-aware degrade ladder: Performance protection must be predictable without canceling planner compensation behavior such as low-confidence reranking.

## Dependencies / Assumptions

- Phase 1 router contract is already accepted:
  - `docs/brainstorms/2026-04-12-memory-router-phase1-requirements.md`
- Phase 2 planner contract is already accepted:
  - `docs/brainstorms/2026-04-12-memory-planner-phase2-requirements.md`
- Current code concepts likely to evolve toward this split include:
  - `src/opencortex/context/manager.py`
  - `src/opencortex/cognition/recall_planner.py`
  - `src/opencortex/retrieve/cone_scorer.py`
  - `src/opencortex/orchestrator.py`
- Current request context already provides ambient execution identity dimensions such as tenant, user, and project:
  - `src/opencortex/http/request_context.py`
- A thin server-side adapter may translate request context into explicit runtime execution context for the first implementation.

## Outstanding Questions

### Deferred to Planning
- [Affects R12][Technical] Which concrete retrieval sources should be considered primary versus secondary for each planner strategy?
- [Affects R22][Technical] What exact `trace` schema should be standardized for benchmarks and production telemetry?
- [Affects R24][Technical] What exact `degrade` action vocabulary should be standardized?
- [Affects R33][Technical] What quantitative thresholds should trigger each degrade step?
- [Affects R29][Needs research] For which planner strategies is `core_cone` mandatory versus optional?
- [Affects R10][Technical] Where should cache keys and cache ownership live inside `Protector` without leaking runtime policy into semantic layers?

## Next Steps

-> /ce:review on this Runtime doc, or /ce:plan for cross-phase implementation planning
