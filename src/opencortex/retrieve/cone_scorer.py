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
        tenant_id: str = "", user_id: str = "", project_id: str = "",
    ) -> List[Dict]:
        """Stage 1: Pull related memories from entity index, filtered by access control.

        SECURITY: Expanded records are filtered by tenant/user/scope to prevent
        cross-tenant leakage. Only records visible to the current user are added.
        """
        existing_ids = {str(c.get("id", "")) for c in candidates}
        expansion_ids: Set[str] = set()

        for entity in query_entities:
            for mem_id in self._index.get_memories_for_entity(collection, entity):
                if mem_id not in existing_ids:
                    expansion_ids.add(mem_id)

        sorted_cands = sorted(candidates, key=lambda x: x.get("_score", 0), reverse=True)
        for c in sorted_cands[:5]:
            for entity in self._index.get_entities_for_memory(collection, str(c.get("id", ""))):
                degree = len(self._index.get_memories_for_entity(collection, entity))
                if degree <= ENTITY_DEGREE_CAP:
                    for mem_id in self._index.get_memories_for_entity(collection, entity):
                        if mem_id not in existing_ids:
                            expansion_ids.add(mem_id)

        expansion_list = list(expansion_ids)[:MAX_EXPANSION]

        if expansion_list:
            try:
                expanded_records = await storage.get(collection, expansion_list)
                for r in expanded_records:
                    # Access control: filter out records not visible to current user
                    r_tenant = r.get("source_tenant_id", "")
                    r_scope = r.get("scope", "")
                    r_user = r.get("source_user_id", "")
                    if tenant_id and r_tenant and r_tenant != tenant_id and r_tenant != "":
                        continue  # Wrong tenant
                    if r_scope == "private" and r_user != user_id:
                        continue  # Private record, wrong user
                    # Project isolation
                    r_project = r.get("project_id", "public")
                    if project_id and project_id != "public" and r_project not in (project_id, "public", ""):
                        continue  # Wrong project
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
            for c in candidates:
                c["_cone_bonus"] = c.get("_score", 0.0)
            return candidates

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

            direct_cost = dist
            c_entities = self._index.get_entities_for_memory(collection, cid)
            if not c_entities and raw_score < 0.9:
                direct_cost += DIRECT_HIT_PENALTY
            paths.append(direct_cost)

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
            candidate["_cone_bonus"] = 1.0 - min(1.0, cone_cost)

        return candidates
