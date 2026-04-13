# Skill Engine Design — OpenSpace-Aligned Extraction from Memories

**Date**: 2026-04-03
**Status**: Draft (rev.2 — Codex adversarial review fixes)
**Author**: Hugo + Claude

---

## 1. Problem

OpenCortex stores memories but doesn't extract reusable operational knowledge from them. Users accumulate hundreds of memories describing workflows, debugging patterns, and deployment procedures, but these remain passive facts rather than actionable skills that can guide future agent behavior.

OpenSpace solves a similar problem through its Skill Engine — a self-evolving system that extracts, refines, and versions operational skills. We adopt this mechanism with OpenCortex memories as the data source, keeping the skill system **completely independent** from the existing memory system.

## 2. Design Decisions (Confirmed)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Extraction target | Operational skills (workflows, procedures, patterns) | Direct value for agent execution |
| Data source | All stored memories (memory + resource + knowledge) | Richest signal source |
| Consumer | recall() injection + SkillHub frontend | Both agents and humans benefit |
| Automation | Semi-automatic: auto-extract candidates, human approve | Risk-controlled |
| Storage | Independent `skills` Qdrant collection | Zero coupling with memory system |
| Architecture | Mirror OpenSpace interfaces + adapter pattern | Easy to track OpenSpace updates |
| Conflict resolution | Conditional branch merge for divergent steps | Preserves all evidence |
| Independence | Zero imports from/to memory system code | Not invasive |

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    skill_engine/ (standalone module)            │
│                                                                │
│  ┌──────────── Core (mirrors OpenSpace) ──────────────────┐   │
│  │  types.py       SkillRecord, SkillLineage, enums       │   │
│  │  store.py       SkillStore → StorageAdapter             │   │
│  │  analyzer.py    SkillAnalyzer (memories → suggestions)  │   │
│  │  evolver.py     SkillEvolver (CAPTURED/DERIVED/FIX)     │   │
│  │  ranker.py      SkillRanker (BM25 + embedding)          │   │
│  │  patch.py       Patch application (ported from OpenSpace)│   │
│  │  prompts.py     All LLM prompts                         │   │
│  │  memory_formatter.py  Format memory clusters for LLM    │   │
│  └────────────────────────────────────────────────────────┘   │
│                                                                │
│  ┌──────────── Adapters (OpenCortex bridge) ──────────────┐   │
│  │  source_adapter.py      Read memories (read-only)       │   │
│  │  storage_adapter.py     Write to skills Qdrant collection│  │
│  │  embedding_adapter.py   Delegate to local e5-large       │  │
│  │  llm_adapter.py         Delegate to OpenCortex LLM      │   │
│  └────────────────────────────────────────────────────────┘   │
│                                                                │
│  ┌──────────── Integration (thin glue) ───────────────────┐   │
│  │  skill_manager.py       Top-level API                   │   │
│  │  http_routes.py         REST API for SkillHub           │   │
│  └────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────┘

        只读 ↓                                    检索 ↓
┌─────────────────┐                    ┌──────────────────────┐
│ memories (Qdrant)│                    │ Orchestrator.recall() │
│ (完全不修改)      │                    │ ↳ search skills coll  │
│                  │                    │ ↳ ContextType.SKILL   │
└─────────────────┘                    └──────────────────────┘
```

### 3.1 Module Boundary Rules

- `skill_engine/` has **zero imports** from `opencortex.alpha`, `opencortex.context`, `opencortex.storage`, `opencortex.retrieve`, `opencortex.ingest`.
- All OpenCortex dependencies injected via adapter interfaces at initialization.
- The existing codebase has **zero imports** from `skill_engine`, except two thin touch points:
  - `orchestrator.py` — instantiates `SkillManager`, calls `skill_search()` during recall
  - `http/server.py` — mounts skill HTTP routes

### 3.2 OpenSpace Alignment Map

When OpenSpace updates, use this mapping to locate the corresponding file:

| OpenSpace File | OpenCortex File | Alignment |
|---------------|-----------------|-----------|
| `skill_engine/types.py` | `skill_engine/types.py` | 1:1 mirrored types |
| `skill_engine/store.py` | `skill_engine/store.py` | Same interface, StorageAdapter impl |
| `skill_engine/analyzer.py` | `skill_engine/analyzer.py` | Interface mirrored, data source adapted |
| `skill_engine/evolver.py` | `skill_engine/evolver.py` | 1:1 mirrored logic |
| `skill_engine/skill_ranker.py` | `skill_engine/ranker.py` | Same interface, local embedding |
| `skill_engine/patch.py` | `skill_engine/patch.py` | Direct port |
| `skill_engine/conversation_formatter.py` | `skill_engine/memory_formatter.py` | Adapted: formats memories not conversations |
| `tool_layer.py` (OpenSpace class) | `skill_engine/skill_manager.py` | Top-level API equivalent |

**Update procedure**: `git diff` OpenSpace's `skill_engine/` → map via table → apply changes to core, keep adapter impl.

## 4. Data Model (types.py)

### 4.1 Enums

```python
class SkillOrigin(str, Enum):
    IMPORTED = "imported"     # User-provided or external
    CAPTURED = "captured"     # Extracted from memory patterns
    DERIVED = "derived"       # Enhanced from existing skill
    FIXED = "fixed"           # Repaired version

class SkillCategory(str, Enum):
    WORKFLOW = "workflow"       # End-to-end procedure
    TOOL_GUIDE = "tool_guide"  # How to use a specific tool
    PATTERN = "pattern"        # Reusable behavioral pattern

class SkillVisibility(str, Enum):
    PRIVATE = "private"       # Only visible to owning user
    SHARED = "shared"         # Visible to all users in tenant

class SkillStatus(str, Enum):
    CANDIDATE = "candidate"   # Extracted, awaiting approval
    ACTIVE = "active"         # Approved, searchable in recall
    DEPRECATED = "deprecated" # Superseded or rejected
```

### 4.2 SkillRecord

```python
@dataclass
class SkillLineage:
    origin: SkillOrigin
    generation: int = 0
    parent_skill_ids: List[str] = field(default_factory=list)
    source_memory_ids: List[str] = field(default_factory=list)
    change_summary: str = ""
    content_diff: str = ""
    content_snapshot: Dict[str, str] = field(default_factory=dict)
    created_by: str = ""    # LLM model name
    created_at: str = ""

@dataclass
class SkillRecord:
    skill_id: str
    name: str
    description: str
    content: str              # Skill body (Markdown instructions)
    category: SkillCategory
    status: SkillStatus = SkillStatus.CANDIDATE
    visibility: SkillVisibility = SkillVisibility.PRIVATE
    lineage: SkillLineage = field(default_factory=SkillLineage)
    tags: List[str] = field(default_factory=list)
    tenant_id: str = ""
    user_id: str = ""
    # Stable URI for recall integration (see §4.5)
    uri: str = ""
    # Quality metrics (mirrors OpenSpace)
    total_selections: int = 0
    total_applied: int = 0
    total_completions: int = 0
    total_fallbacks: int = 0
    # CortexFS-style layers
    abstract: str = ""        # L0: one-line summary
    overview: str = ""        # L1: structured overview
    # Timestamps
    created_at: str = ""
    updated_at: str = ""
    # Extraction fingerprint for idempotency (see §4.6)
    source_fingerprint: str = ""
```

### 4.3 Evolution Types

```python
@dataclass
class EvolutionSuggestion:
    evolution_type: SkillOrigin  # CAPTURED | DERIVED | FIXED
    target_skill_ids: List[str]  # Empty for CAPTURED
    category: SkillCategory
    direction: str               # What to do
    confidence: float = 0.0
    source_memory_ids: List[str] = field(default_factory=list)
```

### 4.4 Action Steps with Conditional Branches

For conflicting steps from different users/memories:

```python
# Plain step: "build project"
# Conditional step:
{
    "step": "发布策略",
    "type": "conditional",
    "branches": [
        {
            "condition": "常规发布",
            "action": "deploy to staging",
            "evidence": {"memory_count": 3, "user_count": 1, "ratio": 0.37}
        },
        {
            "condition": "高风险变更",
            "action": "canary release",
            "evidence": {"memory_count": 5, "user_count": 1, "ratio": 0.63}
        }
    ]
}
```

`SkillRecord.content` stores Markdown with these branches rendered as sub-lists.

### 4.5 Skill URI Scheme

Every skill has a stable URI for integration with the existing `MatchedContext` / `FindResult` recall contract:

```
opencortex://{tenant_id}/{user_id}/skills/{skill_id}
```

**Why**: `FindResult.skills` is `List[MatchedContext]`, and `MatchedContext` requires a `uri` field. Without it, skills cannot participate in recall results, access-stat updates, or citation tracking.

The URI is generated at creation time (`SkillStore.save_record`) and stored in the `uri` payload field. It is immutable — FIX evolution creates a new skill_id, thus a new URI.

### 4.6 Extraction Idempotency

To prevent duplicate candidates when `/api/v1/skills/extract` is called repeatedly on the same corpus:

**Source fingerprint**: A deterministic hash derived from sorted `source_memory_ids` of the cluster:

```python
source_fingerprint = hashlib.sha256(
    "|".join(sorted(cluster.memory_ids)).encode()
).hexdigest()[:16]
```

**Dedup rule**: Before creating a new CAPTURED suggestion, the analyzer checks for any existing skill (CANDIDATE **or** ACTIVE) with the same `source_fingerprint`. If found, skip.

This ensures:
- Retrying extraction before approval doesn't create duplicates
- Retrying extraction after approval doesn't create duplicates
- Different memory clusters produce different fingerprints

### 4.7 FIX Evolution — Version-Based, Not In-Place

FIX does **not** modify an ACTIVE skill directly. Instead:

1. FIX creates a **new SkillRecord** with `status=CANDIDATE`, linked to the ACTIVE parent via `lineage.parent_skill_ids`
2. The new candidate appears in SkillHub for review alongside a diff against the parent
3. On approval: new skill becomes ACTIVE, parent becomes DEPRECATED
4. On rejection: candidate is DEPRECATED, parent stays ACTIVE

This ensures:
- Agent-visible behavior never changes without human review
- Clean rollback: if the fix is bad, the parent is still ACTIVE
- Full version history preserved via lineage DAG

## 5. Visibility & Scope Model

### 5.1 Rules

| Visibility | Who can see in recall | Who can see in SkillHub |
|------------|----------------------|------------------------|
| `PRIVATE` | Only the owning `user_id` | Only the owning `user_id` |
| `SHARED` | All users in `tenant_id` | All users in `tenant_id` |

**Default**: New skills extracted from user memories are `PRIVATE`. A user or admin can promote to `SHARED`.

### 5.2 Filter Semantics

All search/list APIs enforce visibility:

```python
# Storage filter for skill queries
scope_filter = {"op": "or", "conds": [
    # SHARED skills: visible to all in tenant
    {"op": "and", "conds": [
        {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
        {"op": "must", "field": "visibility", "conds": ["shared"]},
    ]},
    # PRIVATE skills: only visible to owner
    {"op": "and", "conds": [
        {"op": "must", "field": "tenant_id", "conds": [tenant_id]},
        {"op": "must", "field": "visibility", "conds": ["private"]},
        {"op": "must", "field": "user_id", "conds": [user_id]},
    ]},
]}
```

### 5.3 API Signatures

**Every** search/list/modify API takes `tenant_id` and `user_id`:

```python
# SkillManager
async def search(self, query, tenant_id, user_id, top_k=5) -> List[SkillRecord]
async def list_skills(self, tenant_id, user_id, status=None) -> List[SkillRecord]
async def get_skill(self, skill_id, tenant_id, user_id) -> Optional[SkillRecord]
async def approve(self, skill_id, tenant_id, user_id) -> None
async def reject(self, skill_id, tenant_id, user_id) -> None

# Orchestrator integration
skill_results = await self._skill_manager.search(query, tid, uid, top_k=3)
```

HTTP routes derive `tenant_id`/`user_id` from JWT via `get_effective_identity()`, same as all other routes.

## 6. Adapter Interfaces

### 6.1 SourceAdapter — Read from memories (read-only)

```python
class SourceAdapter(Protocol):
    async def scan_memories(
        self, tenant_id: str, user_id: str,
        context_types: List[str] | None = None,
        categories: List[str] | None = None,
        min_count: int = 3,
    ) -> List[MemoryCluster]: ...

    async def get_cluster_memories(
        self, cluster: MemoryCluster,
    ) -> List[MemoryRecord]: ...

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
    meta: Dict[str, Any]
```

**Implementation**: Reads from existing memory Qdrant collections via `QdrantStorageAdapter`. Greedy clustering by cosine similarity (threshold >= 0.75). Skips clusters with < `min_count` memories.

### 6.2 StorageAdapter — Write to skills collection

```python
class StorageAdapter(Protocol):
    async def initialize(self) -> None: ...
    async def save(self, record: SkillRecord) -> None: ...
    async def load(self, skill_id: str) -> Optional[SkillRecord]: ...
    async def load_all(self, tenant_id: str, user_id: str,
                       status: Optional[SkillStatus] = None) -> List[SkillRecord]: ...
    async def update_status(self, skill_id: str, status: SkillStatus) -> None: ...
    async def update_metrics(self, skill_id: str, **counters) -> None: ...
    async def search(self, query: str, tenant_id: str, user_id: str,
                     top_k: int = 5,
                     status: Optional[SkillStatus] = None) -> List[SkillRecord]: ...
    async def find_by_fingerprint(self, fingerprint: str) -> Optional[SkillRecord]: ...
    async def delete(self, skill_id: str) -> None: ...
```

**Implementation**: Independent `skills` Qdrant collection with correct filter DSL (`conds`/`op:must`) and visibility filtering.

### 6.3 EmbeddingAdapter + LLMAdapter

```python
class EmbeddingAdapter(Protocol):
    def embed(self, text: str) -> List[float]: ...
    def embed_batch(self, texts: List[str]) -> List[List[float]]: ...

class LLMAdapter(Protocol):
    async def complete(self, messages: List[Dict], **kwargs) -> str: ...
```

**Implementation**: Delegate to existing OpenCortex embedder (local e5-large) and LLM client.

## 7. Core Components

### 7.1 SkillStore

Mirrors OpenSpace `SkillStore` interface, delegates to `StorageAdapter`:

```python
class SkillStore:
    async def save_record(self, record: SkillRecord) -> None
    async def load_record(self, skill_id: str) -> Optional[SkillRecord]
    async def load_active(self, tenant_id: str, user_id: str) -> List[SkillRecord]
    async def load_by_status(self, tenant_id: str, user_id: str,
                              status: SkillStatus) -> List[SkillRecord]
    async def activate(self, skill_id: str) -> None
    async def deprecate(self, skill_id: str) -> None
    async def evolve_skill(self, new: SkillRecord, parent_ids: List[str]) -> None
    async def record_selection(self, skill_id: str) -> None
    async def record_application(self, skill_id: str, completed: bool) -> None
    async def search(self, query: str, tenant_id: str, user_id: str,
                     top_k: int = 5) -> List[SkillRecord]
    async def find_by_fingerprint(self, fingerprint: str) -> Optional[SkillRecord]
```

### 7.2 SkillAnalyzer — Core divergence from OpenSpace

Instead of analyzing execution recordings, it analyzes **memory clusters**.

```python
class SkillAnalyzer:
    async def extract_candidates(
        self, tenant_id: str, user_id: str, **filters,
    ) -> List[EvolutionSuggestion]:
        """1. source.scan_memories() → clusters
           2. Per cluster: compute source_fingerprint, check dedup
           3. Per new cluster: LLM analysis → EvolutionSuggestion[]
           4. Dedup against CANDIDATE + ACTIVE skills
        """

    async def analyze_cluster(
        self, cluster: MemoryCluster,
    ) -> Optional[List[EvolutionSuggestion]]:
        """LLM receives: cluster memories + existing skills (all statuses)
           LLM returns: operational patterns found (CAPTURED/DERIVED)
        """
```

**Prompt design**: LLM is instructed to:
1. Read memory cluster content
2. Identify repeated operational patterns (workflows, debugging steps, deployment procedures)
3. Classify: WORKFLOW / TOOL_GUIDE / PATTERN
4. Rate confidence (0-1)
5. Generate structured skill content (name, description, steps, preconditions)
6. **Conflict handling**: For divergent steps, produce conditional branches with evidence counts
7. Compare against existing skills (CANDIDATE + ACTIVE) to avoid duplication

### 7.3 SkillEvolver — Mirrors OpenSpace

Three evolution types:

| Type | Trigger | Behavior |
|------|---------|----------|
| **CAPTURED** | New pattern found in memories | Brand-new skill, no parents, status=CANDIDATE |
| **DERIVED** | Enhancement of existing skill | New skill, links to parent(s), status=CANDIDATE |
| **FIX** | Skill outdated or broken | New CANDIDATE version linked to ACTIVE parent; parent stays ACTIVE until new version approved (see §4.7) |

Evolution loop mirrors OpenSpace:
- Max 5 LLM iterations per evolution
- Termination tokens: `<EVOLUTION_COMPLETE>` / `<EVOLUTION_FAILED>`
- Apply-retry cycle: max 3 attempts with error feedback
- Concurrency: `asyncio.Semaphore(3)` for parallel evolutions

### 7.4 SkillRanker

Hybrid search using local models:

```python
class SkillRanker:
    async def rank(
        self, query: str, candidates: List[SkillRecord], top_k: int = 5,
    ) -> List[SkillRecord]:
        """BM25 + embedding (local e5-large). No API calls."""
```

### 7.5 patch.py — Direct Port

Ported from OpenSpace's `openspace/skill_engine/patch.py`. Pure string processing:
- `detect_patch_type(content) -> PatchType`
- `parse_multi_file_full(content) -> Dict[str, str]`
- `parse_patch(patch_text) -> PatchResult`
- `apply_search_replace(patch, original) -> (new, count, error)`

4-level fuzzy anchor matching (exact -> rstrip -> strip -> unicode normalize).

## 8. Integration

### 8.1 SkillManager — Top-Level API

```python
class SkillManager:
    def __init__(self, source, storage, embedding, llm): ...

    # Extraction pipeline
    async def extract(self, tenant_id, user_id, **filters) -> List[SkillRecord]

    # Approval workflow
    async def approve(self, skill_id, tenant_id, user_id) -> None
    async def reject(self, skill_id, tenant_id, user_id) -> None
    async def deprecate(self, skill_id, tenant_id, user_id) -> None
    async def promote(self, skill_id, tenant_id, user_id) -> None  # PRIVATE→SHARED

    # Search (for recall integration)
    async def search(self, query, tenant_id, user_id, top_k=5) -> List[SkillRecord]

    # Listing (for frontend)
    async def list_skills(self, tenant_id, user_id, status=None) -> List[SkillRecord]
    async def get_skill(self, skill_id, tenant_id, user_id) -> Optional[SkillRecord]

    # Manual evolution
    async def fix_skill(self, skill_id, tenant_id, user_id, direction) -> Optional[SkillRecord]
    async def derive_skill(self, skill_id, tenant_id, user_id, direction) -> Optional[SkillRecord]
```

### 8.2 Orchestrator Integration (2 touch points)

```python
# orchestrator.py — lazy init
self._skill_manager: Optional[SkillManager] = None

# orchestrator.py — in search(), after memory/resource search
if self._skill_manager:
    tid, uid = get_effective_identity()
    skill_results = await self._skill_manager.search(query, tid, uid, top_k=3)
    # Convert SkillRecord → MatchedContext for FindResult.skills
    for sr in skill_results:
        find_result.skills.append(MatchedContext(
            uri=sr.uri,
            context_type=ContextType.SKILL,
            is_leaf=True,
            abstract=sr.abstract,
            overview=sr.overview,
            content=sr.content,
            category=sr.category.value,
            score=0.0,  # Ranked by SkillRanker, not by retriever score
        ))
```

**Why MatchedContext**: The existing recall contract serializes `FindResult.skills` as `List[MatchedContext]` with `uri` required. By converting `SkillRecord` → `MatchedContext` at the orchestrator boundary, we avoid modifying the retrieval types and keep the skill engine independent.

### 8.3 HTTP Routes

All routes derive `tenant_id`/`user_id` from JWT via `get_effective_identity()`.

```
POST /api/v1/skills/extract          # Trigger extraction
GET  /api/v1/skills                  # List skills (filterable by status)
GET  /api/v1/skills/:id              # Skill detail + lineage
POST /api/v1/skills/:id/approve      # Approve candidate → ACTIVE
POST /api/v1/skills/:id/reject       # Reject candidate → DEPRECATED
POST /api/v1/skills/:id/deprecate    # Deprecate active skill
POST /api/v1/skills/:id/promote      # PRIVATE → SHARED
POST /api/v1/skills/:id/fix          # Trigger FIX evolution → new CANDIDATE
POST /api/v1/skills/:id/derive       # Trigger DERIVED evolution → new CANDIDATE
GET  /api/v1/skills/search?q=...     # Search skills (visibility-filtered)
```

## 9. Storage Schema

```
Collection: "skills" (independent from all existing collections)

Vectors:
  dense:   1024 dims, COSINE (matches e5-large)
  sparse:  BM25 (matches existing sparse setup)

Payload (keyword indexed):
  skill_id          str
  name              str (text indexed)
  description       str (text indexed)
  content           str (text indexed)
  abstract          str (text indexed)
  overview          str
  uri               str (keyword indexed)
  category          str (keyword indexed)
  status            str (keyword indexed)
  visibility        str (keyword indexed)   # "private" | "shared"
  tenant_id         str (keyword indexed)
  user_id           str (keyword indexed)
  tags              List[str] (keyword indexed)
  source_fingerprint str (keyword indexed)  # For extraction idempotency
  lineage           Dict (JSON, not indexed)
  total_selections, total_applied, total_completions, total_fallbacks  int
  reward_score      float
  accessed_at       str (ISO datetime)
  active_count      int
  created_at        str (ISO datetime)
  updated_at        str (ISO datetime)
```

## 10. Data Flow

### 10.1 Extraction Pipeline

```
POST /api/v1/skills/extract
    │
    ▼
SourceAdapter.scan_memories(tid, uid)    ← read-only from memory Qdrant
    ├─ Filter by tenant_id, user_id, context_type, category
    ├─ Fetch embeddings, greedy cluster (cosine >= 0.75)
    ├─ Skip clusters < min_count (3)
    └─ Return: List[MemoryCluster]
    │
    ▼
SkillAnalyzer — per cluster:
    ├─ Compute source_fingerprint (sorted memory_ids hash)
    ├─ Check store: existing skill with same fingerprint? → skip
    ├─ Fetch full memory content
    ├─ Load existing skills (CANDIDATE + ACTIVE, for dedup)
    ├─ LLM: identify patterns, classify, handle conflicts
    └─ Return: EvolutionSuggestion[]
    │
    ▼
SkillEvolver.evolve(suggestion)          ← per suggestion, LLM generation
    ├─ CAPTURED: generate full skill from memory patterns
    ├─ DERIVED: enhance existing skill with new evidence
    ├─ Generate uri: opencortex://{tid}/{uid}/skills/{skill_id}
    ├─ Set source_fingerprint from cluster
    └─ Return: SkillRecord (status=CANDIDATE, visibility=PRIVATE)
    │
    ▼
SkillStore.save_record()                 ← write to skills Qdrant collection
    │
    ▼
SkillHub frontend                        ← human reviews candidates
    ├─ Approve → status=ACTIVE (searchable in recall)
    ├─ Reject → status=DEPRECATED
    └─ Edit → modify content, re-save
```

### 10.2 FIX Evolution Flow

```
POST /api/v1/skills/:id/fix  (direction="修复过时的步骤3")
    │
    ▼
SkillEvolver._evolve_fix(parent_skill, direction)
    ├─ Load parent skill content
    ├─ LLM generates fix (max 5 iterations)
    ├─ Create NEW SkillRecord:
    │     status=CANDIDATE
    │     lineage.origin=FIXED
    │     lineage.parent_skill_ids=[parent.skill_id]
    │     lineage.content_diff=unified_diff
    └─ Return: new CANDIDATE skill
    │
    ▼
SkillHub: reviewer sees diff (parent vs candidate)
    ├─ Approve → new=ACTIVE, parent=DEPRECATED (atomic swap)
    └─ Reject → candidate=DEPRECATED, parent stays ACTIVE
```

### 10.3 Recall Integration

```
Agent calls recall(query)
    │
    ▼
Orchestrator.search()
    ├─ Existing: search memories, resources, knowledge (unchanged)
    ├─ New: skill_manager.search(query, tid, uid, top_k=3)
    │    ├─ StorageAdapter: search skills collection
    │    ├─ Filter: status=ACTIVE + visibility scope
    │    └─ Return: List[SkillRecord]
    ├─ Convert SkillRecord → MatchedContext (uri, abstract, content)
    └─ Merge into FindResult.skills (ContextType.SKILL)
```

## 11. SkillHub Frontend

New route group under existing Console SPA:

```
/console/skills                 # Skill list (tabs: Candidates | Active | All)
/console/skills/:id             # Skill detail
  ├─ Content preview (Markdown rendered)
  ├─ Lineage (parent → current → children)
  ├─ Version diff (if evolved, especially for FIX candidates)
  ├─ Quality metrics (selections, applied, completion rates)
  ├─ Source memories (links to originating memories)
  ├─ Visibility badge (PRIVATE / SHARED)
  └─ Actions: Approve / Reject / Edit / Fix / Derive / Promote
/console/skills/extract         # Extraction trigger
  ├─ Filter options (context_type, category)
  └─ Run extraction button
```

## 12. Directory Structure

```
src/opencortex/skill_engine/
├── __init__.py
├── types.py                    # SkillRecord, SkillLineage, enums
├── store.py                    # SkillStore (delegates to StorageAdapter)
├── analyzer.py                 # SkillAnalyzer (memory clusters → suggestions)
├── evolver.py                  # SkillEvolver (CAPTURED/DERIVED/FIX)
├── ranker.py                   # SkillRanker (BM25 + embedding)
├── patch.py                    # Patch application (ported from OpenSpace)
├── prompts.py                  # All LLM prompts
├── memory_formatter.py         # Format memory clusters for LLM context
├── skill_manager.py            # Top-level API
├── http_routes.py              # REST API routes
└── adapters/
    ├── __init__.py
    ├── source_adapter.py       # Read from memories Qdrant (read-only)
    ├── storage_adapter.py      # Write to skills Qdrant collection
    ├── embedding_adapter.py    # Delegate to local e5-large
    └── llm_adapter.py          # Delegate to OpenCortex LLM client
```

## 13. Non-Goals

1. **Cloud sharing** — No open-space.cloud equivalent. Local-only.
2. **Execution backends** — No Shell/GUI/MCP grounding. Skills are knowledge, not executors.
3. **Automatic activation** — All candidates require human approval via SkillHub.
4. **Modifying existing code** — Zero changes to memory/knowledge/trace systems beyond the 2 orchestrator touch points.
5. **CortexFS for skills** — Skills live in Qdrant only (independence). No filesystem layer.
6. **SKILL.md files** — Skills are Qdrant records, not filesystem artifacts.
7. **Cross-tenant visibility** — Skills are tenant-scoped. No global/cross-tenant sharing.
