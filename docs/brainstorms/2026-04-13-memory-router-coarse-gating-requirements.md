---
date: 2026-04-13
topic: memory-bootstrap-probe
---

# Memory Bootstrap Probe

> Naming alignment: in the current refactor, Phase 1 is `probe`, not `router`; Phase 3 is `executor`, not `runtime`.

## Problem Frame

OpenCortex's current hot path still assumes that Phase 1 should perform semantic routing before retrieval begins. That made the router responsible for deciding:

- whether memory retrieval should happen
- what broad semantic posture the query belongs to
- how much retrieval depth and association the downstream system should prepare for

This direction is no longer aligned with the retrieval strategy we want:

- `OpenViking`-style staged loading should make the first step a cheap evidence probe rather than a semantic classifier
- `M-Flow`-style fine-grained anchor and cone expansion should be driven by retrieved evidence, not by query-only intent guesses
- planner should become evidence-aware instead of depending on a front-loaded semantic guess

Under this architecture, a semantic router becomes both expensive and misleading:

- it guesses before seeing evidence
- it introduces avoidable latency variance in the hottest path
- it recreates hidden semantic coupling between router and planner
- it encourages correctness debates about query classes instead of improving retrieval evidence quality

The new Phase 1 direction is therefore not a coarse classifier. It is a **bootstrap probe**:

- every normal query first performs one cheap `L0` retrieval probe
- the probe returns lightweight evidence signals
- planner decides whether to stop or escalate based on those signals

Under the current hybrid direction, the probe should be understood as **anchor-aware `L0` probing**, not semantic routing:

- it searches derived `L0` anchor projections first
- those anchor hits point back to one parent `MemoryObject`
- object-level hypotheses are then formed cheaply from anchor support
- `MemoryKind` is therefore observed from retrieved evidence, not predicted ahead of retrieval as a separate router output

## Requirements

**Phase 1 Responsibility**
- R1. Phase 1 must remain an explicit hot-path boundary.
- R2. Phase 1 must be redefined as a bootstrap retrieval probe rather than a semantic classifier.
- R3. Phase 1 must accept only `query` as semantic input.
- R4. Phase 1 must not emit semantic class labels such as `lookup`, `profile`, `explore`, or `relational`.
- R5. Phase 1 must not decide whether a normal query enters the memory path; all normal queries must run through the probe first.
- R6. Phase 1 may bypass execution only for technical invalid cases such as empty or malformed query input.

**Bootstrap Probe Behavior**
- R7. The bootstrap probe must perform a cheap first-pass memory lookup against `L0` evidence only.
- R8. The bootstrap probe must keep `top_k` intentionally small in the first version.
- R9. The bootstrap probe must not enable cone expansion during the first pass.
- R10. The bootstrap probe must not enable rerank during the first pass.
- R11. The bootstrap probe must prefer low and predictable latency over semantic completeness.
- R12. The bootstrap probe must be local and hot-path safe, and must not depend on remote LLM reasoning.

**Probe Retrieval Shape**
- R13. The bootstrap probe must retrieve lightweight anchor candidates from the `L0` retrieval surface rather than performing standalone query intent classification.
- R14. The first-pass retrieval target must be derived anchor projections built from `.abstract.json` or an equivalent canonical machine-readable `L0` structure surface.
- R15. Each anchor hit must preserve its parent object identity so later phases can hydrate or expand from the same `MemoryObject` rather than from detached snippets.
- R16. The probe may also consult object-level `L0` summaries as a lightweight fallback or support signal, but anchor-level `L0` retrieval must remain the primary first-pass surface.
- R17. Each anchor candidate must carry cheaply available metadata including at least:
  - `anchor_id`
  - `parent_object_id`
  - `anchor_type`
  - `MemoryKind`
  - first-pass score or equivalent similarity strength
  - anchor-level `L0` text
- R18. The probe may include a minimal amount of cheap store metadata when available, but must not trigger deep object hydration in the first pass.
- R19. The probe must not query `L2` payloads during the first pass.
- R20. The probe must be allowed to form cheap object hypotheses from multiple supporting anchor hits that point to the same parent object.

**Probe Output Contract**
- R21. Phase 1 must output a structured `probe_result`.
- R22. `probe_result` must contain the first-pass `L0` hits or an equivalent machine-readable candidate surface.
- R23. `probe_result` must contain enough evidence signals for planner to decide whether to stop or escalate.
- R24. The first-version probe output should include at least:
  - `query`
  - `anchor_hits`
  - `object_hypotheses`
  - `top_score`
  - `score_gap` or equivalent separation signal
  - `hit_count`
  - optional probe latency / retrieval metadata
- R25. Each `anchor_hit` in `probe_result` should include at least:
  - `anchor_id`
  - `parent_object_id`
  - `anchor_type`
  - `memory_kind`
  - `score`
  - `anchor_l0`
  - optional cheap anchor metadata such as shared entities, time refs, or topics when already available from the `L0` surface
- R26. Each `object_hypothesis` in `probe_result` should include at least:
  - `object_id`
  - `memory_kind`
  - `supporting_anchor_ids`
  - `aggregated_score`
  - `supporting_anchor_count`
  - optional `anchor_coherence`
- R27. `probe_result` must not contain planner-owned semantic class output.
- R28. Phase 1 may include retrieval metadata in `probe_result`, but must not include execution-policy decisions such as cone enablement, rerank enablement, or final depth selection.

**Boundary With Planner**
- R29. Phase 2 planner must consume:
  - raw `query`
  - `probe_result`
- R30. Planner must become the first stage allowed to decide whether retrieval should stop after the bootstrap probe.
- R31. Planner must become the first stage allowed to compute `class_prior` or any class-like retrieval prior.
- R32. Planner must become the first stage allowed to decide whether deeper retrieval, cone expansion, or rerank is justified.
- R33. Planner may infer object-type priors from the retrieved `memory_kind` distribution inside `probe_result`, but Phase 1 must not convert those observations into a standalone semantic route output.
- R34. Phase 1 must not reintroduce semantic classes indirectly through trace-only or metadata-only side channels.

**Boundary With Executor**
- R35. Executor must treat Phase 1 output as first-pass evidence, not as a semantic route.
- R36. Executor must not recreate a semantic router from `probe_result`.
- R37. Executor must execute planner decisions that were informed by the probe rather than adding a hidden pre-planning classifier of its own.

**Performance Direction**
- R38. Phase 1 must make latency more predictable than the current classifier-first approach.
- R39. Probe cost must be fixed and bounded enough that obvious non-memory queries paying the probe toll is still acceptable.
- R40. The architecture must prefer one cheap evidence probe for all normal queries over a separate semantic classification step with higher variance.
- R41. Future optimization should focus first on making `L0` probe quality high enough that planner can make good escalation decisions.
- R42. The default local embedding model for bootstrap probe must be a lightweight multilingual model rather than a large general-purpose multilingual model.
- R43. The first default local embedding model for bootstrap probe and subsequent local vectorization must be `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, unless an explicit configuration override is supplied.
- R44. Probe latency budgets and rollout expectations must be calibrated against the selected lightweight default model rather than against `multilingual-e5-large`.

## Success Criteria

- Phase 1 stops being a semantic bottleneck.
- All normal queries follow the same cheap first-pass entry path.
- Planner decisions are based on real evidence rather than pre-retrieval intent guesses.
- Phase 1 latency becomes more predictable and easier to budget.
- Future retrieval quality improvements can come from `L0` quality, planner policy, and cone/executor evolution rather than more query classification logic.

## Scope Boundaries

- This document does not define exact scoring formulas for probe sufficiency.
- This document does not define exact `L0` storage or summary generation strategy.
- This document does not define class-prior computation details.
- This document does not define exact executor escalation policy.
- This document does not remove the `probe -> planner -> executor` phase split; it changes the meaning of Phase 1.
- This document does not define the exact vector index implementation or ANN backend for the probe.

## Key Decisions

- Replace semantic routing with bootstrap probing: The first decision should follow cheap evidence, not precede it.
- Probe every normal query: This removes the need for a semantic gate while keeping hot-path behavior uniform and predictable.
- Keep Phase 1 deliberately weak: The probe should only expose first-pass evidence, not semantic retrieval policy.
- Move `class_prior` downstream: If class-like retrieval hints still help, planner should derive them after seeing the query and probe evidence together.
- Default to a lightweight multilingual local embedding model: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` is the new local default because it keeps bootstrap probe cost low enough for hot-path use.
- Probe retrieves anchors first, then cheap object hypotheses: `MemoryKind` should be read off retrieved evidence rather than guessed from query text alone.

## Dependencies / Assumptions

- The three-phase hot-path split remains accepted:
  - `probe -> planner -> executor`
- `L0` evidence exists or can be made reliable enough to support a cheap first-pass probe.
- Local vectorization defaults are allowed to change when hot-path latency requires it.
- Future retrieval quality should come primarily from staged loading, better object surfaces, and evidence-driven cone expansion rather than stronger query classification.

## Outstanding Questions

### Deferred to Planning
- [Affects R14][Technical] What exact derived anchor projection format should Phase 1 read from `.abstract.json`?
- [Affects R16][Technical] What first-version mix between anchor-level and object-level `L0` lookup keeps probe cheap without hurting recall?
- [Affects R20][Technical] Which object-hypothesis aggregation signals belong directly in `probe_result` versus planner-local derivation?
- [Affects R39][Needs research] What latency envelope keeps universal probing acceptable for clearly non-memory queries?
- [Affects R41][Needs research] Which benchmark slices are most sensitive to anchor-level `L0` quality versus planner escalation policy?
- [Affects R25][Technical] Which cheap anchor metadata should be included directly in each `anchor_hit` without turning the probe into early hydration?
- [Affects R44][Technical] What first-version `top_k` and score-gap heuristics keep probe both cheap and planner-useful?

## Next Steps

-> /ce:plan for structured implementation planning
