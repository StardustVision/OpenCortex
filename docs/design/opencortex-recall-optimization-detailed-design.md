# OpenCortex 召回优化详细设计

> 状态：Draft  
> 日期：2026-03-19  
> 范围：Phase 1.5 / 检索主链路  
> 目标：同时提升召回准确率与召回性能，不引入超出当前系统成熟度的复杂架构

## 1. 文档目标

本文档整合当前 benchmark 暴露的问题、已有实现能力以及可落地的工程改造，给出一版面向 OpenCortex 的召回优化详细设计。

本文档聚焦两个指标：

- 召回准确率（Recall Accuracy）
- 召回性能 / 延迟（Recall Latency / Performance）

本文档不追求一次性引入最前沿、最复杂的 RAG 架构，而强调：

- 先把指标量准
- 先修主链路
- 先做高 ROI 改造
- 保持可解释与可回滚

## 2. 背景与问题定义

### 2.1 当前 benchmark 反映出的真实问题

从当前 `HotPotQA / LoCoMo / PersonaMem / QASPER` 的结果看，OpenCortex 的问题不是单点缺陷，而是三类问题叠加：

1. 检索准确率在不同任务上高度不均衡  
   文档问答、单跳事实、时序问题表现明显偏弱。

2. 检索延迟偏高  
   p50 约 7-12 秒，主瓶颈集中在意图分析和后续较重的检索路径。

3. benchmark 自身还不完全可信  
   当前存在 `expected_uri` 对齐问题，以及部分 run 的跨数据集污染问题，导致 `Recall@k` 不能直接作为最终依据。

### 2.2 当前系统已具备的能力

OpenCortex 并不是“完全没有检索优化能力”，当前已经具备：

- Dense + BM25 sparse hybrid embedding
- lexical path + RRF 融合
- reranker 与分数阈值跳过
- `project_id` / `source_tenant_id` / `context_type` / `category` / `created_at` 等过滤字段
- `parent_uri` 层级结构
- `IntentRouter` 的关键词直通、短 TTL cache、time scope 识别
- batch access stats update
- frontier batching 与 flat search fallback

因此本次设计的重点不是从零发明新系统，而是：

- 把现有能力真正用对
- 在缺失处补最小字段和最短链路
- 对不同 query 走不同路径

### 2.3 根因归纳

结合代码与 benchmark，当前召回问题的根因可归纳为：

1. 指标体系不稳定  
   `Recall@k` 受 URI 对齐与 run 污染影响，难以作为唯一决策依据。

2. 查询分流不够  
   文档问答、事实定位、时序查询仍过多共享同一路径。

3. 文档模式缺少稳定的“源文档范围”约束  
   导致本应在单文档内完成的搜索退化为全局 chunk 搜索。

4. 层级结构没有充分用于“命中小块、返回大块”
   现有 `parent_uri` 已存在，但文档模式没有形成标准的 Small-to-Big 路径。

5. 时间意图已识别但未真正转化为硬过滤
   `IntentRouter.time_scope` 没有完整落到检索层。

6. 意图分析成本过高  
   简单查询仍可能走 LLM 路径，拉高整体 p50 / p95。

## 3. 设计原则

### 3.1 原则一：先校准 benchmark，再优化 recall

如果 benchmark 本身不能稳定回答“到底召回对没对”，就不应直接把 `Recall@k` 当北极星。

本设计将 benchmark 校准列为 P0 正式工作项，而不是附属清理工作。

### 3.2 原则二：优先使用结构化过滤，而不是盲目堆模型

对于：

- “这篇论文里……”
- “上次/最近/昨天……”
- “某个人的某个事实……”

这类查询，结构化过滤通常比额外上更大的 embedding / reranker 更高效、更稳。

### 3.3 原则三：Small-to-Big 先于 RAPTOR，GraphRAG 后置

当前系统的主要短板仍在：

- query 路由
- 过滤范围
- chunk 表达
- 返回粒度

在这些主链路没有收敛前，不引入 RAPTOR/GraphRAG 作为 P0/P1 方案。

### 3.4 原则四：性能优化优先砍“错误路径占比”

延迟优化优先级：

1. 减少进入 LLM intent 的比例
2. 减少无效 rerank
3. 缩小搜索空间
4. 再做更激进的投机式并发

## 4. 目标指标

### 4.1 准确率目标

以当前 benchmark 为基线，Phase 1.5 目标不是“所有数据集超过 baseline”，而是：

- `QASPER`：先消除跨数据污染，恢复单文档内检索语义
- `LoCoMo`：降低单跳事实和时序问题的错误率
- `PersonaMem`：保持现有压缩率优势，不明显回退
- `HotPotQA`：维持当前接近基线的水平，不因优化造成退化

### 4.2 性能目标

在本地部署、相同硬件上，目标：

- 简单查询 fast path：`p50 < 1.5s`
- 一般检索请求：`p50 < 3.0s`
- 带 LLM intent 的复杂请求：`p50 < 5.0s`
- rerank 触发率下降
- 检索阶段的平均搜索候选数下降

## 5. 非目标

本设计不在当前阶段解决：

- 全量知识图谱推理
- 自动用户画像与复杂长期知识治理
- 基于训练/微调的专用大规模召回模型
- 复杂多跳 reasoning 的全自动 query planning 学习

这些能力可以作为后续 Phase 2/3 的扩展，而不是本次召回主链路的前置依赖。

## 6. 总体方案概览

本次优化分为四条主线：

1. benchmark 与观测校准
2. 召回准确率优化
3. 召回性能优化
4. 分阶段上线与回滚保护

总体结构如下：

```text
query
  -> Query Fast Classifier
      -> Fast Path
      -> Session/Complex Path
  -> Retrieval Planner
      -> Document Scoped Search
      -> Fact / Keyword Search
      -> Temporal Search
  -> Candidate Generation
      -> dense + sparse + lexical
  -> Candidate Fusion
      -> dynamic RRF / rerank gate
  -> Context Assembly
      -> hit small chunk, return parent section
```

## 7. 详细设计

### 7.1 P0：Benchmark 校准与隔离

#### 7.1.1 目标

让 benchmark 首先能稳定回答两个问题：

- 检索结果是不是来自正确的数据范围？
- `Recall@k` 到底是否可比？

#### 7.1.2 设计

每次 benchmark run 必须强制写入独立隔离域：

- `run_id`
- `tenant_id`
- `project_id`
- `dataset_id`

并在查询时强制注入相同过滤条件。

#### 7.1.3 新增字段

建议在 `meta` 或顶层 payload 中统一补充：

- `benchmark_run_id`
- `dataset_id`
- `source_doc_id`
- `source_section_path`
- `speaker`
- `event_date`

其中：

- `source_doc_id` 是文档问答的关键字段
- `speaker`、`event_date` 是对话与记忆时序检索的关键字段

#### 7.1.4 指标修正

benchmark 输出拆成两组：

1. 检索有效性指标
   - pollution rate
   - in-scope retrieval ratio
   - exact expected-uri match ratio

2. 下游任务指标
   - J-Score
   - F1
   - token reduction
   - latency breakdown

在 URI 对齐完全修复前，不再把 `Recall@k` 单独作为对外主结论。

### 7.2 查询分类与路由重构

#### 7.2.1 目标

让不同查询进入不同检索策略，而不是共用默认路径。

#### 7.2.2 新增 Query Classifier

在 `IntentRouter` 前增加一个更轻量的 Query Fast Classifier，用于零或极低成本判断：

- `document_scoped`
- `fact_lookup`
- `temporal_lookup`
- `conversational_analysis`
- `summary_request`

输入：

- query 文本
- 是否有 target URI / target document
- 是否有 session context

输出：

- `query_class`
- `need_llm_intent`
- `lexical_priority`
- `time_filter_hint`
- `doc_scope_hint`

#### 7.2.3 路由规则

1. 明确文件/论文/文档目标的查询  
   直接进入 `document_scoped`，默认不先走全局 recall。

2. 命中硬关键词、人名、文件名、数字、日期的查询  
   直接提高 lexical 权重，优先走 `fact_lookup`。

3. 命中“最近/上次/昨天/最后一次”等词的查询  
   进入 `temporal_lookup`，强制增加时间过滤。

4. 只有复杂多句、模糊意图、需要总结/分析的查询  
   才进入 LLM intent 路径。

### 7.3 文档模式：Document Scoped Search + Small-to-Big

#### 7.3.1 目标

解决 `QASPER` 这类“问题属于某一篇文档，但 chunk 在全局空间里游走”的问题。

#### 7.3.2 数据模型补充

文档导入时，每个 chunk 补齐：

- `source_doc_id`
- `source_doc_title`
- `source_section_path`
- `chunk_level`
- `chunk_role`

其中：

- `chunk_role = leaf | section | document`
- `chunk_level` 对应层级深度

#### 7.3.3 检索流程

文档问答分两段：

1. 文档范围确定
   - 若 query 已绑定文档，直接过滤 `source_doc_id`
   - 若 query 未绑定文档但上下文可解析文档目标，先做文档级候选确定

2. 文档内 chunk 检索
   - 在 `source_doc_id` 过滤范围内跑 dense + sparse + lexical
   - 命中叶子 chunk 后，向上提升父 section

#### 7.3.4 返回策略

命中最小 chunk，但返回：

- 命中 chunk 本身
- 所属 parent section 的 `overview`
- 相邻 sibling chunk 的有限扩展

即：

- 匹配粒度小
- 返回上下文大

这就是 OpenCortex 当前最适合的 Small-to-Big 实现。

#### 7.3.5 明确后置项

以下能力不纳入当前 P0/P1：

- RAPTOR 全局摘要树
- 文档级聚类召回
- 自动跨文档 reasoning 图

### 7.4 事实型查询：Dynamic Hybrid Retrieval

#### 7.4.1 目标

提升人名、数字、术语、路径名、具体事件等 query 的准确率。

#### 7.4.2 当前问题

OpenCortex 已经有 hybrid 与 lexical path，但当前核心问题是：

- query 类型没有明确分流
- lexical 权重仍偏保守
- exact-ish query 没有单独策略

#### 7.4.3 设计

将当前固定 lexical boost 扩展为动态策略：

| Query 类型 | Dense | Sparse / Lexical | Rerank |
|---|---:|---:|---:|
| 普通语义查询 | 0.7 | 0.3 | 按阈值 |
| 硬关键词 / 文件名 / 术语 | 0.4 | 0.6 | 条件触发 |
| 事实查找 / 人名 + 数字 | 0.3 | 0.7 | 可跳过 |
| 文档范围内 chunk 查询 | 0.5 | 0.5 | 按候选质量 |

注意：

- 不采用全局固定 `Dense 0.3 + BM25 0.7`
- 采用 query-class-driven 的动态加权

#### 7.4.4 结构化字段增强

在不引入完整 NER pipeline 的前提下，优先补充轻量结构化字段：

- `speaker`
- `event_date`
- `source_doc_id`
- `source_section_path`
- `keywords`

后续若需要，再加：

- `entities_person`
- `entities_org`
- `entities_location`

### 7.5 时序查询：Time Scope 落地为硬过滤

#### 7.5.1 目标

让 “昨天 / 最近 / 上次 / 最后一次” 这类 query 不再只停留在语义层，而是真正限制搜索空间。

#### 7.5.2 设计

把 `IntentRouter.time_scope` 落到 `metadata_filter`：

- `recent` -> 基于 `created_at/event_date` 的时间窗口过滤
- `session` -> 当前 session 或当前 turn 范围过滤
- `all` -> 不额外限制

对于明显时序问题，再追加排序项：

- 先按语义得分筛选
- 再对近时间结果轻微加权

#### 7.5.3 不优先做的方案

暂不直接修改向量相似度公式为“时间衰减向量”，原因：

- 会影响历史事实 recall
- 调参复杂
- 可解释性较差

优先级更高的是：

- 时间硬过滤
- 时间排序加权

### 7.6 意图分析降延迟：Fast Path + Cache + 小模型

#### 7.6.1 目标

把最重的 2-5 秒意图分析开销从热路径中剥掉。

#### 7.6.2 分层策略

1. `Fast Path`
   - 无 session context
   - 命中硬关键词
   - 明显 fact lookup / document scoped / temporal lookup
   - 这些请求直接跳过 LLM intent

2. `Intent Cache`
   - 现有 cache 保留，但扩展 key 维度
   - 增加 query canonicalization
   - 增加 target_doc_id / query_class 参与 cache key

3. `Small Model Intent`
   - 对必须走 intent 的请求，优先使用小模型或轻量分类器
   - 大模型仅保留为 fallback

#### 7.6.3 路径优先级

`heuristic fast path` > `cache hit` > `small model` > `large model fallback`

这样才能真正把高延迟路径压缩成少数情况。

### 7.7 检索后处理：Rerank Gate 与组装预算

#### 7.7.1 目标

避免“已经够准的结果”还继续做昂贵 rerank 和组装。

#### 7.7.2 设计

新增 rerank gate：

- top1 与 top2 分差足够大时，跳过 rerank
- exact-ish lexical 命中时，跳过 rerank
- document-scoped 且候选池很小时，缩小 rerank 候选数

结果组装预算：

- L0/L1 默认只读必要字段
- 文档模式优先返回 parent section，不展开整篇
- relations 继续维持批量预取，禁止回到逐条 fan-out

### 7.8 Frontier Search 的预算与回退

#### 7.8.1 目标

避免层级检索在“目录稀疏 / 父节点饥饿”场景下退化成高代价路径。

#### 7.8.2 设计

增加三个硬预算：

- max wave count
- max compensation query count
- max tiny-query count per request

一旦超预算：

- 自动回退 flat search
- 记录 explain / trace 事件

这样既保住 recall，又控制 tail latency。

## 8. 需要修改的数据模型与接口

### 8.1 Payload 新增字段

建议在 `context` collection 中新增或正式化以下字段：

| 字段 | 用途 |
|---|---|
| `dataset_id` | benchmark 隔离 |
| `benchmark_run_id` | benchmark 隔离 |
| `source_doc_id` | 文档范围过滤 |
| `source_doc_title` | 文档 explain |
| `source_section_path` | 文档层级返回 |
| `speaker` | 对话事实过滤 |
| `event_date` | 时序过滤 |
| `query_class_hint` | 可选，用于 explain |

### 8.2 API 行为变化

`search()` 不必改 public signature，但内部支持：

- 从 request context 注入 `benchmark_run_id`
- 从 `target_uri` 推导 `source_doc_id`
- 从 `time_scope` 推导时间过滤

`batch_add()` / 文档导入链路需要在写入时补齐：

- `source_doc_id`
- `source_section_path`
- `chunk_role`

## 9. Explain 与观测增强

用户明确偏好链路追踪，因此本次设计必须补 explain。

每次 search 返回内部 explain 结构，至少包含：

- query_class
- fast path / llm path
- doc scope 是否命中
- time filter 是否命中
- dense / lexical / rerank 是否启用
- candidates before rerank
- candidates after rerank
- frontier / flat fallback 信息
- assemble cost

对外默认可不暴露全部字段，但 debug 接口必须可读。

## 10. 实施优先级

### P0

1. benchmark 隔离与污染修复
2. `source_doc_id` / `dataset_id` 字段接入
3. document scoped search
4. fast path 跳过 LLM intent

### P1

1. Small-to-Big 返回策略
2. time_scope -> 时间硬过滤
3. 动态 hybrid 权重
4. rerank gate

### P2

1. 小模型 intent classifier
2. 轻量实体字段抽取
3. frontier 预算与 explain

### P3

1. RAPTOR / 文档摘要树试验
2. 更强的实体索引
3. GraphRAG / 知识图结构探索

## 11. 风险与回滚

### 11.1 主要风险

- 字段新增后历史数据不完整
- 文档 scoped search 误判文档目标，导致 recall 变窄
- 时间过滤过严，误伤历史事实
- fast path 误判，跳过了本该进入 LLM intent 的复杂查询

### 11.2 缓解措施

- 所有新策略 behind feature flags
- benchmark 与线上请求分离验证
- 对每个新过滤项加 explain 输出
- 所有 narrowing filter 都允许 fallback 到较宽路径

## 12. 验收标准

### 12.1 准确率

- benchmark 无跨 run 污染
- `Recall@k` 与 `expected_uri` 对齐逻辑恢复可用
- `QASPER` J-Score 明显回升
- `LoCoMo` Cat 1 / Cat 4 不再是当前级别的大幅落后

### 12.2 性能

- fast path 查询不再进入大模型意图分析
- average rerank trigger rate 下降
- latency report 可拆分到子阶段
- p50 / p95 明显下降，且 tail latency 更稳定

## 13. 最终结论

对 OpenCortex 来说，当前最值得先做的不是更复杂的上层架构，而是把检索主链路收紧成四件事：

1. 把 benchmark 量准
2. 把查询分流做对
3. 把文档范围和 Small-to-Big 做实
4. 把 LLM intent 从默认热路径中移开

只要这四件事落地，召回准确率和召回性能都会比继续堆更复杂的架构更快见效。
