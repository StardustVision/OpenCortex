---
date: 2026-04-13
topic: memory-store-domain-module
---

# Memory Store Domain Module Phase 4

> Naming alignment: in the current refactor, the hot path is `probe -> planner -> executor`. Older `runtime` wording in this document should be read as `executor`.

## Problem Frame

OpenCortex now has a clear direction for the first three hot-path phases:

- Phase 1: bootstrap probe
- Phase 2: object-aware retrieval planner
- Phase 3: bounded adaptive executor

That architecture only works well if the retrieval stack shares a stable memory-object surface. Without that surface, planner and executor would still have to reason over loosely typed context records, which would recreate the old problem in a different place:

- planner would emit `target_memory_kinds` that store cannot represent cleanly
- executor hydration and cone expansion would keep depending on ad hoc record shape
- store evolution would force planner/executor changes through scattered coupling

The next step is therefore not a new memory subsystem. It is a **shared domain module** that defines the memory-object contract used by store, planner, executor, and later cone evolution.

This phase must stay deliberately lightweight:

- keep the existing unified store as the primary storage surface
- introduce a shared typed domain module
- avoid adding a second main object store

Under the now-selected hybrid direction, Phase 4 store should combine:

- **OpenViking-like layered retrieval surfaces** for cheap `L0/L1` probing and deeper hydration
- **M-Flow-like cone-ready structure** so executor can expand from anchors through typed object signals rather than flat co-occurrence alone

The durable store unit should still remain the **memory object**, not the anchor itself:

- a `MemoryObject` remains the primary persisted identity
- layered files such as `.abstract.md`, `.abstract.json`, `.overview.md`, and `content.md` attach to that object
- anchors are derived retrieval projections carried by the object rather than independent first-class memory records

## Requirements

**Phase 4 Direction**
- R1. Phase 4 must enhance the existing unified memory store rather than introduce a second primary memory-object store.
- R2. Phase 4 must introduce a shared domain module that becomes the single source of truth for memory-object typing and object-surface rules.
- R3. Planner, store, executor, and later cone logic must import domain types from this shared module rather than redefining them locally.
- R4. The shared domain module must remain a domain-contract module, not a new retrieval engine or storage engine.

**Shared Domain Module Responsibility**
- R5. The shared domain module must define:
  - `MemoryKind`
  - `StructuredSlots`
  - `MemoryObjectView`
  - kind-level policy metadata
- R6. The shared domain module must not own physical persistence, query planning, executor execution, or training logic.
- R7. The shared domain module must use code-level registration rather than configuration-defined open schemas in the first version.

**Memory Kind Model**
- R8. `MemoryKind` must be a strong, closed enum in the first version.
- R9. The first supported `MemoryKind` set must be:
  - `event`
  - `profile`
  - `preference`
  - `constraint`
  - `relation`
  - `document_chunk`
  - `summary`
- R10. `profile` and `preference` must remain distinct kinds rather than being merged into one generic profile bucket.
- R11. The first version must not fold downstream knowledge-layer concepts such as `belief`, `workflow`, `skill`, or `root_cause` into `MemoryKind`.
- R12. Store records must be mappable into exactly one primary `MemoryKind`.

**Primary Kind Inference**
- R13. Each persisted `MemoryObject` must have exactly one primary `MemoryKind`.
- R14. Primary `MemoryKind` must be inferred from the object-level semantic center rather than from a flat vote across anchor types.
- R15. Anchors may assist kind inference, but anchor presence alone must not determine `MemoryKind`.
- R16. The first-version typing rule should be:
  - object extraction and normalized slots decide the primary kind first
  - anchor distribution may refine or validate that choice
  - a single primary kind must still be emitted even when anchor types are mixed
- R17. The store must allow an object of one primary kind to carry anchors associated with other semantic facets.
- R18. Example expectation:
  - an `event` object may still carry `time`, `entity`, `location`, and `preference` anchors
  - a `relation` object may still carry profile-like or topic-like anchors
  - mixed anchors must not force object fragmentation into multiple primary records by default

**Structured Slots Model**
- R19. `StructuredSlots` must use a two-layer model:
  - shared cross-kind slots
  - kind-specific slots
- R20. The first shared cross-kind slot set must include:
  - `entities`
  - `time_refs`
  - `topics`
- R21. Kind-specific slots must remain bounded and typed rather than open arbitrary blobs.
- R22. The first version should support kind-specific slots such as:
  - `preferences` for `preference`
  - `constraints` for `constraint`
  - `relations` for `relation`
  - document lineage/reference fields for `document_chunk`
  - summary lineage/reference fields for `summary`
- R23. Slots must be designed so planner anchors and executor/cone expansion can consume them directly without custom per-call payload translation.

**Memory Object View**
- R24. The shared domain module must define a normalized `MemoryObjectView` that planner and executor can rely on instead of raw store payload shape.
- R25. `MemoryObjectView` must preserve access to store-backed evidence layers, but expose kind and slots in a normalized way.
- R26. Planner and executor must be allowed to reason over `MemoryObjectView` without depending on storage-specific payload quirks.

**Kind Policy Metadata**
- R27. The shared domain module must define bounded kind-level policy metadata for each `MemoryKind`.
- R28. First-version kind policy metadata should cover at least:
  - default mergeability posture
  - default association friendliness
  - default hydration ceiling
  - default retrieval-surface expectations
- R29. Kind policy metadata must guide planner/executor/cone behavior, but must not become a hidden planning engine by itself.

**Cone Alignment**
- R30. Future cone evolution must expand primarily through `MemoryKind` and `StructuredSlots`, not only through raw entity co-occurrence.
- R31. Cone behavior must be allowed to differ by `MemoryKind`.
- R32. Shared slots and kind-specific slots must therefore be designed for structure-aware expansion, not only for display or analytics.

**Compatibility and Evolution**
- R33. The first version may reuse the existing unified store infrastructure, but it must not preserve legacy record shapes as a supported durable contract.
- R34. Legacy pre-refactor memory data does not need migration; Phase 4 may restart from the new contract rather than carrying old durable objects forward.
- R35. The shared domain module must make future store evolution easier, but must not require Phase 4 to complete every future object-surface improvement immediately.

**Layered Retrieval Surface**
- R36. Store-backed `MemoryObjectView` must expose layered retrieval surfaces so the hot path can separate cheap probe text from richer hydration material.
- R37. The first version should preserve at least these practical layers:
  - `.abstract.md` as the human-readable object-level `L0` summary surface
  - `.abstract.json` as the machine-readable `L0` structure surface carrying summary, anchors, and cheap slots
  - `.overview.md` as the `L1` normalized object view
  - `content.md` as the `L2` full evidence payload
- R38. `L0` and `L1` must be representable independently so retrieval can search cheaply first and hydrate later.
- R39. Layered surfaces must attach to the same `MemoryObjectView` rather than creating separate disconnected object identities.
- R40. `.abstract.json` must be the canonical durable source for derived anchor projections in the first version.
- R40a. `.abstract.json` must use one fixed shared top-level schema across all `MemoryKind` values.
- R40b. `MemoryKind` may only change which fields are required, usually populated, or semantically interpreted; it must not introduce kind-specific top-level schema variants.
- R41. Derived anchor projections must be rebuildable from the object's layered files rather than existing only inside vector-store metadata.

**Cone-Ready Expansion Surface**
- R42. Store must expose a bounded, typed expansion surface that executor can use for cone expansion without depending on raw record quirks.
- R43. The first-version expansion surface should support at least these edge families when available:
  - shared entity edges
  - typed relation edges
  - near-time edges
  - shared topic edges
  - lineage edges such as same session, episode, summary source, or document ancestry
- R44. Expansion edges must be attributable and typed so executor trace can explain why a candidate was expanded.
- R45. Store must allow cone expansion to differ by `MemoryKind` rather than treating all objects as equally expandable.
- R46. Store must support retrieving a small bounded neighborhood around an anchor object without requiring executor to manually reconstruct graph logic from raw payload fields.
- R47. The expansion surface must be suitable for anchor-first retrieval, where probe finds likely anchors and executor asks store for a limited structure-aware neighborhood.
- R48. Anchors must be treated as derived retrieval projections:
  - they may have their own vector entries
  - they must point back to one parent `MemoryObject`
  - they must not become independent durable lifecycle units in the first version

**Mode Alignment Across Memory / Document / Conversation**
- R49. Phase 4 must align all three ingest modes with the same `MemoryObject + layered files + derived anchors` store contract.
- R50. The system must not keep one store contract for `memory` mode and separate incompatible contracts for `document` or `conversation` mode.

**Memory Mode Alignment**
- R51. `memory` mode must become the reference write path for direct `MemoryObject` persistence.
- R52. `memory` mode writes must persist the same layered object surface as other modes:
  - `.abstract.md`
  - `.abstract.json`
  - `.overview.md`
  - `content.md`
- R53. `memory` mode dedup and merge must evolve from category-level text merge into object-aware merge policy keyed by `MemoryKind`, normalized slots, and object identity cues.

**Document Mode Alignment**
- R54. `document` mode must continue to preserve document hierarchy, but each stored document unit must still project into the shared `MemoryObject` contract.
- R55. `document` mode chunks or section objects must emit `document_chunk` or other appropriate first-version `MemoryKind` values instead of remaining only path-shaped text records.
- R56. `document` mode must generate the same layered object surfaces as `memory` mode, including `.abstract.json` for anchor projections.
- R57. Document hierarchy and parent-child path structure may remain, but they must not replace object typing, layered files, or anchor projection generation.
- R58. `document` mode is allowed to keep a richer write path than other modes:
  - parse
  - chunk or section hierarchy construction
  - object projection
  - layered file generation
  but its read path must still converge to the same `probe -> planner -> executor` contract as other modes.
- R59. First-version document objects should default to `document_chunk` leaf objects, with parent section objects allowed only when hierarchy materially improves navigation or hydration.
- R60. `.abstract.json` for document objects must carry document-lineage metadata sufficient for retrieval and expansion, including fields such as:
  - `source_doc_id`
  - `source_doc_title`
  - `section_path`
  - `chunk_index`
  - parent or sibling lineage hints when available
- R61. Document anchor projections should focus on high-value document retrieval facets such as:
  - `entity`
  - `time`
  - `topic`
  - `relation` or `claim` only when extraction quality is strong enough
- R62. Document anchors may include limited section-aware context in their `L0` text when that improves first-pass retrieval without inflating payload size.
- R63. Document updates must default to source-aware replace or subtree rebuild rather than semantic text merge.
- R64. First-version document identity for replace behavior should be grounded in source and hierarchy cues such as:
  - `source_doc_id`
  - `section_path`
  - `chunk_index`
  - content hash when needed
- R65. Document retrieval expansion should prefer lineage-aware edges such as:
  - same document
  - parent section
  - child section
  - adjacent chunk
  - same topic
  rather than relying only on generic semantic association.

**Conversation Mode Alignment**
- R66. `conversation` mode must stop treating merged transcript text as the primary durable memory unit.
- R67. Raw conversation turns or traces may remain append-only evidence, but they must be treated as source evidence rather than as the main long-lived memory object surface.
- R68. `conversation` mode must derive `MemoryObject` records from dialogue content and persist them through the same layered object contract used by `memory` and `document` modes.
- R68a. In v1, conversation-derived retrieval objects must use `session_id` as the sole conversation isolation key.
  - The shared durable contract must not introduce a separate `conversation_id` field unless product semantics later require one conversation to span multiple OpenCortex sessions.
- R69. Conversation-derived objects must be allowed to produce kinds such as:
  - `event`
  - `profile`
  - `preference`
  - `constraint`
  - `relation`
  - `summary`
- R70. `conversation` mode must no longer rely on transcript concatenation as its main merge behavior for durable memory.
- R71. Any conversation buffering or batching may still exist for efficiency, but its output must be object extraction and object persistence rather than large merged transcript blobs.
- R72. Session transcripts, turn buffers, or trace stores may still preserve original content for replay and evidence, but that preservation must map to `L2` or trace storage rather than replacing object-level memory.
- R73. Conversation-derived memory must evolve through one superseding object line rather than three long-lived parallel retrieval lines:
  - `immediate` objects may exist briefly for short-latency searchability
  - `merged` objects must supersede and replace the corresponding `immediate` objects
  - `final` objects, if produced at session end, must supersede selected `merged` objects rather than coexist as duplicate long-lived retrieval copies
- R74. When conversation memory advances from one stage to the next, the superseded stage's retrieval objects and anchor projections must be removed or marked inactive so probe does not search multiple stale copies of the same dialogue slice.

**Conversation Merge Policy**
- R75. Conversation-derived merge behavior must be kind-sensitive rather than category-only.
- R76. First-version merge posture should follow at least these defaults:
  - `event` objects default to append/new-object rather than merge-by-text
  - `profile` objects may merge into a stable profile object
  - `preference` objects may merge or update when subject and normalized preference target align
  - `constraint` objects may update structurally, not by raw transcript concatenation
  - `relation` objects may merge only when relation identity is stable enough
  - `summary` objects may be recomputed or overwritten rather than text-appended
- R77. Anchor projections must be regenerated from the stage's current content whenever a parent object changes rather than merged independently across stages.
- R78. `merged` anchors must be recomputed from the merged content itself rather than inherited by concatenating `immediate` anchors.
- R79. `final` anchors, if a final session-end object is produced, must be recomputed from the final consolidated object rather than copied forward mechanically from `merged`.
- R80. Raw conversation evidence must remain recoverable even when object-level merge or overwrite occurs.

**Implementation Drift To Eliminate**
- R81. Existing text-first merge patterns, category-only mergeability, and transcript-concatenation behavior must be treated as legacy behavior to remove rather than behavior to preserve.
- R82. The store contract must not depend on `memory`, `document`, and `conversation` modes flattening different metadata shapes into the same loosely typed top-level record forever.

## Success Criteria

- Planner can name `target_memory_kinds` against a real shared object contract rather than an aspirational taxonomy.
- Executor hydration and cone evolution can consume a normalized object view rather than raw record quirks.
- Store, planner, and executor stop duplicating object-type assumptions.
- The system gains a stronger memory-object surface without introducing a second primary object store.
- Future object-surface evolution becomes incremental instead of cross-cutting and fragile.
- Probe can stay cheap because layered surfaces and cone-ready neighborhoods are already part of the store contract.
- Type inference becomes more stable because primary kind is object-owned while anchors remain auxiliary retrieval projections.
- All three ingest modes land on one shared memory-object contract instead of three incompatible storage semantics.

## Scope Boundaries

- This document does not introduce a second main memory store.
- This document does not define final data reset or cutover procedure details.
- This document does not redesign knowledge or skill storage.
- This document does not define the full future cone algorithm.
- This document does not define custom user-configurable kind schemas in the first version.
- This document does not require all possible expansion edges to exist on day one.
- This document does not require anchors to become standalone user-visible memory records.
- This document does not require raw traces and object memories to collapse into one collection or one lifecycle.

## Key Decisions

- Enhance unified store instead of adding an object store: The current architecture needs a shared object surface more urgently than a second storage system.
- Shared domain module over independent subsystem: The goal is contract centralization and typed consistency, not a new autonomous memory engine.
- Strong enum first: A closed first-version taxonomy keeps planner/executor/store aligned and prevents premature schema drift.
- One shared `.abstract.json` schema: All kinds and modes must fit one fixed top-level structure; differences belong in field population and field semantics, not separate JSON shapes.
- `profile` and `preference` stay separate: They represent different retrieval surfaces and should remain distinguishable.
- Two-layer slot model: Shared slots keep cross-kind retrieval simple; kind-specific slots preserve meaningful object structure.
- Cone should grow from object surface: Structure-aware retrieval should follow typed object signals, not only raw entity overlap.
- Layered surfaces and cone-ready edges must coexist: OpenViking-style cheap probe and M-Flow-style expansion solve different stages of the same retrieval path.
- Primary kind stays object-owned: Anchor projections help retrieval and typing, but they do not replace the object's semantic center.
- Anchors are durable-derived, not primary records: This keeps retrieval fine-grained without making memory lifecycle explode into many tiny managed items.
- `conversation` needs the largest store correction: Transcript buffering may remain, but durable memory must move from merged text blobs to extracted objects.
- Conversation stages must supersede rather than accumulate: `immediate` is temporary, `merged` replaces it, and `final` is optional and selective rather than a third permanent duplicate layer.
- `document` keeps the richest write path but not a separate read path: it may parse and preserve hierarchy on ingest, but once written it must be retrievable through the same probe/planner/executor path as other modes.
- `document` is structurally closest, but still needs to emit the shared layered object contract instead of only path-shaped chunk records.
- `memory` mode becomes the canonical object writer: Other modes should converge to the same persistence contract rather than inventing special store semantics.

## Dependencies / Assumptions

- The new coarse Phase 1 contract is accepted:
  - `docs/brainstorms/2026-04-13-memory-router-coarse-gating-requirements.md`
- The new object-aware Phase 2 contract is accepted:
  - `docs/brainstorms/2026-04-13-memory-planner-object-aware-requirements.md`
- The new bounded adaptive Phase 3 executor contract is accepted:
  - `docs/brainstorms/2026-04-13-memory-runtime-bounded-adaptive-requirements.md`
- Existing unified-store architecture remains the primary persistence surface for now.

## Outstanding Questions

### Deferred to Planning
- [Affects R16][Technical] What exact object-extraction signals should decide primary kind before anchor-assisted refinement?
- [Affects R21][Technical] What exact slot schemas are the minimum viable first-version set for each `MemoryKind`?
- [Affects R24][Technical] What exact `MemoryObjectView` shape best balances normalization and fit with the current store infrastructure?
- [Affects R28][Technical] What exact kind-policy metadata is needed immediately versus safe to defer?
- [Affects R30][Technical] How should cone consume shared slots and kind-specific slots without overfitting to one object taxonomy?
- [Affects R37][Technical] What exact unified top-level fields belong in `.abstract.md`, `.abstract.json`, `.overview.md`, and `content.md`, and which of those fields are required or typically populated by each first-version `MemoryKind`?
- [Affects R40][Technical] What is the minimum anchor schema that remains expressive without making `.abstract.json` too heavy?
- [Affects R43][Technical] Which edge families are cheap and reliable enough for first-version cone expansion?
- [Affects R46][Technical] What is the minimum store API needed to fetch a bounded typed neighborhood around an anchor?
- [Affects R53][Technical] What exact object-aware dedup and merge key should replace category-only mergeability in `memory` mode?
- [Affects R56][Technical] How should document parent/child path structure map cleanly onto `MemoryObjectView` without losing hierarchy?
- [Affects R59][Technical] What explicit threshold should allow parent section objects beyond the default leaf `document_chunk` path?
- [Affects R60][Technical] Under the unified `.abstract.json` schema, which lineage fields are mandatory on day one for `document_chunk` objects, and which are optional enrichments?
- [Affects R63][Technical] What exact source-aware replace key should document mode use when rebuilding chunks or subtrees?
- [Affects R65][Technical] What exact document-lineage edge set should the store expose first for document-mode cone expansion?
- [Affects R71][Technical] What exact conversation extraction boundary should convert buffered dialogue into one or more durable `MemoryObject` records?
- [Affects R74][Technical] What exact stage-transition policy should govern `immediate -> merged -> optional final`, including deletion or inactivation of superseded retrieval objects?
- [Affects R76][Technical] What first-version merge/update rules are safe enough for `profile`, `preference`, `constraint`, `relation`, and `summary` objects?

## Next Steps

-> /ce:plan for structured implementation planning
