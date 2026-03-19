# OpenCortex v0.6 召回优化实施 Spec

> 状态：Approved Design
> 日期：2026-03-19
> 范围：Phase 1.5 — 检索主链路准确率 + 性能（全部 13 项，P0-P2）
> 策略：Bottom-Up 基础设施先行

---

## 1. 目标

基于 v0.5.1 benchmark 结果，通过 13 项改造提升召回准确率和性能。

### 准确率目标

| 数据集 | 当前 J-Score | 目标 J-Score |
|--------|:-----------:|:-----------:|
| QASPER | 0.15 | >= 0.65 |
| LoCoMo | 0.56 | >= 0.70 |
| PersonaMem | 0.83 | >= 0.83（不退化） |
| HotPotQA | 0.80 | >= 0.80（不退化） |

### 性能目标

| 场景 | 当前 p50 | 目标 p50 |
|------|:-------:|:-------:|
| 简单查询 (fast path) | 5-12s | < 1.5s |
| 一般查询 | 5-12s | < 3s |
| 复杂查询 (LLM intent) | 5-12s | < 5s |

---

## 2. 根因分析

| # | 根因 | 影响 |
|---|------|------|
| 1 | Benchmark 数据未物理隔离 | Recall@k 不可信，数据膨胀致延迟异常 |
| 2 | 查询不分流 | 所有 query 共享同一检索路径 |
| 3 | 文档范围无约束 | QASPER 单文档问题退化为全局 chunk 搜索 |
| 4 | Embedding 文本表达力不足 | chunk 脱离上下文后语义退化 |
| 5 | LLM intent 占据热路径 | 简单查询触发 2-5s LLM 调用 |

---

## 3. 架构概览

```
query
  ┌─── Layer 0: 结构信号判断（has_target_uri → document_scoped）
  │
  ├─── Layer 1: QueryFastClassifier（Embedding Nearest Centroid）
  │      类别描述向量预计算 → cosine similarity → 最近类别
  │      ├─ document_scoped   → 文档范围限定 + 两阶段检索
  │      ├─ fact_lookup        → 提高 lexical 权重，可跳过 rerank
  │      ├─ temporal_lookup    → 时间硬过滤 + 时间排序加权
  │      ├─ simple_recall      → 直接检索，跳过 LLM intent
  │      └─ complex            → LLM IntentRouter（仅此路径走 LLM）
  │
  ├─── Candidate Generation
  │      dense + sparse(BM25) + lexical
  │      权重由 query_class 驱动（hybrid_weights 配置）
  │
  ├─── Rerank Gate
  │      top1-top2 分差 > 阈值 → 跳过
  │      exact lexical 命中 → 跳过
  │      doc-scoped 小候选池 → 缩小或跳过
  │
  └─── Context Assembly（Small-to-Big）
         命中叶子 chunk → 返回 parent section overview + 相邻 sibling
```

---

## 4. 详细设计

### 4.1 [P0] SearchExplain 延迟追踪

**新增文件**: 无（在 `src/opencortex/retrieve/types.py` 中新增 dataclass）

```python
@dataclass
class SearchExplain:
    query_class: str              # document_scoped / fact_lookup / temporal / complex / simple
    path: str                     # fast_path / cache_hit / llm_intent
    intent_ms: float              # 意图分析耗时（0 = fast path）
    embed_ms: float               # 文本向量化耗时
    search_ms: float              # Qdrant ANN 检索耗时
    rerank_ms: float              # Cross-Encoder 排序耗时（0 = 跳过）
    assemble_ms: float            # 结果组装耗时
    doc_scope_hit: bool           # 是否命中文档范围限定
    time_filter_hit: bool         # 是否启用时间过滤
    candidates_before_rerank: int
    candidates_after_rerank: int
    frontier_waves: int           # 0 = flat search
    frontier_budget_exceeded: bool
    total_ms: float               # 总耗时（含分段间隙）
```

**打点位置**: `hierarchical_retriever.search()` 用 `time.perf_counter()` 在每个阶段前后打点。

**暴露方式**: `FindResult` 新增 `explain: Optional[SearchExplain]` 字段。`orchestrator.search()` 将 explain 序列化为 dict 附到 HTTP 响应。默认不返回，`?explain=true` 参数启用。

---

### 4.2 [P0] Qdrant Payload 新字段

**修改文件**: `src/opencortex/storage/collection_schemas.py`

新增 6 个字段定义 + ScalarIndex：

| 字段 | 类型 | 用途 |
|------|------|------|
| `source_doc_id` | keyword | 文档范围过滤 |
| `source_doc_title` | keyword | explain / 调试 |
| `source_section_path` | keyword | 层级定位 |
| `chunk_role` | keyword | leaf / section / document |
| `speaker` | keyword | 对话事实过滤 |
| `event_date` | date_time | 时序过滤 |

**兼容性**: Qdrant 动态 schema，新字段对已有数据为空值，不影响现有查询。新字段仅在显式过滤时参与检索。

---

### 4.3 [P0] Benchmark 独立 Collection 隔离

**修改文件**: `tests/benchmark/runner.py`

**设计**:
- 每次 benchmark run 创建独立 Collection: `bench_{dataset}_{run_id}`
- Ingest 和 Search 均指向该 Collection
- Run 完成后删除 Collection

**实现方式**:
当前 runner 通过 HTTP API 操作（`_http_post`），不直接访问 adapter。两种实现路径：

- **方案 A（推荐）: HTTP API 增加 collection 路由** — 在 `server.py` 的 store/search API 中支持 `X-Collection` header 或 query param，orchestrator 将请求路由到指定 collection。Runner 只需在请求中附加 collection 名称。新增 `/api/v1/admin/collection` 端点用于 create/delete。
- **方案 B: Runner 直接访问 adapter** — Runner 实例化本地 `QdrantStorageAdapter` 创建/删除 collection，但 ingest/search 仍走 HTTP。需要 runner 知道 Qdrant data path。

方案 A 更干净，保持 runner 的 HTTP-only 架构。

**清理保障**: `finally` 块中调用 delete collection 确保清理。

**过渡期指标**:
- 污染率：Top-K 中非目标文档/数据集的比例
- 命中来源分布：dense / lexical / rerank 各贡献的最终结果比例
- 这些指标通过 SearchExplain 收集

---

### 4.4 [P0] QueryFastClassifier (Embedding Nearest Centroid)

**新增文件**: `src/opencortex/retrieve/query_classifier.py`

**设计**:

```python
@dataclass
class QueryClassification:
    query_class: str              # document_scoped / fact_lookup / temporal / complex / simple
    need_llm_intent: bool
    lexical_boost: float          # 传递给 retriever 的 lexical 权重
    time_filter_hint: str | None  # recent / today / session / None
    doc_scope_hint: str | None    # source_doc_id

class QueryFastClassifier:
    """两层查询分类：结构信号 + Embedding Nearest Centroid。"""

    def __init__(self, embedder, config):
        self.embedder = embedder
        # 类别描述从 config 加载
        class_descriptions = config.query_classifier_classes
        # 启动时一次性 embed 类别描述 → centroid 向量
        self.centroids = {
            cls: embedder.embed(desc)
            for cls, desc in class_descriptions.items()
        }
        # 混合权重从 config 加载
        self.hybrid_weights = config.query_classifier_hybrid_weights
        self.confidence_threshold = config.query_classifier_threshold  # 默认 0.3

    def classify(self, query: str, target_uri: str | None,
                 session_context: dict | None) -> QueryClassification:
        # Layer 0: 结构信号（确定性）
        if target_uri:
            return self._make("document_scoped", doc_scope=extract_doc_id(target_uri))

        # Layer 1: Embedding Nearest Centroid
        query_vec = self.embedder.embed(query)
        scores = {cls: cosine_sim(query_vec, c) for cls, c in self.centroids.items()}
        best_class = max(scores, key=scores.get)

        if scores[best_class] < self.confidence_threshold:
            # 置信度不足 → complex (需要 LLM IntentRouter)
            return self._make("complex", need_llm=True)

        return self._make(best_class)
```

**配置** (`server.json`):
```json
{
  "query_classifier": {
    "classes": {
      "document_scoped": "查找特定文档、论文、文件中的内容",
      "temporal_lookup": "查找最近、上次、昨天等时间相关的记忆",
      "fact_lookup": "查找特定人名、数字、术语、文件名等精确事实",
      "simple_recall": "简单的记忆召回，回忆之前存储的信息"
    },
    "threshold": 0.3,
    "hybrid_weights": {
      "document_scoped": { "dense": 0.5, "lexical": 0.5 },
      "fact_lookup":     { "dense": 0.3, "lexical": 0.7 },
      "temporal_lookup": { "dense": 0.6, "lexical": 0.4 },
      "simple_recall":   { "dense": 0.7, "lexical": 0.3 },
      "complex":         { "dense": 0.7, "lexical": 0.3 }
    }
  }
}
```

**集成到 Orchestrator**:
```
orchestrator.search()
  → QueryFastClassifier.classify(query, target_uri, session_context)
  → if need_llm_intent: IntentRouter.route()
     else: 构造 TypedQuery，用分类结果的 lexical_boost
  → HierarchicalRetriever.search(typed_queries, classification)
```

---

### 4.5 [P0] Document Scoped Search + 两阶段检索

**修改文件**: `src/opencortex/orchestrator.py`, `src/opencortex/retrieve/hierarchical_retriever.py`, `src/opencortex/parse/parsers/markdown.py`

#### 4.5.1 数据写入增强

`orchestrator._add_document()` 中：
- 生成 `source_doc_id`：基于 `source_path` 的确定性 hash（`hashlib.sha256(source_path).hexdigest()[:16]`），无 source_path 时用 UUID
- 从 MarkdownParser chunk 层级构建 `source_section_path`：遍历 `parent_index` 链拼接 title（如 "Chapter 1 > Section 2.3"）
- 设置 `chunk_role`：根节点=`document`，有子节点=`section`，叶子=`leaf`
- 将这些字段写入每个 chunk 的 Qdrant payload（通过 `meta` 字段传递）

#### 4.5.2 两阶段检索

当 `query_class == document_scoped` 时，`hierarchical_retriever.search()` 内部：

```
阶段 1：确定 source_doc_id
  路径 A: target_uri 存在 → 从 URI 提取 source_doc_id（确定性）
  路径 B: target_uri 不存在但分类器语义匹配到 document_scoped
          → 先对 source_doc_title 字段做 keyword/lexical 搜索
          → 命中 1 篇 → 用该 doc_id 限定
          → 命中多篇或 0 篇 → 不限定（安全降级到全局搜索）
  路径 C: 无法确定 → 不限定（安全降级到全局搜索）

阶段 2：文档内检索
  - 注入 Qdrant payload filter: source_doc_id == X
  - 在限定范围内跑 dense + sparse + lexical
```

**关键点**: QASPER 场景中，benchmark runner 在 ingest 时已知 source_doc_id，可通过 query meta 传入 `target_doc_id` 直接走路径 A。生产环境中，路径 B 的 title 匹配作为推断机制。

**不做**: RAPTOR 摘要树、文档级聚类召回。

---

### 4.6 [P1] Small-to-Big 返回策略

**修改文件**: `src/opencortex/retrieve/hierarchical_retriever.py`

在 `_convert_to_matched_contexts()` 中扩展：
- 命中叶子 chunk 后，通过 `parent_uri` 查找父 section 的 `overview`（已在 Qdrant payload 中，零 FS I/O）
- 可选扩展 ±2 个 sibling chunk（通过 `source_doc_id` + `meta.chunk_index` 范围查询）
- 返回：叶子 chunk abstract + 父 section overview + sibling 摘要
- 复用已有的 batch prefetch 机制（v0.5.1 已优化）

---

### 4.7 [P1] Context Flattening (embed_text 增强)

**修改文件**: `src/opencortex/orchestrator.py`

利用 `store()` 已有的 `embed_text` 参数，入库时拼接上下文信息：

| 模式 | embed_text 格式 |
|------|----------------|
| 对话/记忆 | `[{speaker}] [{event_date}] [{category}] {abstract}` |
| 文档 (add) | `[{source_doc_title}] [{source_section_path}] {abstract}` |
| 文档 (batch_add) | `[{meta.file_path}] {abstract}`（batch_add items 通常无 section_path，用 file_path 代替） |

**实现点**:
1. `_add_document()`: chunk 写入时拼接 doc_title + section_path → 传递 embed_text 到 `add()`
2. `_write_immediate()`: 对话消息写入时从 meta 提取 speaker + date → 拼接 embed_text
3. `batch_add()`: **补齐 embed_text 参数传递**（当前缺失，需要在 `_process_one()` 中传递给 `add()`）

**abstract 不变**: embed_text 仅影响向量化输入，存储的 abstract 保持原样。

---

### 4.8 [P1] 动态混合检索权重

**修改文件**: `src/opencortex/retrieve/hierarchical_retriever.py`

由 `QueryClassification.query_class` 查表获取 dense/lexical 权重。权重定义在 `server.json` 的 `query_classifier.hybrid_weights` 中（见 4.4 配置）。

**实现**: `search()` 方法接收 `QueryClassification` 参数，用 `hybrid_weights[query_class]` 覆盖默认 RRF 权重。影响 `_global_vector_search()` 中的 dense/sparse 融合比例。

**前置修复**: flat-search rerank 路径读取 `_score` 但 RRF 融合后写入的是 `_final_score`，导致 rerank 融合公式中 retrieval 分量为 0。此任务需同时修复该 pre-existing bug，统一为读取 `_final_score`。

---

### 4.9 [P1] 时间范围硬过滤

**修改文件**: `src/opencortex/retrieve/hierarchical_retriever.py`, `src/opencortex/storage/qdrant/filter_translator.py`

当 `classification.time_filter_hint` 非 None 时：

| time_filter_hint | Qdrant 过滤条件 |
|------------------|-----------------|
| `recent` | `created_at >= now - 7d` 或 `event_date >= now - 7d` |
| `today` | `created_at >= today_00:00` |
| `session` | `session_id == current_session` |

**安全降级**: 时间过滤返回 < 3 条结果时，自动放宽到不过滤并重新搜索。

**后排序加权**: 对时序查询结果追加 `+0.05 * recency_factor` 轻微加权（recency_factor = 1.0 - age_days/30，clamp to [0, 1]）。

---

### 4.10 [P2] Rerank Gate 增强

**修改文件**: `src/opencortex/retrieve/hierarchical_retriever.py`

扩展 `_should_rerank()` 逻辑：

| 条件 | 动作 |
|------|------|
| top1-top2 分差 > 0.15 | 跳过 rerank（现有） |
| `query_class == fact_lookup` 且 lexical 精确命中 | 跳过 rerank |
| `query_class == document_scoped` 且候选池 < 5 | 跳过 rerank |

`_should_rerank()` 需要接收 `QueryClassification` 参数。

---

### 4.11 [P2] Frontier 硬预算

**修改文件**: `src/opencortex/retrieve/hierarchical_retriever.py`

新增 `CortexConfig` 配置项：
```python
max_compensation_queries: int = 3     # 单次请求最大补偿查询数
max_total_search_calls: int = 12      # 单次请求最大搜索调用总数
```

在 wave loop 中维护 `total_search_calls` 计数器。超预算时：
- 立即停止 wave loop
- 回退 flat search
- 在 SearchExplain 中设置 `frontier_budget_exceeded = True`

---

### 4.12 [P2] 消融实验框架

**新增文件**: `tests/benchmark/ablation.py`

基于已有 `runner.py` 扩展，支持单变量扫描：

```bash
python tests/benchmark/ablation.py \
  --variable lexical_boost \
  --values 0.2,0.4,0.6,0.8 \
  --dataset qasper \
  --limit 50 \
  --output results/ablation_lexical.csv
```

**变量空间**:

| 变量 | 默认范围 |
|------|----------|
| `lexical_boost` | 0.2, 0.4, 0.6, 0.8 |
| `rerank_top_n` | 5, 10, 20, 30 |
| `doc_scope_filter` | on, off |
| `context_flattening` | on, off |
| `rerank_gate_threshold` | 0.10, 0.15, 0.20 |

逐变量扫描（fix others, sweep one），输出 CSV：variable, value, j_score, f1, p50, rerank_rate。

每组 run 使用独立 Collection 隔离（复用 4.3 的机制）。

---

### 4.13 [P2] ONNX Runtime 线程调优

**修改文件**: `src/opencortex/config.py`, `src/opencortex/models/embedder/` (embedder 初始化处)

新增 `CortexConfig` 配置项：
```python
onnx_intra_op_threads: int = 0  # 0 = auto (min(4, cpu_count // 2))
```

在 FastEmbed / ONNX 模型初始化时传递 `model_kwargs={"intra_op_num_threads": config.onnx_intra_op_threads}`。

---

## 5. 数据模型变更汇总

### 5.1 新增/修改的文件

| 文件 | 变更类型 | 内容 |
|------|----------|------|
| `src/opencortex/retrieve/types.py` | 修改 | 新增 SearchExplain dataclass |
| `src/opencortex/retrieve/query_classifier.py` | **新增** | QueryFastClassifier + QueryClassification |
| `src/opencortex/storage/collection_schemas.py` | 修改 | 6 个新 payload 字段 |
| `src/opencortex/config.py` | 修改 | 新增 classifier/frontier/onnx 配置项 |
| `src/opencortex/orchestrator.py` | 修改 | 集成 classifier, embed_text 增强, doc scoped search |
| `src/opencortex/retrieve/hierarchical_retriever.py` | 修改 | 动态权重, 时间过滤, rerank gate, frontier 预算, explain |
| `src/opencortex/retrieve/intent_router.py` | 修改 | 接收 classifier 结果减少不必要的 LLM 调用 |
| `src/opencortex/storage/qdrant/filter_translator.py` | 修改 | 支持时间范围 filter |
| `src/opencortex/parse/parsers/markdown.py` | 修改 | 输出 section_path |
| `tests/benchmark/runner.py` | 修改 | 独立 Collection 隔离 |
| `tests/benchmark/ablation.py` | **新增** | 消融实验框架 |

### 5.2 配置变更 (server.json)

```json
{
  "query_classifier": {
    "classes": { ... },
    "threshold": 0.3,
    "hybrid_weights": { ... }
  },
  "max_compensation_queries": 3,
  "max_total_search_calls": 12,
  "onnx_intra_op_threads": 0
}
```

### 5.3 API 行为变化

- `search()` 响应新增可选 `explain` 字段（`?explain=true` 启用）
- 内部检索路径根据 query_class 分流
- 无 public API 签名变更

### 5.4 历史数据兼容

- 新 payload 字段对历史数据为空值
- 所有 narrowing filter 允许 fallback 到无过滤路径
- 空值不参与过滤，不影响现有查询

---

## 6. 实施优先级

### P0（基础设施 + 核心准确率）

| # | 任务 | 预期收益 |
|---|------|----------|
| 1 | SearchExplain 延迟追踪 | 量化所有后续改动效果 |
| 2 | Qdrant 新字段 schema | 多个功能的共享前提 |
| 3 | Benchmark 独立 Collection 隔离 | 指标可信，消除数据膨胀 |
| 4 | QueryFastClassifier (Embedding Centroid) | 减少 LLM intent 触发率 |
| 5 | Document Scoped Search | QASPER J-Score 0.15 → 0.65+ |

### P1（准确率全面提升）

| # | 任务 | 预期收益 |
|---|------|----------|
| 6 | Context Flattening (embed_text 增强) | LoCoMo embedding 区分度提升 |
| 7 | Small-to-Big 返回策略 | 文档问答上下文完整性 |
| 8 | 时间范围硬过滤 + speaker/event_date | LoCoMo Cat 1/Cat 4 提升 |
| 9 | 动态混合检索权重 | 事实查询准确率提升 |

### P2（性能打磨 + 实验框架）

| # | 任务 | 预期收益 |
|---|------|----------|
| 10 | Rerank Gate 增强 | 减少无效 rerank |
| 11 | Frontier 硬预算 | tail latency 稳定 |
| 12 | 消融实验框架 | 量化验证，超参调优 |
| 13 | ONNX 线程调优 | 高并发 CPU 抢占缓解 |

---

## 7. 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| Document scoped search 误判文档目标 | 无法确定 doc_id 时降级为全局搜索 |
| 时间过滤过严 | 结果 < 3 条自动放宽到不过滤 |
| Classifier 置信度不足 | 低于阈值时 fallback 到 LLM IntentRouter |
| Context Flattening 改变 embedding 分布 | 仅影响新写入数据；可对比测试 |
| 新字段对历史数据为空 | 空值不参与过滤，不影响现有查询 |

所有新策略均可通过 `server.json` 配置开关控制。

---

## 8. 验收标准

### 准确率
- [ ] Benchmark 无跨 run/数据集污染
- [ ] QASPER J-Score >= 0.65
- [ ] LoCoMo J-Score >= 0.70
- [ ] PersonaMem J-Score >= 0.83（不退化）
- [ ] HotPotQA J-Score >= 0.80（不退化）

### 性能
- [ ] fast path 查询 p50 < 1.5s
- [ ] 一般查询 p50 < 3s
- [ ] SearchExplain 可拆分到 5 个子阶段
- [ ] rerank 触发率下降

### 工程
- [ ] 所有新策略有配置开关
- [ ] explain 接口可用
- [ ] 消融实验脚本可运行
- [ ] 现有测试不退化
