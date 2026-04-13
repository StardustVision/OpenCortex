# Cone Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add entity-based cone retrieval that propagates path costs via entity co-occurrence edges, improving recall for relationship and attribution queries.

**Architecture:** LLM extracts entities at write time → stored in Qdrant payload → per-collection in-memory EntityIndex built at startup → ConeScorer expands candidates + computes min-path costs → cone_bonus added to existing score fusion. Performance-critical: entity index O(1) lookup, cone scoring O(candidates × avg_entity_degree), startup build non-blocking.

**Tech Stack:** Python 3.10+, asyncio, unittest, Qdrant (embedded)

**Spec:** `docs/superpowers/specs/2026-04-05-cone-retrieval-design.md` (rev.4)

---

## File Structure

```
src/opencortex/retrieve/               # EXISTING directory
├── entity_index.py                     # CREATE: per-collection inverted index
├── cone_scorer.py                      # CREATE: path-cost propagation + expansion

src/opencortex/
├── orchestrator.py                     # MODIFY: entity sync on add/remove, init index
├── retrieve/hierarchical_retriever.py  # MODIFY: apply cone scoring before convert
├── prompts.py                          # MODIFY: add entities to derive prompt
├── config.py                           # MODIFY: add cone config knobs

tests/
├── test_entity_index.py                # CREATE
├── test_cone_scorer.py                 # CREATE
├── test_cone_e2e.py                    # CREATE: integration test
```

---

### Task 1: EntityIndex — Per-Collection In-Memory Inverted Index

**Files:**
- Create: `src/opencortex/retrieve/entity_index.py`
- Test: `tests/test_entity_index.py`

- [ ] **Step 1: Write tests**

Create `tests/test_entity_index.py`:

```python
import unittest
from opencortex.retrieve.entity_index import EntityIndex


class TestEntityIndex(unittest.TestCase):

    def setUp(self):
        self.idx = EntityIndex()

    def test_add_and_lookup(self):
        self.idx.add("col", "m1", ["melanie", "caroline"])
        self.assertEqual(self.idx.get_memories_for_entity("col", "melanie"), {"m1"})
        self.assertEqual(self.idx.get_entities_for_memory("col", "m1"), {"melanie", "caroline"})

    def test_add_normalizes_to_lowercase(self):
        self.idx.add("col", "m1", ["OpenCortex", "REDIS"])
        self.assertEqual(self.idx.get_memories_for_entity("col", "opencortex"), {"m1"})
        self.assertEqual(self.idx.get_memories_for_entity("col", "redis"), {"m1"})

    def test_remove(self):
        self.idx.add("col", "m1", ["melanie"])
        self.idx.add("col", "m2", ["melanie"])
        self.idx.remove("col", "m1")
        self.assertEqual(self.idx.get_memories_for_entity("col", "melanie"), {"m2"})
        self.assertEqual(self.idx.get_entities_for_memory("col", "m1"), set())

    def test_remove_batch(self):
        self.idx.add("col", "m1", ["a"])
        self.idx.add("col", "m2", ["a"])
        self.idx.add("col", "m3", ["a"])
        self.idx.remove_batch("col", ["m1", "m2"])
        self.assertEqual(self.idx.get_memories_for_entity("col", "a"), {"m3"})

    def test_update(self):
        self.idx.add("col", "m1", ["old_entity"])
        self.idx.update("col", "m1", ["new_entity"])
        self.assertEqual(self.idx.get_entities_for_memory("col", "m1"), {"new_entity"})
        self.assertEqual(self.idx.get_memories_for_entity("col", "old_entity"), set())

    def test_per_collection_isolation(self):
        self.idx.add("col_a", "m1", ["entity1"])
        self.idx.add("col_b", "m1", ["entity2"])
        self.assertEqual(self.idx.get_entities_for_memory("col_a", "m1"), {"entity1"})
        self.assertEqual(self.idx.get_entities_for_memory("col_b", "m1"), {"entity2"})

    def test_empty_collection(self):
        self.assertEqual(self.idx.get_memories_for_entity("nonexist", "x"), set())
        self.assertEqual(self.idx.get_entities_for_memory("nonexist", "y"), set())

    def test_entity_degree(self):
        for i in range(100):
            self.idx.add("col", f"m{i}", ["popular"])
        self.assertEqual(len(self.idx.get_memories_for_entity("col", "popular")), 100)

    def test_is_ready(self):
        self.assertFalse(self.idx.is_ready("col"))
        self.idx.add("col", "m1", ["a"])
        self.assertTrue(self.idx.is_ready("col"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests — expect FAIL**

Run: `uv run python3 -m unittest tests.test_entity_index -v`

- [ ] **Step 3: Implement EntityIndex**

Create `src/opencortex/retrieve/entity_index.py`:

```python
"""
EntityIndex — per-collection in-memory inverted index for entity co-occurrence.

Used by ConeScorer to find memories sharing entities.
Built at startup via async scroll, updated on add/remove/update.
All entity names normalized to lowercase.
"""

import logging
from collections import defaultdict
from typing import Dict, List, Set

logger = logging.getLogger(__name__)


class EntityIndex:

    def __init__(self):
        self._forward: Dict[str, Dict[str, Set[str]]] = {}   # col → entity → {mem_id}
        self._reverse: Dict[str, Dict[str, Set[str]]] = {}   # col → mem_id → {entity}
        self._ready: Set[str] = set()                          # collections that have been built

    def _ensure_collection(self, collection: str) -> None:
        if collection not in self._forward:
            self._forward[collection] = defaultdict(set)
            self._reverse[collection] = defaultdict(set)

    def is_ready(self, collection: str) -> bool:
        return collection in self._ready

    # --- Lifecycle ---

    def add(self, collection: str, memory_id: str, entities: List[str]) -> None:
        self._ensure_collection(collection)
        for raw in entities:
            entity = raw.strip().lower()
            if not entity:
                continue
            self._forward[collection][entity].add(memory_id)
            self._reverse[collection][memory_id].add(entity)
        if collection not in self._ready:
            self._ready.add(collection)

    def remove(self, collection: str, memory_id: str) -> None:
        if collection not in self._reverse:
            return
        entities = self._reverse[collection].pop(memory_id, set())
        for entity in entities:
            s = self._forward[collection].get(entity)
            if s:
                s.discard(memory_id)
                if not s:
                    del self._forward[collection][entity]

    def remove_batch(self, collection: str, memory_ids: List[str]) -> None:
        for mid in memory_ids:
            self.remove(collection, mid)

    def update(self, collection: str, memory_id: str, entities: List[str]) -> None:
        self.remove(collection, memory_id)
        self.add(collection, memory_id, entities)

    # --- Query ---

    def get_memories_for_entity(self, collection: str, entity: str) -> Set[str]:
        return set(self._forward.get(collection, {}).get(entity, set()))

    def get_entities_for_memory(self, collection: str, memory_id: str) -> Set[str]:
        return set(self._reverse.get(collection, {}).get(memory_id, set()))

    # --- Bulk build ---

    async def build_for_collection(self, storage, collection: str) -> int:
        """Scroll all records in collection, extract entities field, build index.
        Returns count of records processed.
        """
        count = 0
        cursor = None
        while True:
            try:
                records, cursor = await storage.scroll(
                    collection, limit=200, cursor=cursor,
                )
            except Exception as exc:
                logger.warning("[EntityIndex] Scroll failed for %s: %s", collection, exc)
                break
            if not records:
                break
            for r in records:
                entities = r.get("entities", [])
                if entities and isinstance(entities, list):
                    rid = str(r.get("id", ""))
                    if rid:
                        self.add(collection, rid, entities)
                        count += 1
            if not cursor:
                break
        self._ready.add(collection)
        logger.info("[EntityIndex] Built for %s: %d records with entities", collection, count)
        return count
```

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m unittest tests.test_entity_index -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/retrieve/entity_index.py tests/test_entity_index.py
git commit -m "feat(cone): add EntityIndex — per-collection in-memory inverted index"
```

---

### Task 2: ConeScorer — Path-Cost Propagation + Candidate Expansion

**Files:**
- Create: `src/opencortex/retrieve/cone_scorer.py`
- Test: `tests/test_cone_scorer.py`

- [ ] **Step 1: Write tests**

Create `tests/test_cone_scorer.py`:

```python
import unittest
from unittest.mock import AsyncMock
from opencortex.retrieve.entity_index import EntityIndex
from opencortex.retrieve.cone_scorer import ConeScorer


class TestConeScorer(unittest.TestCase):

    def setUp(self):
        self.idx = EntityIndex()
        self.idx.add("col", "m1", ["melanie"])
        self.idx.add("col", "m2", ["melanie", "caroline"])
        self.idx.add("col", "m3", ["caroline"])
        self.idx.add("col", "m4", ["redis"])
        self.scorer = ConeScorer(self.idx)

    def test_direct_hit_no_entity(self):
        """Memory with no entities gets direct cost only."""
        candidates = [{"id": "m99", "_score": 0.8}]
        result = self.scorer.compute_cone_scores(candidates, set(), "col")
        self.assertAlmostEqual(result[0]["_cone_bonus"], 0.8, places=1)

    def test_entity_propagation(self):
        """m1 (high score) shares 'melanie' with m2 → m2 gets boosted."""
        candidates = [
            {"id": "m1", "_score": 0.9},   # Strong hit
            {"id": "m2", "_score": 0.5},   # Weak hit, shares melanie
        ]
        result = self.scorer.compute_cone_scores(candidates, set(), "col")
        # m2's cone should be better than its direct score (boosted via m1)
        m2 = next(r for r in result if r["id"] == "m2")
        self.assertGreater(m2["_cone_bonus"], 0.5)

    def test_query_entity_half_hop(self):
        """Query mentions 'melanie' → hop cost halved."""
        candidates = [
            {"id": "m1", "_score": 0.9},
            {"id": "m2", "_score": 0.5},
        ]
        result_no_qe = self.scorer.compute_cone_scores(candidates, set(), "col")
        result_with_qe = self.scorer.compute_cone_scores(candidates, {"melanie"}, "col")
        m2_no = next(r for r in result_no_qe if r["id"] == "m2")
        m2_qe = next(r for r in result_with_qe if r["id"] == "m2")
        self.assertGreaterEqual(m2_qe["_cone_bonus"], m2_no["_cone_bonus"])

    def test_high_degree_suppression(self):
        """Entity with > DEGREE_CAP memories is suppressed."""
        for i in range(60):
            self.idx.add("col", f"pop{i}", ["popular"])
        self.idx.add("col", "target", ["popular"])
        candidates = [
            {"id": "pop0", "_score": 0.9},
            {"id": "target", "_score": 0.3},
        ]
        result = self.scorer.compute_cone_scores(candidates, set(), "col")
        target = next(r for r in result if r["id"] == "target")
        # popular entity suppressed → target not boosted
        self.assertLess(target["_cone_bonus"], 0.5)

    def test_broad_match_penalty(self):
        """No-entity candidate with low score gets penalty."""
        candidates = [{"id": "no_entity", "_score": 0.6}]
        result = self.scorer.compute_cone_scores(candidates, set(), "col")
        # broad match → penalized → cone_bonus < 0.6
        self.assertLess(result[0]["_cone_bonus"], 0.6)

    def test_empty_candidates(self):
        result = self.scorer.compute_cone_scores([], set(), "col")
        self.assertEqual(result, [])

    def test_no_entity_index(self):
        """When index not ready, cone_bonus = raw score."""
        candidates = [{"id": "m1", "_score": 0.8}]
        result = self.scorer.compute_cone_scores(candidates, set(), "unknown_col")
        self.assertAlmostEqual(result[0]["_cone_bonus"], 0.8, places=1)


class TestQueryEntityExtraction(unittest.TestCase):

    def setUp(self):
        self.idx = EntityIndex()
        self.idx.add("col", "m1", ["melanie", "caroline"])
        self.idx.add("col", "m2", ["redis"])
        self.scorer = ConeScorer(self.idx)

    def test_extracts_matching_entity(self):
        candidates = [{"id": "m1", "_score": 0.8}]
        entities = self.scorer.extract_query_entities("melanie有孩子吗", candidates, "col")
        self.assertIn("melanie", entities)

    def test_no_match(self):
        candidates = [{"id": "m1", "_score": 0.8}]
        entities = self.scorer.extract_query_entities("天气怎么样", candidates, "col")
        self.assertEqual(len(entities), 0)


class TestCandidateExpansion(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.idx = EntityIndex()
        self.idx.add("col", "m1", ["melanie"])
        self.idx.add("col", "m2", ["melanie"])  # Not in initial candidates
        self.scorer = ConeScorer(self.idx)

    async def test_expansion_adds_related(self):
        candidates = [{"id": "m1", "_score": 0.9}]
        storage = AsyncMock()
        storage.get = AsyncMock(return_value=[
            {"id": "m2", "abstract": "test", "entities": ["melanie"]},
        ])
        expanded = await self.scorer.expand_candidates(
            candidates, {"melanie"}, "col", storage,
        )
        ids = {c["id"] for c in expanded}
        self.assertIn("m2", ids)

    async def test_expansion_limited(self):
        """Expansion capped at 20."""
        for i in range(30):
            self.idx.add("col", f"x{i}", ["common"])
        candidates = [{"id": "x0", "_score": 0.9}]
        storage = AsyncMock()
        storage.get = AsyncMock(return_value=[
            {"id": f"x{i}", "abstract": "test"} for i in range(1, 30)
        ])
        expanded = await self.scorer.expand_candidates(
            candidates, {"common"}, "col", storage,
        )
        self.assertLessEqual(len(expanded), 21)  # original 1 + max 20


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Implement ConeScorer**

Create `src/opencortex/retrieve/cone_scorer.py`:

```python
"""
ConeScorer — entity-based path-cost propagation for memory recall.

Two-stage process:
  1. Expand: pull related memories from entity index into candidate set
  2. Score: compute min-path costs via entity co-occurrence edges

Stateless w.r.t. collection — collection passed per-call.
Performance: O(candidates × avg_entity_degree) per query.
"""

import logging
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)

DIRECT_HIT_PENALTY = 0.3
HOP_COST = 0.05
EDGE_MISS_COST = 0.9
ENTITY_DEGREE_CAP = 50
MAX_EXPANSION = 20


class ConeScorer:

    def __init__(self, entity_index):
        self._index = entity_index

    def extract_query_entities(
        self, query: str, candidates: List[Dict], collection: str,
    ) -> Set[str]:
        """Extract entities from query — only checks entities in candidate set."""
        query_lower = query.lower()
        candidate_entities: Set[str] = set()
        for c in candidates:
            for e in self._index.get_entities_for_memory(collection, str(c.get("id", ""))):
                candidate_entities.add(e)

        return {e for e in candidate_entities if e in query_lower}

    async def expand_candidates(
        self, candidates: List[Dict], query_entities: Set[str],
        collection: str, storage,
    ) -> List[Dict]:
        """Stage 1: Pull related memories from entity index into candidate set."""
        existing_ids = {str(c.get("id", "")) for c in candidates}
        expansion_ids: Set[str] = set()

        # From query entities
        for entity in query_entities:
            for mem_id in self._index.get_memories_for_entity(collection, entity):
                if mem_id not in existing_ids:
                    expansion_ids.add(mem_id)

        # From top-5 candidates' entities (only low-degree)
        sorted_cands = sorted(candidates, key=lambda x: x.get("_score", 0), reverse=True)
        for c in sorted_cands[:5]:
            for entity in self._index.get_entities_for_memory(collection, str(c.get("id", ""))):
                degree = len(self._index.get_memories_for_entity(collection, entity))
                if degree <= ENTITY_DEGREE_CAP:
                    for mem_id in self._index.get_memories_for_entity(collection, entity):
                        if mem_id not in existing_ids:
                            expansion_ids.add(mem_id)

        # Cap expansion
        expansion_list = list(expansion_ids)[:MAX_EXPANSION]

        if expansion_list:
            try:
                expanded_records = await storage.get(collection, expansion_list)
                for r in expanded_records:
                    r["_score"] = 0.0
                    r["_expanded"] = True
                    candidates.append(r)
            except Exception as exc:
                logger.debug("[ConeScorer] Expansion fetch failed: %s", exc)

        return candidates

    def compute_cone_scores(
        self, candidates: List[Dict], query_entities: Set[str],
        collection: str,
    ) -> List[Dict]:
        """Stage 2: Compute min-path cost for each candidate."""
        if not candidates:
            return candidates

        if not self._index.is_ready(collection):
            # Index not built yet → just pass through raw scores
            for c in candidates:
                c["_cone_bonus"] = c.get("_score", 0.0)
            return candidates

        # Build lookup: id → candidate
        by_id: Dict[str, Dict] = {}
        for c in candidates:
            cid = str(c.get("id", ""))
            if cid:
                by_id[cid] = c

        query_entities_lower = {e.lower() for e in query_entities}

        for candidate in candidates:
            cid = str(candidate.get("id", ""))
            raw_score = candidate.get("_score", 0.0)
            dist = 1.0 - min(1.0, max(0.0, raw_score))
            paths: List[float] = []

            # Path 1: Direct hit
            direct_cost = dist
            c_entities = self._index.get_entities_for_memory(collection, cid)
            if not c_entities and raw_score < 0.9:
                direct_cost += DIRECT_HIT_PENALTY  # Broad match penalty
            paths.append(direct_cost)

            # Path 2+: Entity propagation
            for entity in c_entities:
                entity_mems = self._index.get_memories_for_entity(collection, entity)
                if len(entity_mems) > ENTITY_DEGREE_CAP:
                    if entity not in query_entities_lower:
                        continue

                for other_id in entity_mems:
                    if other_id == cid:
                        continue
                    other = by_id.get(other_id)
                    if other:
                        hop = HOP_COST
                        if entity in query_entities_lower:
                            hop *= 0.5
                        other_dist = 1.0 - min(1.0, max(0.0, other.get("_score", 0.0)))
                        paths.append(other_dist + hop)

            cone_cost = min(paths) if paths else EDGE_MISS_COST
            # Convert cost back to bonus (higher = better)
            candidate["_cone_bonus"] = 1.0 - min(1.0, cone_cost)

        return candidates
```

- [ ] **Step 3: Run tests**

Run: `uv run python3 -m unittest tests.test_cone_scorer -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
git add src/opencortex/retrieve/cone_scorer.py tests/test_cone_scorer.py
git commit -m "feat(cone): add ConeScorer — path-cost propagation + candidate expansion"
```

---

### Task 3: Entity Extraction in LLM Derive + Config

**Files:**
- Modify: `src/opencortex/prompts.py:169-198`
- Modify: `src/opencortex/orchestrator.py:942-1005` (_derive_layers)
- Modify: `src/opencortex/config.py`
- Test: `tests/test_cone_e2e.py` (partial — entity extraction part)

- [ ] **Step 1: Add entity extraction to derive prompt**

Edit `src/opencortex/prompts.py`. In `build_layer_derivation_prompt()`, modify the JSON output format to include entities. Replace the JSON template (around line 188-192):

```python
Return a JSON object with exactly these fields:
{{
  "abstract": "1-2 sentence standalone summary, max 200 chars",
  "overview": "3-8 sentence overview covering key facts, decisions, and actionable details",
  "keywords": ["term1", "term2", "..."],
  "entities": ["entity1", "entity2", "..."]
}}

Rules:
- abstract: A concise, self-contained summary. If user description is provided above, use it as-is.
- overview: Covers the main points, decisions, and context. Do NOT repeat the abstract verbatim.
- keywords: 3-15 key terms (names, tools, technologies, concepts) that aid search. No generic words.
- entities: Named entities only — people, systems, tools, organizations, places. NOT generic concepts. Max 10.
- Return ONLY the JSON object, no other text.
```

- [ ] **Step 2: Parse entities in _derive_layers()**

Edit `src/opencortex/orchestrator.py`, in `_derive_layers()`. After parsing keywords (around line 976), add:

```python
                    entities_list = result.get("entities", [])
                    if isinstance(entities_list, list):
                        entities = [str(e).strip().lower() for e in entities_list if e][:20]
                    else:
                        entities = []
                    return {
                        "abstract": user_abstract or result.get("abstract", ""),
                        "overview": user_overview or result.get("overview", ""),
                        "keywords": keywords,
                        "entities": entities,
                    }
```

Do the same for the non-chunked path (around line 998).

- [ ] **Step 3: Store entities in Qdrant payload**

In `orchestrator.add()`, where the record payload is built before `storage.upsert()`, add the entities field:

```python
# After derive_result = await self._derive_layers(...)
entities = derive_result.get("entities", [])

# In the record dict before upsert:
record["entities"] = entities
```

- [ ] **Step 4: Sync EntityIndex on add**

After the Qdrant upsert in `add()`:

```python
if self._entity_index and entities:
    self._entity_index.add(self._get_collection(), str(record_id), entities)
```

- [ ] **Step 5: Add config knobs**

Edit `src/opencortex/config.py`, in `CortexConfig`, add after `max_total_search_calls`:

```python
    # Cone Retrieval
    cone_retrieval_enabled: bool = True
    cone_weight: float = 0.1
    cone_direct_hit_penalty: float = 0.3
    cone_hop_cost: float = 0.05
    cone_edge_miss_cost: float = 0.9
    cone_entity_degree_cap: int = 50
```

- [ ] **Step 6: Run existing tests (no regressions)**

Run: `uv run python3 -m unittest tests.test_alpha_config tests.test_context_manager -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add src/opencortex/prompts.py src/opencortex/orchestrator.py src/opencortex/config.py
git commit -m "feat(cone): add entity extraction to LLM derive + cone config knobs"
```

---

### Task 4: Orchestrator Init + Lifecycle Sync + Retriever Integration

**Files:**
- Modify: `src/opencortex/orchestrator.py` (init, remove, search paths)
- Modify: `src/opencortex/retrieve/hierarchical_retriever.py`
- Test: `tests/test_cone_e2e.py`

- [ ] **Step 1: Init EntityIndex + ConeScorer in orchestrator**

In `orchestrator.__init__()`, add:
```python
self._entity_index = None
self._cone_scorer = None
```

In `orchestrator.init()`, after `_init_skill_engine()`:
```python
# Cone Retrieval: entity index + scorer
if self._config.cone_retrieval_enabled:
    from opencortex.retrieve.entity_index import EntityIndex
    from opencortex.retrieve.cone_scorer import ConeScorer
    self._entity_index = EntityIndex()
    self._cone_scorer = ConeScorer(self._entity_index)
    # Non-blocking background build
    asyncio.create_task(self._entity_index.build_for_collection(
        self._storage, self._get_collection()
    ))
```

- [ ] **Step 2: Sync on remove**

In `orchestrator.remove()`, before the actual delete call:
```python
# Sync entity index (pre-delete: get affected IDs)
if self._entity_index:
    collection = self._get_collection()
    try:
        affected = await self._storage.filter(
            collection,
            {"op": "must", "field": "uri", "conds": [uri]},
            limit=10000,
        )
        affected_ids = [str(r["id"]) for r in affected]
    except Exception:
        affected_ids = []
```

After the delete completes:
```python
if self._entity_index and affected_ids:
    self._entity_index.remove_batch(collection, affected_ids)
```

- [ ] **Step 3: Wire ConeScorer into HierarchicalRetriever**

Pass cone_scorer to HierarchicalRetriever constructor. In `orchestrator.init()` where retriever is created:
```python
self._retriever = HierarchicalRetriever(
    storage=self._storage,
    embedder=self._embedder,
    rerank_config=rerank_cfg,
    llm_completion=self._llm_completion,
    cone_scorer=self._cone_scorer,  # NEW
    ...
)
```

In `hierarchical_retriever.py.__init__()`, add parameter:
```python
def __init__(self, ..., cone_scorer=None):
    ...
    self._cone_scorer = cone_scorer
```

Before each `_convert_to_matched_contexts` call (there are 2-3 locations: flat search path, frontier path, and potentially fallback path), add:

```python
# Apply cone scoring
if self._cone_scorer and results:
    collection = self._type_to_collection(query.context_type)
    query_text = query.query
    query_entities = self._cone_scorer.extract_query_entities(
        query_text, results, collection,
    )
    results = await self._cone_scorer.expand_candidates(
        results, query_entities, collection, self._storage,
    )
    results = self._cone_scorer.compute_cone_scores(
        results, query_entities, collection,
    )
    # Apply cone bonus to _final_score
    cone_weight = getattr(self, '_cone_weight', 0.1)
    for r in results:
        bonus = r.get("_cone_bonus", 0.0)
        r["_final_score"] = r.get("_final_score", r.get("_score", 0.0)) + cone_weight * bonus
    results.sort(key=lambda r: r.get("_final_score", 0), reverse=True)
```

Pass `cone_weight` from config through to retriever.

- [ ] **Step 4: Write E2E test**

Create `tests/test_cone_e2e.py`:

```python
import unittest
from opencortex.retrieve.entity_index import EntityIndex
from opencortex.retrieve.cone_scorer import ConeScorer


class TestConeE2E(unittest.TestCase):
    """End-to-end cone retrieval: entity path changes ranking."""

    def test_entity_path_boosts_related_memory(self):
        """Memory B (weak vector match) sharing entity with A (strong match) gets boosted."""
        idx = EntityIndex()
        idx.add("col", "m_strong", ["melanie"])
        idx.add("col", "m_weak", ["melanie"])
        idx.add("col", "m_noise", ["unrelated"])

        scorer = ConeScorer(idx)
        candidates = [
            {"id": "m_strong", "_score": 0.9},
            {"id": "m_weak", "_score": 0.4},
            {"id": "m_noise", "_score": 0.5},
        ]

        result = scorer.compute_cone_scores(candidates, {"melanie"}, "col")
        scores = {r["id"]: r["_cone_bonus"] for r in result}

        # m_weak should be boosted above m_noise via entity path
        self.assertGreater(scores["m_weak"], scores["m_noise"])

    def test_no_entity_graceful_degradation(self):
        """Without entities, cone_bonus equals raw score."""
        idx = EntityIndex()
        scorer = ConeScorer(idx)
        candidates = [
            {"id": "m1", "_score": 0.8},
            {"id": "m2", "_score": 0.6},
        ]
        result = scorer.compute_cone_scores(candidates, set(), "col")
        # Order preserved (no entity = no change)
        self.assertGreaterEqual(result[0]["_cone_bonus"], result[1]["_cone_bonus"])

    def test_ranking_with_cone_weight(self):
        """Simulate full fusion: vector + cone_weight * cone_bonus."""
        idx = EntityIndex()
        idx.add("col", "m1", ["redis"])
        idx.add("col", "m2", ["redis"])

        scorer = ConeScorer(idx)
        candidates = [
            {"id": "m1", "_score": 0.9, "_final_score": 0.85},
            {"id": "m2", "_score": 0.4, "_final_score": 0.35},
        ]

        result = scorer.compute_cone_scores(candidates, {"redis"}, "col")
        cone_weight = 0.1
        for r in result:
            r["_final_with_cone"] = r.get("_final_score", 0) + cone_weight * r["_cone_bonus"]

        # m2 should be closer to m1 after cone boost
        m1 = next(r for r in result if r["id"] == "m1")
        m2 = next(r for r in result if r["id"] == "m2")
        gap_before = m1["_final_score"] - m2["_final_score"]
        gap_after = m1["_final_with_cone"] - m2["_final_with_cone"]
        self.assertLess(gap_after, gap_before)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 5: Run all tests**

Run: `uv run python3 -m unittest tests.test_entity_index tests.test_cone_scorer tests.test_cone_e2e -v`
Expected: All PASS

Run regression: `uv run python3 -m unittest tests.test_alpha_config tests.test_context_manager tests.test_qdrant_adapter -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/orchestrator.py src/opencortex/retrieve/hierarchical_retriever.py tests/test_cone_e2e.py
git commit -m "feat(cone): wire EntityIndex + ConeScorer into orchestrator + retriever"
```

---

### Task 5: Final Verification

- [ ] **Step 1: Run complete test suite**

```bash
uv run python3 -m unittest tests.test_entity_index tests.test_cone_scorer tests.test_cone_e2e -v
uv run python3 -m unittest discover -s tests/skill_engine -v
uv run python3 -m unittest tests.test_alpha_types tests.test_alpha_config tests.test_context_manager tests.test_qdrant_adapter -v
```

- [ ] **Step 2: Verify no performance regressions**

```bash
# EntityIndex build should complete in background (check logs)
# ConeScorer should add < 5ms per query (check with timing)
```

- [ ] **Step 3: Spec coverage check**

| Spec Section | Task |
|-------------|------|
| §4.1 Entity Extractor | Task 3 |
| §4.2 EntityIndex | Task 1 |
| §4.3 ConeScorer | Task 2 |
| §4.4 Score Fusion | Task 4 |
| §4.5 Query Entity Extraction | Task 2 |
| §6.1 Write Path | Task 3 |
| §6.2 Delete Path | Task 4 |
| §6.3 Search Path | Task 4 |
| §6.4 Startup | Task 4 |
| §9 Configuration | Task 3 |
| §10 Graceful Degradation | Task 1+2 (is_ready check) |
| §10.1 SearchExplain | Deferred (can add in follow-up) |
