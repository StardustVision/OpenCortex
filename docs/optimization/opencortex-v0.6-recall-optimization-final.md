# OpenCortex v0.6 召回优化设计（终稿）

> 状态：Draft
> 日期：2026-03-19
> 范围：Phase 1.5 — 检索主链路准确率 + 性能
> 综合来源：GPT-5.4 详细设计 + Gemini 3.1 Pro 架构提议 + 代码审查
> 编辑：Claude Opus 4.6

---

## 1. 文档目标

基于 v0.5.1 benchmark 暴露的问题，给出一版**可落地的召回优化设计**。

核心追求：
- 召回准确率：QASPER J-Score 从 0.15 → 0.65+，LoCoMo 从 0.56 → 0.70+
- 召回延迟：简单查询 p50 < 1.5s，一般查询 p50 < 3s，复杂查询 p50 < 5s

不追求的：RAPTOR、GraphRAG、知识图谱推理、专用微调模型。这些留 Phase 2+。

---

## 2. 根因分析

从 v0.5.1 benchmark 结果 + 代码审查归纳出五个根因：

| # | 根因 | 影响 | 证据 |
|---|------|------|------|
| 1 | **Benchmark 数据未隔离** | Recall@k 不可信，跨 run/数据集污染 | expected_uri=0.0，PersonaMem 12s 异常延迟 |
| 2 | **查询不分流** | 所有 query 共享同一检索路径 | 事实查找、文档问答、时序查询走相同 dense+rerank |
| 3 | **文档范围无约束** | QASPER 单文档问题退化为全局 chunk 搜索 | QASPER J-Score=0.15，token reduction=88.6% 但答案错误 |
| 4 | **Embedding 文本表达力不足** | chunk 脱离上下文后语义退化 | LoCoMo cosine std=0.014，embedding 无法区分相似观察 |
| 5 | **LLM intent 占据热路径** | 简单查询仍可能触发 2-5s LLM 调用 | p50 全线 5-12s |

---

## 3. 设计原则

1. **先量准再优化** — benchmark 指标不可信就不动主链路
2. **结构化过滤优先于堆模型** — 文档范围、时间窗口、speaker 过滤比更大的 embedding 模型 ROI 更高
3. **Small-to-Big 先于 RAPTOR** — 命中小 chunk，返回父 section，不引入摘要树
4. **砍掉错误路径比加速正确路径更有效** — 优先减少进入 LLM intent 的比例

---

## 4. 总体架构

```
query
  ┌─── Query Fast Classifier（零 LLM，正则 + 规则）
  │      ├─ document_scoped   → 文档范围限定 + 两阶段检索
  │      ├─ fact_lookup        → 提高 lexical 权重，可跳过 rerank
  │      ├─ temporal_lookup    → 时间硬过滤 + 时间排序加权
  │      └─ complex/summary    → LLM IntentRouter（仅此路径走 LLM）
  │
  ├─── Candidate Generation
  │      dense + sparse(BM25) + lexical，权重由 query_class 驱动
  │
  ├─── Rerank Gate
  │      top1-top2 分差 > 阈值 → 跳过 rerank
  │      exact lexical 命中 → 跳过 rerank
  │      doc-scoped 小候选池 → 缩小 rerank 数
  │
  └─── Context Assembly
         命中小 chunk → 返回 parent section overview + 相邻 sibling
```

---

## 5. 详细设计

### 5.1 [P0] Benchmark 隔离与观测校准

**问题**：当前 benchmark 跨 run 数据残留，Recall@k = 0.0，PersonaMem 因历史数据膨胀延迟 12s。

#### 5.1.1 短暂态隔离（Ephemeral Isolation）

每次 benchmark run 使用独立隔离域：

```python
# 方案 A（推荐）：独立 Collection
collection_name = f"bench_{dataset}_{run_id}"
# 跑完即删，物理隔离，零泄漏

# 方案 B（备选）：同 Collection + 硬过滤
# 写入时附加 benchmark_run_id, dataset_id
# 查询时强制注入过滤条件
```

方案 A（独立 Collection）优于方案 B（字段过滤），原因：
- 物理隔离，不依赖查询端正确注入过滤
- 跑完删除，不膨胀生产数据
- 实现更简单

#### 5.1.2 五段式延迟追踪

抛弃笼统的 "Search API 耗时"，每次 search 返回结构化 explain：

```python
@dataclass
class SearchExplain:
    query_class: str           # document_scoped / fact_lookup / temporal / complex
    path: str                  # fast_path / cache_hit / llm_intent
    intent_ms: float           # 意图分析耗时
    embed_ms: float            # 文本向量化耗时
    search_ms: float           # Qdrant ANN 检索耗时
    rerank_ms: float           # Cross-Encoder 排序耗时（0 = 跳过）
    assemble_ms: float         # 结果组装耗时
    doc_scope_hit: bool        # 是否命中文档范围限定
    time_filter_hit: bool      # 是否启用时间过滤
    candidates_before_rerank: int
    candidates_after_rerank: int
    frontier_waves: int        # 0 = flat search
```

对外 API 默认不返回 explain，但 `?explain=true` 或 debug 接口必须可读。

#### 5.1.3 过渡期指标

在 expected_uri 彻底修复前，用组合代理指标：

- **QA 准确率**：J-Score + F1（已有）
- **污染率**：Top-K 中非目标文档/数据集的比例（新增）
- **命中来源分布**：dense / lexical / rerank 各贡献了多少最终结果（新增）

---

### 5.2 [P0] Query Fast Classifier

**问题**：当前所有查询共享同一检索路径，简单查询也可能触发 LLM intent。

#### 5.2.1 设计

在 IntentRouter 前新增零 LLM 的 Fast Classifier。基于正则 + 规则 + 上下文信号：

```python
class QueryFastClassifier:
    """零 LLM 成本的查询预分类。"""

    def classify(self, query: str, target_uri: str | None,
                 session_context: dict | None) -> QueryClassification:
        ...

@dataclass
class QueryClassification:
    query_class: QueryClass      # enum
    need_llm_intent: bool        # 是否需要走 LLM IntentRouter
    lexical_boost: float         # 0.0-1.0, 动态 lexical 权重
    time_filter_hint: TimeScope | None
    doc_scope_hint: str | None   # source_doc_id
```

#### 5.2.2 分类规则

| 信号 | 分类 | 处理 |
|------|------|------|
| `target_uri` 存在 或 query 含 "这篇/这个文档/论文" | `document_scoped` | 跳过全局搜索，进入文档范围检索 |
| 命中人名/文件名/数字/术语/CamelCase/路径 | `fact_lookup` | 提高 lexical 权重至 0.6-0.7 |
| 命中 "最近/上次/昨天/最后一次/last week" | `temporal_lookup` | 注入时间硬过滤 |
| `session_context is None` 且无明显复杂句式 | `simple_recall` | 走 fast path，跳过 LLM intent |
| 多句、模糊意图、需要分析/总结 | `complex` | 进入 LLM IntentRouter |

#### 5.2.3 路由优先级

```
heuristic fast path → cache hit → small model (future) → LLM fallback
```

**与现有 IntentRouter 的关系**：Fast Classifier 是 IntentRouter 的前置层，不替换它。分类结果决定是否需要调用 IntentRouter。

---

### 5.3 [P0] 文档范围检索 + Small-to-Big

**问题**：QASPER J-Score=0.15。单文档问答退化为全局 chunk 搜索，命中错误文档的 chunk。

#### 5.3.1 数据模型补充

文档导入（document mode）时，每个 chunk 补齐：

| 字段 | 类型 | 用途 |
|------|------|------|
| `source_doc_id` | keyword | 文档范围过滤（核心） |
| `source_doc_title` | keyword | explain / 调试 |
| `source_section_path` | keyword | 层级定位 |
| `chunk_role` | keyword | `leaf` / `section` / `document` |

这些字段写入 Qdrant payload，并建立 ScalarIndex。

**实现位置**：`MarkdownParser` 已经生成 `parent_index` 层级关系。在 `orchestrator.batch_add()` 中，基于文档路径或首个 chunk 的 meta 生成 `source_doc_id`，向下传播到所有子 chunk。

#### 5.3.2 两阶段检索

```
阶段 1：文档范围确定
  ├─ query 已绑定 target_uri → 提取 source_doc_id → 硬过滤
  ├─ 上下文可推断文档目标 → 先做文档级候选确定
  └─ 无法确定 → 不限定（退化为全局搜索，保底）

阶段 2：文档内 chunk 检索
  在 source_doc_id 过滤范围内跑 dense + sparse + lexical
  命中叶子 chunk 后，向上提升父 section
```

#### 5.3.3 Small-to-Big 返回策略

匹配粒度小，返回上下文大：

- 命中的叶子 chunk 本身
- 所属 parent section 的 `overview`（L1 层，已在 Qdrant payload 中）
- 相邻 sibling chunk 的摘要（有限扩展，最多 ±2 个）

**不做**：RAPTOR 全局摘要树、文档级聚类召回。

---

### 5.4 [P1] 索引表达增强（Context Flattening）

**问题**：chunk 脱离原文上下文后，embedding 退化。LoCoMo cosine std=0.014，模型无法区分相似观察。

#### 5.4.1 设计

利用 `store()` 已支持的 `embed_text` 参数，入库时拼接上下文信息作为 embedding 文本，而 abstract 保持原样：

```python
# 对话/记忆模式
embed_text = f"[{speaker}] [{date}] [{category}] {abstract}"

# 文档模式
embed_text = f"[{doc_title}] [{section_path}] {abstract}"
```

**ROI 极高**：不改模型、不改索引结构、不改查询逻辑，只改写入时的 embedding 输入。

#### 5.4.2 实现要点

- `batch_add()` 当前**不支持** `embed_text`，需补齐传递
- 对话模式 `_write_immediate()` 和 merge 层都需要拼接
- 文档模式在 `MarkdownParser` 产出 chunk 后，由 `batch_add()` 拼接 doc_title + section_path

---

### 5.5 [P1] 动态混合检索权重

**问题**：当前 lexical boost 是固定值（硬关键词 0.55，默认 0.3），不区分查询类型。

#### 5.5.1 权重矩阵

由 Query Fast Classifier 的 `query_class` 驱动：

| Query 类型 | Dense 权重 | Lexical 权重 | Rerank |
|---|:---:|:---:|---|
| 普通语义查询 | 0.7 | 0.3 | 按阈值（现有逻辑） |
| 硬关键词 / 文件名 / 术语 | 0.4 | 0.6 | 条件触发 |
| 事实查找 / 人名 + 数字 | 0.3 | 0.7 | 可跳过 |
| 文档范围内 chunk 查询 | 0.5 | 0.5 | 按候选质量 |
| 时序查询 | 0.6 | 0.4 | 按阈值 + 时间排序加权 |

**实现位置**：`hierarchical_retriever.py` 中 `_build_search_params()` 或等效位置，根据分类结果设置 RRF 融合权重。

---

### 5.6 [P1] 时间范围硬过滤

**问题**：IntentRouter 已识别 `time_scope`，但未真正转化为检索层的硬过滤。

#### 5.6.1 设计

将 `time_scope` 映射为 `metadata_filter`：

| time_scope | 过滤条件 |
|---|---|
| `recent` | `created_at >= now - 7d` 或 `event_date >= now - 7d` |
| `session` | `session_id = current_session` |
| `today` | `created_at >= today_00:00` |
| `all` | 不额外限制 |

对于时序查询，追加后排序加权：先按语义得分筛选，再对近时间结果轻微加权（+0.05 * recency_factor）。

#### 5.6.2 补充字段

在 Qdrant payload 中新增：

| 字段 | 类型 | 用途 |
|------|------|------|
| `speaker` | keyword | 对话事实过滤（"张三说过什么"） |
| `event_date` | keyword | 时序过滤（"上周的会议"） |

这两个字段在写入时从 meta 或对话上下文中提取，不需要 NER pipeline。

---

### 5.7 [P2] Rerank Gate 增强

**现状**：已有 `_should_rerank()` 检查 top1-top2 分差 > 0.15 时跳过。

#### 5.7.1 扩展规则

| 条件 | 动作 |
|------|------|
| top1-top2 分差 > 0.15 | 跳过 rerank（现有） |
| exact lexical match（BM25 精确命中） | 跳过 rerank |
| document-scoped 且候选池 < 5 | 缩小 rerank 候选数或跳过 |
| fact_lookup 类型 + lexical 权重 > 0.6 | 降低 rerank 优先级 |

#### 5.7.2 结果组装预算

- L0/L1 默认只读必要字段（已从 Qdrant payload 获取，零 FS I/O）
- 文档模式优先返回 parent section overview，不展开整篇
- relations 继续维持批量预取（v0.5.1 已实现）

---

### 5.8 [P2] Frontier 硬预算与回退

**现状**：已有 max_waves=8, MAX_FRONTIER_SIZE=64, MIN_CHILDREN_PER_DIR=2, 3 波收敛退出。

#### 5.8.1 补充约束

```python
MAX_COMPENSATION_QUERIES = 3   # 单次请求最大补偿查询数
MAX_TOTAL_SEARCH_CALLS = 12    # 单次请求最大搜索调用总数
```

超预算时：
- 自动回退 flat search
- 在 SearchExplain 中记录 `frontier_budget_exceeded = True`

---

### 5.9 [P2] 自动化消融实验框架

**来源**：Gemini 提议。在 benchmark 隔离修复后，构建自动化脚本量化单点收益。

变量空间：

| 变量 | 范围 |
|------|------|
| `lexical_boost` | 0.2 / 0.4 / 0.6 / 0.8 |
| `chunk_size` | 256 / 512 / 1024 tokens |
| `rerank_top_n` | 5 / 10 / 20 / 30 |
| `doc_scope_filter` | on / off |
| `context_flattening` | on / off |
| `rerank_gate_threshold` | 0.10 / 0.15 / 0.20 |

每组跑 QASPER + LoCoMo 子集（各 50 QA），输出 J-Score + F1 + p50 + rerank_trigger_rate。

**不做全量网格搜索**，采用逐变量扫描（fix others, sweep one），6 × ~6 值 ≈ 36 runs，可控。

---

## 6. 数据模型变更汇总

### 6.1 Qdrant Payload 新增字段

| 字段 | 类型 | 索引 | 用途 | 写入时机 |
|------|------|------|------|----------|
| `source_doc_id` | keyword | ScalarIndex | 文档范围过滤 | document mode 导入 |
| `source_doc_title` | keyword | ScalarIndex | explain / 调试 | document mode 导入 |
| `source_section_path` | keyword | ScalarIndex | 层级定位 | document mode 导入 |
| `chunk_role` | keyword | ScalarIndex | leaf/section/document | document mode 导入 |
| `speaker` | keyword | ScalarIndex | 对话事实过滤 | conversation mode 写入 |
| `event_date` | keyword | ScalarIndex | 时序过滤 | 从 meta 提取 |

### 6.2 API 行为变化

- `search()` 签名不变，内部支持：
  - 从 `target_uri` 推导 `source_doc_id`
  - 从 `time_scope` 推导时间过滤条件
  - explain 结构体附在响应中
- `batch_add()` 需要：
  - 补齐 `embed_text` 参数传递
  - 写入时生成 `source_doc_id` / `source_section_path` / `chunk_role`
- `store()` 的 `embed_text` 行为不变，仅对话模式调整拼接逻辑

### 6.3 历史数据兼容

新增字段对历史数据为空值（None），检索时空值不参与过滤。所有 narrowing filter 都允许 fallback 到无过滤路径，不会因字段缺失导致零结果。

---

## 7. 实施优先级

### P0（必须先做，后续所有优化的基础）

| # | 任务 | 预期收益 |
|---|------|----------|
| 1 | Benchmark 隔离（独立 Collection） | 指标可信，消除 12s 异常延迟 |
| 2 | 五段式延迟追踪 | 定位瓶颈，驱动后续优化 |
| 3 | Query Fast Classifier | 减少 LLM intent 触发率，p50 立降 |
| 4 | `source_doc_id` 字段 + Document Scoped Search | QASPER J-Score 从 0.15 → 0.65+ |

### P1（核心准确率提升）

| # | 任务 | 预期收益 |
|---|------|----------|
| 5 | Context Flattening（embed_text 增强） | LoCoMo embedding 区分度提升 |
| 6 | Small-to-Big 返回策略 | 文档问答上下文完整性 |
| 7 | 时间范围硬过滤 + speaker/event_date 字段 | LoCoMo Cat 1/Cat 4 提升 |
| 8 | 动态混合检索权重 | 事实查询准确率提升 |

### P2（性能打磨 + 实验框架）

| # | 任务 | 预期收益 |
|---|------|----------|
| 9 | Rerank Gate 增强 | 减少无效 rerank，p50 再降 |
| 10 | Frontier 硬预算 | tail latency 稳定 |
| 11 | 消融实验框架 | 量化验证，超参调优 |
| 12 | ONNX Runtime `intra_op_num_threads` 调优 | 高并发下 CPU 抢占缓解 |

### P3（后续探索，不在本次范围）

- RAPTOR / 文档摘要树
- 小模型 intent classifier（替代 LLM）
- 实体 NER pipeline
- GraphRAG / 知识图结构

---

## 8. 风险与回滚

| 风险 | 缓解措施 |
|------|----------|
| 文档 scoped search 误判文档目标，recall 变窄 | 所有 narrowing filter 允许 fallback 到全局搜索 |
| 时间过滤过严，误伤历史事实 | 时间过滤仅在明确时序 query 激活，默认不过滤 |
| Fast Classifier 误判，跳过了复杂查询 | 分类置信度不足时 fallback 到 LLM IntentRouter |
| Context Flattening 改变 embedding 分布 | 仅影响新写入数据，历史数据不受影响；可对比测试 |
| 新增字段导致历史数据查询异常 | 空值不参与过滤，字段缺失不影响现有查询 |

所有新策略均可通过 `CortexConfig` 开关控制，支持逐项回滚。

---

## 9. 验收标准

### 准确率

- [ ] Benchmark 无跨 run/数据集污染（独立 Collection 隔离）
- [ ] QASPER J-Score ≥ 0.65（当前 0.15）
- [ ] LoCoMo J-Score ≥ 0.70（当前 0.56）
- [ ] PersonaMem J-Score 不退化（当前 0.83）
- [ ] HotPotQA J-Score 不退化（当前 0.80）

### 性能

- [ ] fast path 查询 p50 < 1.5s（当前 5-12s）
- [ ] 一般查询 p50 < 3s
- [ ] rerank 触发率下降（通过 explain 统计）
- [ ] 延迟可拆分到 5 个子阶段

### 工程

- [ ] 所有新策略有 feature flag
- [ ] explain 接口可用
- [ ] 消融实验脚本可运行

---

## 10. 采纳与舍弃说明

### 从 GPT-5.4 设计中采纳

- 根因分析框架（三类问题叠加）
- 设计原则（先量准再优化、结构化过滤优先）
- Query Classifier 的详细分类规则与路由优先级
- Document Scoped Search + Small-to-Big 的完整两阶段设计
- 动态混合检索权重矩阵
- 时间范围硬过滤设计
- Rerank Gate 与组装预算
- Frontier 硬预算
- 风险与回滚策略

### 从 Gemini 3.1 Pro 提议中采纳

- 短暂态 Qdrant 隔离（优于字段级过滤）
- 五段式延迟追踪结构
- Context Flattening（embed_text 增强，高 ROI）
- 自动化消融实验框架
- ONNX Runtime 线程调优建议
- 污染率 + 命中来源分布作为过渡指标

### 舍弃内容

| 建议 | 来源 | 舍弃原因 |
|------|------|----------|
| Redis 向量缓存做 Intent Cache | Gemini | 过度工程，当前 LRU cache 已覆盖；规模不到需要 Redis 的程度 |
| 联合预取 / N+1 修复 / batch access stats | Gemini | **已在 v0.5.1 实现**（6 项 hot-path fixes） |
| 实体 NER pipeline | 两者 | 当前阶段不需要完整 NER，轻量字段（speaker/event_date）够用 |
| 时间衰减向量相似度 | GPT-5.4 | 影响历史 recall，调参复杂，可解释性差 |
| Day-by-day 时间线 | Gemini | 不给出不可靠的时间估算，按优先级执行 |
| benchmark_run_id 作为 payload 字段 | GPT-5.4 | 独立 Collection 方案下不需要 |
