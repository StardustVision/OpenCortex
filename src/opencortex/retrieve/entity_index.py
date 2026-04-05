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
        self._forward: Dict[str, Dict[str, Set[str]]] = {}
        self._reverse: Dict[str, Dict[str, Set[str]]] = {}
        self._ready: Set[str] = set()

    def _ensure_collection(self, collection: str) -> None:
        if collection not in self._forward:
            self._forward[collection] = defaultdict(set)
            self._reverse[collection] = defaultdict(set)

    def is_ready(self, collection: str) -> bool:
        return collection in self._ready

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

    def get_memories_for_entity(self, collection: str, entity: str) -> Set[str]:
        return set(self._forward.get(collection, {}).get(entity, set()))

    def get_entities_for_memory(self, collection: str, memory_id: str) -> Set[str]:
        return set(self._reverse.get(collection, {}).get(memory_id, set()))

    async def build_for_collection(self, storage, collection: str) -> int:
        count = 0
        cursor = None
        while True:
            try:
                records, cursor = await storage.scroll(collection, limit=200, cursor=cursor)
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
