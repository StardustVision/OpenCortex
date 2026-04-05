# Cone Retrieval — Entity-Based Path Propagation for Memory Recall

**Date**: 2026-04-05
**Status**: Draft (rev.4 — Codex x2 + superpowers review fixes)
**Author**: Hugo + Claude
**Inspired by**: m_flow episodic bundle search

---

## 1. Problem

OpenCortex retrieves memories by **flat vector similarity** — each memory is scored independently. When a user asks "谁有孩子？", the system finds memories containing "孩子" by embedding distance, but:

- Cannot follow entity relationships (Melanie → m8 "有孩子" → m4 "为孩子做榜样")
- Returns semantically similar but factually irrelevant memories as noise
- Cannot distinguish "directly answers the question" from "vaguely related"

m_flow solves this with a four-level inverted cone (FacetPoint → Entity → Facet → Episode). We adapt the core insight — **path-cost propagation via entity co-occurrence** — while keeping OpenCortex's three-layer FS storage unchanged.

## 2. Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Storage model | Unchanged (3-layer FS + Qdrant) | No migration, no new collection |
| Entity extraction | LLM at write time, stored in payload `entities` field | Piggyback on existing LLM derive |
| Entity index | **Per-collection** in-memory Dict with full lifecycle sync | Request-scoped collection 兼容，每个 collection 独立索引 |
| Trigger | Always — cone score as a signal in score fusion | Simple, no query classifier needed |
| Scoring | `min(path_costs)` 作为**辅助信号叠加**（不替换 rerank） | m_flow 核心思想 + 保留 rerank 精度保障 |
| Direct hit penalty | L2 content direct match penalized +0.3 | Prefer precise entity-path hits over broad abstract matches |

## 3. Architecture

```
                    ┌────────────────────────┐
                    │   HierarchicalRetriever │
                    │   (existing, unchanged)  │
                    └───────────┬────────────┘
                                │ candidates[]
                                ▼
                    ┌────────────────────────┐
                    │      ConeScorer        │  ← NEW (~150 lines)
                    │                        │
                    │  1. Classify hit layer  │
                    │  2. Extract entities    │
                    │  3. Query entity index  │
                    │  4. Compute path costs  │
                    │  5. min(paths) per item │
                    └───────────┬────────────┘
                                │ rescored[]
                                ▼
                    ┌────────────────────────┐
                    │   Score Fusion          │
                    │   (modified formula)    │
                    │                        │
                    │  final = λ × cone_score │
                    │  + (1-λ) × vector_score │
                    │  + reward + hotness     │
                    └────────────────────────┘

  Entity Index (in-memory):

    ┌──────────────────────────────────────┐
    │  "Melanie"  → {m4, m8}              │
    │  "Caroline" → {m3, m4, m5, m10}     │
    │  "OpenCortex" → {m1, m2}            │
    │  "CortexFS" → {m2, m9}             │
    │  ...                                 │
    └──────────────────────────────────────┘
    Built at startup from Qdrant scroll.
    Updated on add/remove/update.
```

## 4. Components

### 4.1 Entity Extractor (Write Path)

**When**: During `orchestrator.add()`, after LLM derives abstract/overview, before Qdrant upsert.

**How**: Add entity extraction to the existing LLM derive prompt:

```
Current prompt produces: abstract, overview, keywords
New prompt also produces: entities (list of named entities)
```

**Output**: `entities: List[str]` stored in Qdrant payload alongside existing fields.

**Entity types**: Person names, system/tool names, organization names, place names. NOT generic concepts (e.g., "性能" is not an entity, "Redis" is).

**Prompt addition** (append to existing derive prompt):
```
Also extract named entities (people, systems, organizations, places).
Return as: "entities": ["entity1", "entity2"]
Only concrete named things, not abstract concepts.
```

**分块合并策略**（长文档 > 4000 tokens 时分块 derive）：

当前 `chunked_llm_derive` 对长内容分块处理。Entity 提取需要跨块合并：

```python
# 每个 chunk 独立提取 entities
chunk_entities = [derive_chunk(c)["entities"] for c in chunks]
# 合并去重（保留所有出现过的 entity）
merged_entities = list(set(e for chunk in chunk_entities for e in chunk))
# 截断到 max 20 个（避免超长列表拖慢索引）
final_entities = merged_entities[:20]
```

**已有记忆的回填**：存量记忆没有 `entities` 字段。不做主动回填 — 当记忆被编辑或 reward 更新时，借机提取。新写入的记忆自动带 entity。EntityIndex 对无 entity 的记忆返回空集，降级为纯向量评分。

### 4.2 Entity Index (Per-Collection, In-Memory)

**关键设计**：索引按 collection 分区，兼容 request-scoped collection 切换。

```python
class EntityIndex:
    """Per-collection in-memory inverted index: entity_name → set of memory IDs."""
    
    def __init__(self):
        # Keyed by collection_name → {entity → {mem_id, ...}}
        self._indexes: Dict[str, Dict[str, Set[str]]] = {}
        # Keyed by collection_name → {mem_id → {entity, ...}}
        self._reverses: Dict[str, Dict[str, Set[str]]] = {}
    
    # --- Lifecycle ---
    async def build_for_collection(self, storage, collection: str) -> None
        """Startup/lazy: scroll collection, build index for it."""
    
    def get_or_build(self, collection: str) -> Tuple[Dict, Dict]
        """Get index for collection, or return empty (lazy init)."""
    
    def add(self, collection: str, memory_id: str, entities: List[str]) -> None
        """Write path: called after upsert."""
    
    def remove(self, collection: str, memory_id: str) -> None
        """Delete path: remove single record."""
    
    def remove_batch(self, collection: str, memory_ids: List[str]) -> None
        """Recursive delete path: remove multiple records at once."""
    
    def update(self, collection: str, memory_id: str, entities: List[str]) -> None
        """Content edit path: re-derive entities, remove old + add new."""
    
    # --- Query ---
    def get_related_memories(self, collection: str, memory_id: str) -> Dict[str, Set[str]]
    def get_memories_for_entity(self, collection: str, entity: str) -> Set[str]
    def get_entities_for_memory(self, collection: str, memory_id: str) -> Set[str]
```

**Startup**: 默认 collection (`context`) 在 init 时构建。其他 collection 按需懒加载（benchmark/test 的覆盖 collection 首次搜索时自动构建）。

**生命周期同步（完整覆盖）**：

| 操作 | 同步动作 |
|------|---------|
| `add()` 单条写入 | `entity_index.add(collection, id, entities)` |
| `remove()` 单条删除 | `entity_index.remove(collection, id)` |
| `batch_delete()` 递归删除 | 先查出所有受影响 ID → `entity_index.remove_batch(collection, ids)` |
| 内容编辑（content 变更） | 重新调 LLM 提取 entities → `entity_index.update(collection, id, new_entities)` + 更新 Qdrant payload |
| 会话合并（conversation merge） | 旧 immediate 记录被删 → `remove_batch`；新合并记录 → `add` |

**Entity 归一化**：所有 entity 在写入索引时统一 lowercase。`add("OpenCortex")` → 存为 `"opencortex"`。查询匹配也用 lowercase。避免大小写不一致导致同一实体分裂为多个索引项。

**内存估算**: per-collection, 正向索引 + 反向索引各 ~6 MB（100K records × 3 entities × 20 chars），合计 **~12 MB per collection**。多 collection 场景下线性增长，benchmark 的临时 collection 在测试结束后 GC。

### 4.3 ConeScorer (Search Path)

```python
class ConeScorer:
    """Path-cost propagation scorer using entity co-occurrence edges.
    
    Stateless w.r.t. collection — collection passed per-call to support
    request-scoped collection overrides (X-Collection header).
    """
    
    DIRECT_HIT_PENALTY = 0.3
    HOP_COST = 0.05
    EDGE_MISS_COST = 0.9
    ENTITY_DEGREE_CAP = 50
    
    def __init__(self, entity_index: EntityIndex):
        self._index = entity_index
        # 不绑定 collection — 搜索时传入
    
    def score(self, candidates: List[Dict], query: str,
              collection: str) -> List[Dict]:
        """Rescore + expand candidates using entity path propagation.
        
        Args:
            candidates: 向量搜索粗召回结果
            query: 原始查询文本
            collection: 当前请求的 collection（request-scoped）
        """
```

**距离代理**：使用**原始 dense similarity `_score`**（Qdrant 返回的 cosine similarity，[0,1] 归一化），**不用 `_final_score`**。

原因：`_final_score` 跨检索路径尺度不一致 — RRF 路径产出 `1/(60+rank)` 量级，frontier 路径有 parent_score 叠加可能 > 1.0。而 `_score` 始终是 cosine similarity ∈ [0, 1]，跨路径一致。

```python
def _to_distance(candidate: Dict) -> float:
    """Convert raw dense similarity to distance. Uses _score (not _final_score)."""
    raw_score = candidate.get("_score", 0.5)  # Qdrant cosine similarity [0, 1]
    return 1.0 - min(1.0, max(0.0, raw_score))
```

**Broad match 定义**：

```python
def _is_broad_match(self, candidate: Dict, collection: str) -> bool:
    """无 entity 且非精确命中 → 宽泛匹配，加惩罚。"""
    entities = self._index.get_entities_for_memory(collection, candidate["id"])
    high_score = candidate.get("_score", 0) >= 0.9
    return len(entities) == 0 and not high_score
```

**两阶段算法：扩展 + 评分**

**Stage 1: Entity 扩展（从索引拉入新候选）**

核心改进：不仅重排已有候选，还能**召回向量搜索漏掉的记忆**。

```python
def _expand_candidates(self, candidates, query_entities, collection, storage):
    """从 entity 索引中拉入向量搜索未返回的关联记忆。"""
    existing_ids = {c["id"] for c in candidates}
    expansion_ids = set()
    
    # 从查询 entity 扩展
    for entity in query_entities:
        for mem_id in self._index.get_memories_for_entity(collection, entity):
            if mem_id not in existing_ids:
                expansion_ids.add(mem_id)
    
    # 从候选 entity 扩展（只取 top-5 候选的 entity，避免过度扩展）
    for c in sorted(candidates, key=lambda x: x.get("_score", 0), reverse=True)[:5]:
        for entity in self._index.get_entities_for_memory(collection, c["id"]):
            if len(self._index.get_memories_for_entity(collection, entity)) <= ENTITY_DEGREE_CAP:
                for mem_id in self._index.get_memories_for_entity(collection, entity):
                    if mem_id not in existing_ids:
                        expansion_ids.add(mem_id)
    
    # 限制扩展数量（避免拉入过多）
    expansion_ids = list(expansion_ids)[:20]
    
    # 从 Qdrant 获取扩展记录的完整 payload
    if expansion_ids:
        expanded = await storage.get(collection, expansion_ids)
        for r in expanded:
            r["_score"] = 0.0       # 无向量匹配分，纯靠 entity 路径
            r["_expanded"] = True   # 标记为扩展召回
            candidates.append(r)
    
    return candidates
```

**Stage 2: 路径成本评分**

```python
for candidate in candidates:
    paths = []
    dist = _to_distance(candidate)
    
    # Path 1: Direct hit (+ penalty if broad)
    direct_cost = dist
    if self._is_broad_match(candidate, collection):
        direct_cost += DIRECT_HIT_PENALTY
    paths.append(direct_cost)
    
    # Path 2+: Entity propagation
    candidate_entities = self._index.get_entities_for_memory(collection, candidate["id"])
    for entity in candidate_entities:
        entity_memories = self._index.get_memories_for_entity(collection, entity)
        if len(entity_memories) > ENTITY_DEGREE_CAP:
            if entity not in query_entities_lower:
                continue
        
        for other_id in entity_memories:
            if other_id == candidate["id"]:
                continue
            other = candidates_by_id.get(other_id)
            if other:
                hop = HOP_COST
                if entity in query_entities_lower:
                    hop *= 0.5
                other_dist = _to_distance(other)
                path_cost = other_dist + hop
                paths.append(path_cost)
    
    candidate["_cone_score"] = min(paths) if paths else EDGE_MISS_COST
```

**接入点**：在 HierarchicalRetriever **最终候选列表汇合后**、`_convert_to_matched_contexts` 前。所有检索路径（frontier/flat/RRF）统一接入。

```python
# hierarchical_retriever.py — 三条路径汇合后
if self._cone_scorer and all_matched:
    collection = self._type_to_collection(context_type)
    all_matched = await self._cone_scorer.score(
        all_matched, query, collection, self._storage
    )
```

**高频 entity 抑制**：`ENTITY_DEGREE_CAP = 50`。一个 entity 如果关联超过 50 条记忆（如 "OpenCortex" 出现在所有技术记忆中），传播会引入大量噪声。除非查询显式提到该 entity，否则不参与传播。

**查询 entity 参与加分**：查询中提到的 entity 跳转成本减半 (`HOP_COST * 0.5`)，强化与查询直接相关的路径。

### 4.4 Score Fusion (Additive — 保留 rerank)

**核心原则**：Cone 是辅助信号，叠加到现有公式上，**不替换 rerank**。

当前公式:
```
final = beta × rerank + (1-beta) × vector + reward_weight × reward + hot_weight × hotness
```

新公式（**在现有基础上加一项**）:
```
final = beta × rerank + (1-beta) × vector
      + reward_weight × reward
      + hot_weight × hotness
      + cone_weight × cone_bonus        ← NEW: 叠加项
```

Where:
- `cone_bonus`: 从 ConeScorer 计算。`1.0 - min(1.0, cone_score)` 归一化到 [0, 1]
- `cone_weight = 0.1` (默认, 可配): 保守权重，只做微调
- 当无 entity 时 `cone_bonus = 0`（无影响）

**为什么叠加而非替换**:
- Rerank 是经过验证的精度保障（jina-reranker-v2 本地模型）
- Cone 是新信号，初期应保守引入
- `cone_weight = 0.1` 意味着锥形传播最多影响排名 ±10%
- 可以通过调高 `cone_weight` 逐步增加影响力

### 4.5 Query Entity Extraction

在搜索时，从查询中识别 entity 以加强路径传播。

**实现策略**：不调 LLM，仅在**候选集已有的 entity** 中匹配（不查全量索引）。

```python
def extract_query_entities(self, query: str, candidates: List[Dict],
                            collection: str) -> Set[str]:
    """从查询中提取 entity — 仅匹配候选集中出现的 entity。
    
    避免全量索引子串匹配的误匹配问题（特别是中文短 entity）。
    候选集通常 < 50 条，entity 数量 < 150，匹配成本极低。
    """
    query_lower = query.lower()
    candidate_entities = set()
    for c in candidates:
        for e in self._index.get_entities_for_memory(collection, c["id"]):
            candidate_entities.add(e)
    
    matched = set()
    for entity in candidate_entities:
        if entity in query_lower:  # entity 已 lowercase（写入时归一化）
            matched.add(entity)
    return matched
```

**为什么只查候选集**：
- 全量索引可能有 300K entity，中文 2 字 entity 会大量误匹配
- 候选集通常 < 50 条 × 3 entity = ~150 个候选 entity
- O(150 × len(query)) 极快，且误匹配范围受限于已检索到的记忆

## 5. Data Model Changes

### 5.1 Qdrant Payload Addition

Add to context collection records:
```
entities: List[str]    # Named entities extracted by LLM at write time
```

No schema change needed — Qdrant's flexible payload accepts new fields automatically. Existing records without `entities` field simply have empty entity set (graceful degradation).

### 5.2 No New Collections

Entity index is in-memory only. Rebuilt from Qdrant scroll at startup.

## 6. Integration Points

### 6.1 Write Path (orchestrator.add)

```python
# In orchestrator._llm_derive() or equivalent:
# Current: returns {abstract, overview, keywords}
# New: also returns {entities: ["entity1", "entity2"]}

# After Qdrant upsert:
if self._entity_index and entities:
    self._entity_index.add(record_id, entities)
```

### 6.2 Delete Path (orchestrator.remove / batch_delete)

```python
# 单条删除:
if self._entity_index:
    collection = self._get_collection()
    self._entity_index.remove(collection, record_id)

# 递归删除 (remove_by_uri 删除 URI 前缀匹配的所有记录):
# 问题: remove_by_uri 只返回 count，不返回受影响的 ID
# 方案: 删除前先 scroll 查出受影响的记录 ID
if self._entity_index:
    collection = self._get_collection()
    # Pre-delete scroll: 获取所有匹配 URI 的记录 ID
    affected = await self._storage.filter(collection, uri_filter, limit=10000)
    affected_ids = [str(r["id"]) for r in affected]

# 执行实际删除
await self._storage.remove_by_uri(collection, uri)

# 删除后同步索引
if self._entity_index and affected_ids:
    self._entity_index.remove_batch(collection, affected_ids)
```

**注意**: pre-delete scroll 增加一次 filter 查询。对于大量递归删除（> 10K 条），可能需要分批 scroll。但递归删除本身就是低频操作，额外延迟可接受。

### 6.2.1 Content Edit Path

```python
# 内容变更时，重新提取 entity:
new_entities = await self._extract_entities(new_content)
if self._entity_index:
    collection = self._get_collection()
    self._entity_index.update(collection, record_id, new_entities)
    # 同时更新 Qdrant payload
    await self._storage.update(collection, record_id, {"entities": new_entities})
```

### 6.3 Search Path (hierarchical_retriever.py)

```python
# After existing retrieval + rerank, before returning:
if self._cone_scorer:
    query_entities = self._cone_scorer.extract_query_entities(query)
    candidates = self._cone_scorer.score(candidates, query_entities)
    # Re-sort by cone-adjusted score
```

### 6.4 Startup (orchestrator.init)

```python
# In init(), after storage ready:
self._entity_index = EntityIndex()
self._cone_scorer = ConeScorer(self._entity_index)  # 不绑定 collection

# 后台异步构建默认 collection 索引（不阻塞启动）
asyncio.create_task(self._entity_index.build_for_collection(
    self._storage, self._get_collection()
))
# 其他 collection 的索引在首次搜索时懒加载
# ConeScorer 在索引未就绪时返回 cone_bonus=0（graceful degradation）
```

**启动不阻塞**：匹配现有 `_startup_maintenance()` 模式。100K 记录 scroll 约需 5 秒，期间 cone scoring 降级为纯向量评分。

## 7. New Files

```
src/opencortex/retrieve/
├── entity_index.py       # EntityIndex: in-memory inverted index (~100 lines)
├── cone_scorer.py        # ConeScorer: path-cost propagation (~150 lines)
```

## 8. Modified Files

```
src/opencortex/orchestrator.py                    # Init entity index + cone scorer, sync on add/remove
src/opencortex/retrieve/hierarchical_retriever.py # Apply cone scoring after retrieval
src/opencortex/prompts.py                         # Add entity extraction to LLM derive prompt
src/opencortex/storage/collection_schemas.py      # (optional) Add entities field to schema docs
```

## 9. Configuration

```python
# In CortexConfig:
cone_retrieval_enabled: bool = True        # Enable cone scoring
cone_weight: float = 0.1                   # Cone bonus weight in fusion (conservative)
cone_direct_hit_penalty: float = 0.3       # Penalty for broad L2 matches
cone_hop_cost: float = 0.05               # Cost per entity-hop
cone_edge_miss_cost: float = 0.9          # Cost when no entity edge
cone_entity_degree_cap: int = 50           # Suppress entities with > N memories
```

## 10. Graceful Degradation

- **No entities on record**: `cone_bonus = 0`（无影响）
- **No entities in query**: Cone scoring 仍通过候选间 entity 共现边工作
- **Empty entity index** (cold start): 降级为纯向量评分
- **Entity index rebuild**: 后台异步任务，构建期间 cone_bonus=0
- **会话模式 immediate 记录**: `_write_immediate` 跳过 LLM derive，无 entity 提取。**Cone scoring 仅对合并后的记忆生效**。这是已知限制 — 会话内的实时查询不受 cone 影响，会话结束合并后才生效。

## 10.1 SearchExplain 集成

在现有 `SearchExplain` dataclass 中增加 cone 相关字段：

```python
cone_entities_found: int = 0      # 候选集中找到的 entity 数量
cone_query_entities: List[str] = []  # 从查询中识别的 entity
cone_ms: float = 0.0              # Cone scoring 耗时 (ms)
```

## 11. Non-Goals

1. **Full m_flow graph** — No Facet/FacetPoint hierarchy. Entity edges only.
2. **Entity resolution** — No canonical name normalization (e.g., "张三" ≠ "Zhang San"). Future work.
3. **Edge semantics** — Edges are binary (share entity or not). No edge text vectorization.
4. **Persistent entity store** — Index rebuilt from Qdrant at startup. No separate persistence.
5. **Multi-hop** — Only 1-hop entity paths. No A→B→C traversal. Future work.

## 12. Testing Strategy

- Unit tests: EntityIndex CRUD + lifecycle sync
- Unit tests: ConeScorer with mock candidates + known entities
- Integration test: add memories with entities → search → verify cone-adjusted ranking
- A/B comparison: same queries, cone on vs off, compare top-3 quality
