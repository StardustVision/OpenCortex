# Skill Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independent skill engine module that extracts operational skills from stored memories, with OpenSpace-aligned interfaces, human approval workflow, and recall integration.

**Architecture:** Standalone `src/opencortex/skill_engine/` module with zero imports from existing memory code. Adapter interfaces bridge to OpenCortex's Qdrant storage and local embedder. Skills stored in independent `skills` Qdrant collection, surfaced via `ContextType.SKILL` in recall.

**Tech Stack:** Python 3.10+, asyncio, unittest, Qdrant (embedded), local e5-large embedder, FastAPI routes

**Spec:** `docs/superpowers/specs/2026-04-03-skill-engine-design.md` (rev.2)

---

## File Structure

```
src/opencortex/skill_engine/          # NEW module (all files created)
├── __init__.py                        # Package exports
├── types.py                           # SkillRecord, SkillLineage, enums
├── adapters/
│   ├── __init__.py
│   ├── source_adapter.py              # Read from memories Qdrant (read-only)
│   ├── storage_adapter.py             # Write to skills Qdrant collection
│   ├── embedding_adapter.py           # Delegate to local e5-large
│   └── llm_adapter.py                 # Delegate to OpenCortex LLM client
├── store.py                           # SkillStore (CRUD + lifecycle)
├── analyzer.py                        # SkillAnalyzer (memories → suggestions)
├── evolver.py                         # SkillEvolver (CAPTURED/DERIVED/FIX)
├── ranker.py                          # SkillRanker (BM25 + embedding)
├── patch.py                           # Patch application (port from OpenSpace)
├── prompts.py                         # All LLM prompts
├── memory_formatter.py                # Format memory clusters for LLM context
├── skill_manager.py                   # Top-level API
└── http_routes.py                     # REST API routes

src/opencortex/orchestrator.py         # MODIFY: 2 touch points (~20 lines)
src/opencortex/http/server.py          # MODIFY: mount skill routes (~5 lines)
src/opencortex/storage/collection_schemas.py  # MODIFY: add skills schema (~30 lines)

tests/skill_engine/                    # NEW test directory
├── __init__.py
├── test_types.py
├── test_store.py
├── test_analyzer.py
├── test_evolver.py
├── test_ranker.py
├── test_patch.py
├── test_skill_manager.py
├── test_storage_adapter.py
└── test_http_routes.py
```

---

### Task 1: Types + Package Skeleton

**Files:**
- Create: `src/opencortex/skill_engine/__init__.py`
- Create: `src/opencortex/skill_engine/types.py`
- Create: `src/opencortex/skill_engine/adapters/__init__.py`
- Test: `tests/skill_engine/__init__.py`
- Test: `tests/skill_engine/test_types.py`

- [ ] **Step 1: Create package skeleton**

Create `src/opencortex/skill_engine/__init__.py`:

```python
"""
Skill Engine — OpenSpace-aligned skill extraction from memories.

Independent module with zero imports from opencortex.alpha, opencortex.context,
opencortex.storage, opencortex.retrieve, or opencortex.ingest.
"""
```

Create `src/opencortex/skill_engine/adapters/__init__.py`:

```python
"""Adapter interfaces for bridging OpenCortex infrastructure."""
```

Create `tests/skill_engine/__init__.py` (empty file).

- [ ] **Step 2: Write failing test for types**

Create `tests/skill_engine/test_types.py`:

```python
import hashlib
import unittest
from opencortex.skill_engine.types import (
    SkillOrigin, SkillCategory, SkillVisibility, SkillStatus,
    SkillLineage, SkillRecord, EvolutionSuggestion,
    make_skill_uri, make_source_fingerprint,
)


class TestSkillEnums(unittest.TestCase):

    def test_skill_origin_values(self):
        self.assertEqual(SkillOrigin.IMPORTED, "imported")
        self.assertEqual(SkillOrigin.CAPTURED, "captured")
        self.assertEqual(SkillOrigin.DERIVED, "derived")
        self.assertEqual(SkillOrigin.FIXED, "fixed")

    def test_skill_category_values(self):
        self.assertEqual(SkillCategory.WORKFLOW, "workflow")
        self.assertEqual(SkillCategory.TOOL_GUIDE, "tool_guide")
        self.assertEqual(SkillCategory.PATTERN, "pattern")

    def test_skill_visibility_values(self):
        self.assertEqual(SkillVisibility.PRIVATE, "private")
        self.assertEqual(SkillVisibility.SHARED, "shared")

    def test_skill_status_values(self):
        self.assertEqual(SkillStatus.CANDIDATE, "candidate")
        self.assertEqual(SkillStatus.ACTIVE, "active")
        self.assertEqual(SkillStatus.DEPRECATED, "deprecated")


class TestSkillRecord(unittest.TestCase):

    def test_minimal_record(self):
        r = SkillRecord(
            skill_id="sk-001",
            name="deploy-flow",
            description="Standard deployment workflow",
            content="# Deploy\n1. Build\n2. Test\n3. Deploy",
            category=SkillCategory.WORKFLOW,
            tenant_id="team1",
            user_id="hugo",
        )
        self.assertEqual(r.status, SkillStatus.CANDIDATE)
        self.assertEqual(r.visibility, SkillVisibility.PRIVATE)
        self.assertEqual(r.total_selections, 0)
        self.assertEqual(r.uri, "")

    def test_to_dict_excludes_none_lists(self):
        r = SkillRecord(
            skill_id="sk-001", name="test", description="d",
            content="c", category=SkillCategory.WORKFLOW,
            tenant_id="t", user_id="u",
        )
        d = r.to_dict()
        self.assertEqual(d["skill_id"], "sk-001")
        self.assertEqual(d["status"], "candidate")
        self.assertEqual(d["visibility"], "private")
        self.assertIn("lineage", d)

    def test_to_dict_roundtrip_preserves_fields(self):
        r = SkillRecord(
            skill_id="sk-002", name="debug-flow",
            description="Debug workflow", content="# Debug",
            category=SkillCategory.PATTERN,
            tenant_id="t", user_id="u",
            uri="opencortex://t/u/skills/sk-002",
            tags=["debug", "workflow"],
            source_fingerprint="abc123",
        )
        d = r.to_dict()
        self.assertEqual(d["uri"], "opencortex://t/u/skills/sk-002")
        self.assertEqual(d["tags"], ["debug", "workflow"])
        self.assertEqual(d["source_fingerprint"], "abc123")


class TestSkillLineage(unittest.TestCase):

    def test_default_lineage(self):
        l = SkillLineage()
        self.assertEqual(l.generation, 0)
        self.assertEqual(l.parent_skill_ids, [])
        self.assertEqual(l.source_memory_ids, [])

    def test_captured_lineage(self):
        l = SkillLineage(
            origin=SkillOrigin.CAPTURED,
            source_memory_ids=["m1", "m2", "m3"],
            created_by="claude-opus-4",
        )
        self.assertEqual(l.origin, SkillOrigin.CAPTURED)
        self.assertEqual(len(l.source_memory_ids), 3)


class TestEvolutionSuggestion(unittest.TestCase):

    def test_captured_suggestion(self):
        s = EvolutionSuggestion(
            evolution_type=SkillOrigin.CAPTURED,
            target_skill_ids=[],
            category=SkillCategory.WORKFLOW,
            direction="Extract deploy workflow from memory cluster",
            confidence=0.85,
            source_memory_ids=["m1", "m2"],
        )
        self.assertEqual(s.evolution_type, SkillOrigin.CAPTURED)
        self.assertEqual(len(s.target_skill_ids), 0)


class TestHelperFunctions(unittest.TestCase):

    def test_make_skill_uri(self):
        uri = make_skill_uri("team1", "hugo", "sk-001")
        self.assertEqual(uri, "opencortex://team1/hugo/skills/sk-001")

    def test_make_source_fingerprint(self):
        fp = make_source_fingerprint(["m3", "m1", "m2"])
        # Should be deterministic regardless of input order
        fp2 = make_source_fingerprint(["m1", "m2", "m3"])
        self.assertEqual(fp, fp2)
        self.assertEqual(len(fp), 16)

    def test_make_source_fingerprint_empty(self):
        fp = make_source_fingerprint([])
        self.assertEqual(len(fp), 16)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.skill_engine.test_types -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'opencortex.skill_engine.types'`

- [ ] **Step 4: Implement types.py**

Create `src/opencortex/skill_engine/types.py`:

```python
"""
Skill Engine data types — mirrors OpenSpace skill_engine/types.py.

See spec §4 for design rationale.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SkillOrigin(str, Enum):
    IMPORTED = "imported"
    CAPTURED = "captured"
    DERIVED = "derived"
    FIXED = "fixed"


class SkillCategory(str, Enum):
    WORKFLOW = "workflow"
    TOOL_GUIDE = "tool_guide"
    PATTERN = "pattern"


class SkillVisibility(str, Enum):
    PRIVATE = "private"
    SHARED = "shared"


class SkillStatus(str, Enum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    DEPRECATED = "deprecated"


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------

@dataclass
class SkillLineage:
    origin: SkillOrigin = SkillOrigin.CAPTURED
    generation: int = 0
    parent_skill_ids: List[str] = field(default_factory=list)
    source_memory_ids: List[str] = field(default_factory=list)
    change_summary: str = ""
    content_diff: str = ""
    content_snapshot: Dict[str, str] = field(default_factory=dict)
    created_by: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "origin": self.origin.value,
            "generation": self.generation,
            "parent_skill_ids": self.parent_skill_ids,
            "source_memory_ids": self.source_memory_ids,
            "change_summary": self.change_summary,
            "content_diff": self.content_diff,
            "content_snapshot": self.content_snapshot,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# SkillRecord
# ---------------------------------------------------------------------------

@dataclass
class SkillRecord:
    skill_id: str
    name: str
    description: str
    content: str
    category: SkillCategory
    status: SkillStatus = SkillStatus.CANDIDATE
    visibility: SkillVisibility = SkillVisibility.PRIVATE
    lineage: SkillLineage = field(default_factory=SkillLineage)
    tags: List[str] = field(default_factory=list)
    tenant_id: str = ""
    user_id: str = ""
    uri: str = ""
    # Quality metrics
    total_selections: int = 0
    total_applied: int = 0
    total_completions: int = 0
    total_fallbacks: int = 0
    # Summary layers
    abstract: str = ""
    overview: str = ""
    # Timestamps
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    # Extraction idempotency
    source_fingerprint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "content": self.content,
            "category": self.category.value,
            "status": self.status.value,
            "visibility": self.visibility.value,
            "lineage": self.lineage.to_dict(),
            "tags": self.tags,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "uri": self.uri,
            "total_selections": self.total_selections,
            "total_applied": self.total_applied,
            "total_completions": self.total_completions,
            "total_fallbacks": self.total_fallbacks,
            "abstract": self.abstract,
            "overview": self.overview,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "source_fingerprint": self.source_fingerprint,
        }


# ---------------------------------------------------------------------------
# Evolution
# ---------------------------------------------------------------------------

@dataclass
class EvolutionSuggestion:
    evolution_type: SkillOrigin
    target_skill_ids: List[str] = field(default_factory=list)
    category: SkillCategory = SkillCategory.WORKFLOW
    direction: str = ""
    confidence: float = 0.0
    source_memory_ids: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_skill_uri(tenant_id: str, user_id: str, skill_id: str) -> str:
    """Generate a stable skill URI for recall integration."""
    return f"opencortex://{tenant_id}/{user_id}/skills/{skill_id}"


def make_source_fingerprint(memory_ids: List[str]) -> str:
    """Deterministic fingerprint from source memory IDs for extraction idempotency."""
    key = "|".join(sorted(memory_ids))
    return hashlib.sha256(key.encode()).hexdigest()[:16]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run python3 -m unittest tests.skill_engine.test_types -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/skill_engine/ tests/skill_engine/
git commit -m "feat(skill_engine): add types, enums, and package skeleton"
```

---

### Task 2: Adapter Interfaces + StorageAdapter

**Files:**
- Create: `src/opencortex/skill_engine/adapters/source_adapter.py`
- Create: `src/opencortex/skill_engine/adapters/storage_adapter.py`
- Create: `src/opencortex/skill_engine/adapters/embedding_adapter.py`
- Create: `src/opencortex/skill_engine/adapters/llm_adapter.py`
- Modify: `src/opencortex/storage/collection_schemas.py`
- Test: `tests/skill_engine/test_storage_adapter.py`

- [ ] **Step 1: Write adapter protocol interfaces**

Create `src/opencortex/skill_engine/adapters/source_adapter.py`:

```python
"""Source adapter — reads from OpenCortex memory store (read-only)."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass
class MemoryCluster:
    cluster_id: str
    theme: str
    memory_ids: List[str]
    centroid_embedding: List[float]
    avg_score: float


@dataclass
class MemoryRecord:
    memory_id: str
    abstract: str
    overview: str
    content: str
    context_type: str
    category: str
    meta: Dict[str, Any] = field(default_factory=dict)


class SourceAdapter(Protocol):
    """Protocol for reading memories. Implementation bridges to Qdrant."""

    async def scan_memories(
        self, tenant_id: str, user_id: str,
        context_types: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        min_count: int = 3,
    ) -> List[MemoryCluster]: ...

    async def get_cluster_memories(
        self, cluster: MemoryCluster,
    ) -> List[MemoryRecord]: ...
```

Create `src/opencortex/skill_engine/adapters/embedding_adapter.py`:

```python
"""Embedding adapter — delegates to existing OpenCortex embedder."""

from typing import List, Protocol


class EmbeddingAdapter(Protocol):
    """Protocol for embedding generation."""

    def embed(self, text: str) -> List[float]: ...
    def embed_batch(self, texts: List[str]) -> List[List[float]]: ...
```

Create `src/opencortex/skill_engine/adapters/llm_adapter.py`:

```python
"""LLM adapter — delegates to existing OpenCortex LLM client."""

from typing import Dict, List, Protocol


class LLMAdapter(Protocol):
    """Protocol for LLM completion."""

    async def complete(self, messages: List[Dict], **kwargs) -> str: ...
```

- [ ] **Step 2: Write storage adapter with Qdrant implementation**

Create `src/opencortex/skill_engine/adapters/storage_adapter.py`:

```python
"""Storage adapter — writes to independent skills Qdrant collection."""

import logging
from typing import Any, Dict, List, Optional

from opencortex.skill_engine.types import (
    SkillRecord, SkillStatus, SkillVisibility, SkillLineage, SkillOrigin,
    SkillCategory,
)

logger = logging.getLogger(__name__)

SKILLS_COLLECTION = "skills"


class SkillStorageAdapter:
    """Qdrant-backed storage for skills in an independent collection."""

    def __init__(self, storage, embedder, collection_name: str = SKILLS_COLLECTION,
                 embedding_dim: int = 1024):
        self._storage = storage
        self._embedder = embedder
        self._collection = collection_name
        self._dim = embedding_dim

    async def initialize(self) -> None:
        """Ensure skills collection exists."""
        from opencortex.storage.collection_schemas import init_skills_collection
        await init_skills_collection(self._storage, self._collection, self._dim)

    async def save(self, record: SkillRecord) -> None:
        """Upsert a skill record with embedding."""
        embed_text = f"{record.name} {record.description} {record.abstract}"
        embed_result = self._embedder.embed(embed_text)

        payload = record.to_dict()
        payload["id"] = record.skill_id
        payload["vector"] = embed_result.dense_vector
        # Flatten lineage for storage (JSON payload)
        payload["lineage"] = record.lineage.to_dict()

        await self._storage.upsert(self._collection, payload)

    async def load(self, skill_id: str) -> Optional[SkillRecord]:
        """Load a single skill by ID."""
        results = await self._storage.get(self._collection, [skill_id])
        if not results:
            return None
        return self._dict_to_record(results[0])

    async def load_all(self, tenant_id: str, user_id: str,
                       status: Optional[SkillStatus] = None) -> List[SkillRecord]:
        """Load all skills visible to this user."""
        conds = [self._visibility_filter(tenant_id, user_id)]
        if status:
            conds.append({"op": "must", "field": "status", "conds": [status.value]})

        filter_expr = {"op": "and", "conds": conds}
        results = await self._storage.filter(self._collection, filter_expr, limit=500)
        return [self._dict_to_record(r) for r in results]

    async def update_status(self, skill_id: str, status: SkillStatus) -> None:
        """Update skill status."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await self._storage.update(
            self._collection, skill_id,
            {"status": status.value, "updated_at": now},
        )

    async def update_metrics(self, skill_id: str, **counters) -> None:
        """Increment quality counters."""
        existing = await self.load(skill_id)
        if not existing:
            return
        updates = {}
        for k, v in counters.items():
            if hasattr(existing, k):
                updates[k] = getattr(existing, k) + v
        if updates:
            await self._storage.update(self._collection, skill_id, updates)

    async def search(self, query: str, tenant_id: str, user_id: str,
                     top_k: int = 5,
                     status: Optional[SkillStatus] = None) -> List[SkillRecord]:
        """Vector search with visibility + status filter."""
        embed_result = self._embedder.embed(query)

        conds = [self._visibility_filter(tenant_id, user_id)]
        if status:
            conds.append({"op": "must", "field": "status", "conds": [status.value]})
        else:
            # Default: only ACTIVE skills in search
            conds.append({"op": "must", "field": "status", "conds": [SkillStatus.ACTIVE.value]})

        filter_expr = {"op": "and", "conds": conds}
        results = await self._storage.search(
            self._collection, embed_result.dense_vector, filter_expr, limit=top_k,
        )
        return [self._dict_to_record(r) for r in results]

    async def find_by_fingerprint(self, fingerprint: str) -> Optional[SkillRecord]:
        """Find skill by source fingerprint (for extraction idempotency)."""
        filter_expr = {"op": "must", "field": "source_fingerprint", "conds": [fingerprint]}
        results = await self._storage.filter(self._collection, filter_expr, limit=1)
        if not results:
            return None
        return self._dict_to_record(results[0])

    async def delete(self, skill_id: str) -> None:
        """Delete a skill by ID."""
        await self._storage.delete(self._collection, [skill_id])

    def _visibility_filter(self, tenant_id: str, user_id: str) -> Dict[str, Any]:
        """Build scope filter: SHARED visible to tenant, PRIVATE to owner only."""
        return {"op": "or", "conds": [
            {"op": "and", "conds": [
                {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
                {"op": "must", "field": "visibility", "conds": [SkillVisibility.SHARED.value]},
            ]},
            {"op": "and", "conds": [
                {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
                {"op": "must", "field": "visibility", "conds": [SkillVisibility.PRIVATE.value]},
                {"op": "must", "field": "user_id", "conds": [user_id]},
            ]},
        ]}

    def _dict_to_record(self, d: Dict[str, Any]) -> SkillRecord:
        """Convert Qdrant payload dict to SkillRecord."""
        lineage_data = d.get("lineage", {})
        lineage = SkillLineage(
            origin=SkillOrigin(lineage_data.get("origin", "captured")),
            generation=lineage_data.get("generation", 0),
            parent_skill_ids=lineage_data.get("parent_skill_ids", []),
            source_memory_ids=lineage_data.get("source_memory_ids", []),
            change_summary=lineage_data.get("change_summary", ""),
            content_diff=lineage_data.get("content_diff", ""),
            content_snapshot=lineage_data.get("content_snapshot", {}),
            created_by=lineage_data.get("created_by", ""),
            created_at=lineage_data.get("created_at", ""),
        )
        return SkillRecord(
            skill_id=d.get("skill_id", d.get("id", "")),
            name=d.get("name", ""),
            description=d.get("description", ""),
            content=d.get("content", ""),
            category=SkillCategory(d.get("category", "workflow")),
            status=SkillStatus(d.get("status", "candidate")),
            visibility=SkillVisibility(d.get("visibility", "private")),
            lineage=lineage,
            tags=d.get("tags", []),
            tenant_id=d.get("tenant_id", ""),
            user_id=d.get("user_id", ""),
            uri=d.get("uri", ""),
            total_selections=d.get("total_selections", 0),
            total_applied=d.get("total_applied", 0),
            total_completions=d.get("total_completions", 0),
            total_fallbacks=d.get("total_fallbacks", 0),
            abstract=d.get("abstract", ""),
            overview=d.get("overview", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            source_fingerprint=d.get("source_fingerprint", ""),
        )
```

- [ ] **Step 3: Add skills collection schema**

Edit `src/opencortex/storage/collection_schemas.py`. Add before the final empty line (after line 207):

```python
async def init_skills_collection(
    storage: StorageInterface, name: str, vector_dim: int,
) -> bool:
    """Initialize the skills collection with proper schema."""
    schema = CollectionSchemas.skills_collection(name, vector_dim)
    return await storage.create_collection(name, schema)
```

Add to `CollectionSchemas` class a new static method (after `knowledge_collection`):

```python
    @staticmethod
    def skills_collection(name: str, vector_dim: int) -> Dict[str, Any]:
        """Skills collection schema — independent from memory collections."""
        return {
            "CollectionName": name,
            "Fields": [
                {"FieldName": "skill_id", "FieldType": "string"},
                {"FieldName": "name", "FieldType": "string"},
                {"FieldName": "description", "FieldType": "string"},
                {"FieldName": "category", "FieldType": "string"},
                {"FieldName": "status", "FieldType": "string"},
                {"FieldName": "visibility", "FieldType": "string"},
                {"FieldName": "tenant_id", "FieldType": "string"},
                {"FieldName": "user_id", "FieldType": "string"},
                {"FieldName": "uri", "FieldType": "string"},
                {"FieldName": "source_fingerprint", "FieldType": "string"},
                {"FieldName": "vector", "FieldType": "vector", "Dim": vector_dim},
                {"FieldName": "abstract", "FieldType": "string"},
                {"FieldName": "overview", "FieldType": "string"},
                {"FieldName": "created_at", "FieldType": "date_time"},
                {"FieldName": "updated_at", "FieldType": "date_time"},
            ],
            "ScalarIndex": [
                "skill_id", "name", "category", "status", "visibility",
                "tenant_id", "user_id", "uri", "source_fingerprint",
                "created_at", "updated_at",
            ],
        }
```

- [ ] **Step 4: Write storage adapter tests**

Create `tests/skill_engine/test_storage_adapter.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillVisibility,
    make_skill_uri,
)
from opencortex.skill_engine.adapters.storage_adapter import SkillStorageAdapter


class TestSkillStorageAdapter(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.storage = AsyncMock()
        self.storage.collection_exists = AsyncMock(return_value=True)
        self.embedder = MagicMock()
        self.embedder.embed = MagicMock(
            return_value=MagicMock(dense_vector=[0.1] * 4)
        )
        self.adapter = SkillStorageAdapter(
            storage=self.storage, embedder=self.embedder,
            collection_name="skills", embedding_dim=4,
        )

    def _make_record(self, skill_id="sk-001", status=SkillStatus.CANDIDATE):
        return SkillRecord(
            skill_id=skill_id, name="deploy-flow",
            description="Deploy workflow", content="# Deploy",
            category=SkillCategory.WORKFLOW,
            status=status,
            tenant_id="team1", user_id="hugo",
            uri=make_skill_uri("team1", "hugo", skill_id),
        )

    async def test_save_calls_upsert(self):
        r = self._make_record()
        await self.adapter.save(r)
        self.storage.upsert.assert_called_once()
        call_args = self.storage.upsert.call_args
        self.assertEqual(call_args[0][0], "skills")
        payload = call_args[0][1]
        self.assertEqual(payload["id"], "sk-001")
        self.assertEqual(payload["status"], "candidate")
        self.assertEqual(payload["visibility"], "private")

    async def test_load_returns_record(self):
        self.storage.get = AsyncMock(return_value=[{
            "skill_id": "sk-001", "name": "test", "description": "d",
            "content": "c", "category": "workflow", "status": "active",
            "visibility": "private", "tenant_id": "t", "user_id": "u",
            "uri": "opencortex://t/u/skills/sk-001",
            "lineage": {"origin": "captured", "generation": 0},
        }])
        r = await self.adapter.load("sk-001")
        self.assertIsNotNone(r)
        self.assertEqual(r.skill_id, "sk-001")
        self.assertEqual(r.status, SkillStatus.ACTIVE)

    async def test_load_returns_none_for_missing(self):
        self.storage.get = AsyncMock(return_value=[])
        r = await self.adapter.load("nonexistent")
        self.assertIsNone(r)

    async def test_search_applies_visibility_filter(self):
        self.storage.search = AsyncMock(return_value=[])
        await self.adapter.search("deploy", "team1", "hugo", top_k=5)
        call_args = self.storage.search.call_args
        filter_expr = call_args[0][2]
        # Should have AND with visibility OR + status filter
        self.assertEqual(filter_expr["op"], "and")
        self.assertTrue(len(filter_expr["conds"]) >= 2)

    async def test_find_by_fingerprint(self):
        self.storage.filter = AsyncMock(return_value=[{
            "skill_id": "sk-001", "name": "test", "description": "d",
            "content": "c", "category": "workflow", "status": "candidate",
            "visibility": "private", "tenant_id": "t", "user_id": "u",
            "source_fingerprint": "abc123",
            "lineage": {"origin": "captured"},
        }])
        r = await self.adapter.find_by_fingerprint("abc123")
        self.assertIsNotNone(r)
        self.assertEqual(r.source_fingerprint, "abc123")

    async def test_update_status(self):
        await self.adapter.update_status("sk-001", SkillStatus.ACTIVE)
        self.storage.update.assert_called_once()
        call_args = self.storage.update.call_args
        self.assertEqual(call_args[0][2]["status"], "active")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 5: Run tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_types tests.skill_engine.test_storage_adapter -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/skill_engine/adapters/ src/opencortex/storage/collection_schemas.py tests/skill_engine/test_storage_adapter.py
git commit -m "feat(skill_engine): add adapter interfaces + storage adapter + skills collection schema"
```

---

### Task 3: SkillStore

**Files:**
- Create: `src/opencortex/skill_engine/store.py`
- Test: `tests/skill_engine/test_store.py`

- [ ] **Step 1: Write failing test**

Create `tests/skill_engine/test_store.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillVisibility,
    SkillLineage, SkillOrigin, make_skill_uri,
)
from opencortex.skill_engine.store import SkillStore


class TestSkillStore(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.storage_adapter = AsyncMock()
        self.store = SkillStore(self.storage_adapter)

    def _make_record(self, skill_id="sk-001", status=SkillStatus.CANDIDATE):
        return SkillRecord(
            skill_id=skill_id, name="deploy-flow",
            description="Deploy workflow", content="# Deploy",
            category=SkillCategory.WORKFLOW, status=status,
            tenant_id="team1", user_id="hugo",
            uri=make_skill_uri("team1", "hugo", skill_id),
        )

    async def test_save_record(self):
        r = self._make_record()
        await self.store.save_record(r)
        self.storage_adapter.save.assert_called_once_with(r)

    async def test_load_record(self):
        r = self._make_record()
        self.storage_adapter.load = AsyncMock(return_value=r)
        result = await self.store.load_record("sk-001")
        self.assertEqual(result.skill_id, "sk-001")

    async def test_activate(self):
        await self.store.activate("sk-001")
        self.storage_adapter.update_status.assert_called_once_with(
            "sk-001", SkillStatus.ACTIVE,
        )

    async def test_deprecate(self):
        await self.store.deprecate("sk-001")
        self.storage_adapter.update_status.assert_called_once_with(
            "sk-001", SkillStatus.DEPRECATED,
        )

    async def test_evolve_skill_saves_new_and_deprecates_parents(self):
        new = self._make_record(skill_id="sk-002")
        new.lineage = SkillLineage(
            origin=SkillOrigin.FIXED,
            parent_skill_ids=["sk-001"],
        )
        await self.store.evolve_skill(new, parent_ids=["sk-001"])
        # Should save new record
        self.storage_adapter.save.assert_called_once_with(new)
        # Should deprecate parent
        self.storage_adapter.update_status.assert_called_once_with(
            "sk-001", SkillStatus.DEPRECATED,
        )

    async def test_search_delegates(self):
        self.storage_adapter.search = AsyncMock(return_value=[])
        await self.store.search("deploy", "team1", "hugo", top_k=5)
        self.storage_adapter.search.assert_called_once_with(
            "deploy", "team1", "hugo", top_k=5,
        )

    async def test_find_by_fingerprint(self):
        self.storage_adapter.find_by_fingerprint = AsyncMock(return_value=None)
        result = await self.store.find_by_fingerprint("abc123")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python3 -m unittest tests.skill_engine.test_store -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'opencortex.skill_engine.store'`

- [ ] **Step 3: Implement store.py**

Create `src/opencortex/skill_engine/store.py`:

```python
"""
SkillStore — CRUD + lifecycle management for skills.

Mirrors OpenSpace SkillStore interface, delegates to StorageAdapter.
"""

import logging
from typing import List, Optional

from opencortex.skill_engine.types import SkillRecord, SkillStatus

logger = logging.getLogger(__name__)


class SkillStore:
    def __init__(self, storage_adapter):
        self._storage = storage_adapter

    async def save_record(self, record: SkillRecord) -> None:
        await self._storage.save(record)

    async def load_record(self, skill_id: str) -> Optional[SkillRecord]:
        return await self._storage.load(skill_id)

    async def load_active(self, tenant_id: str, user_id: str) -> List[SkillRecord]:
        return await self._storage.load_all(tenant_id, user_id, status=SkillStatus.ACTIVE)

    async def load_by_status(self, tenant_id: str, user_id: str,
                              status: SkillStatus) -> List[SkillRecord]:
        return await self._storage.load_all(tenant_id, user_id, status=status)

    async def activate(self, skill_id: str) -> None:
        await self._storage.update_status(skill_id, SkillStatus.ACTIVE)

    async def deprecate(self, skill_id: str) -> None:
        await self._storage.update_status(skill_id, SkillStatus.DEPRECATED)

    async def evolve_skill(self, new: SkillRecord, parent_ids: List[str]) -> None:
        """Save new skill version and deprecate parents (for FIX evolution)."""
        await self._storage.save(new)
        for pid in parent_ids:
            await self._storage.update_status(pid, SkillStatus.DEPRECATED)

    async def record_selection(self, skill_id: str) -> None:
        await self._storage.update_metrics(skill_id, total_selections=1)

    async def record_application(self, skill_id: str, completed: bool) -> None:
        counters = {"total_applied": 1}
        if completed:
            counters["total_completions"] = 1
        else:
            counters["total_fallbacks"] = 1
        await self._storage.update_metrics(skill_id, **counters)

    async def search(self, query: str, tenant_id: str, user_id: str,
                     top_k: int = 5) -> List[SkillRecord]:
        return await self._storage.search(query, tenant_id, user_id, top_k=top_k)

    async def find_by_fingerprint(self, fingerprint: str) -> Optional[SkillRecord]:
        return await self._storage.find_by_fingerprint(fingerprint)
```

- [ ] **Step 4: Run tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_store -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
git add src/opencortex/skill_engine/store.py tests/skill_engine/test_store.py
git commit -m "feat(skill_engine): add SkillStore with CRUD + lifecycle + evolve"
```

---

### Task 4: SkillManager + HTTP Routes + Orchestrator Integration

**Files:**
- Create: `src/opencortex/skill_engine/skill_manager.py`
- Create: `src/opencortex/skill_engine/http_routes.py`
- Modify: `src/opencortex/http/server.py:248-273`
- Modify: `src/opencortex/orchestrator.py:249-313` (_init_alpha), `orchestrator.py:1700-1724` (search)
- Test: `tests/skill_engine/test_skill_manager.py`
- Test: `tests/skill_engine/test_http_routes.py`

- [ ] **Step 1: Write SkillManager test**

Create `tests/skill_engine/test_skill_manager.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, make_skill_uri,
)
from opencortex.skill_engine.skill_manager import SkillManager


class TestSkillManager(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.store = AsyncMock()
        self.manager = SkillManager(store=self.store)

    def _make_record(self, skill_id="sk-001", status=SkillStatus.ACTIVE):
        return SkillRecord(
            skill_id=skill_id, name="deploy-flow",
            description="Deploy workflow", content="# Deploy",
            category=SkillCategory.WORKFLOW, status=status,
            tenant_id="team1", user_id="hugo",
            uri=make_skill_uri("team1", "hugo", skill_id),
            abstract="Standard deployment workflow",
        )

    async def test_search_delegates_to_store(self):
        self.store.search = AsyncMock(return_value=[self._make_record()])
        results = await self.manager.search("deploy", "team1", "hugo")
        self.assertEqual(len(results), 1)
        self.store.search.assert_called_once()

    async def test_approve_activates_skill(self):
        await self.manager.approve("sk-001", "team1", "hugo")
        self.store.activate.assert_called_once_with("sk-001")

    async def test_reject_deprecates_skill(self):
        await self.manager.reject("sk-001", "team1", "hugo")
        self.store.deprecate.assert_called_once_with("sk-001")

    async def test_list_skills(self):
        self.store.load_by_status = AsyncMock(return_value=[])
        await self.manager.list_skills("team1", "hugo", status=SkillStatus.ACTIVE)
        self.store.load_by_status.assert_called_once()

    async def test_get_skill(self):
        self.store.load_record = AsyncMock(return_value=self._make_record())
        r = await self.manager.get_skill("sk-001", "team1", "hugo")
        self.assertIsNotNone(r)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Implement SkillManager**

Create `src/opencortex/skill_engine/skill_manager.py`:

```python
"""
SkillManager — top-level API for the Skill Engine.

Orchestrates extraction, approval, search, and evolution.
"""

import logging
from typing import List, Optional

from opencortex.skill_engine.types import SkillRecord, SkillStatus

logger = logging.getLogger(__name__)


class SkillManager:
    def __init__(self, store):
        self._store = store

    # --- Search (for recall integration) ---

    async def search(self, query: str, tenant_id: str, user_id: str,
                     top_k: int = 5) -> List[SkillRecord]:
        return await self._store.search(query, tenant_id, user_id, top_k=top_k)

    # --- Approval workflow ---

    async def approve(self, skill_id: str, tenant_id: str, user_id: str) -> None:
        await self._store.activate(skill_id)

    async def reject(self, skill_id: str, tenant_id: str, user_id: str) -> None:
        await self._store.deprecate(skill_id)

    async def deprecate(self, skill_id: str, tenant_id: str, user_id: str) -> None:
        await self._store.deprecate(skill_id)

    # --- Listing ---

    async def list_skills(self, tenant_id: str, user_id: str,
                          status: Optional[SkillStatus] = None) -> List[SkillRecord]:
        if status:
            return await self._store.load_by_status(tenant_id, user_id, status)
        return await self._store.load_by_status(
            tenant_id, user_id, SkillStatus.ACTIVE,
        ) + await self._store.load_by_status(
            tenant_id, user_id, SkillStatus.CANDIDATE,
        )

    async def get_skill(self, skill_id: str, tenant_id: str,
                        user_id: str) -> Optional[SkillRecord]:
        return await self._store.load_record(skill_id)
```

- [ ] **Step 3: Implement HTTP routes**

Create `src/opencortex/skill_engine/http_routes.py`:

```python
"""
Skill Engine HTTP routes — REST API for SkillHub frontend.

All routes derive tenant_id/user_id from JWT via get_effective_identity().
"""

from fastapi import APIRouter, HTTPException
from typing import Optional

from opencortex.http.request_context import get_effective_identity

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])

# SkillManager is injected at app startup
_skill_manager = None


def set_skill_manager(manager) -> None:
    global _skill_manager
    _skill_manager = manager


def _get_manager():
    if _skill_manager is None:
        raise HTTPException(status_code=503, detail="Skill engine not initialized")
    return _skill_manager


@router.get("")
async def list_skills(status: Optional[str] = None):
    """List skills (filterable by status)."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    from opencortex.skill_engine.types import SkillStatus
    s = SkillStatus(status) if status else None
    results = await mgr.list_skills(tid, uid, status=s)
    return {"skills": [r.to_dict() for r in results], "count": len(results)}


@router.get("/search")
async def search_skills(q: str, top_k: int = 5):
    """Search active skills."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    results = await mgr.search(q, tid, uid, top_k=top_k)
    return {"skills": [r.to_dict() for r in results], "count": len(results)}


@router.get("/{skill_id}")
async def get_skill(skill_id: str):
    """Get skill detail + lineage."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    r = await mgr.get_skill(skill_id, tid, uid)
    if not r:
        raise HTTPException(status_code=404, detail="Skill not found")
    return r.to_dict()


@router.post("/{skill_id}/approve")
async def approve_skill(skill_id: str):
    """Approve candidate → ACTIVE."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    await mgr.approve(skill_id, tid, uid)
    return {"status": "active", "skill_id": skill_id}


@router.post("/{skill_id}/reject")
async def reject_skill(skill_id: str):
    """Reject candidate → DEPRECATED."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    await mgr.reject(skill_id, tid, uid)
    return {"status": "deprecated", "skill_id": skill_id}


@router.post("/{skill_id}/deprecate")
async def deprecate_skill(skill_id: str):
    """Deprecate active skill."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    await mgr.deprecate(skill_id, tid, uid)
    return {"status": "deprecated", "skill_id": skill_id}
```

- [ ] **Step 4: Mount routes in server.py**

Edit `src/opencortex/http/server.py`. After the insights router block (around line 249), add:

```python
    # Skill Engine routes
    try:
        from opencortex.skill_engine.http_routes import router as skill_router
        app.include_router(skill_router)
        logger.info("[HTTP] Skill Engine routes registered")
    except Exception as e:
        logger.info("[HTTP] Skill Engine routes not available: %s", e)
```

- [ ] **Step 5: Add skill search to orchestrator recall**

Edit `src/opencortex/orchestrator.py`. Add `_skill_manager` init in `__init__` (around line 100):

```python
        self._skill_manager = None  # Lazy init via _init_skill_engine()
```

Add a new method after `_init_alpha()` (after line 313):

```python
    async def _init_skill_engine(self) -> None:
        """Initialize Skill Engine if storage and embedder are available."""
        if not self._storage or not self._embedder:
            return
        try:
            from opencortex.skill_engine.adapters.storage_adapter import SkillStorageAdapter
            from opencortex.skill_engine.store import SkillStore
            from opencortex.skill_engine.skill_manager import SkillManager
            from opencortex.skill_engine.http_routes import set_skill_manager

            storage_adapter = SkillStorageAdapter(
                storage=self._storage,
                embedder=self._embedder,
                embedding_dim=self._config.embedding_dimension,
            )
            await storage_adapter.initialize()

            store = SkillStore(storage_adapter)
            self._skill_manager = SkillManager(store=store)
            set_skill_manager(self._skill_manager)

            logger.info("[MemoryOrchestrator] Skill Engine initialized")
        except Exception as exc:
            logger.info("[MemoryOrchestrator] Skill Engine not available: %s", exc)
```

Call it in `init()` after `_init_alpha()` (line 241):

```python
        await self._init_skill_engine()
```

Add skill search to the end of the `search()` method (after line 1724, before `return result`):

```python
        # Skill Engine: search active skills and merge into FindResult.skills
        if self._skill_manager:
            try:
                from opencortex.retrieve.types import MatchedContext, ContextType
                skill_results = await self._skill_manager.search(
                    query, tid, uid, top_k=3,
                )
                for sr in skill_results:
                    result.skills.append(MatchedContext(
                        uri=sr.uri,
                        context_type=ContextType.SKILL,
                        is_leaf=True,
                        abstract=sr.abstract,
                        overview=sr.overview,
                        content=sr.content,
                        category=sr.category.value,
                        score=0.0,
                    ))
            except Exception as exc:
                logger.debug("[search] Skill search failed: %s", exc)
```

- [ ] **Step 6: Run all tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_types tests.skill_engine.test_storage_adapter tests.skill_engine.test_store tests.skill_engine.test_skill_manager -v`
Expected: All PASS

- [ ] **Step 7: Run regression**

Run: `uv run python3 -m unittest tests.test_alpha_types tests.test_alpha_config tests.test_context_manager tests.test_qdrant_adapter -v`
Expected: All PASS (no regressions)

- [ ] **Step 8: Commit**

```bash
git add src/opencortex/skill_engine/skill_manager.py src/opencortex/skill_engine/http_routes.py src/opencortex/http/server.py src/opencortex/orchestrator.py tests/skill_engine/test_skill_manager.py
git commit -m "feat(skill_engine): add SkillManager, HTTP routes, orchestrator integration"
```

---

### Task 5: Prompts + MemoryFormatter

**Files:**
- Create: `src/opencortex/skill_engine/prompts.py`
- Create: `src/opencortex/skill_engine/memory_formatter.py`

- [ ] **Step 1: Create prompts.py**

Create `src/opencortex/skill_engine/prompts.py`:

```python
"""
Skill Engine LLM prompts — extraction, evolution, and analysis.
"""

SKILL_EXTRACT_PROMPT = """You are analyzing a cluster of related memories to extract reusable operational skills.

## Memory Cluster
{cluster_content}

## Existing Skills (do NOT duplicate these)
{existing_skills}

## Instructions

1. Read all memories in the cluster carefully.
2. Identify repeated operational patterns — workflows, debugging procedures, deployment steps, or tool usage guides.
3. For each pattern found, produce a skill with:
   - name: concise, lowercase-hyphenated (max 50 chars)
   - description: one sentence
   - category: "workflow" | "tool_guide" | "pattern"
   - confidence: 0.0-1.0
   - content: Markdown instructions with numbered steps

4. **Conflict handling**: If memories show different approaches for the same step:
   - Do NOT pick one and discard the other
   - Merge into conditional branches:
     - Step N (choose by scenario):
       - Scenario A → approach 1 (from X memories, Y users)
       - Scenario B → approach 2 (from X memories, Y users)

5. Skip patterns that duplicate existing skills listed above.

## Output Format (JSON array)

```json
[
  {{
    "name": "skill-name",
    "description": "One sentence",
    "category": "workflow",
    "confidence": 0.85,
    "content": "# Skill Name\\n\\n1. Step one\\n2. Step two\\n...",
    "source_memory_ids": ["id1", "id2"]
  }}
]
```

Return an empty array [] if no reusable patterns are found."""

SKILL_EVOLVE_FIX_PROMPT = """You are fixing an existing operational skill.

## Current Skill
{current_content}

## What needs fixing
{direction}

## Instructions
1. Analyze the issue described above
2. Fix the affected content while preserving the overall structure
3. Keep the skill name and purpose intact
4. Be surgical — fix what's broken without unnecessary rewrites

## Output
Provide the complete fixed skill content in Markdown.
End with <EVOLUTION_COMPLETE> if the fix is satisfactory.
End with <EVOLUTION_FAILED> Reason: ... if you cannot complete the fix."""

SKILL_EVOLVE_DERIVED_PROMPT = """You are creating an enhanced version of an existing skill.

## Parent Skill
{parent_content}

## Enhancement Direction
{direction}

## Instructions
1. Create an improved version addressing the enhancement direction
2. Give a different, concise name (max 50 chars, lowercase, hyphens)
3. Should be self-contained (no reference to parent needed)
4. Preserve what works, improve what doesn't

## Output
Provide the complete enhanced skill content in Markdown.
End with <EVOLUTION_COMPLETE> if the derived skill is a meaningful improvement.
End with <EVOLUTION_FAILED> Reason: ... if not a worthwhile enhancement."""

SKILL_EVOLVE_CAPTURED_PROMPT = """You are creating a brand-new operational skill from observed patterns.

## Pattern to Capture
{direction}

## Category
{category}

## Source Context
{source_context}

## Instructions
1. Distill the observed pattern into clear, reusable instructions
2. Choose a concise name (max 50 chars, lowercase, hyphens)
3. Write a brief description
4. Structure as clear, actionable steps
5. Generalize — abstract away task-specific details

## Output
Provide the complete skill content in Markdown.
End with <EVOLUTION_COMPLETE> if the skill is genuinely reusable.
End with <EVOLUTION_FAILED> Reason: ... if the pattern is too task-specific."""
```

- [ ] **Step 2: Create memory_formatter.py**

Create `src/opencortex/skill_engine/memory_formatter.py`:

```python
"""
Format memory clusters for LLM context in skill extraction.

Adapted from OpenSpace's conversation_formatter.py — formats memories
instead of execution conversations.
"""

from typing import List, TYPE_CHECKING

if TYPE_CHECKING:
    from opencortex.skill_engine.adapters.source_adapter import MemoryRecord
    from opencortex.skill_engine.types import SkillRecord

MAX_MEMORY_CHARS = 3000
MAX_CLUSTER_CHARS = 60000


def format_cluster_for_extraction(
    memories: List["MemoryRecord"],
    max_chars: int = MAX_CLUSTER_CHARS,
) -> str:
    """Format a memory cluster for the extraction LLM prompt."""
    parts = []
    total = 0
    for i, m in enumerate(memories):
        section = f"### Memory {i+1} [{m.memory_id}]\n"
        section += f"**Type**: {m.context_type} / {m.category}\n"
        if m.abstract:
            section += f"**Summary**: {m.abstract}\n"
        if m.overview:
            section += f"**Overview**: {m.overview}\n\n"
        if m.content:
            content = m.content[:MAX_MEMORY_CHARS]
            if len(m.content) > MAX_MEMORY_CHARS:
                content += "\n... (truncated)"
            section += f"{content}\n"
        section += "\n---\n\n"

        if total + len(section) > max_chars:
            break
        parts.append(section)
        total += len(section)

    return "".join(parts)


def format_existing_skills(
    skills: List["SkillRecord"],
    max_chars: int = 10000,
) -> str:
    """Format existing active skills for dedup context."""
    if not skills:
        return "(No existing skills)"
    parts = []
    total = 0
    for s in skills:
        line = f"- **{s.name}** ({s.category.value}): {s.description}\n"
        if total + len(line) > max_chars:
            break
        parts.append(line)
        total += len(line)
    return "".join(parts)
```

- [ ] **Step 3: Commit**

```bash
git add src/opencortex/skill_engine/prompts.py src/opencortex/skill_engine/memory_formatter.py
git commit -m "feat(skill_engine): add extraction/evolution prompts + memory formatter"
```

---

### Task 6: SkillAnalyzer + SkillEvolver

**Files:**
- Create: `src/opencortex/skill_engine/analyzer.py`
- Create: `src/opencortex/skill_engine/evolver.py`
- Test: `tests/skill_engine/test_analyzer.py`
- Test: `tests/skill_engine/test_evolver.py`

This is the largest task. The analyzer and evolver are the core LLM-driven components.

- [ ] **Step 1: Write analyzer test**

Create `tests/skill_engine/test_analyzer.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillOrigin,
    EvolutionSuggestion, make_source_fingerprint,
)
from opencortex.skill_engine.analyzer import SkillAnalyzer
from opencortex.skill_engine.adapters.source_adapter import MemoryCluster, MemoryRecord


class TestSkillAnalyzer(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.source = AsyncMock()
        self.llm = AsyncMock()
        self.store = AsyncMock()
        self.store.find_by_fingerprint = AsyncMock(return_value=None)
        self.store.load_active = AsyncMock(return_value=[])
        self.analyzer = SkillAnalyzer(
            source=self.source, llm=self.llm, store=self.store,
        )

    def _make_cluster(self):
        return MemoryCluster(
            cluster_id="c1", theme="deployment",
            memory_ids=["m1", "m2", "m3"],
            centroid_embedding=[0.1] * 4, avg_score=0.8,
        )

    def _make_memories(self):
        return [
            MemoryRecord(
                memory_id=f"m{i}", abstract=f"Deploy step {i}",
                overview=f"Overview {i}", content=f"Content {i}",
                context_type="memory", category="events",
            )
            for i in range(1, 4)
        ]

    async def test_skips_cluster_with_existing_fingerprint(self):
        """If fingerprint already exists, skip extraction."""
        self.store.find_by_fingerprint = AsyncMock(
            return_value=MagicMock(skill_id="existing")
        )
        self.source.scan_memories = AsyncMock(return_value=[self._make_cluster()])
        results = await self.analyzer.extract_candidates("t", "u")
        self.assertEqual(len(results), 0)
        self.llm.complete.assert_not_called()

    async def test_calls_llm_for_new_cluster(self):
        """New cluster triggers LLM analysis."""
        cluster = self._make_cluster()
        self.source.scan_memories = AsyncMock(return_value=[cluster])
        self.source.get_cluster_memories = AsyncMock(return_value=self._make_memories())
        self.llm.complete = AsyncMock(return_value='[]')

        results = await self.analyzer.extract_candidates("t", "u")
        self.llm.complete.assert_called_once()

    async def test_parses_llm_suggestions(self):
        """LLM returns valid skill → produces EvolutionSuggestion."""
        cluster = self._make_cluster()
        self.source.scan_memories = AsyncMock(return_value=[cluster])
        self.source.get_cluster_memories = AsyncMock(return_value=self._make_memories())
        self.llm.complete = AsyncMock(return_value="""[
            {
                "name": "deploy-flow",
                "description": "Standard deploy",
                "category": "workflow",
                "confidence": 0.9,
                "content": "# Deploy\\n1. Build\\n2. Test",
                "source_memory_ids": ["m1", "m2"]
            }
        ]""")

        results = await self.analyzer.extract_candidates("t", "u")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].evolution_type, SkillOrigin.CAPTURED)
        self.assertEqual(results[0].direction, "deploy-flow")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Implement analyzer.py**

Create `src/opencortex/skill_engine/analyzer.py`:

```python
"""
SkillAnalyzer — extract operational skill candidates from memory clusters.

Core divergence from OpenSpace: analyzes memories instead of execution recordings.
"""

import logging
from typing import List, Optional

import orjson

from opencortex.skill_engine.types import (
    EvolutionSuggestion, SkillOrigin, SkillCategory,
    make_source_fingerprint,
)
from opencortex.skill_engine.prompts import SKILL_EXTRACT_PROMPT
from opencortex.skill_engine.memory_formatter import (
    format_cluster_for_extraction, format_existing_skills,
)

logger = logging.getLogger(__name__)


class SkillAnalyzer:
    def __init__(self, source, llm, store):
        self._source = source
        self._llm = llm
        self._store = store

    async def extract_candidates(
        self, tenant_id: str, user_id: str,
        context_types: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
    ) -> List[EvolutionSuggestion]:
        """Full extraction pipeline: scan → cluster → analyze → dedup."""
        clusters = await self._source.scan_memories(
            tenant_id, user_id,
            context_types=context_types,
            categories=categories,
        )

        all_suggestions = []
        for cluster in clusters:
            # Idempotency: check fingerprint
            fp = make_source_fingerprint(cluster.memory_ids)
            existing = await self._store.find_by_fingerprint(fp)
            if existing:
                logger.debug(
                    "[SkillAnalyzer] Cluster %s already extracted (fp=%s)",
                    cluster.cluster_id, fp,
                )
                continue

            suggestions = await self._analyze_cluster(
                cluster, tenant_id, user_id, fp,
            )
            if suggestions:
                all_suggestions.extend(suggestions)

        return all_suggestions

    async def _analyze_cluster(
        self, cluster, tenant_id: str, user_id: str,
        fingerprint: str,
    ) -> List[EvolutionSuggestion]:
        """Analyze a single cluster via LLM."""
        memories = await self._source.get_cluster_memories(cluster)
        if not memories:
            return []

        # Load existing skills for dedup context (CANDIDATE + ACTIVE)
        existing_active = await self._store.load_active(tenant_id, user_id)

        cluster_content = format_cluster_for_extraction(memories)
        existing_text = format_existing_skills(existing_active)

        prompt = SKILL_EXTRACT_PROMPT.format(
            cluster_content=cluster_content,
            existing_skills=existing_text,
        )

        try:
            response = await self._llm.complete([
                {"role": "user", "content": prompt},
            ])
            items = orjson.loads(response)
        except Exception as exc:
            logger.warning("[SkillAnalyzer] LLM/parse failed for cluster %s: %s",
                           cluster.cluster_id, exc)
            return []

        if not isinstance(items, list):
            return []

        suggestions = []
        for item in items:
            cat_str = item.get("category", "workflow")
            try:
                category = SkillCategory(cat_str)
            except ValueError:
                category = SkillCategory.WORKFLOW

            suggestions.append(EvolutionSuggestion(
                evolution_type=SkillOrigin.CAPTURED,
                target_skill_ids=[],
                category=category,
                direction=item.get("name", ""),
                confidence=item.get("confidence", 0.0),
                source_memory_ids=item.get("source_memory_ids", cluster.memory_ids),
            ))

        return suggestions
```

- [ ] **Step 3: Write evolver test**

Create `tests/skill_engine/test_evolver.py`:

```python
import unittest
from unittest.mock import AsyncMock, MagicMock
from opencortex.skill_engine.types import (
    SkillRecord, SkillCategory, SkillStatus, SkillOrigin,
    EvolutionSuggestion, SkillLineage,
)
from opencortex.skill_engine.evolver import SkillEvolver


class TestSkillEvolver(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.llm = AsyncMock()
        self.store = AsyncMock()
        self.evolver = SkillEvolver(llm=self.llm, store=self.store)

    async def test_evolve_captured_returns_candidate(self):
        """CAPTURED evolution generates a new skill from LLM output."""
        self.llm.complete = AsyncMock(
            return_value="# Deploy Flow\n\n1. Build\n2. Test\n\n<EVOLUTION_COMPLETE>"
        )

        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.CAPTURED,
            category=SkillCategory.WORKFLOW,
            direction="deploy-flow",
            confidence=0.9,
            source_memory_ids=["m1", "m2"],
        )

        result = await self.evolver.evolve(
            suggestion, tenant_id="t", user_id="u",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.status, SkillStatus.CANDIDATE)
        self.assertEqual(result.lineage.origin, SkillOrigin.CAPTURED)
        self.assertIn("Deploy Flow", result.content)

    async def test_evolve_fix_creates_candidate_linked_to_parent(self):
        """FIX creates a new CANDIDATE, not in-place update."""
        parent = SkillRecord(
            skill_id="sk-001", name="old-flow",
            description="Old", content="# Old\n1. Step",
            category=SkillCategory.WORKFLOW, status=SkillStatus.ACTIVE,
            tenant_id="t", user_id="u",
        )
        self.store.load_record = AsyncMock(return_value=parent)
        self.llm.complete = AsyncMock(
            return_value="# Fixed Flow\n\n1. Better Step\n\n<EVOLUTION_COMPLETE>"
        )

        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.FIXED,
            target_skill_ids=["sk-001"],
            category=SkillCategory.WORKFLOW,
            direction="Fix outdated step 1",
        )

        result = await self.evolver.evolve(suggestion, tenant_id="t", user_id="u")
        self.assertIsNotNone(result)
        self.assertEqual(result.status, SkillStatus.CANDIDATE)
        self.assertEqual(result.lineage.origin, SkillOrigin.FIXED)
        self.assertIn("sk-001", result.lineage.parent_skill_ids)
        # Should NOT be the same skill_id
        self.assertNotEqual(result.skill_id, "sk-001")

    async def test_evolve_returns_none_on_failure(self):
        """If LLM fails, return None."""
        self.llm.complete = AsyncMock(
            return_value="<EVOLUTION_FAILED> Reason: too vague"
        )

        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.CAPTURED,
            category=SkillCategory.WORKFLOW,
            direction="vague-pattern",
        )

        result = await self.evolver.evolve(suggestion, tenant_id="t", user_id="u")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Implement evolver.py**

Create `src/opencortex/skill_engine/evolver.py`:

```python
"""
SkillEvolver — generates new/improved skills via LLM.

Mirrors OpenSpace's three evolution types: CAPTURED, DERIVED, FIX.
FIX creates a new CANDIDATE version (not in-place) per spec §4.7.
"""

import asyncio
import logging
import uuid
from typing import Optional

from opencortex.skill_engine.types import (
    SkillRecord, SkillLineage, SkillOrigin, SkillStatus,
    SkillCategory, SkillVisibility, EvolutionSuggestion,
    make_skill_uri, make_source_fingerprint,
)
from opencortex.skill_engine.prompts import (
    SKILL_EVOLVE_CAPTURED_PROMPT,
    SKILL_EVOLVE_DERIVED_PROMPT,
    SKILL_EVOLVE_FIX_PROMPT,
)

logger = logging.getLogger(__name__)

EVOLUTION_COMPLETE = "<EVOLUTION_COMPLETE>"
EVOLUTION_FAILED = "<EVOLUTION_FAILED>"
MAX_ITERATIONS = 5


class SkillEvolver:
    def __init__(self, llm, store):
        self._llm = llm
        self._store = store
        self._semaphore = asyncio.Semaphore(3)

    async def evolve(
        self, suggestion: EvolutionSuggestion,
        tenant_id: str, user_id: str,
    ) -> Optional[SkillRecord]:
        """Route to appropriate evolution method."""
        async with self._semaphore:
            match suggestion.evolution_type:
                case SkillOrigin.CAPTURED:
                    return await self._evolve_captured(suggestion, tenant_id, user_id)
                case SkillOrigin.DERIVED:
                    return await self._evolve_derived(suggestion, tenant_id, user_id)
                case SkillOrigin.FIXED:
                    return await self._evolve_fix(suggestion, tenant_id, user_id)
            return None

    async def process_suggestions(
        self, suggestions: list, tenant_id: str, user_id: str,
    ) -> list:
        """Process all suggestions with concurrency control."""
        tasks = [self.evolve(s, tenant_id, user_id) for s in suggestions]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if isinstance(r, SkillRecord)]

    async def _evolve_captured(self, s, tid, uid) -> Optional[SkillRecord]:
        prompt = SKILL_EVOLVE_CAPTURED_PROMPT.format(
            direction=s.direction,
            category=s.category.value,
            source_context=f"Pattern from {len(s.source_memory_ids)} memories",
        )
        content = await self._run_evolution_loop(prompt)
        if not content:
            return None

        skill_id = f"sk-{uuid.uuid4().hex[:12]}"
        name = s.direction or "unnamed-skill"
        return SkillRecord(
            skill_id=skill_id, name=name,
            description=f"Extracted: {name}",
            content=content, category=s.category,
            status=SkillStatus.CANDIDATE,
            visibility=SkillVisibility.PRIVATE,
            lineage=SkillLineage(
                origin=SkillOrigin.CAPTURED,
                source_memory_ids=s.source_memory_ids,
                created_by="skill-evolver",
            ),
            tenant_id=tid, user_id=uid,
            uri=make_skill_uri(tid, uid, skill_id),
            abstract=name,
            source_fingerprint=make_source_fingerprint(s.source_memory_ids),
        )

    async def _evolve_derived(self, s, tid, uid) -> Optional[SkillRecord]:
        parent = None
        if s.target_skill_ids:
            parent = await self._store.load_record(s.target_skill_ids[0])
        parent_content = parent.content if parent else "(no parent)"

        prompt = SKILL_EVOLVE_DERIVED_PROMPT.format(
            parent_content=parent_content,
            direction=s.direction,
        )
        content = await self._run_evolution_loop(prompt)
        if not content:
            return None

        skill_id = f"sk-{uuid.uuid4().hex[:12]}"
        gen = (parent.lineage.generation + 1) if parent else 0
        return SkillRecord(
            skill_id=skill_id, name=s.direction or "derived-skill",
            description=f"Derived: {s.direction}",
            content=content, category=s.category,
            status=SkillStatus.CANDIDATE,
            visibility=SkillVisibility.PRIVATE,
            lineage=SkillLineage(
                origin=SkillOrigin.DERIVED,
                generation=gen,
                parent_skill_ids=s.target_skill_ids,
                source_memory_ids=s.source_memory_ids,
                created_by="skill-evolver",
            ),
            tenant_id=tid, user_id=uid,
            uri=make_skill_uri(tid, uid, skill_id),
            abstract=s.direction or "",
        )

    async def _evolve_fix(self, s, tid, uid) -> Optional[SkillRecord]:
        if not s.target_skill_ids:
            return None
        parent = await self._store.load_record(s.target_skill_ids[0])
        if not parent:
            return None

        prompt = SKILL_EVOLVE_FIX_PROMPT.format(
            current_content=parent.content,
            direction=s.direction,
        )
        content = await self._run_evolution_loop(prompt)
        if not content:
            return None

        skill_id = f"sk-{uuid.uuid4().hex[:12]}"
        return SkillRecord(
            skill_id=skill_id, name=parent.name,
            description=parent.description,
            content=content, category=parent.category,
            status=SkillStatus.CANDIDATE,
            visibility=parent.visibility,
            lineage=SkillLineage(
                origin=SkillOrigin.FIXED,
                generation=parent.lineage.generation + 1,
                parent_skill_ids=[parent.skill_id],
                created_by="skill-evolver",
                change_summary=s.direction,
            ),
            tenant_id=tid, user_id=uid,
            uri=make_skill_uri(tid, uid, skill_id),
            abstract=parent.abstract,
            tags=parent.tags,
        )

    async def _run_evolution_loop(self, initial_prompt: str) -> Optional[str]:
        """LLM evolution loop: max 5 iterations, termination tokens."""
        messages = [{"role": "user", "content": initial_prompt}]

        for i in range(MAX_ITERATIONS):
            try:
                response = await self._llm.complete(messages)
            except Exception as exc:
                logger.warning("[SkillEvolver] LLM failed iteration %d: %s", i, exc)
                return None

            if EVOLUTION_COMPLETE in response:
                return response.replace(EVOLUTION_COMPLETE, "").strip()
            if EVOLUTION_FAILED in response:
                logger.info("[SkillEvolver] Evolution failed: %s", response)
                return None

            messages.append({"role": "assistant", "content": response})
            if i < MAX_ITERATIONS - 1:
                messages.append({"role": "user", "content":
                    f"Iteration {i+1}/{MAX_ITERATIONS}. "
                    f"End with {EVOLUTION_COMPLETE} or {EVOLUTION_FAILED}."
                })

        return None
```

- [ ] **Step 5: Run tests**

Run: `uv run python3 -m unittest tests.skill_engine.test_analyzer tests.skill_engine.test_evolver -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/skill_engine/analyzer.py src/opencortex/skill_engine/evolver.py tests/skill_engine/test_analyzer.py tests/skill_engine/test_evolver.py
git commit -m "feat(skill_engine): add SkillAnalyzer + SkillEvolver with LLM extraction/evolution"
```

---

### Task 7: Wire extract + evolve into SkillManager

**Files:**
- Modify: `src/opencortex/skill_engine/skill_manager.py`
- Modify: `src/opencortex/skill_engine/http_routes.py`
- Modify: `src/opencortex/orchestrator.py` (_init_skill_engine)

- [ ] **Step 1: Add extraction pipeline to SkillManager**

Edit `src/opencortex/skill_engine/skill_manager.py`. Add imports and extend __init__:

```python
from opencortex.skill_engine.types import SkillRecord, SkillStatus, EvolutionSuggestion


class SkillManager:
    def __init__(self, store, analyzer=None, evolver=None):
        self._store = store
        self._analyzer = analyzer
        self._evolver = evolver

    # --- Extraction pipeline ---

    async def extract(self, tenant_id: str, user_id: str,
                      **filters) -> List[SkillRecord]:
        """Full pipeline: scan → analyze → evolve → save candidates."""
        if not self._analyzer or not self._evolver:
            return []

        suggestions = await self._analyzer.extract_candidates(
            tenant_id, user_id, **filters,
        )
        if not suggestions:
            return []

        candidates = await self._evolver.process_suggestions(
            suggestions, tenant_id, user_id,
        )

        saved = []
        for c in candidates:
            await self._store.save_record(c)
            saved.append(c)

        return saved

    # --- Manual evolution ---

    async def fix_skill(self, skill_id: str, tenant_id: str, user_id: str,
                        direction: str) -> Optional[SkillRecord]:
        if not self._evolver:
            return None
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.FIXED,
            target_skill_ids=[skill_id],
            category=SkillCategory.WORKFLOW,
            direction=direction,
        )
        result = await self._evolver.evolve(suggestion, tenant_id, user_id)
        if result:
            await self._store.save_record(result)
        return result

    async def derive_skill(self, skill_id: str, tenant_id: str, user_id: str,
                           direction: str) -> Optional[SkillRecord]:
        if not self._evolver:
            return None
        suggestion = EvolutionSuggestion(
            evolution_type=SkillOrigin.DERIVED,
            target_skill_ids=[skill_id],
            category=SkillCategory.WORKFLOW,
            direction=direction,
        )
        result = await self._evolver.evolve(suggestion, tenant_id, user_id)
        if result:
            await self._store.save_record(result)
        return result
```

Also add the missing imports at the top:

```python
from opencortex.skill_engine.types import (
    SkillRecord, SkillStatus, SkillOrigin, SkillCategory, EvolutionSuggestion,
)
```

- [ ] **Step 2: Add extract/fix/derive HTTP routes**

Add to `src/opencortex/skill_engine/http_routes.py`:

```python
@router.post("/extract")
async def extract_skills(context_types: Optional[str] = None,
                         categories: Optional[str] = None):
    """Trigger skill extraction from memories."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    ct = context_types.split(",") if context_types else None
    cats = categories.split(",") if categories else None
    results = await mgr.extract(tid, uid, context_types=ct, categories=cats)
    return {"extracted": [r.to_dict() for r in results], "count": len(results)}


@router.post("/{skill_id}/fix")
async def fix_skill(skill_id: str, direction: str = ""):
    """Trigger FIX evolution → new CANDIDATE."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    r = await mgr.fix_skill(skill_id, tid, uid, direction)
    if not r:
        raise HTTPException(status_code=400, detail="Fix evolution failed")
    return r.to_dict()


@router.post("/{skill_id}/derive")
async def derive_skill(skill_id: str, direction: str = ""):
    """Trigger DERIVED evolution → new CANDIDATE."""
    mgr = _get_manager()
    tid, uid = get_effective_identity()
    r = await mgr.derive_skill(skill_id, tid, uid, direction)
    if not r:
        raise HTTPException(status_code=400, detail="Derive evolution failed")
    return r.to_dict()
```

**IMPORTANT**: The `extract` route must be defined BEFORE the `/{skill_id}` route to avoid FastAPI treating "extract" as a skill_id. Move it above the `get_skill` route.

- [ ] **Step 3: Wire analyzer + evolver into orchestrator init**

Update `_init_skill_engine()` in `src/opencortex/orchestrator.py`:

```python
    async def _init_skill_engine(self) -> None:
        """Initialize Skill Engine if storage and embedder are available."""
        if not self._storage or not self._embedder:
            return
        try:
            from opencortex.skill_engine.adapters.storage_adapter import SkillStorageAdapter
            from opencortex.skill_engine.store import SkillStore
            from opencortex.skill_engine.analyzer import SkillAnalyzer
            from opencortex.skill_engine.evolver import SkillEvolver
            from opencortex.skill_engine.skill_manager import SkillManager
            from opencortex.skill_engine.http_routes import set_skill_manager

            storage_adapter = SkillStorageAdapter(
                storage=self._storage,
                embedder=self._embedder,
                embedding_dim=self._config.embedding_dimension,
            )
            await storage_adapter.initialize()

            store = SkillStore(storage_adapter)

            # Analyzer needs source adapter (reads from memory collections)
            # For now, analyzer and evolver require LLM
            analyzer = None
            evolver = None
            if self._llm_completion:
                evolver = SkillEvolver(llm=self._llm_completion, store=store)
                # Source adapter will be implemented when memory scanning is needed
                # For now, SkillManager works without analyzer (search/approve/list only)

            self._skill_manager = SkillManager(
                store=store, analyzer=analyzer, evolver=evolver,
            )
            set_skill_manager(self._skill_manager)

            logger.info("[MemoryOrchestrator] Skill Engine initialized")
        except Exception as exc:
            logger.info("[MemoryOrchestrator] Skill Engine not available: %s", exc)
```

- [ ] **Step 4: Run all skill engine tests**

Run: `uv run python3 -m unittest discover -s tests/skill_engine -v`
Expected: All PASS

- [ ] **Step 5: Run regression**

Run: `uv run python3 -m unittest tests.test_alpha_types tests.test_alpha_config tests.test_context_manager -v`
Expected: All PASS

- [ ] **Step 6: Commit**

```bash
git add src/opencortex/skill_engine/skill_manager.py src/opencortex/skill_engine/http_routes.py src/opencortex/orchestrator.py
git commit -m "feat(skill_engine): wire extract + evolve into SkillManager + HTTP routes"
```

---

### Task 8: Final Verification

**Files:** None (test-only)

- [ ] **Step 1: Run complete skill engine test suite**

Run: `uv run python3 -m unittest discover -s tests/skill_engine -v`
Expected: All PASS

- [ ] **Step 2: Run full regression**

Run: `uv run python3 -m unittest tests.test_alpha_types tests.test_alpha_config tests.test_alpha_knowledge_store tests.test_alpha_sandbox_integration tests.test_knowledge_store tests.test_qdrant_adapter tests.test_context_manager -v`
Expected: All PASS

- [ ] **Step 3: Verify module independence**

Run: `grep -r "from opencortex.alpha\|from opencortex.context\|from opencortex.storage\|from opencortex.retrieve\|from opencortex.ingest" src/opencortex/skill_engine/ --include="*.py" | grep -v adapters/`
Expected: No output (zero imports from memory system in core files)

Note: `adapters/storage_adapter.py` importing from `opencortex.storage.collection_schemas` is expected — adapters are the bridge layer.

- [ ] **Step 4: Verify spec coverage**

Manually check each spec section:
- §4 Data Model → Task 1 (types.py)
- §5 Visibility & Scope → Task 2 (storage_adapter visibility filter)
- §6 Adapters → Task 2 (all adapter files)
- §7 Core Components → Tasks 3, 5, 6 (store, analyzer, evolver, prompts)
- §8 Integration → Task 4 (skill_manager, http_routes, orchestrator)
- §9 Storage Schema → Task 2 (collection_schemas)
- §10 Data Flow → Tasks 6, 7 (extraction pipeline)
- §11 SkillHub Frontend → Deferred (separate plan)
- §12 Directory Structure → Verified by file listing
- §13 Non-Goals → N/A

**Not implemented in this plan** (deferred):
- SourceAdapter Qdrant implementation (needs memory collection read access — separate task)
- patch.py (port from OpenSpace — can be done when FIX evolution needs it)
- ranker.py (BM25 + embedding — can be added when skill count warrants it)
- SkillHub frontend (separate plan)
