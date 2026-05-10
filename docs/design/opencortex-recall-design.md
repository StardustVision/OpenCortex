# OpenCortex Recall Design

## Boundary

`opencortex` keeps write-path side effects out of the primary writer.

Primary write path:

- Accept request input and identity.
- Build one primary Qdrant record.
- For `/session/message` immediate records, synchronously write a
  retrieval-ready primary payload with embedding because immediate records are
  cleaned up after merge.
- For memory/resource/merged/final records, write raw primary first and let the
  worker complete LLM derivation and embedding.
- Publish store events.

Worker side effects:

- Write CFS files through `CortexStorageWriter`.
- Write anchor/fact search indexes through `SearchIndexWriter`.
- Write entity Qdrant indexes through `EntityIndexWriter`.
- Write CFS tree retrieval projections through `ReasonTreeIndexWriter`.
- Clean up merged immediate records through `SessionCleanupWriter`.

`PrimaryRecordWriter` must not write secondary indexes, CFS files, reason-tree projections, or any other worker-owned data. It should only persist the primary Qdrant payload it is given.

## Recall Surfaces

The recall design keeps complementary Qdrant retrieval surfaces plus recall-time
expansion strategies.

1. Primary semantic recall

   Primary Qdrant records are the canonical retrieval objects. They carry:

   - `retrieval_ready`
   - `retrieval_surface`
   - `abstract`
   - `overview`
   - `content`
   - `entities`
   - `keywords`
   - `anchor_hits`
   - `memory_kind`
   - dense vector and optional sparse vector

2. Entity index

   `EntityIndexWriter` writes one Qdrant record per source entity:

   - `retrieval_surface="entity_index"`
   - `entity_text`
   - `source_uri`
   - `source_record_id`
   - dense vector and optional sparse vector

   This replaces the old in-memory entity inverted index. Entity expansion and
   Cone can query Qdrant instead of depending on process-local state.

3. Anchor/fact search indexes

   `SearchIndexWriter` writes lightweight Qdrant records below each source URI:

   - `retrieval_surface="anchor_index"` for keywords, abstract-json anchors,
     and explicit anchor handles.
   - `retrieval_surface="fact_index"` for short atomic facts from
     `abstract_json.fact_points`.

   These records are recall entry points. They must include `source_uri` and
   `source_record_id` so recall can jump back to the primary object and then to
   CFS when hydration is needed.

4. Hierarchical FS-tree recall

   CFS already expresses the context tree through URI structure and L0/L1/L2 files. We do not duplicate PageIndex's tree builder. Instead, worker-side indexing projects the existing CFS tree into Qdrant records with enough metadata to navigate:

   - `uri`
   - `parent_uri`
   - `level`
   - `context_type`
   - `category`
   - `abstract`
   - `overview`
   - `is_leaf`
   - `retrieval_surface="reason_tree_index"`
   - `tree_uri`
   - `path`
   - `path_segments`
   - `reason_role`
   - `context_window`
   - `parent_source_uri`
   - `source_uris`
   - `merged_uris`
   - `cone_neighbors`

5. Cone diffusion recall

   Cone recall is a recall-time expansion strategy, not another storage module.
   It expands from initial semantic/search/tree hits through URI neighbors,
   parent/child links, source links, merged links, shared entities, anchors,
   session scope, and time/entity filters. The write path prepares these fields
   but does not run Cone itself.

6. LLM reasoned hydration

   PageIndex's useful idea is not vectorless indexing itself, but LLM-guided navigation over a compact structure. In `thinking` mode, recall can provide a compact tree/candidate set to an LLM, let it choose URIs or ranges, then hydrate full L2 content from CFS.

## Modes

`quick` mode:

- Query Qdrant primary records and reason-tree projections.
- Add anchor/fact index hits.
- Apply cone expansion from `source_uri`, `parent_uri`, `cone_neighbors`,
  `entities`, `anchor_hits`, session scope, and tree path fields.
- Fuse semantic, tree, anchor, fact, and cone scores.

`thinking` mode:

- Run `quick` candidate discovery.
- Build a compact tree from candidate URIs.
- Ask LLM to select the useful branches/leaves.
- Read selected L2 content from CFS.

## Write Path Implication

Reason-tree indexing belongs to the event worker. It is a secondary retrieval projection over records that were already accepted by the primary path.

The writer should not generate summaries and should not write CFS content. It
only copies existing L0/L1 metadata from the primary event payload into Qdrant
index records. LLM generation remains in the semantic-derive worker for raw
records, while immediate session records are synchronously made ready in the
session write path to match the old immediate recall behavior.

For future recall, the write path must prepare:

- Primary records for semantic candidate discovery.
- EntityIndex records for entity seed discovery and entity expansion.
- SearchIndex records for anchor/fact seed discovery.
- ReasonTreeIndex records for tree navigation and hydration windows.
- Cone-ready relationship fields: `source_uri`, `source_record_id`,
  `parent_uri`, `tree_uri`, `path_segments`, `source_uris`, `merged_uris`,
  `anchor_hits`, `entities`, `keywords`, and `cone_neighbors`.
