---
date: 2026-04-13
topic: memory-planner-evidence-driven
---

# Memory Planner Evidence-Driven Evolution

## Problem Frame

OpenCortex originally positioned Phase 2 planner behind a semantic router. That meant planner largely translated a query-class guess into retrieval posture. Under the new direction, that assumption no longer holds:

- Phase 1 is now a bootstrap `L0` probe, not a semantic classifier
- every normal query reaches planner with both raw `query` and first-pass evidence
- retrieval quality should come from staged loading plus evidence-driven escalation rather than from increasingly smart query-only classification

This changes the planner's job. Planner should no longer ask, "which intent class did the router pick?" It should ask:

- did the first-pass `L0` probe already produce enough evidence?
- if not, what is the cheapest justified escalation?
- which retrieval posture best matches the query plus the observed probe evidence?

Under the selected staged-loading direction, this must be interpreted carefully:

- planner is deciding whether `L0` is sufficient to **stop retrieval escalation**
- planner is not proving that `L0` alone is globally sufficient for final answer generation in every case
- `L1` should remain the default next step whenever `L0` evidence is strong enough to anchor relevance but not strong enough to trust as complete answer support

Under the anchor-aware `L0` direction, planner should treat the probe as two coupled evidence surfaces:

- `anchor_hits` show which local semantic units matched
- `object_hypotheses` show which parent objects have the strongest aggregate support

The new Phase 2 planner therefore needs to become **evidence-driven and object-aware**.

## Requirements

**Planner Responsibility**
- R1. Phase 2 planner must run after Phase 1 has produced a `probe_result`.
- R2. Phase 2 planner must consume:
  - raw `query`
  - `probe_result`
- R3. Planner must become the first stage allowed to decide whether retrieval should stop after the bootstrap probe.
- R4. Planner must remain a bounded local hot-path component and must not depend on remote LLM reasoning in the production recall path.

**Planner Output Shape**
- R5. Phase 2 planner must output exactly four primary fields:
  - `target_memory_kinds`
  - `query_plan`
  - `search_profile`
  - `retrieval_depth`
- R6. `target_memory_kinds` must remain required planner output rather than an optional hint.
- R7. `query_plan` must remain a structured object rather than a free-form string.
- R8. `search_profile` must remain a structured object rather than a single abstract strategy label.
- R9. `retrieval_depth` must remain explicit planner output and must support:
  - `l0`
  - `l1`
  - `l2`

**Evidence-Driven Planning**
- R10. Planner must use `probe_result` as first-pass evidence rather than treating it as a weak route hint.
- R11. Planner must be allowed to stop after `L0` when probe evidence is already sufficient.
- R12. Planner must escalate only when the observed probe evidence does not justify stopping.
- R13. Escalation decisions must be justified by evidence weakness, evidence ambiguity, or explicit query demands for richer evidence.
- R14. Planner must not require a semantic classifier result to produce a valid retrieval posture.
- R15. Planner may derive `class_prior` internally if useful, but it must remain planner-internal support signal rather than a public upstream contract.

**Probe Sufficiency Judgment**
- R16. Planner must treat `L0` sufficiency as a retrieval-stop decision, not as a formal proof that no richer evidence could ever help.
- R17. Planner may stop at `L0` only when both of these are true:
  - probe evidence is strong and non-ambiguous
  - the query appears answerable from summary-level memory content
- R18. Planner must escalate beyond `L0` when any of these conditions hold:
  - top hits are ambiguous or tightly clustered
  - the query asks for original wording, exact detail, or full content
  - the query requires conflict resolution or evidence arbitration
  - the query likely depends on richer relation, timeline, or document structure than `L0` can safely expose
- R19. `L1` must be the default escalation target when `L0` found likely anchors but summary-level evidence may still be insufficient for safe answer support.
- R20. `L2` must remain a narrower escalation reserved for cases where even `L1` is not enough.
- R21. The first-version planner should derive `L0` sufficiency from a bounded combination of probe-observable signals such as:
  - best-anchor strength
  - score-gap / rank separation
  - `object_hypothesis` strength
  - supporting-anchor count per object hypothesis
  - `MemoryKind` agreement across the strongest supporting evidence
  - anchor coherence across the best hits
  - whether the query asks for coarse summary versus exact detail
- R22. Planner must prefer `L1` escalation over premature `L2` escalation when richer local context is needed but full raw detail is not yet justified.
- R23. Planner must treat anchor-level ambiguity and object-level ambiguity separately:
  - anchor ambiguity means the local match is weak or conflicting
  - object ambiguity means multiple parent objects remain plausible after anchor aggregation
- R24. Strong `anchor_hits` that converge on one parent object should be treated as a stronger stop signal than a single isolated object-level `L0` match.

**Target Memory Kinds**
- R25. `target_memory_kinds` must represent the primary memory-object surface this query should search next.
- R26. `target_memory_kinds` must be planner-owned output.
- R27. Planner must be allowed to derive `target_memory_kinds` from both raw query cues and probe evidence.
- R28. `target_memory_kinds` must be ordered by planner priority rather than treated as an unordered bag.
- R29. The planner must not assume that one fixed query pattern always maps to one fixed memory-kind set.

**Query Plan**
- R30. `query_plan` must contain:
  - `anchors`
  - `rewrite_mode`
- R31. `anchors` must be planner-extracted structured retrieval anchors rather than raw copied substrings.
- R32. The first supported anchor classes must remain limited to:
  - `entity`
  - `time`
  - `location`
  - `preference`
  - `constraint`
  - `relation`
  - `topic`
- R33. Planner may derive anchors from both raw query and probe candidates.
- R34. `rewrite_mode` must remain limited to:
  - `none`
  - `light`
  - `decompose`
- R35. `light` rewrite must preserve original intent while clarifying retrieval cues.
- R36. `decompose` must be reserved for queries that likely require multiple retrieval subpaths.
- R37. Planner must not turn rewrite into an open-ended query-generation subsystem in the first version.
- R38. `rewrite_mode` must default to `none`.
- R39. Planner should prefer staged retrieval plus cone expansion over query rewrite when likely anchors are already available from the probe.
- R40. Planner may enable rewrite only as a bounded recovery tool when:
  - the query is highly elliptical or referential
  - probe anchors are weak or conflicting
  - decomposition is needed to separate multiple retrieval asks
- R41. Planner must not use rewrite as a routine substitute for better `L0/L1/L2` retrieval and cone design.

**Search Profile**
- R42. `search_profile` must contain:
  - `recall_budget`
  - `association_budget`
  - `rerank`
- R43. `recall_budget` must control how widely flat recall should spread in the next stage.
- R44. `association_budget` must control how strongly structure-aware expansion such as cone-style propagation should be used.
- R45. `recall_budget` and `association_budget` must remain separate planner knobs.
- R46. `rerank` must remain explicit planner output.
- R47. Planner must be allowed to keep both budgets low when `L0` evidence is already strong enough.

**Retrieval Depth**
- R48. `retrieval_depth` must represent the evidence granularity planner wants runtime to fetch next.
- R49. `l0` must remain valid as a planner terminal posture when the probe already produced sufficient summary-level evidence.
- R50. `l1` must be the default escalation target when additional local detail is needed.
- R51. `l2` must remain planner-addressable, but must be treated as a non-default escalation that requires explicit justification.
- R52. Planner must emit `l2` only when:
  - the query explicitly asks for original or full content
  - document-like evidence requires full detail
  - conflict resolution requires deeper arbitration

**Boundary With Phase 1**
- R53. Planner must not depend on any upstream semantic class contract.
- R54. Planner must treat `probe_result` as evidence, not as a hidden route label.
- R55. If planner derives `class_prior`, that signal must not become a new public replacement for the old router `coarse_class` contract.

**Boundary With Runtime**
- R56. Planner must stop at retrieval posture. It must not become an execution binder.
- R57. Planner must not own timeout policy, degrade policy, cache policy, or circuit breaking.
- R58. Runtime must execute planner posture and perform only bounded execution-level adaptation.
- R59. Runtime may deepen or expand execution only within planner-authorized posture and runtime rules, not by inventing a new semantic plan.

## Success Criteria

- Planner becomes the first owner of retrieval judgment after the probe.
- Planner can stop early when `L0` is sufficient for retrieval purposes instead of forcing unnecessary deeper recall.
- Planner can escalate selectively when evidence is weak or ambiguous.
- The system no longer depends on semantic query classes to make retrieval posture decisions.
- Future improvements can focus on `L0` quality, anchor extraction, and cone/runtime behavior rather than reviving router taxonomy.

## Scope Boundaries

- This document does not define exact numeric calibration for sufficiency or budget thresholds.
- This document does not define exact `class_prior` computation mechanics.
- This document does not define final storage schema or `MemoryKind` taxonomy changes beyond the existing shared-domain direction.
- This document does not define exact runtime trace schema.
- This document does not define exact benchmark harness changes.

## Key Decisions

- Planner becomes evidence-driven: The planner should decide after seeing cheap evidence, not before retrieval starts.
- `probe_result` replaces semantic route input: This removes the need for a public query-class contract in Phase 1.
- `class_prior` becomes planner-internal only: If useful, it should help planning without recreating router coupling.
- `l0` can be terminal: The system should be allowed to stop after the first probe when summary-level evidence is already retrieval-sufficient.
- Escalation is selective: `l1`, `l2`, cone, and rerank should happen because evidence justifies them, not because a class label predicted them.
- `l1` is the normal arbitration layer: Following the OpenViking staged-loading pattern, `L0` finds and `L1` helps decide whether deeper detail is necessary.
- Rewrite becomes exceptional: With staged loading and cone expansion available, rewrite should be a bounded recovery tool rather than the planner's default move.
- Anchor support and object support are both first-class signals: Planner should judge local semantic match and parent-object convergence separately.

## Dependencies / Assumptions

- The new Phase 1 bootstrap probe direction is accepted:
  - `docs/brainstorms/2026-04-13-memory-router-coarse-gating-requirements.md`
- The shared memory domain direction remains accepted:
  - `docs/brainstorms/2026-04-13-memory-store-domain-module-requirements.md`
- Runtime will remain bounded and execution-focused rather than regaining planning semantics.

## Outstanding Questions

### Deferred to Planning
- [Affects R21][Technical] What exact sufficiency signals should planner compute from `anchor_hits` and `object_hypotheses`?
- [Affects R15][Technical] Should planner derive `class_prior` from vector prototypes, query-only cues, or both?
- [Affects R33][Technical] Which anchors should be query-derived first versus candidate-derived first?
- [Affects R46][Technical] What evidence conditions should enable rerank after the probe?
- [Affects R52][Technical] What exact conditions justify direct `l2` versus `l1` escalation?
- [Affects R59][Needs research] Which benchmark slices most clearly show good versus wasteful escalation behavior?
- [Affects R40][Technical] Which concrete probe failure modes justify `light` rewrite versus `decompose`?

## Next Steps

-> /ce:plan for structured implementation planning
