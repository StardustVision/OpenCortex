# Cone Retrieval

## Why This Exists

Pure vector similarity can miss records that are closely related by shared
entities but are not semantically adjacent in embedding space. Cone retrieval
adds a lightweight entity-neighborhood expansion layer so that related memories
move together when they share concrete people, systems, or artifacts.

## Core Components

- Entity extraction during write/update: entities are derived by `_derive_layers`
  in `MemoryOrchestrator` using the layer-derivation prompt when an LLM is
  available, normalized to lowercase, and stored as `record["entities"]`
  (capped at 20). If no LLM is configured, new writes typically do not derive
  entities, but cone retrieval can still operate on entities already stored in
  the collection.
- `EntityIndex`: per-collection in-memory inverted index that maps
  `entity -> {memory_id}` and `memory_id -> {entity}`. It is built at startup
  by scrolling the collection, then kept in sync on add/update/remove.
  Update-time entity sync is best-effort and can fail or skip persistence in
  edge cases. Only fully built collections are considered "ready" for cone
  scoring.
- `ConeScorer`: extracts query entities, expands candidates via the index,
  and computes cone bonuses with hop costs and penalties.
- `HierarchicalRetriever` integration: applies cone expansion and scoring
  after ordinary retrieval and rerank, then adds a weighted cone bonus to the
  final score.

## Expansion Flow

1. Ordinary retrieval produces an initial candidate list (vector + lexical,
   optional rerank).
2. `ConeScorer.extract_query_entities()` gathers entities from current
   candidates and keeps those that appear as substrings in the query text.
3. Expansion stage: for each query entity, pull all memory IDs from
   `EntityIndex`; then, for the top 5 candidates by score, expand across their
   entities as well, only including entities whose degree is at or below
   `ENTITY_DEGREE_CAP`.
4. Expansion is capped at `MAX_EXPANSION` (20). The additional records are
   fetched via `storage.get()` and filtered by tenant/user/project access
   control before being appended to candidates with `_expanded=True`.

## Scoring Model

Cone scoring computes a cost per candidate using its own score-derived distance
plus single-hop comparisons through shared entities with other current
candidates:

- Base distance is `1 - raw_score`, clamped to [0, 1].
- A direct-hit penalty (`DIRECT_HIT_PENALTY`) is added when a candidate has no
  entities and its raw score is below 0.9.
- Each shared-entity hop adds `HOP_COST` (halved for query entities) plus the
  neighbor's distance. High-degree entities are ignored unless they are query
  entities.
- If no paths exist, `EDGE_MISS_COST` is the fallback, but the direct-hit path
  is always appended for non-empty candidates in normal runtime flow.
- The cone bonus is `1 - min(1, cone_cost)`.

`HierarchicalRetriever` then adds `cone_weight * _cone_bonus` to the candidate
score and re-sorts by `_final_score`. Cone bonuses are additive; they do not
replace the base retrieval score.

## Failure and Degradation Behavior

- Disabled when `CortexConfig.cone_retrieval_enabled` is false, when
  `cone_weight <= 0`, or when the retriever is constructed without a
  `ConeScorer`.
- `EntityIndex.build_for_collection()` runs asynchronously at startup; if the
  build is incomplete, the index is not "ready" and cone scoring falls back to
  using the base score as the bonus. Expansion still relies on whatever
  incremental entities are available.
- If entity extraction yields no query entities, expansion can still add
  candidates via the top-5 candidate entity expansion path.
- Expansion fetch and scoring errors are caught; the retriever logs and returns
  the ordinary candidate list.

## Relationship to Normal Search

Cone retrieval is not a separate mode. It is applied inside
`HierarchicalRetriever` after ordinary candidate gathering and rerank, and
before results are converted to `MatchedContext`. Both the embedder and
no-embedder paths pass through cone scoring when enabled, so cone retrieval is
an additive search enhancer, not a replacement retrieval pipeline.

## Constraints and Tradeoffs

- Improves recall for entity-linked material but depends on entity extraction
  quality and coverage.
- Requires an in-memory index per collection and a startup build pass, which
  adds latency and memory overhead.
- Expansion and scoring are bounded (`MAX_EXPANSION`, `ENTITY_DEGREE_CAP`) to
  avoid runaway graph amplification, which can leave some long-tail neighbors
  unexpanded.
- Expansion uses access-control filtering but does not re-apply the caller's
  metadata filter, so it can introduce candidates outside strict filters.
- `cone_weight` is configurable, but other penalty/cost constants are currently
  fixed in `ConeScorer`.

## Current State

Cone retrieval is active when `cone_retrieval_enabled` is true (default) and
`cone_weight` is non-zero (default 0.1). Entities are derived during writes and
updates, normalized, and stored in the vector payload; the `EntityIndex` is
built in the background and kept in sync for subsequent writes/removals.
Cone scoring is already wired into `HierarchicalRetriever`, so the behavior
described above reflects current runtime behavior rather than a future plan.
