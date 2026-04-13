---
date: 2026-04-13
topic: memory-router-hybrid-evolution
---

# Memory Router Hybrid Evolution

## Problem Frame

OpenCortex has already split the memory hot path into `router -> planner -> runtime`, but the current Phase 1 router still sits in an awkward middle state:

- `rule_only` is extremely fast, but it collapses too many benchmark-style queries into `fact`
- `semantic_hybrid` improves some obvious false negatives, but its current prototype coverage is too narrow for benchmark-shaped phrasing
- a future lightweight trained classifier is expected, but not yet available because the memory system has not finished collecting enough stable routing data

This creates a near-term and long-term requirement at the same time:

- near term: ship a `semantic_hybrid` router that can be enabled as the default online mode without changing external API behavior
- long term: evolve the router into a dual-classifier architecture where `semantic_hybrid` and a future `trained_classifier` run together and a resolver chooses the final route decision automatically

The goal of this brainstorm is to define that online router evolution clearly enough that planning does not need to invent rollout behavior, fallback semantics, or future classifier boundaries.

## Requirements

**Router Contract**
- R1. The external Phase 1 router contract must remain unchanged and continue to output only:
  - `should_recall`
  - `task_class`
  - `confidence`
- R2. The router must remain query-only at the semantic layer. It must not consume planner hints, runtime degradation state, or client retrieval controls as routing inputs.
- R3. The router must preserve the existing rule gate semantics where `should_recall=false` is decided only by a narrow deterministic non-memory guard.
- R4. All recallable queries must continue to flow into exactly one primary `task_class` from:
  - `fact`
  - `temporal`
  - `profile`
  - `aggregate`
  - `summarize`

**Near-Term Online Mode**
- R5. The near-term default online mode must be `semantic_hybrid`, not `rule_only`.
- R6. `semantic_hybrid` must preserve the current fast-path structure:
  - narrow `should_recall=false` rule gate first
  - semantic task classification second
- R7. `semantic_hybrid` must improve benchmark-observed false negatives without reintroducing broad lexical over-classification from the old router implementation.
- R8. `semantic_hybrid` must remain local-only on the hot path. It must not depend on remote LLM inference.
- R9. `semantic_hybrid` must remain deployable without a trained router artifact being present.
- R10. The online system must automatically fall back to a safe routing mode when local semantic classification fails to initialize or errors at runtime.

**Future Evolution**
- R11. The router architecture must explicitly reserve a future `trained_classifier` backend, but that backend must be optional until an offline training and artifact pipeline exists.
- R12. When a `trained_classifier` artifact becomes available, the online default must evolve to a dual-classifier mode rather than replacing `semantic_hybrid` outright.
- R13. The dual-classifier mode must run:
  - `semantic_hybrid`
  - `trained_classifier`
  in parallel on recallable queries after the `should_recall=false` gate.
- R14. The absence of a `trained_classifier` artifact must not require deployment-time operator action. The system must continue automatically in `semantic_hybrid` mode.
- R15. The presence of a `trained_classifier` artifact must not require request-shape changes, client flags, or product-surface changes.

**Resolver Semantics**
- R16. A dedicated resolver stage must own final task selection whenever more than one classifier backend is active.
- R17. The resolver must be correction-oriented rather than conservative:
  - if one backend emits a high-confidence non-`fact` decision and the competing backend falls back to `fact`, the resolver should prefer the non-`fact` result
- R18. The resolver must not turn classifier disagreement into `should_recall=false`; disagreement is a classification problem, not a no-recall signal.
- R19. When multiple classifier backends agree on the same non-`fact` task, the resolver should emit that task with the stronger confidence outcome.
- R20. When classifier backends disagree on two different non-`fact` tasks, the resolver must use explicit arbitration rules rather than implicit implementation order.
- R21. Resolver behavior must be deterministic and benchmark-replayable.

**Deployment and Rollout**
- R22. Enabling or disabling router modes must be a server-side concern. Clients must not need to know whether the server is running `rule_only`, `semantic_hybrid`, or dual-classifier mode.
- R23. Deployment of router upgrades must be operationally transparent:
  - no API contract changes
  - no request payload changes
  - no manual runtime intervention required when an optional classifier artifact is absent
- R24. The router must support safe automatic degradation across these effective online states:
  - `rule_only`
  - `semantic_hybrid`
  - `dual_classifier`
- R25. The system must make the active effective router mode visible in trace and benchmark attribution even though the public response contract remains unchanged.

**Performance**
- R26. The current default online mode (`semantic_hybrid`) must stay within `p95 <= 20ms` on target production hardware.
- R27. The future dual-classifier mode must also be designed around `p95 <= 20ms` on target production hardware.
- R28. Router evolution must prefer local model/runtime choices that keep the future dual-classifier mode within that latency budget rather than optimizing only for offline accuracy.

**Evaluation and Success Measurement**
- R29. Router optimization must be judged by dual success criteria:
  - standalone router classification improvement
  - end-to-end benchmark improvement
- R30. Router improvement work must not be accepted solely because classifier outputs look more semantically plausible; benchmark-level recall and answer support must also improve.
- R31. Benchmark attribution must make it possible to distinguish:
  - rule gate behavior
  - semantic classifier behavior
  - resolver arbitration behavior
- R32. Near-term `semantic_hybrid` iteration must prioritize benchmark-shaped query coverage, especially for:
  - implicit profile/preference questions
  - implicit temporal/event-retrieval questions
  - implicit aggregate/count/comparison questions

## Success Criteria

- `semantic_hybrid` can run as the default online router mode without client-visible contract changes.
- The router can automatically remain available when optional classifier backends are missing or fail to initialize.
- Trace and benchmark outputs expose the effective router mode so benchmark regressions can be attributed correctly.
- Router-only evaluation shows fewer benchmark-derived `fact` collapses on profile, temporal, and aggregate queries than `rule_only`.
- End-to-end benchmark results improve alongside router classification quality rather than drifting apart.
- The router architecture can accept a future `trained_classifier` backend without redesigning planner or runtime contracts.
- Future dual-classifier mode has a clear deterministic resolver contract before training artifacts exist.

## Scope Boundaries

- This document does not define the offline data collection, labeling, or training pipeline for the future `trained_classifier`.
- This document does not define the exact model architecture or ML framework for the future `trained_classifier`.
- This document does not redesign planner strategy, retrieval source binding, or runtime execution behavior outside router-facing attribution needs.
- This document does not define exact benchmark query lists, gold labels, or score thresholds for offline evaluation harnesses.
- This document does not define artifact packaging or CI/CD mechanics beyond the requirement that deployment remain operationally transparent.

## Key Decisions

- New document instead of editing Phase 1 requirements: The existing `2026-04-12` Phase 1 requirements captured the first training-free router target. This document defines the next evolution stage without rewriting that historical decision record.
- `semantic_hybrid` as near-term default: The current optimization goal is not only to prepare future architecture, but to improve current online behavior now.
- Dual-classifier future instead of replacement: `semantic_hybrid` remains valuable as a strong local semantic baseline and correction path even after a trained classifier exists.
- Correction-oriented resolver: Current benchmark pain is dominated by over-collapse into `fact`, so future arbitration should prefer meaningful correction rather than conservative fallback.
- Invisible deployment surface: Router evolution should not leak implementation state into client APIs or require operators to coordinate request-level changes.
- Dual success criteria: Router quality must be grounded in benchmark outcomes, not only router-internal classification metrics.

## Dependencies / Assumptions

- The current codebase already has a phase-native router boundary in `src/opencortex/intent/router.py`, with planner and runtime split into separate modules.
- A local semantic classifier path already exists and can serve as the near-term default online backend.
- A future lightweight trained classifier is expected, but no stable training artifact exists yet.
- Future resolver behavior can be specified now even though one backend is still absent.

## Outstanding Questions

### Deferred to Planning
- [Affects R10][Technical] What is the exact fallback ladder and initialization ownership for optional classifier backends in service startup?
- [Affects R16][Technical] What resolver interface should separate backend scoring from final arbitration without over-abstracting the router?
- [Affects R20][Technical] What explicit arbitration rules should resolve non-`fact` vs non-`fact` disagreements while preserving deterministic behavior?
- [Affects R25][Technical] What exact trace schema should expose classifier backend outputs, resolver input, and final arbitration without bloating hot-path payloads?
- [Affects R27][Needs research] What local classifier/runtime combinations can realistically keep future dual-classifier mode within `p95 <= 20ms` on target hardware?
- [Affects R29][Needs research] What smallest benchmark-derived router evaluation set is sufficient to gate `semantic_hybrid` and later dual-classifier regressions?
- [Affects R32][Needs research] Which benchmark-derived prototype additions or semantic examples provide the highest leverage for near-term `semantic_hybrid` improvement?

## Next Steps

-> /ce:plan for structured implementation planning
